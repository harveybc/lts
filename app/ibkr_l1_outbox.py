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
    L1ExecutionError,
    L1Profile,
    build_bracket,
)
from app.ibkr_l1_broker import IbkrClientProtocol
from app.ibkr_l1_capability import CapabilityGate
from app.ibkr_l1_executor import (
    BracketExecutor,
    L1EffectUnknown,
    effect_id_for,
    plan_from_contract,
)
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_l1_recovery import (
    BracketLifecycleController,
    build_flatten_order,
    expected_contract_facts,
    verify_bracket_exact,
)

_PRODUCER = {"name": "lts.ibkr_l1_outbox", "version": "0.1.0"}
_DEFAULT_QUOTE_MAX_AGE_SECONDS = 60.0
# A decision older than this is dead evidence: it terminally rejects rather
# than executing against a market that has moved on. Transient conditions
# (stale/wide/absent quote, missing capability) merely DEFER: the decision
# stays pending and the next tick re-evaluates it.
_DEFAULT_DECISION_MAX_AGE_SECONDS = 300.0
_DEFAULT_ACK_MAX_AGE_SECONDS = 120.0


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
        max_ack_age_seconds: float = _DEFAULT_ACK_MAX_AGE_SECONDS,
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
        self.max_ack_age_seconds = max_ack_age_seconds

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
        if getattr(self.profile, "entry_budget_scope", "activation") == "utc_day":
            budget_used = self.olap.effect_count_since(
                "bracket_entry", f"{now.date().isoformat()}T00:00:00+00:00"
            )
            budget_name = "daily_entry_budget"
        else:
            budget_used = self.olap.l1_entry_count()
            budget_name = "entry_budget"
        if budget_used >= self.profile.max_orders_this_activation:
            return self._reject(
                key, [],
                f"{budget_name}_{self.profile.max_orders_this_activation}_exhausted",
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
            intent_loader = getattr(self.gate, "load_for_intent", None)
            if callable(intent_loader):
                _, record = intent_loader(
                    self.profile, intent, olap=self.olap, now=now
                )
            else:
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
            submit = self.executor.submit_bracket(
                intent, plan, record,
                expected_con_id=record.metadata.get("contract_con_id"),
                price_decimals=self.price_decimals,
                quantity_decimals=self.quantity_decimals,
            )
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

    # -- fills (findings 069/071) ------------------------------------------
    def _refuse_fill_sync(self, effect_id: str, reason: str) -> dict[str, Any]:
        """A fill sync that cannot prove its facts journals, demotes to
        unknown and holds. A missing fact is never read as zero."""
        with self.olap.atomic_unit():
            self.olap.record_broker_fact(
                effect_id, "fill_sync_refusal", {"reason": reason}
            )
            if self.olap.effect_row(effect_id)["state"] != "effect_unknown":
                self.olap.advance_effect(effect_id, "effect_unknown")
            if self.olap.get_state("halt", "none") == "none":
                self.olap.set_state("halt", "hold")
        return {"refused": reason, "state": "effect_unknown"}

    def _applied_cumulative(self, effect_id: str) -> float:
        facts = self.olap.broker_facts(effect_id, "fill_applied")
        return float(facts[-1]["fact"]["cumulative"]) if facts else 0.0

    def _reconcile_l0_after_recovery(
        self, effect_id: str, contract: dict[str, Any], now: datetime
    ) -> None:
        """After executed recovery: release any remaining L0 reservation and
        close any open exposure THROUGH the accepted service API."""
        reservation_id = contract["reservation_id"]
        intent_id = contract["intent_object_id"]
        row = self.olap.reservation_row(reservation_id)
        if row is not None and row["state"] == "active":
            self.service.apply_execution_event(ExecutionReportV2(
                object_id=f"er-cxl-{reservation_id}", as_of=now,
                producer=_PRODUCER, trace_id=contract["trace_id"],
                order_intent_id=intent_id,
                attempt_id=f"attempt-{reservation_id}",
                bracket_role="parent", state="cancelled",
                previous_state=self.olap.last_state(intent_id),
                requested_units=contract["delta_units"],
                filled_units=self._applied_cumulative(effect_id),
            ))
        if self.olap.exposure_state(f"exp-{intent_id}") == "open":
            self.service.apply_position_close(intent_id)

    def _handle_protection_loss(
        self,
        effect_id: str,
        contract: dict[str, Any],
        plan,
        verdict: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        with self.olap.atomic_unit():
            self.olap.record_broker_fact(
                effect_id, "protection_health_failure",
                {"failures": verdict["failures"]},
            )
            self.olap.advance_effect(effect_id, "recovering")
        recovery = self.controller.recover(
            effect_id,
            plan,
            instrument=contract["instrument"],
            expected_con_id=contract["expected_con_id"],
            now=now,
            acknowledgement_timeout_seconds=self.max_ack_age_seconds,
        )
        if recovery.get("complete"):
            self._reconcile_l0_after_recovery(effect_id, contract, now)
        return {
            "protection_lost": True,
            "failures": verdict["failures"],
            "recovery": recovery,
        }

    def sync_parent_fill(
        self, effect_id: str, *, now: Optional[datetime] = None
    ) -> Optional[dict[str, Any]]:
        """One monitoring pass over an acknowledged effect:

        1. re-verify SL/TP protection from CURRENT direct broker facts
           against the immutable contract (finding 069) — any deviation
           executes recovery and reconciles L0;
        2. read the parent's direct cumulative ``filled`` fact (finding
           071) and apply the idempotent cumulative delta through the
           accepted L0 service API; and
        3. reconcile the direct broker position against the applied
           cumulative after every update.
        """
        now = now or datetime.now(timezone.utc)
        effect = self.olap.effect_row(effect_id)
        if effect is None or effect["state"] != "acknowledged":
            return None
        contract = self.olap.effect_contract(effect_id)
        if contract is None:
            return self._refuse_fill_sync(effect_id, "missing_effect_contract")
        account = self.client.connected_account()
        try:
            plan = plan_from_contract(contract, account=str(account))
        except L1ExecutionError as error:
            return self._refuse_fill_sync(effect_id, str(error))

        snapshot = self.client.open_order_facts()
        verdict = verify_bracket_exact(
            plan=plan, open_orders=snapshot,
            instrument=contract["instrument"],
            expected_con_id=contract["expected_con_id"],
        )
        if not verdict["protected"]:
            return self._handle_protection_loss(
                effect_id, contract, plan, verdict, now
            )

        parent_fact = next(
            (f for f in snapshot
             if int(f.get("orderId", -1)) == int(effect["order_ids"][0])),
            None,
        )
        cumulative_raw = None if parent_fact is None else parent_fact.get("filled")
        if cumulative_raw is None:
            return self._refuse_fill_sync(
                effect_id, "broker_filled_fact_missing_never_zero"
            )
        cumulative = float(cumulative_raw)
        magnitude = abs(float(contract["delta_units"]))
        applied = self._applied_cumulative(effect_id)
        epsilon = 1e-9
        if abs(cumulative - magnitude) <= epsilon:
            cumulative = magnitude
        if abs(applied - magnitude) <= epsilon:
            applied = magnitude
        if cumulative < -epsilon:
            return self._refuse_fill_sync(
                effect_id, f"broker_cumulative_{cumulative}_is_negative"
            )
        if cumulative > magnitude + epsilon:
            return self._refuse_fill_sync(
                effect_id, f"filled_{cumulative}_exceeds_requested_{magnitude}"
            )
        if cumulative < applied - epsilon:
            return self._refuse_fill_sync(
                effect_id,
                f"broker_cumulative_{cumulative}_below_applied_{applied}",
            )

        result: dict[str, Any] = {"cumulative": cumulative, "applied": applied}
        if cumulative > applied + epsilon:
            state = "filled" if cumulative >= magnitude - epsilon else (
                "partially_filled"
            )
            intent_id = contract["intent_object_id"]
            reservation_id = contract["reservation_id"]
            report = ExecutionReportV2(
                object_id=f"er-fill-{reservation_id}-{int(cumulative)}",
                as_of=now, producer=_PRODUCER,
                trace_id=contract["trace_id"],
                order_intent_id=intent_id,
                attempt_id=f"attempt-{reservation_id}",
                bracket_role="parent", state=state,
                previous_state=self.olap.last_state(intent_id),
                requested_units=contract["delta_units"],
                filled_units=cumulative,
                protection_legs=self._protection_legs(plan, cumulative),
            )
            result = dict(self.service.apply_execution_event(report))
            with self.olap.atomic_unit():
                self.olap.record_broker_fact(
                    effect_id, "fill_applied",
                    {"cumulative": cumulative, "state": state},
                )
            result["cumulative"] = cumulative

        # 3. mandatory position reconciliation against direct facts
        expected_contract = expected_contract_facts(contract["instrument"])
        sign = 1.0 if contract["delta_units"] > 0 else -1.0
        observed = sum(
            float(p.get("units", 0.0))
            for p in self.client.position_facts()
            if p.get("symbol") == expected_contract["symbol"]
            and p.get("currency") == expected_contract["currency"]
            and p.get("secType") == expected_contract["secType"]
            and p.get("account") == account
            and (
                contract["expected_con_id"] is None
                or (
                    p.get("conId") is not None
                    and int(p["conId"]) == int(contract["expected_con_id"])
                )
            )
        )
        if abs(observed - sign * cumulative) > epsilon:
            return self._refuse_fill_sync(
                effect_id,
                f"position_{observed}_disagrees_with_cumulative_"
                f"{sign * cumulative}",
            )
        self.olap.record_broker_fact(
            effect_id, "position_reconciled",
            {"observed_units": observed, "cumulative": cumulative},
        )
        result["position_reconciled"] = True
        return result

    # -- flattens ----------------------------------------------------------
    def consume_flattens(
        self, *, now: Optional[datetime] = None
    ) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        results = []
        # Reconcile previously submitted asynchronous closes before creating
        # any new effect. Real TWS market orders are not synchronously filled.
        for effect in self.olap.nonterminal_effects():
            if (
                effect["kind"] == "flatten"
                and effect["state"] == "submitted_pending_ack"
            ):
                results.append(self._sync_submitted_flatten(effect, now=now))
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

    @staticmethod
    def exact_reduction_units(
        position: float, intent_delta: float, *, epsilon: float = 1e-9
    ) -> tuple[Optional[float], Optional[str]]:
        """The pure finding-070 predicate: a reduction executes only when
        the PROVEN position agrees exactly with the immutable intent delta;
        the returned units equal the position, so the result is exactly
        zero — never resized, never crossed."""
        if abs(position) <= epsilon:
            return None, (
                "no_matching_position_to_flatten_while_intent_expects_"
                f"{-intent_delta}"
            )
        if abs(position + intent_delta) > epsilon:
            return None, (
                f"position_{position}_disagrees_with_immutable_intent_delta_"
                f"{intent_delta}_never_resized"
            )
        return position, None

    def _refuse_flatten(self, effect_id: str, reason: str) -> dict[str, Any]:
        """A flatten that cannot PROVE exact account, contract and position
        agreement refuses before any broker call, journals, and holds. It
        never resizes, never guesses, never crosses zero."""
        with self.olap.atomic_unit():
            self.olap.record_broker_fact(
                effect_id, "flatten_refusal", {"reason": reason}
            )
            self.olap.advance_effect(effect_id, "effect_unknown")
            if self.olap.get_state("halt", "none") == "none":
                self.olap.set_state("halt", "hold")
        return {"state": "effect_unknown", "refused": reason}

    @staticmethod
    def _position_units(
        facts: list[dict[str, Any]],
        expected: dict[str, Any],
        account: str,
        expected_con_id: Optional[int],
    ) -> float:
        return sum(
            float(fact.get("units", 0.0))
            for fact in facts
            if fact.get("symbol") == expected["symbol"]
            and fact.get("currency") == expected["currency"]
            and fact.get("secType") == expected["secType"]
            and fact.get("account") == account
            and (
                expected_con_id is None
                or (
                    fact.get("conId") is not None
                    and int(fact["conId"]) == int(expected_con_id)
                )
            )
        )

    def _finalize_flatten(
        self,
        effect: dict[str, Any],
        contract: dict[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        effect_id = effect["effect_id"]
        intent = OrderIntentV2.model_validate(contract["intent"])
        target = intent.reduce_target_order_intent_id
        entry_effect = self._entry_effect_for_target(target)
        magnitude = abs(intent.delta_units)
        try:
            with self.olap.atomic_unit():
                self.olap.record_broker_fact(
                    effect_id, "flatten_reconciled", {"remaining_units": 0.0}
                )
                fill_result = self.service.apply_execution_event(
                    ExecutionReportV2(
                        object_id=f"er-{intent.object_id}-filled",
                        as_of=now,
                        producer=_PRODUCER,
                        trace_id=intent.trace_id,
                        order_intent_id=intent.object_id,
                        attempt_id=f"attempt-{intent.object_id}",
                        bracket_role="parent",
                        state="filled",
                        previous_state=self.olap.last_state(intent.object_id),
                        requested_units=intent.delta_units,
                        filled_units=magnitude,
                    )
                )
                if fill_result.get("emergency"):
                    raise L1ExecutionError(fill_result["emergency"])
                self.service.apply_position_close(target)
                self.olap.advance_effect(effect_id, "acknowledged")
                self.olap.advance_effect(effect_id, "terminal_flat")
                if (
                    entry_effect is not None
                    and entry_effect["state"] == "acknowledged"
                ):
                    self.olap.advance_effect(
                        entry_effect["effect_id"], "terminal_flat"
                    )
        except Exception as error:  # noqa: BLE001 - persist and stop risk
            with self.olap.atomic_unit():
                self.olap.record_broker_fact(
                    effect_id,
                    "flatten_l0_failure",
                    {"error": f"{type(error).__name__}: {error}"},
                )
                if self.olap.effect_row(effect_id)["state"] != "effect_unknown":
                    self.olap.advance_effect(effect_id, "effect_unknown")
                self.olap.set_state("halt", "hold")
            return {
                "idempotency_key": effect["idempotency_key"],
                "state": "effect_unknown",
                "error": f"{type(error).__name__}: {error}",
            }
        return {
            "idempotency_key": effect["idempotency_key"],
            "state": "terminal_flat",
            "target": target,
        }

    def _sync_submitted_flatten(
        self, effect: dict[str, Any], *, now: datetime
    ) -> dict[str, Any]:
        effect_id = effect["effect_id"]
        contract = self.olap.effect_contract(effect_id)
        if contract is None or contract.get("kind") != "flatten":
            return self._refuse_flatten(effect_id, "missing_flatten_contract")
        account = self.client.connected_account()
        fingerprint = (
            None if account is None else hashlib.sha256(str(account).encode()).hexdigest()[:16]
        )
        if fingerprint != contract["account_fingerprint"]:
            return self._refuse_flatten(effect_id, "connected_account_not_authorized")
        expected = expected_contract_facts(contract["instrument"])
        expected_con_id = contract.get("expected_con_id")
        order_id = int(contract["order_id"])
        remaining = self._position_units(
            self.client.position_facts(), expected, str(account), expected_con_id
        )
        order = next(
            (
                fact for fact in self.client.open_order_facts()
                if int(fact.get("orderId", -1)) == order_id
            ),
            None,
        )
        if order is None:
            if abs(remaining) <= 1e-9:
                return self._finalize_flatten(effect, contract, now=now)
            age = (
                now - datetime.fromisoformat(effect["updated_at"])
            ).total_seconds()
            if age > self.max_ack_age_seconds:
                return self._refuse_flatten(
                    effect_id, f"flatten_order_fact_timeout:{age:.1f}s"
                )
            return {
                "idempotency_key": effect["idempotency_key"],
                "state": "submitted_pending_ack",
                "reason": "flatten_order_fact_pending",
            }
        status = order.get("status")
        if status in ("PendingSubmit", "PreSubmitted", "Submitted"):
            age = (
                now - datetime.fromisoformat(effect["updated_at"])
            ).total_seconds()
            if age > self.max_ack_age_seconds:
                return self._refuse_flatten(
                    effect_id, f"flatten_ack_timeout:{age:.1f}s:{status}"
                )
            return {
                "idempotency_key": effect["idempotency_key"],
                "state": "submitted_pending_ack",
                "broker_status": status,
            }
        magnitude = abs(float(contract["delta_units"]))
        if (
            status != "Filled"
            or order.get("filled") is None
            or abs(float(order["filled"]) - magnitude) > 1e-9
            or order.get("remaining") is None
            or abs(float(order["remaining"])) > 1e-9
        ):
            return self._refuse_flatten(
                effect_id, f"flatten_order_not_exactly_filled:{status}"
            )
        if abs(remaining) > 1e-9:
            return self._refuse_flatten(
                effect_id, f"flatten_filled_but_position_remaining:{remaining}"
            )
        return self._finalize_flatten(effect, contract, now=now)

    def _consume_flatten(
        self, pending: dict[str, Any], *, now: datetime
    ) -> dict[str, Any]:
        key = pending["idempotency_key"]
        intent = OrderIntentV2.model_validate_json(pending["intent_json"])
        target = intent.reduce_target_order_intent_id
        entry_effect = self._entry_effect_for_target(target)
        effect_id = effect_id_for(key)
        with self.olap.atomic_unit():
            self.olap.create_effect(effect_id, key, "flatten", [], None)

        # ── finding 070: exact risk-reducing preflight BEFORE any call ──
        account = self.client.connected_account()
        if account is None or hashlib.sha256(
            str(account).encode()
        ).hexdigest()[:16] != self.profile.account_fingerprint:
            result = self._refuse_flatten(
                effect_id, "connected_account_not_authorized")
            result["idempotency_key"] = key
            return result
        expected = expected_contract_facts(intent.instrument)
        expected_con_id = None
        if entry_effect is not None:
            entry_contract = self.olap.effect_contract(entry_effect["effect_id"])
            if entry_contract is not None:
                expected_con_id = entry_contract["expected_con_id"]
        position = self._position_units(
            self.client.position_facts(), expected, str(account), expected_con_id
        )
        flatten_units, refusal = self.exact_reduction_units(
            position, intent.delta_units
        )
        if refusal is not None:
            result = self._refuse_flatten(effect_id, refusal)
            result["idempotency_key"] = key
            return result

        order_id = self.client.reserve_order_ids(1)
        contract, order = build_flatten_order(
            instrument=intent.instrument,
            account=str(account),
            net_units=flatten_units,
            order_id=order_id,
        )
        flatten_contract = {
            "schema": "lts.ibkr_l1.flatten_contract.v1",
            "kind": "flatten",
            "intent": intent.model_dump(mode="json"),
            "instrument": intent.instrument,
            "delta_units": intent.delta_units,
            "target_order_intent_id": target,
            "account_fingerprint": self.profile.account_fingerprint,
            "expected_con_id": expected_con_id,
            "order_id": order_id,
        }
        with self.olap.atomic_unit():
            self.olap.set_effect_order_ids(effect_id, [order_id])
            self.olap.store_effect_contract(effect_id, flatten_contract)

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

            self.olap.record_broker_fact(
                effect_id, "call_attempt",
                {"leg": "flatten", "orderId": int(order.orderId)},
            )
            result = self.client.place_order(contract, order)
            self.olap.record_broker_fact(
                effect_id, "call_result", {"leg": "flatten", **result}
            )
            with self.olap.atomic_unit():
                self.olap.advance_effect(effect_id, "submitted_pending_ack")
        except Exception as error:  # noqa: BLE001 — journal, hold, unknown
            with self.olap.atomic_unit():
                self.olap.record_broker_fact(
                    effect_id, "call_failure",
                    {"error": f"{type(error).__name__}: {error}"},
                )
                self.olap.advance_effect(effect_id, "effect_unknown")
                self.olap.set_state("halt", "hold")
            return {"idempotency_key": key, "state": "effect_unknown"}

        return self._sync_submitted_flatten(
            self.olap.effect_row(effect_id), now=now
        )

    # -- restart -----------------------------------------------------------
    def _refuse_resume(self, effect_id: str, reason: str) -> None:
        """A resume that cannot prove its bindings journals, demotes to
        unknown and holds. It never guesses from current configuration."""
        with self.olap.atomic_unit():
            self.olap.record_broker_fact(
                effect_id, "resume_refusal", {"reason": reason}
            )
            if self.olap.effect_row(effect_id)["state"] != "effect_unknown":
                self.olap.advance_effect(effect_id, "effect_unknown")
            if self.olap.get_state("halt", "none") == "none":
                self.olap.set_state("halt", "hold")

    def resume(self, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """After a crash: classify every effect from durable facts, then
        drive entry effects that were submitted-but-unacknowledged (or
        ambiguous) through exact acknowledgement AGAINST THE IMMUTABLE
        EFFECT CONTRACT (finding 072) — never against current profile,
        rounding or account configuration."""
        now = now or datetime.now(timezone.utc)
        report = self.executor.resume_report()
        outcomes = []
        for entry in report:
            effect = self.olap.effect_row(entry["effect_id"])
            if effect["kind"] != "bracket_entry" or effect["state"] not in (
                "submitted_pending_ack", "effect_unknown", "recovering",
            ):
                outcomes.append(entry)
                continue
            entry = dict(entry)
            contract = self.olap.effect_contract(effect["effect_id"])
            if contract is None:
                self._refuse_resume(effect["effect_id"], "missing_effect_contract")
                entry["resume_refused"] = "missing_effect_contract"
                entry["state"] = "effect_unknown"
                outcomes.append(entry)
                continue
            account = self.client.connected_account()
            try:
                plan = plan_from_contract(contract, account=str(account))
            except L1ExecutionError as error:
                self._refuse_resume(effect["effect_id"], str(error))
                entry["resume_refused"] = str(error)
                entry["state"] = "effect_unknown"
                outcomes.append(entry)
                continue
            if effect["state"] == "recovering":
                recovery = self.controller.recover(
                    effect["effect_id"],
                    plan,
                    instrument=contract["instrument"],
                    expected_con_id=contract["expected_con_id"],
                    now=now,
                    acknowledgement_timeout_seconds=self.max_ack_age_seconds,
                )
                verdict = {
                    "protected": False,
                    "recovery": recovery,
                }
            else:
                verdict = self.controller.acknowledge(
                    effect["effect_id"], plan,
                    instrument=contract["instrument"],
                    expected_con_id=contract["expected_con_id"],
                )
            entry["reacknowledged"] = verdict["protected"]
            entry["state"] = self.olap.effect_row(effect["effect_id"])["state"]
            outcomes.append(entry)
        return outcomes
