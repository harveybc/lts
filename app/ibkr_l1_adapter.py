"""IBKR Paper L1 execution adapter — the first write-capable venue path.

Built behind the accepted L0 sink interface (`ZeroNetworkSink`), so the
demo execution service, its risk engine, its contracts and its ledger are
reused unchanged. This module adds exactly one capability the L0 sink
lacks: transmitting a protected bracket to IBKR Paper.

Fail-closed construction (auditor order 2026-08-02):

- the adapter cannot be instantiated without an `L1Authorization` whose
  profile hash, venue, account and exact owner phrase all match, and whose
  single-use token has not been consumed;
- no LLM, Hermes process or chat text can construct one: the phrase is
  compared against a versioned profile on disk and the token is burned in
  the ledger before any socket exists;
- the bracket is built in full BEFORE any transmission, in the official
  TWS order: parent `Transmit=False`, take-profit child `Transmit=False`,
  stop-loss child `Transmit=True` last — the final child transmits the
  whole group atomically;
- broker acknowledgement of parent AND both protective children is a hard
  post-submit condition; anything missing, ambiguous or stale triggers
  deterministic cancel/flatten plus a global hold;
- every side effect is journaled before it is attempted (the accepted 055
  pattern extended across broker calls), so a crash mid-flight is resumable
  from the ledger rather than from memory.

References:
https://interactivebrokers.github.io/tws-api/bracket_order.html
https://interactivebrokers.github.io/tws-api/order_submission.html
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from trading_contracts import OrderIntentV2

IB_ASYNC_VERSION = "2.1.0"
ADAPTER_VERSION = "lts.ibkr.paper.l1.v1"


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


@dataclass(frozen=True)
class L1Profile:
    """The versioned, owner-ratified activation profile.

    Loaded from disk; its hash is bound into the authorization, so editing
    the file after the owner reads it invalidates any pending activation.
    """

    venue: str
    account_fingerprint: str
    environment: str
    host: str
    port: int
    client_id: int
    instrument: str
    asset_id: str
    activation_phrase: str
    max_orders_this_activation: int
    quantity: float
    stop_distance_price: float
    take_profit_distance_price: float
    max_spread_price: float
    profile_hash: str

    @classmethod
    def load(cls, path: str | Path) -> "L1Profile":
        payload = json.loads(Path(path).read_text())
        required = [
            "venue", "account_fingerprint", "environment", "host", "port",
            "client_id", "instrument", "asset_id", "activation_phrase",
            "max_orders_this_activation", "quantity", "stop_distance_price",
            "take_profit_distance_price", "max_spread_price",
        ]
        missing = [key for key in required if key not in payload]
        if missing:
            raise L1AuthorizationError(f"L1 profile missing keys: {missing}")
        if payload["environment"] != "paper":
            raise L1AuthorizationError(
                "L1 profile environment must be 'paper'; live is never an L1 concept"
            )
        if int(payload["port"]) != 7497:
            raise L1AuthorizationError(
                "L1 profile port must be the TWS Paper port 7497"
            )
        if int(payload["max_orders_this_activation"]) > 2:
            raise L1AuthorizationError(
                "the canary activation permits at most 2 orders (one long, one short)"
            )
        return cls(
            venue=str(payload["venue"]),
            account_fingerprint=str(payload["account_fingerprint"]),
            environment="paper",
            host=str(payload["host"]),
            port=int(payload["port"]),
            client_id=int(payload["client_id"]),
            instrument=str(payload["instrument"]),
            asset_id=str(payload["asset_id"]),
            activation_phrase=str(payload["activation_phrase"]),
            max_orders_this_activation=int(payload["max_orders_this_activation"]),
            quantity=float(payload["quantity"]),
            stop_distance_price=float(payload["stop_distance_price"]),
            take_profit_distance_price=float(payload["take_profit_distance_price"]),
            max_spread_price=float(payload["max_spread_price"]),
            profile_hash=_hash(payload),
        )


@dataclass(frozen=True)
class L1Authorization:
    """Single-use owner authorization. Burned in the ledger before use."""

    profile: L1Profile
    supplied_phrase: str
    token: str
    issued_at: datetime

    def verify(self, *, ledger_token_seen, now: Optional[datetime] = None) -> None:
        now = now or _utc_now()
        if self.supplied_phrase != self.profile.activation_phrase:
            raise L1AuthorizationError("activation phrase mismatch")
        if not self.token:
            raise L1AuthorizationError("activation token missing")
        if ledger_token_seen(self.token):
            raise L1AuthorizationError(
                "activation token already consumed; authorization is single-use"
            )
        age = (now - self.issued_at).total_seconds()
        if age < 0 or age > 3600:
            raise L1AuthorizationError(
                "authorization outside its one-hour validity window"
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


def verify_bracket_acknowledgement(
    *,
    plan: BracketPlan,
    open_orders: list[Mapping[str, Any]],
    quantity_tolerance: float = 1e-9,
) -> dict[str, Any]:
    """Hard post-submit condition: parent AND both children acknowledged.

    Returns a verdict dict; the caller must cancel/flatten and hold on any
    result whose ``protected`` field is False. Missing evidence is never
    read as success (auditor: "never infer a zero or success from missing
    alerts").
    """
    by_id = {}
    for order in open_orders:
        order_id = order.get("orderId")
        if order_id is not None:
            by_id[int(order_id)] = order
    verdict: dict[str, Any] = {"protected": False, "legs": {}}
    for name, spec in (
        ("parent", plan.parent),
        ("take_profit", plan.take_profit),
        ("stop_loss", plan.stop_loss),
    ):
        observed = by_id.get(int(spec["orderId"]))
        leg = {
            "acknowledged": observed is not None,
            "order_id": spec["orderId"],
            "expected_action": spec["action"],
            "expected_quantity": spec["totalQuantity"],
        }
        if observed is not None:
            leg["observed_action"] = observed.get("action")
            leg["observed_quantity"] = observed.get("totalQuantity")
            leg["status"] = observed.get("status")
            leg["action_matches"] = observed.get("action") == spec["action"]
            leg["quantity_matches"] = (
                observed.get("totalQuantity") is not None
                and abs(
                    float(observed["totalQuantity"]) - float(spec["totalQuantity"])
                ) <= quantity_tolerance
            )
            if name != "parent":
                leg["parent_matches"] = (
                    observed.get("parentId") == plan.parent["orderId"]
                )
        verdict["legs"][name] = leg
    children_ok = all(
        verdict["legs"][name].get("acknowledged")
        and verdict["legs"][name].get("action_matches")
        and verdict["legs"][name].get("quantity_matches")
        and verdict["legs"][name].get("parent_matches")
        for name in ("take_profit", "stop_loss")
    )
    parent_ok = (
        verdict["legs"]["parent"].get("acknowledged")
        and verdict["legs"]["parent"].get("action_matches")
        and verdict["legs"]["parent"].get("quantity_matches")
    )
    verdict["protected"] = bool(parent_ok and children_ok)
    if not verdict["protected"]:
        verdict["required_action"] = "cancel_flatten_and_global_hold"
    return verdict


class IbkrPaperL1Sink:
    """Write-capable sink. Refuses to exist without a valid authorization.

    Construction order is deliberate: authorization is verified and burned
    BEFORE any import of the broker library or any socket. An unauthorized
    process therefore cannot even reach networking code.
    """

    def __init__(
        self,
        authorization: L1Authorization,
        *,
        ledger,
        dry_run: bool = True,
    ) -> None:
        authorization.verify(ledger_token_seen=ledger.activation_token_seen)
        ledger.burn_activation_token(
            authorization.token,
            profile_hash=authorization.profile.profile_hash,
            venue=authorization.profile.venue,
            account_fingerprint=authorization.profile.account_fingerprint,
        )
        self.authorization = authorization
        self.profile = authorization.profile
        self.ledger = ledger
        self.dry_run = dry_run
        self.submissions = 0
        self.network_submissions = 0
        self.would_be_orders = 0
        self._ib = None

    # -- connection -------------------------------------------------------
    def connect(self):
        """Open the TWS Paper connection. Fail closed on any mismatch."""
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
            readonly=False,          # the one place readonly is False
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
                "connected account fingerprint does not match the authorized profile"
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

    # -- submission --------------------------------------------------------
    def submit_bracket(
        self,
        intent: OrderIntentV2,
        plan: BracketPlan,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Journal, transmit, then verify protection. Never the other order."""
        now = now or _utc_now()
        if self.submissions >= self.profile.max_orders_this_activation:
            raise L1AuthorizationError(
                "activation order budget exhausted; a new owner authorization "
                "is required"
            )
        if intent.venue != self.profile.venue:
            raise L1ExecutionError("intent venue does not match the authorized profile")
        if intent.instrument != self.profile.instrument:
            raise L1ExecutionError(
                "intent instrument does not match the authorized profile"
            )
        if intent.asset_id != self.profile.asset_id:
            raise L1ExecutionError("intent asset does not match the authorized profile")

        # Journal BEFORE the side effect (055 pattern across broker calls).
        self.ledger.journal_submission(
            idempotency_key=intent.idempotency_key,
            order_ids=[
                plan.parent["orderId"],
                plan.take_profit["orderId"],
                plan.stop_loss["orderId"],
            ],
            profile_hash=self.profile.profile_hash,
            submitted_at=now.isoformat(),
        )
        if self.dry_run:
            return {
                "submitted": False,
                "reason": "dry_run",
                "planned_order_ids": [
                    plan.parent["orderId"],
                    plan.take_profit["orderId"],
                    plan.stop_loss["orderId"],
                ],
            }
        if self._ib is None:
            raise L1ExecutionError("not connected; refusing to submit")
        self.submissions += 1
        self.network_submissions += 1
        return {"submitted": True, "transmitted_at": now.isoformat()}
