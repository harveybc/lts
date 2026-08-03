"""IBKR Paper L1 venue binding: strict profile, bracket plan, serialize sink.

Corrected after audit `AUDIT_SATOSHI_II_IBKR_L1_ADAPTER_2026_08_03.md`:

- finding 063: the former ``submit_bracket()`` reported success without any
  broker call. It is REMOVED. The only submission path is
  ``app.ibkr_l1_executor.BracketExecutor``, which journals before every
  broker call and never returns success without acknowledged broker facts.
- finding 064: the former ``L1Authorization`` (repository phrase plus an
  arbitrary local token) is REMOVED. Authority is a separately minted,
  short-lived, single-use owner capability (``app.ibkr_l1_capability``)
  consumed atomically in the durable L0 ledger.
- finding 067: ``L1Profile`` is now a strict v2 schema: exact key set,
  exact Paper venue and loopback host, bounded client id, one-or-two entry
  budget, positive finite numeric ceilings, labeled fingerprint algorithm,
  exact asset/instrument binding. Every retained field is enforced by the
  executor/consumer path or was removed.
- finding 068: the fingerprint algorithm is explicit —
  ``account_id_sha256_16`` is ``sha256(account_id)[:16]`` of the single
  connected account, never the double-hashed account-set digest.

This module still owns the deterministic bracket translation
(``build_bracket``) with the official TWS transmission order:
parent ``Transmit=False``, take-profit ``Transmit=False``, stop-loss
``Transmit=True`` last.

References:
https://interactivebrokers.github.io/tws-api/bracket_order.html
https://interactivebrokers.github.io/tws-api/order_submission.html
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from trading_contracts import OrderIntentV2

IB_ASYNC_VERSION = "2.1.0"
ADAPTER_VERSION = "lts.ibkr.paper.l1.v2"
PROFILE_SCHEMA_VERSION = "lts.ibkr.paper.l1.profile.v2"
FINGERPRINT_ALGORITHM = "account_id_sha256_16"

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")
_INSTRUMENT_RE = re.compile(r"^[A-Z]{3}\.[A-Z]{3}$")

# Canary-scoped sanity ceilings: a profile beyond these is a typo, not a
# bigger canary. Raising them is an owner decision on a new profile version.
_MAX_QUANTITY_CEILING = 1_000_000.0
_MAX_DISTANCE_PRICE = 0.1
_MAX_SPREAD_PRICE = 0.01


class L1AuthorizationError(RuntimeError):
    """The activation gate refused. No socket was opened."""


class L1ExecutionError(RuntimeError):
    """A submitted lifecycle failed its protection contract."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _positive_finite(payload: Mapping[str, Any], key: str, maximum: float) -> float:
    try:
        value = float(payload[key])
    except (TypeError, ValueError) as exc:
        raise L1AuthorizationError(f"L1 profile {key} is not numeric") from exc
    if not value > 0.0 or value != value or value in (float("inf"), float("-inf")):
        raise L1AuthorizationError(f"L1 profile {key} must be positive and finite")
    if value > maximum:
        raise L1AuthorizationError(
            f"L1 profile {key}={value} exceeds the canary sanity ceiling {maximum}"
        )
    return value


@dataclass(frozen=True)
class L1Profile:
    """Versioned venue/account binding. Contains NO authorization secret.

    Enforcement map (finding 067 — every field enforced or removed):

    - venue/environment/host/port/client_id: connection gate here and in the
      capability validator;
    - account_fingerprint(+algorithm): connect-time identity check and
      capability binding;
    - instrument/asset_id: intent binding in the outbox consumer;
    - max_orders_this_activation: entry budget in the outbox consumer;
    - quantity_ceiling: hard ceiling in executor and capability validator
      (never a sizing input — sizing is L0 ``plan_units``);
    - stop_distance_price_max / take_profit_distance_price_max: geometry
      ceilings in the outbox consumer;
    - max_spread_price: quote-quality gate in the outbox consumer and the
      connected preflight.
    """

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
    max_orders_this_activation: int
    quantity_ceiling: float
    stop_distance_price_max: float
    take_profit_distance_price_max: float
    max_spread_price: float
    profile_hash: str

    _REQUIRED = (
        "schema_version", "venue", "environment", "host", "port", "client_id",
        "account_fingerprint_algorithm", "account_fingerprint", "instrument",
        "asset_id", "max_orders_this_activation", "quantity_ceiling",
        "stop_distance_price_max", "take_profit_distance_price_max",
        "max_spread_price",
    )

    @classmethod
    def load(cls, path: str | Path) -> "L1Profile":
        payload = json.loads(Path(path).read_text())
        if not isinstance(payload, dict):
            raise L1AuthorizationError("L1 profile must be a JSON object")
        missing = [key for key in cls._REQUIRED if key not in payload]
        if missing:
            raise L1AuthorizationError(f"L1 profile missing keys: {missing}")
        unknown = sorted(set(payload) - set(cls._REQUIRED))
        if unknown:
            raise L1AuthorizationError(f"L1 profile has unknown keys: {unknown}")
        if payload["schema_version"] != PROFILE_SCHEMA_VERSION:
            raise L1AuthorizationError(
                f"L1 profile schema must be {PROFILE_SCHEMA_VERSION!r}"
            )
        if payload["venue"] != "ibkr_paper":
            raise L1AuthorizationError("L1 profile venue must be 'ibkr_paper'")
        if payload["environment"] != "paper":
            raise L1AuthorizationError(
                "L1 profile environment must be 'paper'; live is never an L1 concept"
            )
        if payload["host"] != "127.0.0.1":
            raise L1AuthorizationError("L1 profile host must be loopback 127.0.0.1")
        if int(payload["port"]) != 7497:
            raise L1AuthorizationError(
                "L1 profile port must be the TWS Paper port 7497"
            )
        client_id = int(payload["client_id"])
        if not 1 <= client_id <= 999:
            raise L1AuthorizationError("L1 profile client_id must be in [1, 999]")
        if payload["account_fingerprint_algorithm"] != FINGERPRINT_ALGORITHM:
            raise L1AuthorizationError(
                f"L1 profile fingerprint algorithm must be {FINGERPRINT_ALGORITHM!r}"
                " (finding 068: never the double-hashed account-set digest)"
            )
        fingerprint = str(payload["account_fingerprint"])
        if not _FINGERPRINT_RE.match(fingerprint):
            raise L1AuthorizationError(
                "L1 profile account_fingerprint must be 16 lowercase hex chars"
            )
        instrument = str(payload["instrument"])
        if not _INSTRUMENT_RE.match(instrument):
            raise L1AuthorizationError(
                "L1 profile instrument must be an FX pair like 'EUR.USD'"
            )
        base, quote = instrument.split(".")
        if payload["asset_id"] != f"fx:{base}/{quote}":
            raise L1AuthorizationError(
                "L1 profile asset_id must bind exactly to the instrument "
                f"(expected 'fx:{base}/{quote}')"
            )
        budget = int(payload["max_orders_this_activation"])
        if budget not in (1, 2):
            raise L1AuthorizationError(
                "the canary activation permits 1 or 2 entries (one long, one short)"
            )
        return cls(
            schema_version=PROFILE_SCHEMA_VERSION,
            venue="ibkr_paper",
            environment="paper",
            host="127.0.0.1",
            port=7497,
            client_id=client_id,
            account_fingerprint_algorithm=FINGERPRINT_ALGORITHM,
            account_fingerprint=fingerprint,
            instrument=instrument,
            asset_id=str(payload["asset_id"]),
            max_orders_this_activation=budget,
            quantity_ceiling=_positive_finite(
                payload, "quantity_ceiling", _MAX_QUANTITY_CEILING),
            stop_distance_price_max=_positive_finite(
                payload, "stop_distance_price_max", _MAX_DISTANCE_PRICE),
            take_profit_distance_price_max=_positive_finite(
                payload, "take_profit_distance_price_max", _MAX_DISTANCE_PRICE),
            max_spread_price=_positive_finite(
                payload, "max_spread_price", _MAX_SPREAD_PRICE),
            profile_hash=_hash(payload),
        )


class SubmissionSink(Protocol):
    """The interface both the L0 zero-network sink and this adapter honor."""

    def serialize(self, intent: OrderIntentV2) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class BracketPlan:
    """Three orders, fully constructed before anything is transmitted."""

    parent: dict[str, Any]
    take_profit: dict[str, Any]
    stop_loss: dict[str, Any]

    def transmission_order(self) -> list[dict[str, Any]]:
        """Official TWS order: parent, TP, then SL with Transmit=True."""
        return [self.parent, self.take_profit, self.stop_loss]


def build_bracket(
    intent: OrderIntentV2,
    *,
    parent_order_id: int,
    account: str,
    price_decimals: int,
    quantity_decimals: int,
) -> BracketPlan:
    """Construct the protected bracket from an accepted OrderIntentV2.

    The intent already guarantees both protective legs and side/price
    geometry (contract findings 039/043); this function only translates and
    rounds to venue precision, then asserts the geometry survived rounding.
    """
    if intent.intent_class != "risk_increasing" or intent.protection is None:
        raise L1ExecutionError("bracket requires a protected risk-increasing intent")
    side_long = intent.delta_units > 0
    action = "BUY" if side_long else "SELL"
    child_action = "SELL" if side_long else "BUY"
    quantity = round(abs(intent.delta_units), quantity_decimals)
    if quantity <= 0:
        raise L1ExecutionError("quantity rounded to zero at venue precision")
    stop = round(intent.protection.stop_loss_price, price_decimals)
    take = round(intent.protection.take_profit_price, price_decimals)
    if side_long and not stop < take:
        raise L1ExecutionError("rounding destroyed long bracket geometry")
    if not side_long and not stop > take:
        raise L1ExecutionError("rounding destroyed short bracket geometry")

    parent = {
        "orderId": parent_order_id,
        "action": action,
        "orderType": "MKT",
        "totalQuantity": quantity,
        "account": account,
        "transmit": False,          # official sequence: parent never transmits
        "tif": "DAY",
        "outsideRth": False,
    }
    take_profit = {
        "orderId": parent_order_id + 1,
        "parentId": parent_order_id,
        "action": child_action,
        "orderType": "LMT",
        "lmtPrice": take,
        "totalQuantity": quantity,
        "account": account,
        "transmit": False,          # still false: the group is incomplete
        "tif": "GTC",
        "outsideRth": False,
    }
    stop_loss = {
        "orderId": parent_order_id + 2,
        "parentId": parent_order_id,
        "action": child_action,
        "orderType": "STP",
        "auxPrice": stop,
        "totalQuantity": quantity,
        "account": account,
        "transmit": True,           # the last child transmits the whole group
        "tif": "GTC",
        "outsideRth": False,
    }
    return BracketPlan(parent=parent, take_profit=take_profit, stop_loss=stop_loss)


# Finding 065: the former presence-only verify_bracket_acknowledgement was
# REMOVED. The only acknowledgement authority is
# app.ibkr_l1_recovery.verify_bracket_exact, which requires status, account,
# contract, type, price, TIF and parent-link agreement from direct broker
# facts, and whose controller EXECUTES cancel/flatten/hold instead of
# returning a string recommendation.


class IbkrPaperL1Sink:
    """Serialize sink plus a READ-ONLY connection helper.

    This class holds no submission path (finding 063: the former lying
    ``submit_bracket`` is removed; ``BracketExecutor`` is the only door) and
    no self-mintable authorization (finding 064). Its connection helper is
    hard-coded read-only: a write-capable session is a later, separately
    audited owner activation step that does not exist in this codebase yet.
    """

    def __init__(self, profile: L1Profile) -> None:
        self.profile = profile
        self.would_be_orders = 0
        self._ib = None

    # -- connection (read-only, zero-submit preflights only) ---------------
    def connect_readonly(self):
        """Open a READ-ONLY TWS Paper session. Fail closed on any mismatch."""
        try:
            import ib_async
            from ib_async import IB
        except ImportError as exc:  # pragma: no cover — env-dependent
            raise L1ExecutionError(
                f"Install ib_async=={IB_ASYNC_VERSION} in the trading-stack environment"
            ) from exc
        if ib_async.__version__ != IB_ASYNC_VERSION:
            raise L1ExecutionError(
                f"Expected ib_async {IB_ASYNC_VERSION}, found {ib_async.__version__}"
            )
        ib = IB()
        ib.connect(
            self.profile.host,
            self.profile.port,
            clientId=self.profile.client_id,
            timeout=15.0,
            readonly=True,           # zero-submit by construction
            raiseSyncErrors=True,
        )
        accounts = list(ib.managedAccounts())
        if not accounts:
            ib.disconnect()
            raise L1ExecutionError("TWS returned no managed account")
        if any(not account.upper().startswith("DU") for account in accounts):
            ib.disconnect()
            raise L1ExecutionError(
                "connected account is not an IBKR Paper DU account; refusing"
            )
        fingerprint = hashlib.sha256(accounts[0].encode()).hexdigest()[:16]
        if fingerprint != self.profile.account_fingerprint:
            ib.disconnect()
            raise L1ExecutionError(
                "connected account fingerprint does not match the authorized "
                f"profile ({FINGERPRINT_ALGORITHM})"
            )
        self._ib = ib
        return ib

    def disconnect(self) -> None:
        if self._ib is not None:
            self._ib.disconnect()
            self._ib = None

    # -- the sink interface ------------------------------------------------
    def serialize(self, intent: OrderIntentV2) -> dict[str, Any]:
        """Same shape the L0 sink produces, so the service is unchanged."""
        side = "BUY" if intent.delta_units > 0 else "SELL"
        payload = {
            "adapter": ADAPTER_VERSION,
            "venue": intent.venue,
            "instrument": intent.instrument,
            "side": side,
            "total_quantity": abs(intent.delta_units),
            "order_type": intent.order_type.upper(),
            "limit_price": intent.limit_price,
            "entry_trigger_price": intent.entry_trigger_price,
            "bracket": None
            if intent.protection is None
            else {
                "stop_loss_price": intent.protection.stop_loss_price,
                "take_profit_price": intent.protection.take_profit_price,
                "transmit_rule": "parent_and_children_atomic",
            },
            "reduce_action": intent.reduce_action,
            "idempotency_key": intent.idempotency_key,
            "intent_class": intent.intent_class,
        }
        self.would_be_orders += 1
        return payload
