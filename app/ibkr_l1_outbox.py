"""Crash-resumable effects consumer behind the accepted L0 path (finding 066).

The outbox IS the accepted L0 decisions table: a committed
``would_be_order``/``would_be_flatten`` decision without an ``l1_effects``
row is pending; creating the effect row consumes it exactly once. No broker
call is ever inserted before the existing atomic L0 decision commit, no
second risk engine exists, and the entry quantity is the L0
account-relative ``plan_units`` result — the profile and capability provide
ceilings and this consumer refuses, never resizes.

Canary gating (cold start §8D): a new entry is IMPOSSIBLE while any prior
L1 effect is non-terminal. The sequence long → reconciled flat → short →
reconciled flat is therefore enforced by the journal itself.

Fail-closed refusals are durable ``terminal_rejected`` effects with the
reason journaled; recoverable conditions (no capability yet, prior effect
in flight) are deferrals that leave the decision pending.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

from trading_contracts import (
    ExecutionReportV2,
    OrderIntentV2,
    ProtectionLegState,
)

from app.demo_execution_service import DemoExecutionService
from app.ibkr_l1_adapter import (
    L1AuthorizationError,
    L1Profile,
    build_bracket,
)
from app.ibkr_l1_broker import IbkrClientProtocol
from app.ibkr_l1_capability import CapabilityGate
from app.ibkr_l1_executor import BracketExecutor, L1EffectUnknown, effect_id_for
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_l1_recovery import (
    BracketLifecycleController,
    build_flatten_order,
    expected_contract_facts,
)

_PRODUCER = {"name": "lts.ibkr_l1_outbox", "version": "0.1.0"}
_DEFAULT_QUOTE_MAX_AGE_SECONDS = 60.0
# A decision older than this is dead evidence: it terminally rejects rather
# than executing against a market that has moved on. Transient conditions
# (stale/wide/absent quote, missing capability) merely DEFER: the decision
# stays pending and the next tick re-evaluates it.
_DEFAULT_DECISION_MAX_AGE_SECONDS = 300.0


class L1OutboxConsumer:
    """Executes accepted L0 decisions through the journaled L1 path."""

    def __init__(
        self,
        service: DemoExecutionService,
        olap: L1ExecutionOlap,
        client: IbkrClientProtocol,
        profile: L1Profile,
        gate: CapabilityGate,
        *,
        price_decimals: int = 5,
        quantity_decimals: int = 0,
        quote_max_age_seconds: float = _DEFAULT_QUOTE_MAX_AGE_SECONDS,
        max_decision_age_seconds: float = _DEFAULT_DECISION_MAX_AGE_SECONDS,
    ) -> None:
        self.service = service
        self.olap = olap
        self.client = client
        self.profile = profile
        self.gate = gate
        self.executor = BracketExecutor(olap, client)
        self.controller = BracketLifecycleController(olap, client)
        self.price_decimals = price_decimals
        self.quantity_decimals = quantity_decimals
        self.quote_max_age_seconds = quote_max_age_seconds
        self.max_decision_age_seconds = max_decision_age_seconds

    # -- helpers -----------------------------------------------------------
    def _reject(
        self, idempotency_key: str, order_ids: list[int], reason: str
    ) -> dict[str, Any]:
        """Durable fail-closed refusal: the decision is consumed into a
        terminal_rejected effect and can never be executed later."""
        effect_id = effect_id_for(idempotency_key)
        with self.olap.atomic_unit():
            self.olap.create_effect(
                effect_id, idempotency_key, "bracket_entry", order_ids, None
            )
            self.olap.record_broker_fact(
                effect_id, "consumer_refusal", {"reason": reason}
            )
            self.olap.advance_effect(effect_id, "terminal_rejected")
        return {
            "idempotency_key": idempotency_key,
            "state": "terminal_rejected",
            "reason": reason,
        }

    def _quote_refusal(self, quote: Any, now: datetime) -> Optional[str]:
        if not isinstance(quote, dict):
            return "quote_missing"
        for key in ("bid", "ask", "time"):
            if key not in quote:
                return f"quote_missing_field:{key}"
        bid, ask = float(quote["bid"]), float(quote["ask"])
        if not bid > 0 or not ask > 0 or ask < bid:
            return "quote_invalid"
        quote_time = quote["time"]
        if isinstance(quote_time, str):
            quote_time = datetime.fromisoformat(quote_time)
        if quote_time.tzinfo is None:
            return "quote_time_naive"
        age = (now - quote_time).total_seconds()
        if age < 0:
            return "quote_time_future"
        if age > self.quote_max_age_seconds:
            return f"quote_stale:{age:.1f}s"
        if (ask - bid) > self.profile.max_spread_price:
            return f"spread_above_profile_max:{ask - bid:.6f}"
        return None

    # -- entries -----------------------------------------------------------
    def consume_entries(
        self, *, quote: dict[str, Any], now: Optional[datetime] = None
    ) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        results = []
        for pending in self.olap.l1_pending_decisions("would_be_order"):
            results.append(self._consume_entry(pending, quote=quote, now=now))
        return results

    def _consume_entry(
        self, pending: dict[str, Any], *, quote: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        key = pending["idempotency_key"]
        intent = OrderIntentV2.model_validate_json(pending["intent_json"])

        # canary gate: nothing new while any effect is not directly terminal
        in_flight = [
            e for e in self.olap.nonterminal_effects()
            if e["idempotency_key"] != key
        ]
        if in_flight:
            return {
                "idempotency_key": key,
                "state": "deferred",
                "reason": "previous_effect_not_terminal",
            }
        if self.olap.get_state("halt", "none") != "none":
            return {
                "idempotency_key": key,
                "state": "deferred",
                "reason": f"halted:{self.olap.get_state('halt')}",
            }

        # durable fail-closed refusals (identity, evidence, ceilings, quote)
        if pending["capability_evidence"] != "live_observed":
            return self._reject(
                key, [],
                "capability_evidence_not_live_observed (ruling R3): "
                f"{pending['capability_evidence']!r}",
            )
        if intent.venue != self.profile.venue:
            return self._reject(key, [], f"venue_mismatch:{intent.venue}")
        if intent.instrument != self.profile.instrument:
            return self._reject(
                key, [], f"instrument_mismatch:{intent.instrument}")
        if intent.asset_id != self.profile.asset_id:
            return self._reject(key, [], f"asset_mismatch:{intent.asset_id}")
        if intent.account_ref != self.profile.account_fingerprint:
            return self._reject(key, [], "account_fingerprint_mismatch")
        if intent.protection is None or intent.risk is None:
            return self._reject(key, [], "unprotected_intent")
        magnitude = abs(intent.delta_units)
        if magnitude > self.profile.quantity_ceiling:
            return self._reject(
                key, [],
                f"quantity_{magnitude}_exceeds_profile_ceiling_"
                f"{self.profile.quantity_ceiling}_never_resized",
            )
        if self.olap.l1_entry_count() >= self.profile.max_orders_this_activation:
            return self._reject(
                key, [],
                f"entry_budget_{self.profile.max_orders_this_activation}_exhausted",
            )
        # age is measured from the decision's own quote evidence: a decision
        # whose market evidence has expired must never execute later
        evidence_time = pending["quote_time"] or pending["decided_at"]
        anchored_at = datetime.fromisoformat(evidence_time)
        if anchored_at.tzinfo is None:
            anchored_at = anchored_at.replace(tzinfo=timezone.utc)
        decision_age = (now - anchored_at).total_seconds()
        if decision_age > self.max_decision_age_seconds:
            return self._reject(
                key, [],
                f"decision_stale:{decision_age:.1f}s_exceeds_"
                f"{self.max_decision_age_seconds}s",
            )
        reference = pending["reference_price"]
        if reference is None:
            return self._reject(key, [], "decision_reference_price_missing")
        epsilon = 1e-9
        stop_distance = abs(float(reference) - intent.protection.stop_loss_price)
        take_distance = abs(intent.protection.take_profit_price - float(reference))
        if stop_distance > self.profile.stop_distance_price_max + epsilon:
            return self._reject(
                key, [],
                f"stop_distance_{stop_distance:.6f}_exceeds_profile_max",
            )
        if take_distance > self.profile.take_profit_distance_price_max + epsilon:
            return self._reject(
                key, [],
                f"take_profit_distance_{take_distance:.6f}_exceeds_profile_max",
            )
        # quote problems are transient: fail closed by DEFERRING, never by
        # destroying a decision that a fresh quote could still execute
        quote_refusal = self._quote_refusal(quote, now)
        if quote_refusal is not None:
            return {
                "idempotency_key": key,
                "state": "deferred",
                "reason": quote_refusal,
            }

        # deferrable authority: no capability is not a terminal condition
        try:
            _, record = self.gate.load(self.profile, olap=self.olap, now=now)
        except L1AuthorizationError as error:
            return {
                "idempotency_key": key,
                "state": "deferred",
                "reason": f"no_capability:{error}",
            }
        if (
            intent.risk.risk_fraction_at_stop
            > float(record.metadata["max_risk_fraction_at_stop"])
        ):
            return self._reject(
                key, [],
                "risk_fraction_exceeds_capability_ceiling",
            )

        account = self.client.connected_account()
        if account is None:
            return {
                "idempotency_key": key,
                "state": "deferred",
                "reason": "no_connected_account",
            }
        if hashlib.sha256(account.encode()).hexdigest()[:16] != (
            self.profile.account_fingerprint
        ):
            return self._reject(key, [], "connected_account_not_authorized")

        parent_id = self.client.reserve_order_ids(3)
        plan = build_bracket(
            intent,
            parent_order_id=parent_id,
            account=account,
            price_decimals=self.price_decimals,
            quantity_decimals=self.quantity_decimals,
        )
        try:
            submit = self.executor.submit_bracket(intent, plan, record)
        except L1EffectUnknown as error:
            return {
                "idempotency_key": key,
                "state": "effect_unknown",
                "effect_id": error.effect_id,
                "reason": str(error),
            }
        verdict = self.controller.acknowledge(
            submit["effect_id"],
            plan,
            instrument=self.profile.instrument,
            expected_con_id=record.metadata.get("contract_con_id"),
        )
        if verdict["protected"]:
            self._apply_accepted(intent, plan, now)
        return {
            "idempotency_key": key,
            "state": self.olap.effect_row(submit["effect_id"])["state"],
            "effect_id": submit["effect_id"],
            "protected": verdict["protected"],
            "order_ids": submit.get("order_ids"),
        }

    def _protection_legs(
        self, plan, covered_units: float
    ) -> list[ProtectionLegState]:
        return [
            ProtectionLegState(
                leg="stop_loss",
                broker_confirmed=True,
                broker_leg_id=str(plan.stop_loss["orderId"]),
                price=plan.stop_loss["auxPrice"],
                covered_units=covered_units,
            ),
            ProtectionLegState(
                leg="take_profit",
                broker_confirmed=True,
                broker_leg_id=str(plan.take_profit["orderId"]),
                price=plan.take_profit["lmtPrice"],
                covered_units=covered_units,
            ),
        ]

    def _apply_accepted(self, intent: OrderIntentV2, plan, now: datetime) -> None:
        reservation_id = intent.risk.reservation_id
        self.service.apply_execution_event(ExecutionReportV2(
            object_id=f"er-ack-{reservation_id}", as_of=now,
            producer=_PRODUCER, trace_id=intent.trace_id,
            order_intent_id=intent.object_id,
            attempt_id=f"attempt-{reservation_id}",
            bracket_role="parent", state="accepted",
            previous_state="requested",
            requested_units=intent.delta_units,
            broker_ids={
                "parent": str(plan.parent["orderId"]),
                "take_profit": str(plan.take_profit["orderId"]),
                "stop_loss": str(plan.stop_loss["orderId"]),
            },
        ))

    # -- fills -------------------------------------------------------------
    def sync_parent_fill(
        self, effect_id: str, *, now: Optional[datetime] = None
    ) -> Optional[dict[str, Any]]:
        """Direct broker fill facts become the L0 filled lifecycle event,
        which consumes the reservation and opens the exposure."""
        now = now or datetime.now(timezone.utc)
        effect = self.olap.effect_row(effect_id)
        if effect is None or effect["state"] != "acknowledged":
            return None
        if self.olap.broker_facts(effect_id, "parent_fill_applied"):
            return None                     # idempotent: applied exactly once
        intent = OrderIntentV2.model_validate_json(
            self.olap.decision_intent_json(effect["idempotency_key"])
        )
        parent_id = effect["order_ids"][0]
        parent_fact = next(
            (
                fact for fact in self.client.open_order_facts()
                if int(fact.get("orderId", -1)) == int(parent_id)
            ),
            None,
        )
        if parent_fact is None or parent_fact.get("status") != "Filled":
            return None
        magnitude = abs(intent.delta_units)
        plan = build_bracket(
            intent, parent_order_id=parent_id,
            account=str(parent_fact["account"]),
            price_decimals=self.price_decimals,
            quantity_decimals=self.quantity_decimals,
        )
        reservation_id = intent.risk.reservation_id
        report = ExecutionReportV2(
            object_id=f"er-fill-{reservation_id}", as_of=now,
            producer=_PRODUCER, trace_id=intent.trace_id,
            order_intent_id=intent.object_id,
            attempt_id=f"attempt-{reservation_id}",
            bracket_role="parent", state="filled",
            previous_state="accepted",
            requested_units=intent.delta_units,
            filled_units=magnitude,
            protection_legs=self._protection_legs(plan, magnitude),
        )
        result = self.service.apply_execution_event(report)
        self.olap.record_broker_fact(
            effect_id, "parent_fill_applied",
            {"filled_units": magnitude, "reservation": result.get("reservation")},
        )
        return result

    # -- flattens ----------------------------------------------------------
    def consume_flattens(
        self, *, now: Optional[datetime] = None
    ) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        results = []
        for pending in self.olap.l1_pending_decisions("would_be_flatten"):
            results.append(self._consume_flatten(pending, now=now))
        return results

    def _entry_effect_for_target(
        self, target_order_intent_id: str
    ) -> Optional[dict[str, Any]]:
        reservation_id = self.olap.exposure_reservation(target_order_intent_id)
        if reservation_id is None:
            return None
        idem = self.olap.reservation_idempotency(reservation_id)
        return self.olap.effect_by_key(idem) if idem else None

    def _consume_flatten(
        self, pending: dict[str, Any], *, now: datetime
    ) -> dict[str, Any]:
        key = pending["idempotency_key"]
        intent = OrderIntentV2.model_validate_json(pending["intent_json"])
        target = intent.reduce_target_order_intent_id
        entry_effect = self._entry_effect_for_target(target)
        effect_id = effect_id_for(key)
        flatten_units = -intent.delta_units  # delta closes; position is -delta
        with self.olap.atomic_unit():
            self.olap.create_effect(effect_id, key, "flatten", [], None)

        try:
            # orphaned protective children must not survive the position
            if entry_effect is not None:
                open_now = {
                    int(fact["orderId"]): fact
                    for fact in self.client.open_order_facts()
                }
                for order_id in entry_effect["order_ids"][1:]:
                    fact = open_now.get(int(order_id))
                    if fact is not None and fact.get("status") in (
                        "PendingSubmit", "PendingCancel", "PreSubmitted",
                        "Submitted",
                    ):
                        self.olap.record_broker_fact(
                            effect_id, "flatten_cancel_child",
                            {"orderId": order_id},
                        )
                        self.client.cancel_order(int(order_id))

            contract, order = build_flatten_order(
                instrument=intent.instrument,
                account=str(self.client.connected_account()),
                net_units=flatten_units,
                order_id=self.client.reserve_order_ids(1),
            )
            self.olap.record_broker_fact(
                effect_id, "call_attempt",
                {"leg": "flatten", "orderId": int(order.orderId)},
            )
            result = self.client.place_order(contract, order)
            self.olap.record_broker_fact(
                effect_id, "call_result", {"leg": "flatten", **result}
            )
        except Exception as error:  # noqa: BLE001 — journal, hold, unknown
            with self.olap.atomic_unit():
                self.olap.record_broker_fact(
                    effect_id, "call_failure",
                    {"error": f"{type(error).__name__}: {error}"},
                )
                self.olap.advance_effect(effect_id, "effect_unknown")
                self.olap.set_state("halt", "hold")
            return {"idempotency_key": key, "state": "effect_unknown"}

        expected = expected_contract_facts(intent.instrument)
        remaining = sum(
            float(position.get("units", 0.0))
            for position in self.client.position_facts()
            if position.get("symbol") == expected["symbol"]
            and position.get("currency") == expected["currency"]
        )
        if remaining != 0.0:
            with self.olap.atomic_unit():
                self.olap.record_broker_fact(
                    effect_id, "flatten_unreconciled",
                    {"remaining_units": remaining},
                )
                self.olap.advance_effect(effect_id, "effect_unknown")
                self.olap.set_state("halt", "hold")
            return {
                "idempotency_key": key,
                "state": "effect_unknown",
                "remaining_units": remaining,
            }

        magnitude = abs(intent.delta_units)
        with self.olap.atomic_unit():
            self.olap.record_broker_fact(
                effect_id, "flatten_reconciled", {"remaining_units": 0.0}
            )
            self.olap.advance_effect(effect_id, "submitted_pending_ack")
            self.olap.advance_effect(effect_id, "acknowledged")
            self.olap.advance_effect(effect_id, "terminal_flat")
        # A flatten is risk-reducing and carries no protection legs by
        # design; service.apply_execution_event would misread that as
        # unprotected exposure (protection_covers_filled) and re-emit an
        # emergency flatten. Append to the same chained ledger with the
        # same continuity rule instead.
        with self.olap.atomic_unit():
            previous = self.olap.last_state(intent.object_id)
            self.olap.append_lifecycle(ExecutionReportV2(
                object_id=f"er-{intent.object_id}-filled", as_of=now,
                producer=_PRODUCER, trace_id=intent.trace_id,
                order_intent_id=intent.object_id,
                attempt_id=f"attempt-{intent.object_id}",
                bracket_role="parent", state="filled",
                previous_state=previous,
                requested_units=intent.delta_units,
                filled_units=magnitude,
            ))
        self.service.apply_position_close(target)
        if entry_effect is not None and entry_effect["state"] == "acknowledged":
            self.olap.advance_effect(entry_effect["effect_id"], "terminal_flat")
        return {
            "idempotency_key": key,
            "state": "terminal_flat",
            "target": target,
        }

    # -- restart -----------------------------------------------------------
    def resume(self, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """After a crash: classify every effect from durable facts, then
        drive entry effects that were submitted-but-unacknowledged (or
        ambiguous) through exact acknowledgement, which recovers on any
        unproven protection. Nothing is promoted without direct facts."""
        now = now or datetime.now(timezone.utc)
        report = self.executor.resume_report()
        outcomes = []
        for entry in report:
            effect = self.olap.effect_row(entry["effect_id"])
            if effect["kind"] != "bracket_entry" or effect["state"] not in (
                "submitted_pending_ack", "effect_unknown",
            ):
                outcomes.append(entry)
                continue
            intent_json = self.olap.decision_intent_json(effect["idempotency_key"])
            if intent_json is None:
                outcomes.append(entry)
                continue
            intent = OrderIntentV2.model_validate_json(intent_json)
            account = self.client.connected_account()
            plan = build_bracket(
                intent,
                parent_order_id=effect["order_ids"][0],
                account=str(account),
                price_decimals=self.price_decimals,
                quantity_decimals=self.quantity_decimals,
            )
            verdict = self.controller.acknowledge(
                effect["effect_id"], plan, instrument=self.profile.instrument
            )
            entry = dict(entry)
            entry["reacknowledged"] = verdict["protected"]
            entry["state"] = self.olap.effect_row(effect["effect_id"])["state"]
            outcomes.append(entry)
        return outcomes
