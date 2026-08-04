"""Durable, Paper-only Alpaca bracket execution.

The module deliberately supports US equities/ETFs only. Alpaca crypto does
not expose native bracket orders, so admitting it here would violate the LTS
rule that every risk-increasing order carries broker-native SL and TP.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional

import requests
from trading_contracts import ExecutionReportV2, OrderIntentV2, ProtectionLegState

from app.alpaca_paper_lab import AlpacaPaperClient, AlpacaPaperError
from app.demo_execution_service import DemoExecutionError, DemoExecutionService
from app.ibkr_l1_journal import L1ExecutionOlap


ALPACA_L1_VERSION = "lts.alpaca.paper.l1.v1"
_PRODUCER = {"name": "lts.alpaca_l1", "version": "0.2.0"}
_TERMINAL_ORDER_STATES = {
    "canceled", "expired", "failed", "filled", "rejected", "replaced",
}


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _effect_id(idempotency_key: str) -> str:
    return "alpaca-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class AlpacaL1Profile:
    venue: str
    environment: str
    account_fingerprint: str
    symbol: str
    asset_id: str
    quantity_ceiling: Decimal
    max_orders_per_day: int
    max_risk_fraction_at_stop: Decimal

    @classmethod
    def load(cls, path: str | Path) -> "AlpacaL1Profile":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema") != "lts.alpaca.paper_l1_profile.v1":
            raise AlpacaPaperError("Unsupported Alpaca L1 profile schema")
        if data.get("venue") != "alpaca_paper" or data.get("environment") != "paper":
            raise AlpacaPaperError("Alpaca L1 is Paper-only")
        if data.get("asset_class") != "us_equity":
            raise AlpacaPaperError("Only US equities/ETFs support native Alpaca brackets")
        if data.get("orders", {}).get("enabled") is not True:
            raise AlpacaPaperError("Alpaca Paper order mandate is disabled")
        fingerprint = str(data.get("account_fingerprint", "")).lower()
        if len(fingerprint) != 16 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise AlpacaPaperError("Invalid Alpaca account fingerprint")
        quantity = _decimal(data.get("quantity_ceiling", 0))
        risk = _decimal(data.get("max_risk_fraction_at_stop", 0))
        max_orders = int(data.get("max_orders_per_day", 0))
        if quantity <= 0 or not 0 < risk <= Decimal("0.01") or max_orders <= 0:
            raise AlpacaPaperError("Alpaca Paper mandate limits are invalid")
        return cls(
            venue="alpaca_paper",
            environment="paper",
            account_fingerprint=fingerprint,
            symbol=str(data["symbol"]).upper(),
            asset_id=str(data["asset_id"]),
            quantity_ceiling=quantity,
            max_orders_per_day=max_orders,
            max_risk_fraction_at_stop=risk,
        )


class AlpacaPaperTradingClient(AlpacaPaperClient):
    """Minimal write client permanently pinned to the Alpaca Paper host."""

    def _write_request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        allow_empty: bool = False,
    ) -> Any:
        if self.trading_base_url != "https://paper-api.alpaca.markets":
            raise AlpacaPaperError("Alpaca write endpoint is not Paper")
        try:
            response = self.session.request(
                method,
                f"{self.trading_base_url}{path}",
                json=None if payload is None else dict(payload),
                params=params,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise AlpacaPaperError(f"Alpaca Paper {method} failed: {type(exc).__name__}") from exc
        if not 200 <= response.status_code < 300:
            try:
                body = response.json()
            except ValueError:
                body = {}
            message = body.get("message", "request rejected") if isinstance(body, dict) else ""
            raise AlpacaPaperError(
                f"Alpaca Paper {method} {path} returned HTTP "
                f"{response.status_code}: {message}"
            )
        if allow_empty and not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise AlpacaPaperError("Alpaca Paper returned invalid JSON") from exc

    def submit_bracket(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        required = {
            "symbol", "qty", "side", "stop_price", "take_profit_price",
            "client_order_id",
        }
        if required.difference(plan):
            raise AlpacaPaperError("Incomplete Alpaca bracket plan")
        payload = {
            "symbol": str(plan["symbol"]).upper(),
            "qty": str(plan["qty"]),
            "side": str(plan["side"]),
            "type": "market",
            "time_in_force": str(plan["time_in_force"]),
            "order_class": "bracket",
            "client_order_id": str(plan["client_order_id"]),
            "extended_hours": False,
            "take_profit": {"limit_price": str(plan["take_profit_price"])},
            "stop_loss": {"stop_price": str(plan["stop_price"])},
        }
        return dict(self._write_request("POST", "/v2/orders", payload=payload))

    def order(self, order_id: str) -> dict[str, Any]:
        return dict(self._write_request(
            "GET", f"/v2/orders/{order_id}", params={"nested": "true"}
        ))

    def cancel_order(self, order_id: str) -> None:
        self._write_request("DELETE", f"/v2/orders/{order_id}", allow_empty=True)

    def close_position(self, symbol: str) -> dict[str, Any]:
        return dict(self._write_request("DELETE", f"/v2/positions/{symbol}"))


def verify_native_bracket(
    order: Mapping[str, Any], contract: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify the parent and both direct broker protection legs exactly."""
    failures: list[str] = []
    expected_qty = _decimal(contract["qty"])
    expected_side = str(contract["side"])
    close_side = "sell" if expected_side == "buy" else "buy"
    if str(order.get("symbol", "")).upper() != str(contract["symbol"]).upper():
        failures.append("symbol")
    if order.get("order_class") != "bracket":
        failures.append("order_class")
    if order.get("time_in_force") != contract.get("time_in_force"):
        failures.append("time_in_force")
    if order.get("side") != expected_side:
        failures.append("side")
    if order.get("client_order_id") != contract["client_order_id"]:
        failures.append("client_order_id")
    try:
        if _decimal(order.get("qty")) != expected_qty:
            failures.append("qty")
    except Exception:  # noqa: BLE001 - malformed broker fact is a refusal
        failures.append("qty")

    legs = order.get("legs")
    if not isinstance(legs, list) or len(legs) != 2:
        failures.append("legs")
        legs = []
    stop_legs = [leg for leg in legs if leg.get("type") in {"stop", "stop_limit"}]
    take_legs = [leg for leg in legs if leg.get("type") == "limit"]
    if len(stop_legs) != 1:
        failures.append("stop_leg")
    if len(take_legs) != 1:
        failures.append("take_profit_leg")
    for name, candidates, price_key, expected_price in (
        ("stop", stop_legs, "stop_price", contract["stop_price"]),
        ("take_profit", take_legs, "limit_price", contract["take_profit_price"]),
    ):
        if len(candidates) != 1:
            continue
        leg = candidates[0]
        if leg.get("side") != close_side:
            failures.append(f"{name}_side")
        if leg.get("time_in_force") != contract.get("time_in_force"):
            failures.append(f"{name}_time_in_force")
        try:
            if _decimal(leg.get("qty")) != expected_qty:
                failures.append(f"{name}_qty")
            if _decimal(leg.get(price_key)) != _decimal(expected_price):
                failures.append(f"{name}_price")
        except Exception:  # noqa: BLE001
            failures.append(f"{name}_fact")
    return {"protected": not failures, "failures": sorted(set(failures))}


class AlpacaL1Executor:
    """Restart-safe native-bracket submitter and protection monitor."""

    def __init__(
        self,
        store: L1ExecutionOlap,
        client: AlpacaPaperTradingClient,
        profile: AlpacaL1Profile,
        service: Optional[DemoExecutionService] = None,
    ) -> None:
        self.store = store
        self.client = client
        self.profile = profile
        self.service = service

    def _account(self) -> dict[str, Any]:
        account = self.client.account()
        observed = self.client.account_fingerprint(account)
        if observed != self.profile.account_fingerprint:
            raise AlpacaPaperError("Connected Alpaca Paper account is not authorized")
        if account.get("status") != "ACTIVE" or account.get("trading_blocked"):
            raise AlpacaPaperError("Connected Alpaca Paper account cannot trade")
        return account

    def submit(
        self,
        *,
        idempotency_key: str,
        symbol: str,
        asset_id: str,
        qty: Decimal,
        side: str,
        stop_price: Decimal,
        take_profit_price: Decimal,
        risk_fraction_at_stop: Decimal,
        model_evidence: Mapping[str, Any],
        l0_context: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        existing = self.store.effect_by_key(idempotency_key)
        if existing is not None:
            return {**existing, "replayed": True}
        if symbol.upper() != self.profile.symbol or asset_id != self.profile.asset_id:
            raise AlpacaPaperError("Alpaca order does not match the active model route")
        if side not in {"buy", "sell"} or qty <= 0 or qty > self.profile.quantity_ceiling:
            raise AlpacaPaperError("Alpaca side or quantity exceeds the Paper mandate")
        if side == "buy" and not stop_price < take_profit_price:
            raise AlpacaPaperError("Long Alpaca bracket geometry is invalid")
        if side == "sell" and not take_profit_price < stop_price:
            raise AlpacaPaperError("Short Alpaca bracket geometry is invalid")
        if not Decimal("0") < risk_fraction_at_stop <= self.profile.max_risk_fraction_at_stop:
            raise AlpacaPaperError("Alpaca stop risk exceeds the Paper mandate")
        utc_day = datetime.now(timezone.utc).date().isoformat()
        if self.store.effect_count_since(
            "alpaca_bracket_entry", f"{utc_day}T00:00:00+00:00"
        ) >= self.profile.max_orders_per_day:
            raise AlpacaPaperError("Alpaca daily Paper order budget is exhausted")
        required_evidence = {"model_id", "artifact_sha256", "config_sha256", "input_sha256"}
        if required_evidence.difference(model_evidence):
            raise AlpacaPaperError("Model evidence is incomplete")
        self._account()

        effect_id = _effect_id(idempotency_key)
        client_order_id = "lts-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
        contract = {
            "venue": "alpaca_paper",
            "environment": "paper",
            "account_fingerprint": self.profile.account_fingerprint,
            "symbol": symbol.upper(),
            "asset_id": asset_id,
            "qty": str(qty),
            "side": side,
            "stop_price": str(stop_price),
            "take_profit_price": str(take_profit_price),
            "time_in_force": "gtc",
            "risk_fraction_at_stop": str(risk_fraction_at_stop),
            "client_order_id": client_order_id,
            "model_evidence": dict(model_evidence),
        }
        if l0_context is not None:
            contract["l0"] = dict(l0_context)
        with self.store.atomic_unit():
            self.store.create_effect(effect_id, idempotency_key, "alpaca_bracket_entry", [])
            self.store.store_effect_contract(effect_id, contract)
            self.store.record_broker_fact(effect_id, "call_attempt", {
                "operation": "submit_native_bracket",
                "client_order_id": client_order_id,
            })
            self.store.advance_effect(effect_id, "effect_unknown")
        try:
            submitted = self.client.submit_bracket(contract)
        except Exception:
            self.store.set_state("halt", "hold")
            raise
        order_id = str(submitted.get("id", ""))
        if not order_id:
            self.store.set_state("halt", "hold")
            raise AlpacaPaperError("Alpaca bracket response has no order id")
        with self.store.atomic_unit():
            self.store.set_effect_order_ids(effect_id, [order_id])
            self.store.record_broker_fact(effect_id, "submit_response", submitted)
            self.store.advance_effect(effect_id, "submitted_pending_ack")
        return self.acknowledge(effect_id)

    def consume_pending(self) -> list[dict[str, Any]]:
        """Consume accepted L0 orders; direct callers never bypass L0 risk."""
        results = []
        for pending in self.store.l1_pending_decisions("would_be_order"):
            intent = OrderIntentV2.model_validate_json(pending["intent_json"])
            if intent.venue != self.profile.venue:
                continue
            if pending["capability_evidence"] != "live_observed":
                raise AlpacaPaperError("Alpaca L1 requires live-observed capability")
            if intent.instrument != self.profile.symbol or intent.protection is None:
                raise AlpacaPaperError("Alpaca L0 intent route or protection mismatch")
            if intent.risk is None:
                raise AlpacaPaperError("Alpaca L0 order lacks its risk envelope")
            evidence = {
                "model_id": intent.preflight.get("source_model_id"),
                "artifact_sha256": intent.preflight.get("source_artifact_sha256"),
                "config_sha256": intent.preflight.get("source_config_sha256"),
                "input_sha256": intent.preflight.get("source_input_sha256"),
            }
            if any(not value for value in evidence.values()):
                raise AlpacaPaperError("L0 order lacks complete source-model evidence")
            results.append(self.submit(
                idempotency_key=pending["idempotency_key"],
                symbol=intent.instrument,
                asset_id=intent.asset_id,
                qty=_decimal(abs(intent.delta_units)),
                side="buy" if intent.delta_units > 0 else "sell",
                stop_price=_decimal(intent.protection.stop_loss_price),
                take_profit_price=_decimal(intent.protection.take_profit_price),
                risk_fraction_at_stop=_decimal(intent.risk.risk_fraction_at_stop),
                model_evidence=evidence,
                l0_context={
                    "reservation_id": intent.risk.reservation_id,
                    "order_intent_id": intent.object_id,
                    "trace_id": intent.trace_id,
                    "delta_units": intent.delta_units,
                },
            ))
        return results

    def _l0_context(
        self, effect: Mapping[str, Any], contract: Mapping[str, Any]
    ) -> Optional[dict[str, Any]]:
        stored = contract.get("l0")
        if isinstance(stored, Mapping):
            return dict(stored)
        for pending in self.store.active_reservation_intents():
            if pending["idempotency_key"] != effect["idempotency_key"]:
                continue
            intent = self.store.decision_intent(pending["idempotency_key"])
            if intent is None:
                return None
            return {
                "reservation_id": pending["reservation_id"],
                "order_intent_id": intent["object_id"],
                "trace_id": intent["trace_id"],
                "delta_units": intent["delta_units"],
            }
        return None

    def _effective_contract(
        self, effect_id: str, contract: Mapping[str, Any]
    ) -> dict[str, Any]:
        effective = dict(contract)
        if effective.get("time_in_force"):
            return effective
        for fact in self.store.broker_facts(effect_id):
            observed = fact.get("fact", {})
            if observed.get("time_in_force"):
                effective["time_in_force"] = observed["time_in_force"]
                break
        return effective

    @staticmethod
    def _protection_legs(
        order: Mapping[str, Any], contract: Mapping[str, Any], covered: float
    ) -> list[ProtectionLegState]:
        legs = order.get("legs")
        if not isinstance(legs, list):
            return []
        result = []
        for leg_name, types, price_key in (
            ("stop_loss", {"stop", "stop_limit"}, "stop_price"),
            ("take_profit", {"limit"}, "limit_price"),
        ):
            matches = [leg for leg in legs if leg.get("type") in types]
            if len(matches) != 1:
                return []
            leg = matches[0]
            result.append(ProtectionLegState(
                leg=leg_name,
                broker_confirmed=True,
                broker_leg_id=str(leg.get("id")),
                price=float(leg[price_key]),
                covered_units=covered,
            ))
        return result

    def _apply_l0_snapshot(
        self,
        effect: Mapping[str, Any],
        contract: Mapping[str, Any],
        order: Mapping[str, Any],
        *,
        positions_open: bool,
    ) -> dict[str, Any]:
        if self.service is None:
            return {"reconciled": False, "reason": "service_unavailable"}
        context = self._l0_context(effect, contract)
        if context is None:
            return {"reconciled": False, "reason": "l0_context_unavailable"}
        reservation_id = str(context["reservation_id"])
        intent_id = str(context["order_intent_id"])
        requested = float(context["delta_units"])
        trace_id = str(context["trace_id"])
        now = datetime.now(timezone.utc)
        previous = self.store.last_state(intent_id)
        changed = False
        broker_ids = {"parent": str(order.get("id", effect["order_ids"][0]))}
        for leg in order.get("legs") or []:
            if leg.get("type") in {"stop", "stop_limit"}:
                broker_ids["stop_loss"] = str(leg.get("id"))
            elif leg.get("type") == "limit":
                broker_ids["take_profit"] = str(leg.get("id"))
        if previous == "requested":
            self.service.apply_execution_event(ExecutionReportV2(
                object_id=f"er-alpaca-ack-{reservation_id}", as_of=now,
                producer=_PRODUCER, trace_id=trace_id,
                order_intent_id=intent_id,
                attempt_id=f"attempt-{reservation_id}",
                bracket_role="parent", state="accepted",
                previous_state="requested", requested_units=requested,
                broker_ids=broker_ids,
            ))
            previous = "accepted"
            changed = True

        filled = float(order.get("filled_qty") or 0.0)
        requested_abs = abs(requested)
        applied = abs(self.store.exposure_units(f"exp-{intent_id}") or 0.0)
        if filled > requested_abs + 1e-9:
            raise DemoExecutionError("Alpaca cumulative fill exceeds immutable quantity")
        if filled > applied + 1e-9 and previous not in {"filled", "closed"}:
            effective = self._effective_contract(str(effect["effect_id"]), contract)
            verdict = verify_native_bracket(order, effective)
            protection = self._protection_legs(order, effective, filled)
            if not verdict["protected"] or len(protection) != 2:
                raise DemoExecutionError(
                    "Alpaca fill lacks exact broker-native protection evidence"
                )
            state = "filled" if abs(filled - requested_abs) <= 1e-9 else "partially_filled"
            digest = hashlib.sha256(str(filled).encode()).hexdigest()[:12]
            self.service.apply_execution_event(ExecutionReportV2(
                object_id=f"er-alpaca-fill-{reservation_id}-{digest}", as_of=now,
                producer=_PRODUCER, trace_id=trace_id,
                order_intent_id=intent_id,
                attempt_id=f"attempt-{reservation_id}",
                bracket_role="parent", state=state,
                previous_state=previous, requested_units=requested,
                filled_units=filled,
                filled_price=(
                    None if order.get("filled_avg_price") is None
                    else float(order["filled_avg_price"])
                ),
                protection_legs=protection, broker_ids=broker_ids,
            ))
            previous = state
            changed = True

        status = str(order.get("status", ""))
        if filled == 0 and status in _TERMINAL_ORDER_STATES and previous in {
            "requested", "accepted"
        }:
            state = "expired" if status == "expired" else (
                "rejected" if status in {"failed", "rejected"} else "cancelled"
            )
            self.service.apply_execution_event(ExecutionReportV2(
                object_id=f"er-alpaca-terminal-{reservation_id}", as_of=now,
                producer=_PRODUCER, trace_id=trace_id,
                order_intent_id=intent_id,
                attempt_id=f"attempt-{reservation_id}",
                bracket_role="parent", state=state,
                previous_state=previous, requested_units=requested,
                broker_ids=broker_ids,
            ))
            previous = state
            changed = True
        if (
            not positions_open
            and self.store.exposure_state(f"exp-{intent_id}") == "open"
        ):
            self.service.apply_position_close(intent_id)
            changed = True
        if changed:
            self.store.record_broker_fact(
                str(effect["effect_id"]), "l0_reconciled", {
                    "reservation_id": reservation_id,
                    "order_intent_id": intent_id,
                    "broker_status": status,
                    "filled_qty": filled,
                    "positions_open": positions_open,
                }
            )
        return {"reconciled": True, "reservation_id": reservation_id,
                "lifecycle_state": previous, "changed": changed}

    def reconcile_terminal_effects(self) -> list[dict[str, Any]]:
        """Repair terminal broker facts that predate L0 lifecycle ingestion."""
        results = []
        candidates: dict[str, dict[str, Any]] = {}
        for pending in self.store.active_reservation_intents():
            effect = self.store.effect_by_key(pending["idempotency_key"])
            if effect is None or effect["kind"] != "alpaca_bracket_entry":
                continue
            candidates[effect["effect_id"]] = effect
        for exposure in self.store.open_exposures():
            reservation_id = self.store.exposure_reservation(
                exposure["order_intent_id"]
            )
            idempotency_key = (
                None if reservation_id is None
                else self.store.reservation_idempotency(reservation_id)
            )
            effect = (
                None if idempotency_key is None
                else self.store.effect_by_key(idempotency_key)
            )
            if effect is not None and effect["kind"] == "alpaca_bracket_entry":
                candidates[effect["effect_id"]] = effect
        for effect in candidates.values():
            contract = self.store.effect_contract(effect["effect_id"])
            if contract is None or not effect["order_ids"]:
                continue
            order = self.client.order(str(effect["order_ids"][0]))
            positions = [
                item for item in self.client.positions()
                if item.get("symbol") == contract["symbol"]
            ]
            if positions or str(order.get("status", "")) not in _TERMINAL_ORDER_STATES:
                continue
            snapshots = [
                fact["fact"] for fact in self.store.broker_facts(effect["effect_id"])
                if fact["fact_kind"] in {"ack_snapshot", "monitor_snapshot"}
                and float(fact["fact"].get("filled_qty") or 0.0) > 0
            ]
            active_snapshots = [
                snapshot for snapshot in snapshots
                if all(
                    leg.get("status") not in _TERMINAL_ORDER_STATES
                    for leg in snapshot.get("legs") or []
                )
            ]
            evidence = active_snapshots[-1] if active_snapshots else (
                snapshots[-1] if snapshots else order
            )
            results.append(self._apply_l0_snapshot(
                effect, contract, evidence, positions_open=False
            ))
        return results

    def acknowledge(self, effect_id: str) -> dict[str, Any]:
        effect = self.store.effect_row(effect_id)
        contract = self.store.effect_contract(effect_id)
        if effect is None or contract is None or not effect["order_ids"]:
            raise DemoExecutionError("Alpaca effect lacks immutable submission facts")
        order_id = str(effect["order_ids"][0])
        order = self.client.order(order_id)
        verdict = verify_native_bracket(order, contract)
        self.store.record_broker_fact(effect_id, "ack_snapshot", order)
        if verdict["protected"]:
            if effect["state"] == "submitted_pending_ack":
                self.store.advance_effect(effect_id, "acknowledged")
            positions_open = any(
                item.get("symbol") == contract["symbol"]
                for item in self.client.positions()
            )
            self._apply_l0_snapshot(
                effect, contract, order, positions_open=positions_open
            )
            return {"effect_id": effect_id, "order_id": order_id, **verdict}
        self._recover(effect_id, order_id, contract, verdict["failures"])
        return {"effect_id": effect_id, "order_id": order_id, **verdict}

    def _recover(
        self,
        effect_id: str,
        order_id: str,
        contract: Mapping[str, Any],
        failures: list[str],
    ) -> None:
        effect = self.store.effect_row(effect_id)
        if effect and effect["state"] != "recovering":
            self.store.advance_effect(effect_id, "recovering")
        self.store.record_broker_fact(effect_id, "protection_failure", {"failures": failures})
        try:
            self.client.cancel_order(order_id)
            self.store.record_broker_fact(effect_id, "cancel_requested", {"order_id": order_id})
        except AlpacaPaperError as exc:
            self.store.record_broker_fact(effect_id, "cancel_refused", {"error": str(exc)})
        positions = [p for p in self.client.positions() if p.get("symbol") == contract["symbol"]]
        if positions:
            closed = self.client.close_position(str(contract["symbol"]))
            self.store.record_broker_fact(effect_id, "flatten_requested", closed)
        self.store.set_state("halt", "hold")

    def monitor(self, effect_id: str) -> dict[str, Any]:
        effect = self.store.effect_row(effect_id)
        contract = self.store.effect_contract(effect_id)
        if effect is None or contract is None or not effect["order_ids"]:
            raise DemoExecutionError("Unknown Alpaca effect")
        order = self.client.order(str(effect["order_ids"][0]))
        verdict = verify_native_bracket(order, contract)
        self.store.record_broker_fact(effect_id, "monitor_snapshot", order)
        status = str(order.get("status", ""))
        positions = [
            position for position in self.client.positions()
            if position.get("symbol") == contract["symbol"]
        ]
        if not verdict["protected"] and (
            positions or status not in _TERMINAL_ORDER_STATES
        ):
            if effect["state"] != "recovering":
                self._recover(
                    effect_id, str(effect["order_ids"][0]), contract,
                    verdict["failures"]
                )
        elif (
            not positions
            and status in _TERMINAL_ORDER_STATES
            and effect["state"] in {"acknowledged", "recovering"}
        ):
            self._apply_l0_snapshot(effect, contract, order, positions_open=False)
            self.store.advance_effect(effect_id, "terminal_flat")
        elif positions:
            self._apply_l0_snapshot(effect, contract, order, positions_open=True)
        return {
            "effect_id": effect_id,
            "status": status,
            "open_positions": len(positions),
            **verdict,
        }
