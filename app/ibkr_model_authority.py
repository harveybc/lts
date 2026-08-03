"""Revocable, bounded authority for continuous model execution on IBKR Paper."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from trading_contracts import OrderIntentV2

from app.ibkr_l1_adapter import FINGERPRINT_ALGORITHM, L1AuthorizationError
from app.ibkr_l1_executor import CapabilityRecord
from app.ibkr_l1_journal import L1ExecutionOlap


PROFILE_SCHEMA = "lts.ibkr.paper.model_profile.v1"
MANDATE_SCHEMA = "lts.ibkr.paper.model_mandate.v1"
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")
_INSTRUMENT_RE = re.compile(r"^[A-Z]{3}\.[A-Z]{3}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _positive(payload: dict[str, Any], name: str, maximum: float) -> float:
    try:
        value = float(payload[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise L1AuthorizationError(f"continuous Paper profile {name} is invalid") from exc
    if not math.isfinite(value) or not 0 < value <= maximum:
        raise L1AuthorizationError(
            f"continuous Paper profile {name} must be in (0, {maximum}]"
        )
    return value


@dataclass(frozen=True)
class ContinuousPaperProfile:
    schema_version: str
    venue: str
    environment: str
    host: str
    port: int
    client_id: int
    account_fingerprint_algorithm: str
    account_fingerprint: str
    instrument: str
    asset_id: str
    max_entries_per_day: int
    quantity_ceiling: float
    stop_distance_price_max: float
    take_profit_distance_price_max: float
    max_spread_price: float
    contract_con_id: Optional[int]
    profile_hash: str

    @property
    def max_orders_this_activation(self) -> int:
        return self.max_entries_per_day

    @property
    def entry_budget_scope(self) -> str:
        return "utc_day"

    @classmethod
    def load(cls, path: str | Path) -> "ContinuousPaperProfile":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        required = {
            "schema_version", "venue", "environment", "host", "port",
            "client_id", "account_fingerprint_algorithm", "account_fingerprint",
            "instrument", "asset_id", "max_entries_per_day", "quantity_ceiling",
            "stop_distance_price_max", "take_profit_distance_price_max",
            "max_spread_price", "contract_con_id",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise L1AuthorizationError("continuous Paper profile keys are not exact")
        if payload["schema_version"] != PROFILE_SCHEMA:
            raise L1AuthorizationError("unsupported continuous Paper profile")
        if (
            payload["venue"] != "ibkr_paper"
            or payload["environment"] != "paper"
            or payload["host"] != "127.0.0.1"
            or int(payload["port"]) != 7497
        ):
            raise L1AuthorizationError("continuous authority is IBKR Paper loopback only")
        client_id = int(payload["client_id"])
        if not 1 <= client_id <= 999:
            raise L1AuthorizationError("continuous Paper client_id is invalid")
        if payload["account_fingerprint_algorithm"] != FINGERPRINT_ALGORITHM:
            raise L1AuthorizationError("continuous Paper fingerprint algorithm is invalid")
        fingerprint = str(payload["account_fingerprint"])
        if not _FINGERPRINT_RE.fullmatch(fingerprint):
            raise L1AuthorizationError("continuous Paper account fingerprint is invalid")
        instrument = str(payload["instrument"])
        if not _INSTRUMENT_RE.fullmatch(instrument):
            raise L1AuthorizationError("continuous Paper route must be one FX pair")
        base, quote = instrument.split(".")
        asset_id = f"fx:{base}/{quote}"
        if payload["asset_id"] != asset_id:
            raise L1AuthorizationError("continuous Paper asset/instrument binding differs")
        budget = int(payload["max_entries_per_day"])
        if not 1 <= budget <= 24:
            raise L1AuthorizationError("continuous Paper daily entry budget is invalid")
        con_id = payload["contract_con_id"]
        if con_id is not None and int(con_id) <= 0:
            raise L1AuthorizationError("continuous Paper contract_con_id is invalid")
        return cls(
            schema_version=PROFILE_SCHEMA, venue="ibkr_paper", environment="paper",
            host="127.0.0.1", port=7497, client_id=client_id,
            account_fingerprint_algorithm=FINGERPRINT_ALGORITHM,
            account_fingerprint=fingerprint, instrument=instrument,
            asset_id=asset_id, max_entries_per_day=budget,
            quantity_ceiling=_positive(payload, "quantity_ceiling", 1_000_000),
            stop_distance_price_max=_positive(
                payload, "stop_distance_price_max", 0.1
            ),
            take_profit_distance_price_max=_positive(
                payload, "take_profit_distance_price_max", 0.1
            ),
            max_spread_price=_positive(payload, "max_spread_price", 0.01),
            contract_con_id=None if con_id is None else int(con_id),
            profile_hash=hashlib.sha256(_canonical(payload)).hexdigest(),
        )


class ContinuousPaperGate:
    """Derive one ledger-burned capability per intent from a local mandate."""

    def __init__(self, mandate_path: str | Path) -> None:
        self.mandate_path = Path(mandate_path).expanduser()

    @staticmethod
    def _parse_time(value: Any, name: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise L1AuthorizationError(f"Paper mandate {name} is invalid") from exc
        if parsed.tzinfo is None:
            raise L1AuthorizationError(f"Paper mandate {name} needs a timezone")
        return parsed.astimezone(timezone.utc)

    def load_for_intent(
        self,
        profile: ContinuousPaperProfile,
        intent: OrderIntentV2,
        *,
        olap: Optional[L1ExecutionOlap] = None,
        now: Optional[datetime] = None,
    ) -> tuple[dict[str, Any], CapabilityRecord]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not self.mandate_path.is_file():
            raise L1AuthorizationError("continuous Paper mandate is absent")
        if stat.S_IMODE(os.stat(self.mandate_path).st_mode) & 0o077:
            raise L1AuthorizationError("continuous Paper mandate must have mode 0600")
        payload = json.loads(self.mandate_path.read_text(encoding="utf-8"))
        required = {
            "schema", "environment", "venue", "profile_hash", "asset_id",
            "instrument", "execution_tier", "issued_at", "expires_at",
            "max_risk_fraction_at_stop", "quantity_ceiling",
            "max_entries_per_day", "mandate_id",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise L1AuthorizationError("continuous Paper mandate keys are not exact")
        if (
            payload["schema"] != MANDATE_SCHEMA
            or payload["environment"] != "paper"
            or payload["venue"] != "ibkr_paper"
            or payload["execution_tier"] != "demo_research_canary"
        ):
            raise L1AuthorizationError("continuous mandate is Paper research only")
        if (
            payload["profile_hash"] != profile.profile_hash
            or payload["asset_id"] != profile.asset_id
            or payload["instrument"] != profile.instrument
        ):
            raise L1AuthorizationError("continuous mandate/profile binding differs")
        if not self._parse_time(payload["issued_at"], "issued_at") <= now < self._parse_time(
            payload["expires_at"], "expires_at"
        ):
            raise L1AuthorizationError("continuous Paper mandate is outside validity")
        risk_ceiling = float(payload["max_risk_fraction_at_stop"])
        quantity_ceiling = float(payload["quantity_ceiling"])
        daily_ceiling = int(payload["max_entries_per_day"])
        if (
            not 0 < risk_ceiling <= 0.01
            or not 0 < quantity_ceiling <= profile.quantity_ceiling
            or not 1 <= daily_ceiling <= profile.max_entries_per_day
        ):
            raise L1AuthorizationError("continuous Paper mandate ceilings are invalid")
        if intent.risk is None or intent.risk.risk_fraction_at_stop > risk_ceiling:
            raise L1AuthorizationError("intent risk exceeds continuous Paper mandate")
        evidence = intent.preflight or {}
        for name in (
            "source_artifact_sha256", "source_config_sha256", "source_input_sha256"
        ):
            value = str(evidence.get(name, "")).removeprefix("sha256:")
            if not _HASH_RE.fullmatch(value):
                raise L1AuthorizationError("continuous Paper intent lacks model evidence")
        mandate_sha = hashlib.sha256(_canonical(payload)).hexdigest()
        identity = {
            "mandate_sha256": mandate_sha,
            "idempotency_key": intent.idempotency_key,
            "artifact": evidence["source_artifact_sha256"],
            "config": evidence["source_config_sha256"],
            "input": evidence["source_input_sha256"],
        }
        capability_sha = hashlib.sha256(_canonical(identity)).hexdigest()
        nonce_sha = hashlib.sha256(
            f"{payload['mandate_id']}|{intent.idempotency_key}".encode()
        ).hexdigest()
        metadata = {
            "authority": "continuous_paper_model_mandate",
            "mandate_sha256": mandate_sha,
            "max_risk_fraction_at_stop": risk_ceiling,
            "quantity_ceiling": quantity_ceiling,
            "max_entries_per_day": daily_ceiling,
            "contract_con_id": profile.contract_con_id,
            "expires_at": payload["expires_at"],
        }
        return payload, CapabilityRecord(capability_sha, nonce_sha, metadata)
