"""Continuous selected-model -> L0 -> protected IBKR Paper execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from prediction_provider_mechanics import build_closed_bar_features
from trading_contracts import AssetIntent, BrokerCapabilitySnapshot, InstrumentCapability

from app.alpaca_model_runner import ModelSessionStore
from app.demo_execution_service import DemoExecutionConfig, DemoExecutionService, ZeroNetworkSink
from app.ibkr_l1_adapter import L1ExecutionError
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_l1_outbox import L1OutboxConsumer
from app.ibkr_model_authority import ContinuousPaperGate, ContinuousPaperProfile
from app.live_model_selection import LiveModelSelectionError, SelectedLinearPolicy
from app.model_runner_heartbeat import write_runner_heartbeat


_OPEN_STATUSES = {"PendingSubmit", "PendingCancel", "PreSubmitted", "Submitted"}


class IbkrModelRunnerError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _client(profile: ContinuousPaperProfile):
    from app.ibkr_l1_tws import IbAsyncTwsClient
    return IbAsyncTwsClient(profile)


class IbkrModelRunner:
    def __init__(
        self,
        config: dict[str, Any],
        client_factory: Optional[Callable[[ContinuousPaperProfile], Any]] = None,
    ) -> None:
        self.config = config
        self.profile = ContinuousPaperProfile.load(
            Path(config["profile_file"]).expanduser()
        )
        self.client = (client_factory or _client)(self.profile)
        database_path = _path(config["service"]["database_path"])
        database_path.parent.mkdir(parents=True, exist_ok=True)
        config["service"]["database_path"] = str(database_path)
        service_config = DemoExecutionConfig.from_dict(config["service"])
        self.olap = L1ExecutionOlap(service_config.database_path)
        self.service = DemoExecutionService(service_config, self.olap, ZeroNetworkSink())
        self.consumer = L1OutboxConsumer(
            self.service, self.olap, self.client, self.profile,
            ContinuousPaperGate(Path(config["mandate_file"]).expanduser()),
            price_decimals=int(config["price_decimals"]),
            quantity_decimals=int(config["quantity_decimals"]),
            max_decision_age_seconds=float(config["max_decision_age_seconds"]),
        )
        self.sessions = ModelSessionStore(self.olap._con)
        self.selector = SelectedLinearPolicy(
            manifest_file=config["model"]["manifest_file"],
            expected_asset_id=self.profile.asset_id,
            expected_timeframe=config["model"]["expected_timeframe"],
            execution_tier=config["model"]["execution_tier"],
        )
        self.manifest = self.selector.manifest
        self.policy = self.selector.policy

    def _route_orders(self) -> list[dict[str, Any]]:
        base, quote = self.profile.instrument.split(".")
        return [
            fact for fact in self.client.open_order_facts()
            if fact.get("status") in _OPEN_STATUSES
            and fact.get("contract", {}).get("symbol") == base
            and fact.get("contract", {}).get("currency") == quote
            and fact.get("contract", {}).get("secType") == "CASH"
        ]

    def _route_position(self) -> float:
        base, quote = self.profile.instrument.split(".")
        account = self.client.connected_account()
        return sum(
            float(fact.get("units", 0.0))
            for fact in self.client.position_facts()
            if fact.get("account") == account
            and fact.get("symbol") == base
            and fact.get("currency") == quote
            and fact.get("secType") == "CASH"
            and (
                self.profile.contract_con_id is None
                or int(fact.get("conId", 0)) == self.profile.contract_con_id
            )
        )

    def _monitor_l1(self, now: datetime) -> dict[str, Any]:
        resumed = self.consumer.resume(now=now)
        fills = []
        for effect in self.olap.nonterminal_effects():
            if effect["kind"] == "bracket_entry" and effect["state"] == "acknowledged":
                result = self.consumer.sync_parent_fill(effect["effect_id"], now=now)
                if result is not None:
                    fills.append({"effect_id": effect["effect_id"], "result": result})
        flattens = self.consumer.consume_flattens(now=now)
        return {"resumed": resumed, "fills": fills, "flattens": flattens}

    def _capability(
        self, quote: dict[str, Any], now: datetime,
    ) -> BrokerCapabilitySnapshot:
        fact_hash = hashlib.sha256(
            json.dumps(quote, sort_keys=True, default=str).encode()
        ).hexdigest()
        return BrokerCapabilitySnapshot(
            object_id=f"ibkr-model-cap-{fact_hash[:20]}", as_of=now,
            producer={"name": "lts.ibkr_model_runner", "version": "0.1.0"},
            trace_id=f"ibkr-model-cap-{fact_hash[:16]}", venue="ibkr_paper",
            account_fingerprint=self.profile.account_fingerprint,
            environment="paper", capability_evidence="live_observed",
            source_artifact_hash="sha256:" + fact_hash,
            source_observed_at=quote["observed_at"],
            instruments=[InstrumentCapability(
                instrument=self.profile.instrument, tradeable=True, shortable=True,
                min_units=float(self.config["route"]["minimum_units"]),
                unit_step=float(self.config["route"]["unit_step"]),
                price_decimals=int(self.config["price_decimals"]),
                margin_rate=float(self.config["route"]["margin_rate"]),
                native_stop_loss=True, native_take_profit=True, native_bracket=True,
            )],
        )

    def tick(self) -> dict[str, Any]:
        now = _utc_now()
        selection_error = None
        try:
            if self.selector.refresh():
                self.manifest = self.selector.manifest
                self.policy = self.selector.policy
        except LiveModelSelectionError as exc:
            selection_error = str(exc)

        monitoring = self._monitor_l1(now)
        account = self.client.connected_account()
        if account is None:
            raise IbkrModelRunnerError("TWS Paper account disconnected")
        balance = self.client.account_balance()
        position = self._route_position()
        orders = self._route_orders()
        current = self.sessions.active(
            "ibkr_paper", self.profile.account_fingerprint, self.profile.instrument
        )
        changed = current is not None and (
            current["model_id"] != self.policy.model_id
            or current["artifact_sha256"] != self.policy.artifact_sha256
            or current["config_sha256"] != self.manifest["config_sha256"]
        )
        if changed:
            exposures = self.olap.open_exposures()
            if exposures:
                emitted = self.service.request_verified_model_switch_flatten(
                    trace_id=f"ibkr-model-switch-{now.isoformat()}",
                    current_session_id=current["session_id"],
                    next_model_artifact_sha256=self.policy.artifact_sha256,
                    now=now,
                )
                drained = self.consumer.consume_flattens(now=now)
                return {"state": "draining_for_model_switch", "emitted": emitted,
                        "flattens": drained, "position": position}
            if position or orders:
                return {"state": "waiting_for_old_model_effect_resolution",
                        "position": position, "orders": len(orders)}
            self.sessions.end(
                current["session_id"], balance=float(balance["cash"]),
                equity=float(balance["equity"]),
            )
            current = None
        if current is None:
            if position or orders or self.olap.open_exposures():
                return {"state": "blocked_foreign_or_unowned_exposure",
                        "position": position, "orders": len(orders)}
            if selection_error:
                return {"state": "selection_refused", "reason": selection_error,
                        "orders_submitted": 0}
            current = self.sessions.activate(
                venue="ibkr_paper", account=self.profile.account_fingerprint,
                symbol=self.profile.instrument, model_id=self.policy.model_id,
                artifact_sha256=self.policy.artifact_sha256,
                config_sha256=self.manifest["config_sha256"],
                balance=float(balance["cash"]), equity=float(balance["equity"]),
            )
        if position or orders:
            return {"state": "monitoring", "position": position,
                    "orders": len(orders), "model_id": self.policy.model_id,
                    "selection_error": selection_error, "l1": monitoring}
        if selection_error:
            return {"state": "selection_refused", "reason": selection_error,
                    "orders_submitted": 0}

        bars = self.client.historical_closed_bars(
            self.profile.instrument,
            timeframe=self.config["model"]["expected_timeframe"], count=60,
        )
        observation = build_closed_bar_features(bars)
        inference = self.policy.predict(observation)
        self.sessions.record_inference(current["session_id"], inference)
        if inference["action"] == "hold":
            return {"state": "hold", "inference": inference, "l1": monitoring}
        try:
            quote = self.client.current_quote(self.profile.instrument)
        except L1ExecutionError as exc:
            return {
                "state": "waiting_for_quote",
                "reason": str(exc),
                "inference": inference,
                "orders_submitted": 0,
                "l1": monitoring,
            }
        decision_now = _utc_now()
        reference = (float(quote["bid"]) + float(quote["ask"])) / 2.0
        side = 1.0 if inference["action"] == "long" else -1.0
        stop_fraction = float(self.config["strategy"]["stop_fraction"])
        take_fraction = float(self.config["strategy"]["take_profit_fraction"])
        decimals = int(self.config["price_decimals"])
        stop = round(reference * (1.0 - side * stop_fraction), decimals)
        take = round(reference * (1.0 + side * take_fraction), decimals)
        bar_start = datetime.fromisoformat(inference["last_closed_bar"])
        intent = AssetIntent(
            object_id=f"{self.policy.model_id}:{inference['last_closed_bar']}",
            as_of=bar_start + timedelta(hours=4),
            valid_until=bar_start + timedelta(hours=8),
            producer={"name": "prediction_provider.live_linear_policy", "version": "0.1.0"},
            trace_id=f"ibkr-{inference['input_sha256'][:16]}",
            config_hash="sha256:" + self.manifest["config_sha256"],
            cell_id=f"{self.profile.asset_id}@4h:{self.policy.model_id}",
            asset_id=self.profile.asset_id, action="target",
            target_exposure=side,
            confidence=max(inference["probability_up"], 1.0 - inference["probability_up"]),
            strategy_rel_volume=1.0,
            risk_geometry={"mode": "fixed_price", "stop_price": stop,
                           "take_profit_price": take},
            reason_codes=[f"model:{self.policy.model_id}",
                          f"input:{inference['input_sha256']}",
                          "ibkr_paper_infrastructure_canary"],
            artifact_hash="sha256:" + self.policy.artifact_sha256,
        )
        capability = self._capability(quote, decision_now)
        decision = self.service.process_intent(
            intent, capability, equity=float(balance["equity"]),
            reference_price=reference, quote_time=quote["observed_at"],
            instrument=self.profile.instrument, now=decision_now,
        )
        entries = self.consumer.consume_entries(quote={
            "bid": quote["bid"], "ask": quote["ask"], "time": quote["observed_at"],
        }, now=decision_now)
        return {"state": "decided", "inference": inference,
                "decision": decision, "entries": entries, "l1": monitoring}

    def close(self) -> None:
        try:
            close = getattr(self.client, "close", None)
            if callable(close):
                close()
        finally:
            self.olap.close()

    def write_heartbeat(self, payload: dict[str, Any]) -> None:
        write_runner_heartbeat(
            self.config["heartbeat_path"],
            schema="lts.ibkr.model_runner.heartbeat.v1",
            payload=payload,
        )


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "lts.ibkr.model_runner.v1":
        raise IbkrModelRunnerError("unsupported IBKR model runner config")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    runner = IbkrModelRunner(config)
    stopped = threading.Event()

    def stop(*_args):
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopped.is_set():
            try:
                result = runner.tick()
                runner.write_heartbeat(result)
                print(json.dumps(result, sort_keys=True, default=str), flush=True)
            except Exception as exc:
                runner.write_heartbeat({
                    "state": "degraded_error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "orders_submitted": None,
                })
                raise
            if args.once:
                break
            stopped.wait(float(config["loop_seconds"]))
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
