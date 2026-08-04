"""Owner-gated ``resume_after_reconciliation`` (Musashi addendum 2026-08-04 §9).

Fail-closed recovery leaves ``halt=hold`` after an incident even when the
broker is proven flat, and no code path may silently clear it. This module
implements the one dedicated, bounded transition ``halt: hold -> none``:

1. Paper/Demo only — the capability schema hard-binds venue ``ibkr_paper``,
   loopback host and the TWS Paper port; the mint tool is TTY-only, so no
   model, LLM, Hermes process or Telegram text can invoke it;
2. consumes a short-lived, nonce-bound owner capability exactly once
   (UNIQUE nonce/digest burn inside the transaction);
3. binds exact venue, environment, account fingerprint, instrument and the
   recovery effect identity (``resume_of_effect_id``);
4. requires fresh direct broker evidence proving zero positions and zero
   open orders for the bound account, gathered immediately before the
   transition;
5. requires every effect terminal; any unknown/pending/unreconciled or
   foreign evidence refuses;
6. refuses while the originating P0/P1 incident condition remains active
   (and refuses when the incident state cannot be established);
7. capability burn, evidence hashes, previous state and the transition
   commit in ONE ``BEGIN IMMEDIATE`` unit of the durable ledger;
8. retry after success is idempotent and can never clear a later hold;
9. crash before commit leaves the hold and the capability; crash after
   commit resumes from committed state without a second capability;
10. the recorded facts carry hashes and fingerprints only — never raw
    account identifiers or secrets.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.ibkr_l1_adapter import L1AuthorizationError, L1Profile
from app.ibkr_l1_capability import (
    FINGERPRINT_ALGORITHM,
    CapabilityGate,
    CapabilityRecord,
    capability_digest,
    _parse_utc,
)
from app.ibkr_l1_journal import L1ExecutionOlap

RESUME_SCHEMA_VERSION = "lts.ibkr.l1.resume_capability.v1"
RESUME_OPERATION = "resume_after_reconciliation"
RESUME_STORE = Path.home() / ".config/lts/ibkr-resume-capabilities"
MAX_RESUME_VALIDITY_SECONDS = 900
MAX_EVIDENCE_AGE_SECONDS = 120.0

_REQUIRED_KEYS = (
    "schema_version", "operation", "venue", "host", "port", "profile_hash",
    "profile_schema_version", "account_fingerprint_algorithm",
    "account_fingerprint", "asset_id", "instrument", "resume_of_effect_id",
    "issued_at", "expires_at", "nonce",
)


def validate_resume_capability(
    payload: dict[str, Any],
    *,
    profile: L1Profile,
    now: Optional[datetime] = None,
) -> CapabilityRecord:
    """Strict v1 validation of a resume capability. Every failure refuses."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict):
        raise L1AuthorizationError("resume capability must be a JSON object")
    missing = [key for key in _REQUIRED_KEYS if key not in payload]
    if missing:
        raise L1AuthorizationError(f"resume capability missing keys: {missing}")
    unknown = sorted(set(payload) - set(_REQUIRED_KEYS))
    if unknown:
        raise L1AuthorizationError(
            f"resume capability has unknown keys: {unknown}")
    if payload["schema_version"] != RESUME_SCHEMA_VERSION:
        raise L1AuthorizationError(
            f"resume capability schema must be {RESUME_SCHEMA_VERSION!r}")
    if payload["operation"] != RESUME_OPERATION:
        raise L1AuthorizationError(
            f"resume capability operation must be {RESUME_OPERATION!r}")
    if payload["venue"] != "ibkr_paper":
        raise L1AuthorizationError("resume capability venue must be 'ibkr_paper'")
    if payload["host"] != "127.0.0.1":
        raise L1AuthorizationError("resume capability host must be loopback")
    if int(payload["port"]) != 7497:
        raise L1AuthorizationError(
            "resume capability port must be the TWS Paper port 7497")
    if payload["profile_hash"] != profile.profile_hash:
        raise L1AuthorizationError(
            "resume capability does not bind to the loaded profile hash")
    if payload["profile_schema_version"] != profile.schema_version:
        raise L1AuthorizationError(
            "resume capability profile schema version mismatch")
    if payload["account_fingerprint_algorithm"] != FINGERPRINT_ALGORITHM:
        raise L1AuthorizationError(
            "resume capability fingerprint algorithm mismatch")
    if payload["account_fingerprint"] != profile.account_fingerprint:
        raise L1AuthorizationError(
            "resume capability account fingerprint does not match the profile")
    if payload["asset_id"] != profile.asset_id:
        raise L1AuthorizationError(
            "resume capability asset does not match the profile")
    if payload["instrument"] != profile.instrument:
        raise L1AuthorizationError(
            "resume capability instrument does not match the profile")
    effect_id = str(payload["resume_of_effect_id"])
    if not effect_id.startswith("l1e-"):
        raise L1AuthorizationError(
            "resume capability must name the recovery effect id (l1e-…)")
    issued_at = _parse_utc(payload["issued_at"], "issued_at")
    expires_at = _parse_utc(payload["expires_at"], "expires_at")
    if (issued_at - now).total_seconds() > 120:
        raise L1AuthorizationError("resume capability issued_at is in the future")
    if expires_at <= issued_at:
        raise L1AuthorizationError("resume capability expires before issue")
    if (expires_at - issued_at).total_seconds() > MAX_RESUME_VALIDITY_SECONDS:
        raise L1AuthorizationError(
            f"resume capability validity exceeds {MAX_RESUME_VALIDITY_SECONDS}s")
    if now >= expires_at:
        raise L1AuthorizationError("resume capability is expired")
    nonce = str(payload["nonce"])
    if len(nonce) < 32 or any(c not in "0123456789abcdef" for c in nonce):
        raise L1AuthorizationError(
            "resume capability nonce must be at least 32 lowercase hex chars")
    return CapabilityRecord(
        capability_sha256=capability_digest(payload),
        nonce_sha256=hashlib.sha256(nonce.encode()).hexdigest(),
        metadata={
            "schema_version": RESUME_SCHEMA_VERSION,
            "operation": RESUME_OPERATION,
            "profile_hash": profile.profile_hash,
            "account_fingerprint": profile.account_fingerprint,
            "instrument": profile.instrument,
            "asset_id": profile.asset_id,
            "resume_of_effect_id": effect_id,
            "issued_at": issued_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
    )


class ResumeGate(CapabilityGate):
    """Loads the single valid resume capability from its own fixed store."""

    _validate = staticmethod(validate_resume_capability)

    def __init__(self, store_dir: Optional[Path] = None) -> None:
        super().__init__(store_dir or RESUME_STORE)


def _evidence_or_refuse(
    evidence: Any, profile: L1Profile, now: datetime
) -> tuple[str, dict[str, Any]]:
    """Validate fresh direct broker evidence; return (sha256, safe summary)."""
    if not isinstance(evidence, dict) or not evidence:
        raise L1AuthorizationError(
            "broker evidence is missing or empty — unknown is a refusal")
    for key in ("source", "gathered_at", "account_fingerprint",
                "positions", "open_orders"):
        if key not in evidence:
            raise L1AuthorizationError(f"broker evidence missing {key!r}")
    if evidence["source"] != "direct_tws":
        raise L1AuthorizationError(
            "broker evidence must come from a direct TWS session")
    gathered_at = _parse_utc(evidence["gathered_at"], "gathered_at")
    age = (now - gathered_at).total_seconds()
    if age < -5.0 or age > MAX_EVIDENCE_AGE_SECONDS:
        raise L1AuthorizationError(
            f"broker evidence is stale ({age:.0f}s old); gather it "
            "immediately before the transition")
    if evidence["account_fingerprint"] != profile.account_fingerprint:
        raise L1AuthorizationError(
            "broker evidence is for a different account fingerprint")
    positions = evidence["positions"]
    open_orders = evidence["open_orders"]
    if not isinstance(positions, list) or not isinstance(open_orders, list):
        raise L1AuthorizationError(
            "broker evidence positions/open_orders must be lists")
    if positions:
        raise L1AuthorizationError(
            f"broker reports {len(positions)} position(s); resume requires"
            " proven flat")
    if open_orders:
        raise L1AuthorizationError(
            f"broker reports {len(open_orders)} open order(s); resume"
            " requires none")
    text = json.dumps(evidence, sort_keys=True, default=str)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    summary = {
        "evidence_sha256": digest,
        "account_fingerprint": profile.account_fingerprint,
        "positions": 0,
        "open_orders": 0,
        "gathered_at": gathered_at.isoformat(),
    }
    return digest, summary


def resume_after_reconciliation(
    *,
    olap: L1ExecutionOlap,
    profile: L1Profile,
    payload: dict[str, Any],
    record: CapabilityRecord,
    broker_evidence: Any,
    active_incidents: Optional[list[dict[str, Any]]],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Execute the bounded ``halt: hold -> none`` transition. Fail-closed."""
    now = now or datetime.now(timezone.utc)
    effect_id = str(payload["resume_of_effect_id"])

    halt = olap.get_state("halt", "none")
    last_resume_raw = olap.get_state("last_resume", "")
    last_resume = json.loads(last_resume_raw) if last_resume_raw else {}

    if halt == "none":
        if last_resume.get("nonce_sha256") == record.nonce_sha256:
            # Property 8/9: retry after a committed success is a no-op.
            return {"applied": False, "already_resumed": True,
                    "effect_id": effect_id,
                    "evidence_sha256": last_resume.get("evidence_sha256")}
        raise L1AuthorizationError(
            "no hold is active; nothing to resume (capability not consumed)")
    if halt != "hold":
        raise L1AuthorizationError(
            f"halt state {halt!r} is not 'hold'; this operation clears only"
            " a reconciliation hold")
    if olap.nonce_consumed(record.nonce_sha256):
        # Property 8: a spent capability can never clear a later hold.
        raise L1AuthorizationError(
            "this capability was already consumed; the current hold needs a"
            " freshly minted capability")

    if active_incidents is None:
        raise L1AuthorizationError(
            "incident state could not be established — unknown is a refusal")
    if active_incidents:
        codes = sorted({str(i.get("event_code")) for i in active_incidents})
        raise L1AuthorizationError(
            f"originating incident condition still active: {codes};"
            " resume refused")

    nonterminal = olap.nonterminal_effects()
    if nonterminal:
        states = sorted({e["state"] for e in nonterminal})
        raise L1AuthorizationError(
            f"{len(nonterminal)} effect(s) not terminal ({states});"
            " resume requires every effect terminal")
    bound = olap.effect_row(effect_id)
    if bound is None:
        raise L1AuthorizationError(
            f"bound recovery effect {effect_id} does not exist")
    if not str(bound["state"]).startswith("terminal_"):
        raise L1AuthorizationError(
            f"bound recovery effect {effect_id} is {bound['state']!r},"
            " not terminal")

    evidence_sha256, evidence_summary = _evidence_or_refuse(
        broker_evidence, profile, now)

    transition = {
        "nonce_sha256": record.nonce_sha256,
        "at": now.isoformat(),
        "evidence_sha256": evidence_sha256,
        "effect_id": effect_id,
    }
    try:
        with olap.atomic_unit():
            olap.consume_capability(
                record.capability_sha256, record.nonce_sha256,
                {**record.metadata, "consumed_for": "resume"}, effect_id)
            olap.record_broker_fact(effect_id, "resume_evidence",
                                    evidence_summary)
            olap.record_broker_fact(effect_id, "halt_cleared", {
                "previous": "hold",
                "nonce_sha256": record.nonce_sha256,
                "evidence_sha256": evidence_sha256,
                "cleared_at": now.isoformat(),
            })
            olap.set_state("halt", "none")
            olap.set_state("last_resume",
                           json.dumps(transition, sort_keys=True))
    except sqlite3.IntegrityError as exc:
        # Concurrent resume: the second consumer's burn violates UNIQUE.
        raise L1AuthorizationError(
            "capability burn conflicted with a concurrent consumer;"
            " treat this capability as spent") from exc

    return {"applied": True, "already_resumed": False,
            "effect_id": effect_id, "previous_halt": "hold",
            "evidence_sha256": evidence_sha256}
