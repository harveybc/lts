"""Owner-issued, single-use IBKR Paper capability: validation and gate.

Corrects finding 064 with the owner-ratified constraints (2026-08-03):

- **privileged authority separation** — minting exists ONLY in the offline,
  TTY-interactive CLI ``tools/mint_paper_capability.py``. This module (and
  the executor) can read, validate and consume a capability; neither
  contains any code path that writes one. On a single-user machine this
  separation is structural and procedural, not cryptographic; that residual
  risk is declared to the auditor, not hidden.
- **fixed protected storage** — capabilities live only in
  ``~/.lts/paper_capabilities`` (directory 0700, files 0600). The gate
  refuses world/group-readable material and refuses ambiguity: exactly one
  valid capability may be present.
- **one bracket per capability** — ``max_entries`` is fixed to 1 and the
  ledger burn (UNIQUE digest + UNIQUE nonce hash) makes reuse impossible.
- **short expiry** — validity is at most one hour; the default mint is 15
  minutes.
- **atomic consumption in the existing L0 ledger** — the burn happens in
  ``BracketExecutor.submit_bracket`` inside the same ``BEGIN IMMEDIATE``
  unit as the first durable effect record, in the same SQLite file as the
  accepted L0 decisions/reservations/lifecycle (no parallel truth).
- **no broker connectivity** — nothing in the mint or validation path
  imports a broker library or opens a socket.

Restart status model (cold start §8B): ``issued`` (valid file, no ledger
row) → ``consumed_before_effect`` / ``effect_unknown`` /
``submitted_pending_ack`` / ``acknowledged`` / terminal, all derived from
the durable ledger, never from memory.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.ibkr_l1_adapter import (
    FINGERPRINT_ALGORITHM,
    L1AuthorizationError,
    L1Profile,
    _canonical,
)
from app.ibkr_l1_executor import CapabilityRecord
from app.ibkr_l1_journal import L1ExecutionOlap

CAPABILITY_SCHEMA_VERSION = "lts.ibkr_paper_capability.v1"
CAPABILITY_STORE = Path.home() / ".lts" / "paper_capabilities"
MAX_VALIDITY_SECONDS = 3600
DEFAULT_VALIDITY_SECONDS = 900
_ISSUED_AT_SKEW_SECONDS = 60.0
_MAX_RISK_FRACTION = 0.01

_REQUIRED_KEYS = (
    "schema_version", "venue", "host", "port", "profile_hash",
    "profile_schema_version", "account_fingerprint_algorithm",
    "account_fingerprint", "asset_id", "instrument", "contract_con_id",
    "max_risk_fraction_at_stop", "quantity_ceiling", "max_entries",
    "issued_at", "expires_at", "nonce",
)


def capability_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _parse_utc(value: Any, key: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise L1AuthorizationError(f"capability {key} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise L1AuthorizationError(f"capability {key} must be timezone-aware")
    return parsed


def validate_capability(
    payload: dict[str, Any],
    *,
    profile: L1Profile,
    now: Optional[datetime] = None,
) -> CapabilityRecord:
    """Validate one capability document against the strict v1 schema and the
    exact profile binding. Returns the consumable record (digests and safe
    metadata only). Every failure is a refusal; nothing degrades."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict):
        raise L1AuthorizationError("capability must be a JSON object")
    missing = [key for key in _REQUIRED_KEYS if key not in payload]
    if missing:
        raise L1AuthorizationError(f"capability missing keys: {missing}")
    unknown = sorted(set(payload) - set(_REQUIRED_KEYS))
    if unknown:
        raise L1AuthorizationError(f"capability has unknown keys: {unknown}")
    if payload["schema_version"] != CAPABILITY_SCHEMA_VERSION:
        raise L1AuthorizationError(
            f"capability schema must be {CAPABILITY_SCHEMA_VERSION!r}"
        )
    if payload["venue"] != "ibkr_paper":
        raise L1AuthorizationError("capability venue must be 'ibkr_paper'")
    if payload["host"] != "127.0.0.1":
        raise L1AuthorizationError("capability host must be loopback 127.0.0.1")
    if int(payload["port"]) != 7497:
        raise L1AuthorizationError("capability port must be the TWS Paper port 7497")
    if payload["profile_hash"] != profile.profile_hash:
        raise L1AuthorizationError(
            "capability does not bind to the loaded profile hash"
        )
    if payload["profile_schema_version"] != profile.schema_version:
        raise L1AuthorizationError("capability profile schema version mismatch")
    if payload["account_fingerprint_algorithm"] != FINGERPRINT_ALGORITHM:
        raise L1AuthorizationError(
            f"capability fingerprint algorithm must be {FINGERPRINT_ALGORITHM!r}"
        )
    if payload["account_fingerprint"] != profile.account_fingerprint:
        raise L1AuthorizationError(
            "capability account fingerprint does not match the profile"
        )
    if payload["asset_id"] != profile.asset_id:
        raise L1AuthorizationError("capability asset does not match the profile")
    if payload["instrument"] != profile.instrument:
        raise L1AuthorizationError("capability instrument does not match the profile")
    con_id = payload["contract_con_id"]
    if con_id is not None and (not isinstance(con_id, int) or con_id <= 0):
        raise L1AuthorizationError("capability contract_con_id must be null or a positive int")
    risk = float(payload["max_risk_fraction_at_stop"])
    if not 0.0 < risk <= _MAX_RISK_FRACTION:
        raise L1AuthorizationError(
            f"capability max_risk_fraction_at_stop must be in (0, {_MAX_RISK_FRACTION}]"
        )
    ceiling = float(payload["quantity_ceiling"])
    if not 0.0 < ceiling <= profile.quantity_ceiling:
        raise L1AuthorizationError(
            "capability quantity_ceiling must be positive and within the "
            f"profile ceiling {profile.quantity_ceiling}"
        )
    if payload["max_entries"] != 1:
        raise L1AuthorizationError(
            "one bracket per capability: max_entries must be exactly 1"
        )
    issued_at = _parse_utc(payload["issued_at"], "issued_at")
    expires_at = _parse_utc(payload["expires_at"], "expires_at")
    if (issued_at - now).total_seconds() > _ISSUED_AT_SKEW_SECONDS:
        raise L1AuthorizationError("capability issued_at is in the future")
    if (expires_at - issued_at).total_seconds() > MAX_VALIDITY_SECONDS:
        raise L1AuthorizationError(
            f"capability validity exceeds {MAX_VALIDITY_SECONDS}s"
        )
    if expires_at <= issued_at:
        raise L1AuthorizationError("capability expires before it is issued")
    if now >= expires_at:
        raise L1AuthorizationError("capability is expired")
    nonce = str(payload["nonce"])
    if len(nonce) < 32 or any(c not in "0123456789abcdef" for c in nonce):
        raise L1AuthorizationError(
            "capability nonce must be at least 32 lowercase hex chars"
        )
    return CapabilityRecord(
        capability_sha256=capability_digest(payload),
        nonce_sha256=hashlib.sha256(nonce.encode()).hexdigest(),
        metadata={
            "schema_version": CAPABILITY_SCHEMA_VERSION,
            "profile_hash": profile.profile_hash,
            "account_fingerprint": profile.account_fingerprint,
            "instrument": profile.instrument,
            "asset_id": profile.asset_id,
            "contract_con_id": con_id,
            "max_risk_fraction_at_stop": risk,
            "quantity_ceiling": ceiling,
            "max_entries": 1,
            "issued_at": issued_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
    )


def capability_status(
    olap: L1ExecutionOlap, record: CapabilityRecord
) -> dict[str, Any]:
    """Durable status of one capability: issued, or consumed plus the exact
    effect state its burn is bound to."""
    row = olap.capability_row(record.capability_sha256)
    if row is None:
        return {"status": "issued", "effect_state": None}
    effect = olap.effect_row(row["consumed_effect_id"])
    effect_state = None if effect is None else effect["state"]
    if effect_state == "journaled_pending":
        attempts = olap.broker_facts(row["consumed_effect_id"], "call_attempt")
        classification = (
            "consumed_before_effect" if not attempts else "effect_unknown"
        )
    else:
        classification = effect_state
    return {
        "status": "consumed",
        "consumed_at": row["consumed_at"],
        "effect_id": row["consumed_effect_id"],
        "effect_state": effect_state,
        "classification": classification,
    }


class CapabilityGate:
    """Loads the single valid capability from the fixed protected store.

    The executor consumes what the gate returns; it can never mint. The
    store path is fixed in production; tests may inject a directory, which
    changes nothing about validation or permission enforcement.
    """

    def __init__(self, store_dir: Optional[Path] = None) -> None:
        self.store_dir = Path(store_dir) if store_dir else CAPABILITY_STORE

    def _check_permissions(self, path: Path, expected: int, kind: str) -> None:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            raise L1AuthorizationError(
                f"capability {kind} {path} has permissive mode {oct(mode)}; "
                f"required {oct(expected)}"
            )

    def load(
        self,
        profile: L1Profile,
        *,
        olap: Optional[L1ExecutionOlap] = None,
        now: Optional[datetime] = None,
    ) -> tuple[dict[str, Any], CapabilityRecord]:
        """Return the exactly-one valid, unconsumed capability, fail-closed.

        Zero valid capabilities, more than one valid capability, permissive
        file modes and already-burned digests are all refusals.
        """
        if not self.store_dir.is_dir():
            raise L1AuthorizationError(
                f"capability store {self.store_dir} does not exist; the owner "
                "must mint a capability with tools/mint_paper_capability.py"
            )
        self._check_permissions(self.store_dir, 0o700, "store")
        candidates: list[tuple[dict[str, Any], CapabilityRecord]] = []
        refusals: list[str] = []
        for path in sorted(self.store_dir.glob("*.json")):
            try:
                self._check_permissions(path, 0o600, "file")
                payload = json.loads(path.read_text())
                record = validate_capability(payload, profile=profile, now=now)
                if olap is not None and (
                    olap.capability_row(record.capability_sha256) is not None
                    or olap.nonce_consumed(record.nonce_sha256)
                ):
                    refusals.append(f"{path.name}: already consumed")
                    continue
                candidates.append((payload, record))
            except (L1AuthorizationError, ValueError, OSError) as error:
                refusals.append(f"{path.name}: {error}")
        if not candidates:
            raise L1AuthorizationError(
                "no valid unconsumed capability in the store; refusals: "
                + ("; ".join(refusals) if refusals else "store is empty")
            )
        if len(candidates) > 1:
            raise L1AuthorizationError(
                f"{len(candidates)} valid capabilities present; ambiguity is a "
                "refusal — the owner keeps exactly one live capability"
            )
        return candidates[0]
