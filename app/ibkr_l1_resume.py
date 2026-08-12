"""Owner-gated ``resume_after_reconciliation`` (Musashi addendum 2026-08-04 §9).

Fail-closed recovery leaves ``halt=hold`` after an incident even when the
broker is proven flat, and no code path may silently clear it. This module
implements the one dedicated, bounded transition ``halt: hold -> none``:

1. Paper/Demo only — the capability schema hard-binds venue ``ibkr_paper``,
   loopback host and the TWS Paper port; the passphrase-protected owner
   SIGNATURE over the capability bytes is the human-authentication
   boundary (findings 094/227) — the mint tool's TTY check is ergonomic
   confirmation only and a minted file stays inert until the owner signs
   it;
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
import os
import sqlite3
import stat
from dataclasses import dataclass, field
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


class ResumeCapabilityExpired(L1AuthorizationError):
    """Typed expiry (finding 227): store classification distinguishes an
    expired capability from a malformed one, so an expired side file is
    ignorable and archivable instead of an anonymous refusal."""


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
        raise ResumeCapabilityExpired("resume capability is expired")
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


RESUME_SIGNATURE_NAMESPACE = "lts-ibkr-resume"
ALLOWED_SIGNERS_PATH = Path("/etc/lts/resume_allowed_signers")
OWNER_PRINCIPAL = "owner"


def _require_signer_pin(
    allowed_signers: Path, require_root_pin: bool
) -> None:
    """The root-pinned allowed-signers file is a store-wide precondition:
    while it is absent or weak, resume is structurally disabled for every
    capability, so this refusal is raised before per-file classification."""
    if not allowed_signers.is_file():
        raise L1AuthorizationError(
            f"owner-signer pin {allowed_signers} does not exist — resume"
            " is disabled until the owner completes the signer setup"
            " packet (docs/security/OWNER_RESUME_SIGNER_SETUP)")
    pin_stat = os.stat(allowed_signers)
    if require_root_pin and pin_stat.st_uid != 0:
        raise L1AuthorizationError(
            f"owner-signer pin {allowed_signers} is not root-owned — an"
            " agent-writable pin is no pin; refusing")
    if pin_stat.st_mode & 0o022:
        raise L1AuthorizationError(
            "owner-signer pin must not be group/other-writable")


def verify_owner_signature(
    capability_path: Path,
    signature_path: Path,
    *,
    allowed_signers: Path = ALLOWED_SIGNERS_PATH,
    require_root_pin: bool = True,
) -> dict[str, Any]:
    """Findings 094/227: a PTY (or a typed public phrase) is ergonomic
    confirmation only — it never authenticates anyone. The capability
    file must carry a detached OpenSSH Ed25519 signature
    (``ssh-keygen -Y sign``) whose key is pinned in a ROOT-OWNED
    allowed-signers file the agent user cannot write. The private key
    lives behind the owner's passphrase — THAT is the human-authenticated
    boundary. Missing pin, missing signature, wrong signer, wrong
    namespace or any byte change in the payload refuses; while the pin
    file does not exist, resume is structurally disabled."""
    import subprocess as _subprocess

    _require_signer_pin(allowed_signers, require_root_pin)
    if not signature_path.is_file():
        raise L1AuthorizationError(
            f"detached owner signature {signature_path} is missing")
    result = _subprocess.run(
        ["ssh-keygen", "-Y", "verify",
         "-f", str(allowed_signers),
         "-I", OWNER_PRINCIPAL,
         "-n", RESUME_SIGNATURE_NAMESPACE,
         "-s", str(signature_path)],
        stdin=capability_path.open("rb"),
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise L1AuthorizationError(
            "owner signature verification FAILED: "
            + (result.stderr.strip() or result.stdout.strip())[:200])
    return {"verified": True, "principal": OWNER_PRINCIPAL,
            "namespace": RESUME_SIGNATURE_NAMESPACE,
            "capability_sha256": hashlib.sha256(
                capability_path.read_bytes()).hexdigest()}


class ResumeGate(CapabilityGate):
    """Loads the single valid resume capability from its own fixed store."""

    _validate = staticmethod(validate_resume_capability)

    def __init__(self, store_dir: Optional[Path] = None) -> None:
        super().__init__(store_dir or RESUME_STORE)


# ── finding 227: typed store classification, owner selection, archival ──

ENTRY_VALID = "valid"
ENTRY_UNSIGNED = "unsigned"
ENTRY_MALFORMED = "malformed"
ENTRY_EXPIRED = "expired"
ENTRY_CONSUMED = "consumed"


@dataclass(frozen=True)
class StoreEntry:
    """One top-level JSON file in the resume-capability store, typed."""

    path: Path
    kind: str  # ENTRY_VALID | ENTRY_UNSIGNED | ENTRY_MALFORMED |
    #            ENTRY_EXPIRED | ENTRY_CONSUMED
    detail: str
    payload: Optional[dict[str, Any]] = field(default=None, repr=False)
    record: Optional[CapabilityRecord] = field(default=None, repr=False)


def _check_store_dir(store_dir: Path) -> None:
    if not store_dir.is_dir():
        raise L1AuthorizationError(
            f"capability store {store_dir} does not exist; the owner must"
            " mint a capability with tools/mint_resume_capability.py")
    mode = stat.S_IMODE(os.stat(store_dir).st_mode)
    if mode & 0o077:
        raise L1AuthorizationError(
            f"capability store {store_dir} has permissive mode {oct(mode)};"
            " required 0o700")


def classify_resume_store(
    store_dir: Path,
    *,
    profile: L1Profile,
    olap: Optional[L1ExecutionOlap] = None,
    now: Optional[datetime] = None,
    allowed_signers: Path = ALLOWED_SIGNERS_PATH,
    require_root_pin: bool = True,
) -> list[StoreEntry]:
    """Type EVERY top-level JSON in the store BEFORE any ambiguity check
    (finding 227). Classification order per file: signature first (the
    passphrase-protected owner signature is the authority boundary), then
    schema/binding validity, then expiry, then consumption. Unsigned,
    malformed, expired and consumed side files come back typed so callers
    can log and ignore them — they can never deny one valid signed
    capability. Nothing here writes, moves or deletes anything."""
    _check_store_dir(store_dir)
    _require_signer_pin(allowed_signers, require_root_pin)
    now = now or datetime.now(timezone.utc)
    entries: list[StoreEntry] = []
    for path in sorted(store_dir.glob("*.json")):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            entries.append(StoreEntry(
                path, ENTRY_MALFORMED,
                f"permissive mode {oct(mode)}; required 0o600"))
            continue
        try:
            payload = json.loads(path.read_text())
        except (ValueError, OSError) as error:
            entries.append(StoreEntry(
                path, ENTRY_MALFORMED, f"unreadable JSON: {error}"))
            continue
        signature_path = Path(str(path) + ".sig")
        try:
            verify_owner_signature(
                path, signature_path,
                allowed_signers=allowed_signers,
                require_root_pin=require_root_pin)
        except L1AuthorizationError as error:
            entries.append(StoreEntry(
                path, ENTRY_UNSIGNED, str(error), payload=payload))
            continue
        try:
            record = validate_resume_capability(
                payload, profile=profile, now=now)
        except ResumeCapabilityExpired as error:
            entries.append(StoreEntry(
                path, ENTRY_EXPIRED, str(error), payload=payload))
            continue
        except (L1AuthorizationError, ValueError, TypeError) as error:
            entries.append(StoreEntry(
                path, ENTRY_MALFORMED, str(error), payload=payload))
            continue
        if olap is not None and (
            olap.capability_row(record.capability_sha256) is not None
            or olap.nonce_consumed(record.nonce_sha256)
        ):
            entries.append(StoreEntry(
                path, ENTRY_CONSUMED,
                "capability already consumed; a spent capability can never"
                " clear a later hold", payload=payload, record=record))
            continue
        entries.append(StoreEntry(
            path, ENTRY_VALID, "signed, current, profile-bound, unconsumed",
            payload=payload, record=record))
    return entries


def select_resume_capability(
    store_dir: Path,
    *,
    profile: L1Profile,
    olap: Optional[L1ExecutionOlap] = None,
    explicit_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    allowed_signers: Path = ALLOWED_SIGNERS_PATH,
    require_root_pin: bool = True,
) -> tuple[StoreEntry, list[StoreEntry]]:
    """Return ``(chosen, ignored)`` — the owner's capability plus the typed
    side files that were logged and ignored. Fail-closed rules
    (finding 227):

    - ``explicit_path`` (the owner's ``--capability PATH``) is preferred:
      it must live INSIDE the protected store and classify as valid
      (signed, current, profile-bound, unconsumed); any other typing is a
      typed refusal. Explicit selection resolves ambiguity by naming.
    - without an explicit path, exactly one VALID entry is required;
      unsigned/malformed/expired/consumed side files are ignored and can
      never deny it, but TWO OR MORE valid signed current capabilities
      still refuse (ambiguity).
    """
    entries = classify_resume_store(
        store_dir, profile=profile, olap=olap, now=now,
        allowed_signers=allowed_signers, require_root_pin=require_root_pin)

    if explicit_path is not None:
        explicit = Path(os.path.expanduser(str(explicit_path))).resolve()
        if explicit.parent != store_dir.resolve():
            raise L1AuthorizationError(
                f"--capability {explicit} is outside the protected store"
                f" {store_dir}; the owner selects a file inside the store")
        chosen = next(
            (e for e in entries if e.path.resolve() == explicit), None)
        if chosen is None:
            raise L1AuthorizationError(
                f"--capability {explicit.name} does not exist in the store"
                " (only top-level *.json files are eligible)")
        if chosen.kind != ENTRY_VALID:
            raise L1AuthorizationError(
                f"--capability {explicit.name} is {chosen.kind}:"
                f" {chosen.detail}")
        return chosen, [e for e in entries if e.path != chosen.path]

    valid = [e for e in entries if e.kind == ENTRY_VALID]
    ignored = [e for e in entries if e.kind != ENTRY_VALID]
    if not valid:
        reasons = "; ".join(
            f"{e.path.name}: {e.kind} ({e.detail})" for e in ignored)
        raise L1AuthorizationError(
            "no valid signed capability in the store; typed side files: "
            + (reasons or "store is empty"))
    if len(valid) > 1:
        names = ", ".join(e.path.name for e in valid)
        raise L1AuthorizationError(
            f"{len(valid)} valid signed current capabilities present"
            f" ({names}); ambiguity is a refusal — the owner keeps exactly"
            " one, or names one with --capability")
    return valid[0], ignored


def archive_invalid_capabilities(
    store_dir: Path,
    *,
    profile: L1Profile,
    olap: Optional[L1ExecutionOlap] = None,
    now: Optional[datetime] = None,
    allowed_signers: Path = ALLOWED_SIGNERS_PATH,
    require_root_pin: bool = True,
    archive_dir: Optional[Path] = None,
) -> dict[str, list[str]]:
    """Separate, explicit archival operation (finding 227 §4). Moves ONLY
    files typed EXPIRED or CONSUMED (with their ``.sig``) into
    ``<store>/archive/``; never deletes anything, never touches a VALID
    capability, and deliberately leaves UNSIGNED/MALFORMED files in place
    as potential tamper evidence for the owner. The default resume flow
    never calls this — it moves nothing."""
    entries = classify_resume_store(
        store_dir, profile=profile, olap=olap, now=now,
        allowed_signers=allowed_signers, require_root_pin=require_root_pin)
    destination = archive_dir or (store_dir / "archive")
    archived: list[str] = []
    kept: list[str] = []
    for entry in entries:
        if entry.kind not in (ENTRY_EXPIRED, ENTRY_CONSUMED):
            kept.append(f"{entry.path.name}: {entry.kind}")
            continue
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for source in (entry.path, Path(str(entry.path) + ".sig")):
            if not source.is_file():
                continue
            target = destination / source.name
            if target.exists():
                target = destination / f"{stamp}_{source.name}"
            source.rename(target)
        archived.append(f"{entry.path.name}: {entry.kind}")
    return {"archived": archived, "kept": kept}


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
    incident_probe: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Execute the bounded ``halt: hold -> none`` transition. Fail-closed.

    Finding 093: every mutable precondition — halt state, resume history,
    nonce consumption, effect states and the incident probe — is read (or
    re-read) INSIDE one serialized ``BEGIN IMMEDIATE`` unit, so a ``hold``,
    ``kill``, new nonterminal effect or qualifying incident that commits at
    any point before this transaction acquires the write lock wins, and a
    losing resume consumes nothing and clears nothing. ``incident_probe``
    is a callable re-invoked inside the unit; the incident ledger lives in
    a separate database, so its answer is fresh-at-lock rather than
    transactional — the residual cross-database window is the probe's own
    execution time and is recorded as a known limitation.
    """
    now = now or datetime.now(timezone.utc)
    effect_id = str(payload["resume_of_effect_id"])
    if not callable(incident_probe):
        raise L1AuthorizationError("incident_probe must be callable")

    # Pure validation of the caller-supplied evidence document only; no
    # ledger state is involved here.
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
            halt = olap.get_state("halt", "none")
            last_resume_raw = olap.get_state("last_resume", "")
            last_resume = (json.loads(last_resume_raw)
                           if last_resume_raw else {})

            if halt == "none":
                if last_resume.get("nonce_sha256") == record.nonce_sha256:
                    # Retry after a committed success is a no-op.
                    return {"applied": False, "already_resumed": True,
                            "effect_id": effect_id,
                            "evidence_sha256":
                            last_resume.get("evidence_sha256")}
                raise L1AuthorizationError(
                    "no hold is active; nothing to resume (capability not"
                    " consumed)")
            if halt != "hold":
                raise L1AuthorizationError(
                    f"halt state {halt!r} is not 'hold'; this operation"
                    " clears only a reconciliation hold")
            if olap.nonce_consumed(record.nonce_sha256):
                # A spent capability can never clear a later hold.
                raise L1AuthorizationError(
                    "this capability was already consumed; the current hold"
                    " needs a freshly minted capability")

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
                    f"bound recovery effect {effect_id} is"
                    f" {bound['state']!r}, not terminal")

            active_incidents = incident_probe()
            if active_incidents is None:
                raise L1AuthorizationError(
                    "incident state could not be established — unknown is"
                    " a refusal")
            if active_incidents:
                codes = sorted({str(i.get("event_code"))
                                for i in active_incidents})
                raise L1AuthorizationError(
                    f"originating incident condition still active: {codes};"
                    " resume refused")

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
