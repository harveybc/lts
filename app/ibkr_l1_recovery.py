"""Exact acknowledgement and executable recovery (finding 065).

Protection is true only when every required identity fact agrees with
direct broker evidence: order id, parent link, account, contract, side,
quantity, order type, protection price, time-in-force and an acceptable
status for each of the three legs. A missing fact is a failure, never a
default; a returned string is never an effect.

When protection cannot be proven, the controller EXECUTES, idempotently
and in this order:

1. global hold persisted in the same L0 ledger key the accepted service
   enforces (``halt``), so no new risk anywhere;
2. cancellation of every still-open bracket leg, journaled per call;
3. flatten of any residual position with a real opposite-side market
   order, followed by MANDATORY position reconciliation — an unreconciled
   flatten leaves the effect ``effect_unknown`` and the hold in place;
4. terminal classification (``terminal_flat`` / ``terminal_cancelled``)
   only from re-read broker facts.

Owner hold/kill: risk-increasing submission is blocked by the executor
while halt is set; the risk-reducing recovery path keeps working under
hold and kill, and never clears the hold itself — only the owner does.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from app.ibkr_l1_adapter import BracketPlan, L1ExecutionError
from app.ibkr_l1_broker import IbkrClientProtocol, forex_pair
from app.ibkr_l1_journal import L1ExecutionOlap, TERMINAL_EFFECT_STATES

ACCEPTABLE_PARENT_STATUSES = frozenset({"PreSubmitted", "Submitted", "Filled"})
ACCEPTABLE_CHILD_STATUSES = frozenset({"PreSubmitted", "Submitted"})
OPEN_STATUSES = frozenset(
    {"PendingSubmit", "PendingCancel", "PreSubmitted", "Submitted"}
)


def expected_contract_facts(instrument: str) -> dict[str, Any]:
    pair = forex_pair(instrument)
    return {
        "secType": "CASH",
        "symbol": pair[:3],
        "currency": pair[3:],
        "exchange": "IDEALPRO",
    }


def build_flatten_order(
    *, instrument: str, account: str, net_units: float, order_id: int
) -> tuple[Any, Any]:
    """A single risk-reducing market order that moves the position to zero."""
    from ib_async import Forex, Order  # object construction only; no socket

    if net_units == 0.0:
        raise L1ExecutionError("nothing to flatten")
    return (
        Forex(forex_pair(instrument)),
        Order(
            orderId=order_id,
            action="SELL" if net_units > 0 else "BUY",
            orderType="MKT",
            totalQuantity=abs(net_units),
            account=account,
            tif="DAY",
            transmit=True,
            outsideRth=False,
        ),
    )


def _leg_failures(
    name: str,
    spec: Mapping[str, Any],
    observed: Optional[Mapping[str, Any]],
    *,
    parent_order_id: int,
    contract: Mapping[str, Any],
    expected_con_id: Optional[int],
    quantity_tolerance: float,
    price_tolerance: float,
) -> list[str]:
    if observed is None:
        return [f"{name}: no direct broker evidence"]
    failures: list[str] = []

    def _require(key: str) -> Any:
        value = observed.get(key)
        if value is None:
            failures.append(f"{name}: broker fact {key!r} missing")
        return value

    status = _require("status")
    acceptable = (
        ACCEPTABLE_PARENT_STATUSES if name == "parent"
        else ACCEPTABLE_CHILD_STATUSES
    )
    if status is not None and status not in acceptable:
        failures.append(f"{name}: status {status!r} not acceptable")
    action = _require("action")
    if action is not None and action != spec["action"]:
        failures.append(f"{name}: action {action!r} != {spec['action']!r}")
    order_type = _require("orderType")
    if order_type is not None and order_type != spec["orderType"]:
        failures.append(
            f"{name}: orderType {order_type!r} != {spec['orderType']!r}"
        )
    quantity = _require("totalQuantity")
    if quantity is not None and not (
        abs(float(quantity) - float(spec["totalQuantity"])) <= quantity_tolerance
    ):
        failures.append(f"{name}: quantity {quantity} != {spec['totalQuantity']}")
    account = _require("account")
    if account is not None and account != spec["account"]:
        failures.append(f"{name}: account mismatch")
    tif = _require("tif")
    if tif is not None and tif != spec["tif"]:
        failures.append(f"{name}: tif {tif!r} != {spec['tif']!r}")
    if name == "take_profit":
        price = _require("lmtPrice")
        if price is not None and not (
            abs(float(price) - float(spec["lmtPrice"])) <= price_tolerance
        ):
            failures.append(f"{name}: lmtPrice {price} != {spec['lmtPrice']}")
    if name == "stop_loss":
        price = _require("auxPrice")
        if price is not None and not (
            abs(float(price) - float(spec["auxPrice"])) <= price_tolerance
        ):
            failures.append(f"{name}: auxPrice {price} != {spec['auxPrice']}")
    if name != "parent":
        parent_link = _require("parentId")
        if parent_link is not None and int(parent_link) != parent_order_id:
            failures.append(f"{name}: parentId {parent_link} != {parent_order_id}")
    observed_contract = observed.get("contract")
    if observed_contract is None:
        failures.append(f"{name}: broker fact 'contract' missing")
    else:
        for key, expected in contract.items():
            if observed_contract.get(key) != expected:
                failures.append(
                    f"{name}: contract {key} {observed_contract.get(key)!r} "
                    f"!= {expected!r}"
                )
        if expected_con_id is not None:
            observed_con = observed_contract.get("conId")
            if observed_con != expected_con_id:
                failures.append(
                    f"{name}: conId {observed_con!r} != {expected_con_id!r}"
                )
    return failures


def verify_bracket_exact(
    *,
    plan: BracketPlan,
    open_orders: list[Mapping[str, Any]],
    instrument: str,
    expected_con_id: Optional[int] = None,
    quantity_tolerance: float = 1e-9,
    price_tolerance: float = 1e-9,
) -> dict[str, Any]:
    """Protection from direct facts only; every deviation is enumerated."""
    contract = expected_contract_facts(instrument)
    by_id: dict[int, Mapping[str, Any]] = {}
    for order in open_orders:
        order_id = order.get("orderId")
        if order_id is not None:
            by_id[int(order_id)] = order
    failures: list[str] = []
    legs: dict[str, Any] = {}
    for name, spec in (
        ("parent", plan.parent),
        ("take_profit", plan.take_profit),
        ("stop_loss", plan.stop_loss),
    ):
        observed = by_id.get(int(spec["orderId"]))
        leg_failures = _leg_failures(
            name, spec, observed,
            parent_order_id=int(plan.parent["orderId"]),
            contract=contract,
            expected_con_id=expected_con_id,
            quantity_tolerance=quantity_tolerance,
            price_tolerance=price_tolerance,
        )
        legs[name] = {
            "order_id": spec["orderId"],
            "observed": None if observed is None else dict(observed),
            "failures": leg_failures,
        }
        failures.extend(leg_failures)
    verdict: dict[str, Any] = {
        "protected": not failures,
        "failures": failures,
        "legs": legs,
    }
    if failures:
        verdict["required_action"] = "cancel_flatten_and_global_hold"
    return verdict


class BracketLifecycleController:
    """Drives one submitted bracket to acknowledged or reconciled-safe."""

    def __init__(self, olap: L1ExecutionOlap, client: IbkrClientProtocol) -> None:
        self.olap = olap
        self.client = client

    # -- acknowledgement ---------------------------------------------------
    def acknowledge(
        self,
        effect_id: str,
        plan: BracketPlan,
        *,
        instrument: str,
        expected_con_id: Optional[int] = None,
    ) -> dict[str, Any]:
        effect = self.olap.effect_row(effect_id)
        if effect is None:
            raise L1ExecutionError(f"effect {effect_id} does not exist")
        if effect["state"] == "acknowledged":
            return {"protected": True, "replayed": True, "failures": []}
        if effect["state"] not in ("submitted_pending_ack", "effect_unknown"):
            raise L1ExecutionError(
                f"effect {effect_id} in state {effect['state']!r} cannot be "
                "acknowledged"
            )
        snapshot = self.client.open_order_facts()
        self.olap.record_broker_fact(
            effect_id, "ack_snapshot", {"open_orders": snapshot}
        )
        verdict = verify_bracket_exact(
            plan=plan, open_orders=snapshot, instrument=instrument,
            expected_con_id=expected_con_id,
        )
        with self.olap.atomic_unit():
            self.olap.record_broker_fact(
                effect_id, "ack_verdict",
                {"protected": verdict["protected"],
                 "failures": verdict["failures"]},
            )
            self.olap.advance_effect(
                effect_id,
                "acknowledged" if verdict["protected"] else "recovering",
            )
        if not verdict["protected"]:
            verdict["recovery"] = self.recover(
                effect_id, plan, instrument=instrument
            )
        return verdict

    # -- executable recovery ----------------------------------------------
    def recover(
        self,
        effect_id: str,
        plan: BracketPlan,
        *,
        instrument: str,
    ) -> dict[str, Any]:
        """Idempotent hold + cancel + flatten + reconcile. Executes effects;
        every attempt and outcome is journaled before and after."""
        effect = self.olap.effect_row(effect_id)
        if effect is None:
            raise L1ExecutionError(f"effect {effect_id} does not exist")
        if effect["state"] in TERMINAL_EFFECT_STATES:
            return {"complete": True, "state": effect["state"], "replayed": True}
        if effect["state"] != "recovering":
            self.olap.advance_effect(effect_id, "recovering")

        # 1. global hold FIRST, in the ledger the L0 service enforces
        with self.olap.atomic_unit():
            if self.olap.get_state("halt", "none") == "none":
                self.olap.set_state("halt", "hold")
            self.olap.record_broker_fact(
                effect_id, "recovery_hold", {"halt": self.olap.get_state("halt")}
            )

        actions: list[str] = []
        # 2. cancel every still-open leg (idempotent: closed legs are skipped)
        try:
            open_now = {
                int(fact["orderId"]): fact
                for fact in self.client.open_order_facts()
                if fact.get("orderId") is not None
            }
            for spec in plan.transmission_order():
                fact = open_now.get(int(spec["orderId"]))
                if fact is not None and fact.get("status") in OPEN_STATUSES:
                    self.olap.record_broker_fact(
                        effect_id, "recovery_cancel_attempt",
                        {"orderId": spec["orderId"]},
                    )
                    result = self.client.cancel_order(int(spec["orderId"]))
                    self.olap.record_broker_fact(
                        effect_id, "recovery_cancel_result", dict(result)
                    )
                    actions.append(f"cancel:{spec['orderId']}")
            # 3. re-read: any leg still open is unproven, never assumed gone
            still_open = [
                fact["orderId"]
                for fact in self.client.open_order_facts()
                if int(fact.get("orderId", -1))
                in {int(s["orderId"]) for s in plan.transmission_order()}
                and fact.get("status") in OPEN_STATUSES
            ]
            if still_open:
                self.olap.advance_effect(effect_id, "effect_unknown")
                self.olap.record_broker_fact(
                    effect_id, "recovery_incomplete",
                    {"still_open": still_open},
                )
                return {"complete": False, "state": "effect_unknown",
                        "actions": actions, "still_open": still_open}

            # 4. flatten residual exposure with mandatory reconciliation
            expected = expected_contract_facts(instrument)
            residual = 0.0
            for position in self.client.position_facts():
                if (
                    position.get("symbol") == expected["symbol"]
                    and position.get("currency") == expected["currency"]
                    and position.get("secType") == expected["secType"]
                    and position.get("account") == plan.parent["account"]
                ):
                    residual += float(position.get("units", 0.0))
            had_position = residual != 0.0
            if had_position:
                contract, order = build_flatten_order(
                    instrument=instrument,
                    account=plan.parent["account"],
                    net_units=residual,
                    order_id=self.client.reserve_order_ids(1),
                )
                self.olap.record_broker_fact(
                    effect_id, "recovery_flatten_attempt",
                    {"orderId": int(order.orderId), "units": residual},
                )
                result = self.client.place_order(contract, order)
                self.olap.record_broker_fact(
                    effect_id, "recovery_flatten_result", dict(result)
                )
                actions.append(f"flatten:{residual}")
                remaining = 0.0
                for position in self.client.position_facts():
                    if (
                        position.get("symbol") == expected["symbol"]
                        and position.get("currency") == expected["currency"]
                    ):
                        remaining += float(position.get("units", 0.0))
                if remaining != 0.0:
                    self.olap.advance_effect(effect_id, "effect_unknown")
                    self.olap.record_broker_fact(
                        effect_id, "recovery_unreconciled",
                        {"remaining_units": remaining},
                    )
                    return {"complete": False, "state": "effect_unknown",
                            "actions": actions, "remaining_units": remaining}
                self.olap.record_broker_fact(
                    effect_id, "recovery_reconciled_flat", {"remaining_units": 0.0}
                )
        except (ConnectionError, OSError) as error:
            with self.olap.atomic_unit():
                self.olap.record_broker_fact(
                    effect_id, "recovery_failure",
                    {"error": f"{type(error).__name__}: {error}"},
                )
                self.olap.advance_effect(effect_id, "effect_unknown")
            return {"complete": False, "state": "effect_unknown",
                    "actions": actions,
                    "error": f"{type(error).__name__}: {error}"}

        terminal = "terminal_flat" if had_position else "terminal_cancelled"
        with self.olap.atomic_unit():
            self.olap.advance_effect(effect_id, terminal)
            self.olap.record_broker_fact(
                effect_id, "recovery_terminal", {"state": terminal}
            )
        return {"complete": True, "state": terminal, "actions": actions}
