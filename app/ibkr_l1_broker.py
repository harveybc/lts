"""Narrow IBKR client protocol, exact object translation and a fake client.

Corrects finding 063 at its root: the executor talks to a broker through a
narrow, effect-observable protocol; the translation from the audited
``BracketPlan`` dicts produces REAL ``ib_async`` ``Contract``/``Order``
objects with exact field values (constructing these objects opens no
socket); and the fake client records every invocation so tests assert
effects, not returned strings.

The real TWS-backed implementation of ``IbkrClientProtocol`` is deliberately
NOT in this module: every Milestone A-E test runs against ``FakeIbkrClient``
with sockets booby-trapped. No submission method exists that bypasses the
durable effects journal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol

from app.ibkr_l1_adapter import BracketPlan, L1ExecutionError

# ib_async encodes "field not set" as a huge sentinel double; anything at or
# beyond this magnitude is "unset" when snapshotting order facts.
_UNSET_THRESHOLD = 1e300


class IbkrClientProtocol(Protocol):
    """The complete broker surface the L1 executor is allowed to touch."""

    def place_order(self, contract: Any, order: Any) -> dict[str, Any]:
        """Transmit one order object. Returns the immediate placement fact."""
        ...

    def cancel_order(self, order_id: int) -> dict[str, Any]:
        """Request cancellation of one order by broker order id."""
        ...

    def open_order_facts(self) -> list[dict[str, Any]]:
        """Direct broker snapshot of open/known orders."""
        ...

    def position_facts(self) -> list[dict[str, Any]]:
        """Direct broker snapshot of positions."""
        ...

    def connected_account(self) -> Optional[str]:
        """The account the session is bound to, or None when unknown."""
        ...

    def next_order_id(self) -> int:
        """A fresh broker-valid order id (TWS: reqIds/getReqId)."""
        ...


@dataclass(frozen=True)
class TranslatedBracket:
    """Real ib_async objects, fully constructed before any transmission."""

    contract: Any
    parent: Any
    take_profit: Any
    stop_loss: Any

    def legs(self) -> list[tuple[str, Any]]:
        """Official TWS sequence: parent, TP, then SL (transmit=True last)."""
        return [
            ("parent", self.parent),
            ("take_profit", self.take_profit),
            ("stop_loss", self.stop_loss),
        ]


def forex_pair(instrument: str) -> str:
    """`EUR.USD` -> `EURUSD`, refusing anything that is not a 3.3 FX pair."""
    parts = instrument.split(".")
    if len(parts) != 2 or not all(len(p) == 3 and p.isalpha() for p in parts):
        raise L1ExecutionError(
            f"instrument {instrument!r} is not a supported FX pair (CCY.CCY)"
        )
    return (parts[0] + parts[1]).upper()


def translate_bracket(plan: BracketPlan, *, instrument: str) -> TranslatedBracket:
    """Translate the audited plan dicts into exact ib_async objects.

    Defense in depth: the plan's own invariants (transmit flags, parent
    links, equal quantities, child sides) are re-checked here so a corrupted
    plan cannot reach a broker object.
    """
    from ib_async import Forex, Order  # object construction only; no socket

    parent, take, stop = plan.parent, plan.take_profit, plan.stop_loss
    if not (
        parent["transmit"] is False
        and take["transmit"] is False
        and stop["transmit"] is True
    ):
        raise L1ExecutionError("bracket transmit flags are not False,False,True")
    if take["parentId"] != parent["orderId"] or stop["parentId"] != parent["orderId"]:
        raise L1ExecutionError("bracket children do not link to the parent id")
    if not (
        parent["totalQuantity"] == take["totalQuantity"] == stop["totalQuantity"]
    ):
        raise L1ExecutionError("bracket legs disagree on quantity")
    if parent["orderType"] != "MKT":
        raise L1ExecutionError("canary parent must be a MKT order")
    if take["orderType"] != "LMT" or stop["orderType"] != "STP":
        raise L1ExecutionError("children must be LMT take-profit and STP stop-loss")
    if not (parent["account"] == take["account"] == stop["account"]):
        raise L1ExecutionError("bracket legs disagree on account")

    contract = Forex(forex_pair(instrument))
    parent_order = Order(
        orderId=parent["orderId"],
        action=parent["action"],
        orderType="MKT",
        totalQuantity=parent["totalQuantity"],
        account=parent["account"],
        tif=parent["tif"],
        transmit=False,
        outsideRth=False,
    )
    take_order = Order(
        orderId=take["orderId"],
        parentId=parent["orderId"],
        action=take["action"],
        orderType="LMT",
        lmtPrice=take["lmtPrice"],
        totalQuantity=take["totalQuantity"],
        account=take["account"],
        tif=take["tif"],
        transmit=False,
        outsideRth=False,
    )
    stop_order = Order(
        orderId=stop["orderId"],
        parentId=parent["orderId"],
        action=stop["action"],
        orderType="STP",
        auxPrice=stop["auxPrice"],
        totalQuantity=stop["totalQuantity"],
        account=stop["account"],
        tif=stop["tif"],
        transmit=True,
        outsideRth=False,
    )
    return TranslatedBracket(
        contract=contract,
        parent=parent_order,
        take_profit=take_order,
        stop_loss=stop_order,
    )


def _price_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    return None if abs(number) >= _UNSET_THRESHOLD else number


def order_fact(contract: Any, order: Any, status: str) -> dict[str, Any]:
    """Immutable snapshot of one broker order as a plain fact dict."""
    return {
        "orderId": int(order.orderId),
        "parentId": int(order.parentId),
        "action": str(order.action),
        "orderType": str(order.orderType),
        "totalQuantity": float(order.totalQuantity),
        "lmtPrice": _price_or_none(order.lmtPrice),
        "auxPrice": _price_or_none(order.auxPrice),
        "tif": str(order.tif),
        "transmit": bool(order.transmit),
        "account": str(order.account),
        "status": status,
        "contract": {
            "secType": str(contract.secType),
            "symbol": str(contract.symbol),
            "currency": str(contract.currency),
            "exchange": str(contract.exchange),
            "conId": int(contract.conId),
        },
    }


class FakeBrokerRefusal(RuntimeError):
    """The fake broker refused an order the way TWS would."""


@dataclass
class FakeIbkrClient:
    """Socket-free IBKR double that records every effect it is asked for.

    ``calls`` is the authoritative invocation log: tests assert against it,
    never against return values alone. Failure injection:

    - ``fail_on_place_call``: 1-based index of the ``place_order`` call that
      raises ``ConnectionError`` AFTER the invocation is recorded (the
      broker may or may not have seen it — exactly the ambiguity a
      disconnect produces).
    - ``fail_cancel`` / ``fail_place_flatten``: recovery-path failures.
    """

    account: str = "DU1234567"
    fail_on_place_call: Optional[int] = None
    fail_cancel: bool = False
    reject_unknown_parent: bool = True
    initial_status: str = "PreSubmitted"
    # standalone MKT orders (no parent link, transmit=True) fill immediately
    # and move the position — the realism recovery flatten tests need
    auto_fill_market_orders: bool = False

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _orders: dict[int, dict[str, Any]] = field(default_factory=dict)
    _positions: list[dict[str, Any]] = field(default_factory=list)
    _next_id: int = 9000

    # -- protocol ----------------------------------------------------------
    def place_order(self, contract: Any, order: Any) -> dict[str, Any]:
        fact = order_fact(contract, order, self.initial_status)
        self.calls.append(("place_order", fact))
        if (
            self.fail_on_place_call is not None
            and self._place_calls() >= self.fail_on_place_call
        ):
            raise ConnectionError(
                "fake TWS: connection lost during order transmission"
            )
        if (
            self.reject_unknown_parent
            and fact["parentId"]
            and fact["parentId"] not in self._orders
        ):
            raise FakeBrokerRefusal(
                f"fake TWS: parent order {fact['parentId']} unknown"
            )
        self._orders[fact["orderId"]] = dict(fact)
        if (
            self.auto_fill_market_orders
            and fact["orderType"] == "MKT"
            and fact["transmit"]
            and not fact["parentId"]
        ):
            self._orders[fact["orderId"]]["status"] = "Filled"
            sign = 1.0 if fact["action"] == "BUY" else -1.0
            existing = 0.0
            for p in self._positions:
                if p["symbol"] == fact["contract"]["symbol"]:
                    existing = p["units"]
            self.set_position(
                symbol=fact["contract"]["symbol"],
                currency=fact["contract"]["currency"],
                units=existing + sign * fact["totalQuantity"],
            )
            return {"orderId": fact["orderId"], "status": "Filled"}
        return {"orderId": fact["orderId"], "status": self.initial_status}

    def cancel_order(self, order_id: int) -> dict[str, Any]:
        self.calls.append(("cancel_order", {"orderId": int(order_id)}))
        if self.fail_cancel:
            raise ConnectionError("fake TWS: connection lost during cancel")
        known = self._orders.get(int(order_id))
        if known is None:
            return {"orderId": int(order_id), "status": "Unknown"}
        known["status"] = "Cancelled"
        return {"orderId": int(order_id), "status": "Cancelled"}

    def open_order_facts(self) -> list[dict[str, Any]]:
        self.calls.append(("open_order_facts", {}))
        return [dict(fact) for fact in self._orders.values()]

    def position_facts(self) -> list[dict[str, Any]]:
        self.calls.append(("position_facts", {}))
        return [dict(p) for p in self._positions]

    def connected_account(self) -> Optional[str]:
        return self.account

    def next_order_id(self) -> int:
        self._next_id += 1
        return self._next_id

    # -- test manipulation (broker-side reality injection) -----------------
    def _place_calls(self) -> int:
        return sum(1 for name, _ in self.calls if name == "place_order")

    def set_order_status(self, order_id: int, status: str) -> None:
        self._orders[int(order_id)]["status"] = status

    def alter_order(self, order_id: int, **fields: Any) -> None:
        self._orders[int(order_id)].update(fields)

    def drop_order(self, order_id: int) -> None:
        self._orders.pop(int(order_id), None)

    def set_position(
        self, *, symbol: str, currency: str, units: float
    ) -> None:
        self._positions = [
            p for p in self._positions
            if not (p["symbol"] == symbol and p["currency"] == currency)
        ]
        if units != 0.0:
            self._positions.append(
                {"account": self.account, "symbol": symbol,
                 "currency": currency, "secType": "CASH", "units": units}
            )

    def fill_parent(self, order_id: int, units: float) -> None:
        """Simulate a (partial) fill: parent gains filled units and the
        account gains a signed FX position."""
        fact = self._orders[int(order_id)]
        fact["status"] = "Filled" if units >= fact["totalQuantity"] else fact["status"]
        sign = 1.0 if fact["action"] == "BUY" else -1.0
        existing = 0.0
        for p in self._positions:
            if p["symbol"] == fact["contract"]["symbol"]:
                existing = p["units"]
        self.set_position(
            symbol=fact["contract"]["symbol"],
            currency=fact["contract"]["currency"],
            units=existing + sign * units,
        )


def place_order_sequence(calls: Iterable[tuple[str, dict[str, Any]]]) -> list[int]:
    """Order ids of ``place_order`` invocations, in invocation order."""
    return [fact["orderId"] for name, fact in calls if name == "place_order"]
