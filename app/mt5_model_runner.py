"""Continuous ETH model -> L0 risk -> MT5 Demo command-outbox runner."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from prediction_provider_mechanics import LiveLinearPolicy, build_closed_bar_features
from trading_contracts import AssetIntent, BrokerCapabilitySnapshot, InstrumentCapability

from app.alpaca_model_runner import ModelSessionStore
from app.demo_execution_service import DemoExecutionConfig, DemoExecutionService, ZeroNetworkSink
from app.ibkr_l1_journal import L1ExecutionOlap
from app.mt5_execution_bridge import Mt5ExecutionConfig, Mt5ExecutionStore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _sha(path: str | Path) -> str:
    return hashlib.sha256(_path(path).read_bytes()).hexdigest()


class Mt5ModelRunnerError(RuntimeError):
    pass


def close_idempotency_key(
    *, session_id: str, reason: str, snapshot_received_at: str, last_bar: str,
) -> tuple[str, str]:
    identity = json.dumps(
        {
            "session_id": session_id,
            "reason": reason,
            "snapshot_received_at": snapshot_received_at,
            "last_bar": last_bar,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"close:{digest}", digest


class Mt5ModelRunner:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.bridge_config = Mt5ExecutionConfig.load(_path(config["bridge_config_file"]))
        self.bridge_store = Mt5ExecutionStore(self.bridge_config.database_path)
        config["service"]["database_path"] = str(self.bridge_config.database_path)
        self.l0 = L1ExecutionOlap(self.bridge_config.database_path)
        self.service = DemoExecutionService(
            DemoExecutionConfig.from_dict(config["service"]), self.l0,
            ZeroNetworkSink(),
        )
        self.sessions = ModelSessionStore(self.l0._con)
        manifest_path = _path(config["model"]["manifest_file"])
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact_path = _path(self.manifest["artifact_file"])
        if _sha(artifact_path) != self.manifest["artifact_sha256"]:
            raise Mt5ModelRunnerError("selected MT5 model artifact hash mismatch")
        if _sha(self.manifest["config_file"]) != self.manifest["config_sha256"]:
            raise Mt5ModelRunnerError("selected MT5 model config hash mismatch")
        self.policy = LiveLinearPolicy.load(
            artifact_path, self.manifest["artifact_sha256"]
        )

    def _latest_snapshot(self) -> Optional[dict[str, Any]]:
        row = self.bridge_store.connection.execute(
            "SELECT payload_json,received_at FROM account_snapshots "
            "WHERE account_fingerprint=? ORDER BY id DESC LIMIT 1",
            (self.bridge_config.account_fingerprint,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        payload["_received_at"] = row[1]
        return payload

    def _symbol_fact(self, snapshot: dict[str, Any]) -> Optional[dict[str, Any]]:
        symbol = self.config["route"]["symbol"]
        return next((item for item in snapshot.get("symbols", [])
                     if item.get("symbol") == symbol), None)

    def _bars(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        symbol = self.config["route"]["symbol"]
        timeframe = self.policy.timeframe
        bars = [
            {
                "time": item["time"], "open": item["open"],
                "high": item["high"], "low": item["low"],
                "close": item["close"], "volume": item["volume"],
                "complete": True,
            }
            for item in snapshot.get("bars", [])
            if item.get("symbol") == symbol and item.get("timeframe") == timeframe
        ]
        return sorted(bars, key=lambda item: item["time"])

    def _capability(
        self, snapshot: dict[str, Any], symbol_fact: dict[str, Any], now: datetime
    ) -> BrokerCapabilitySnapshot:
        symbol = self.config["route"]["symbol"]
        source_hash = hashlib.sha256(
            json.dumps(symbol_fact, sort_keys=True).encode()
        ).hexdigest()
        return BrokerCapabilitySnapshot(
            object_id=f"mt5-cap-{snapshot['_received_at']}", as_of=now,
            producer={"name": "lts.mt5_model_runner", "version": "0.1.0"},
            trace_id=f"mt5-cap-{source_hash[:16]}", venue="mt5_demo",
            account_fingerprint=self.bridge_config.account_fingerprint,
            environment="demo", capability_evidence="live_observed",
            source_artifact_hash="sha256:" + source_hash,
            source_observed_at=datetime.fromisoformat(snapshot["_received_at"]),
            instruments=[InstrumentCapability(
                instrument=symbol, tradeable=int(symbol_fact["trade_mode"]) != 0,
                shortable=True, min_units=float(symbol_fact["volume_min"]),
                unit_step=float(symbol_fact["volume_step"]), price_decimals=max(
                    0, len(str(symbol_fact["point"]).split(".")[-1].rstrip("0"))
                ), margin_rate=1.0, native_stop_loss=True,
                native_take_profit=True, native_bracket=True,
            )],
        )

    def _queue_close(
        self,
        *,
        snapshot: dict[str, Any],
        current_session: dict[str, Any],
        last_bar: str,
        reason: str,
    ) -> dict[str, Any]:
        idempotency_key, input_sha256 = close_idempotency_key(
            session_id=current_session["session_id"],
            reason=reason,
            snapshot_received_at=snapshot["_received_at"],
            last_bar=last_bar,
        )
        return self.bridge_store.enqueue(
            config=self.bridge_config,
            idempotency_key=idempotency_key,
            action="close", symbol=self.config["route"]["symbol"],
            volume=0, stop_loss=0, take_profit=0,
            model_id=self.policy.model_id,
            artifact_sha256=self.policy.artifact_sha256,
            config_sha256=self.manifest["config_sha256"],
            input_sha256=input_sha256,
        )

    def tick(self) -> dict[str, Any]:
        now = _utc_now()
        snapshot = self._latest_snapshot()
        if snapshot is None:
            return {"state": "waiting_for_snapshot"}
        received = datetime.fromisoformat(snapshot["_received_at"])
        if (now - received).total_seconds() > self.config["snapshot_max_age_seconds"]:
            return {"state": "snapshot_stale", "received_at": snapshot["_received_at"]}
        symbol = self.config["route"]["symbol"]
        symbol_fact = self._symbol_fact(snapshot)
        if symbol_fact is None:
            return {"state": "symbol_unavailable", "symbol": symbol}
        bars = self._bars(snapshot)
        if len(bars) < 51:
            return {"state": "waiting_for_closed_bars", "bars": len(bars)}
        positions = [item for item in snapshot.get("positions", [])
                     if item.get("symbol") == symbol]
        orders = [item for item in snapshot.get("orders", [])
                  if item.get("symbol") == symbol]
        current = self.sessions.active(
            "mt5_demo", self.bridge_config.account_fingerprint, symbol
        )
        changed = current is not None and (
            current["model_id"] != self.policy.model_id
            or current["artifact_sha256"] != self.policy.artifact_sha256
            or current["config_sha256"] != self.manifest["config_sha256"]
        )
        if changed:
            if positions or orders:
                command = self._queue_close(
                    snapshot=snapshot,
                    current_session=current,
                    last_bar=bars[-1]["time"],
                    reason="model_switch",
                )
                return {"state": "draining_for_model_switch",
                        "command_id": command["command_id"]}
            self.sessions.end(
                current["session_id"], balance=float(snapshot["balance"]),
                equity=float(snapshot["equity"]),
            )
            current = None
        if current is None:
            if positions or orders:
                return {"state": "blocked_foreign_exposure"}
            current = self.sessions.activate(
                venue="mt5_demo", account=self.bridge_config.account_fingerprint,
                symbol=symbol, model_id=self.policy.model_id,
                artifact_sha256=self.policy.artifact_sha256,
                config_sha256=self.manifest["config_sha256"],
                balance=float(snapshot["balance"]), equity=float(snapshot["equity"]),
            )
        if positions:
            unprotected = [item for item in positions
                           if float(item.get("stop_loss", 0)) <= 0
                           or float(item.get("take_profit", 0)) <= 0]
            if unprotected:
                command = self._queue_close(
                    snapshot=snapshot,
                    current_session=current,
                    last_bar=bars[-1]["time"],
                    reason="unprotected_position",
                )
                return {"state": "closing_unprotected_position",
                        "command_id": command["command_id"]}
            return {"state": "monitoring", "positions": len(positions),
                    "orders": len(orders), "model_id": self.policy.model_id}
        if orders:
            return {"state": "monitoring_pending_order", "orders": len(orders)}

        observation = build_closed_bar_features(bars[-60:])
        inference = self.policy.predict(observation)
        self.sessions.record_inference(current["session_id"], inference)
        if inference["action"] == "hold":
            return {"state": "hold", "inference": inference}
        bid, ask = float(symbol_fact["bid"]), float(symbol_fact["ask"])
        reference = (bid + ask) / 2.0
        side = 1.0 if inference["action"] == "long" else -1.0
        stop_fraction = float(self.config["strategy"]["stop_fraction"])
        take_fraction = float(self.config["strategy"]["take_profit_fraction"])
        digits = int(round(-__import__("math").log10(float(symbol_fact["point"]))))
        stop = round(reference * (1.0 - side * stop_fraction), digits)
        take = round(reference * (1.0 + side * take_fraction), digits)
        last_bar = datetime.fromisoformat(bars[-1]["time"].replace("Z", "+00:00"))
        intent = AssetIntent(
            object_id=f"{self.policy.model_id}:{bars[-1]['time']}",
            as_of=last_bar + timedelta(hours=4),
            valid_until=last_bar + timedelta(hours=8),
            producer={"name": "prediction_provider.live_linear_policy",
                      "version": "0.1.0"},
            trace_id=f"mt5-{inference['input_sha256'][:16]}",
            config_hash="sha256:" + self.manifest["config_sha256"],
            cell_id=f"crypto:ETHUSD@4h:{self.policy.model_id}",
            asset_id=self.policy.asset_id, action="target",
            target_exposure=side, confidence=max(
                inference["probability_up"], 1.0 - inference["probability_up"]
            ), strategy_rel_volume=1.0,
            risk_geometry={"mode": "fixed_price", "stop_price": stop,
                           "take_profit_price": take},
            reason_codes=[f"model:{self.policy.model_id}",
                          f"input:{inference['input_sha256']}",
                          "mt5_demo_infrastructure_canary"],
            artifact_hash="sha256:" + self.policy.artifact_sha256,
        )
        capability = self._capability(snapshot, symbol_fact, now)
        decision = self.service.process_intent(
            intent, capability, equity=float(snapshot["equity"]),
            reference_price=reference,
            quote_time=datetime.fromisoformat(symbol_fact["observed_at"]),
            instrument=symbol, now=now,
        )
        if decision["outcome"] != "would_be_order":
            return {"state": "l0_refused", "decision": decision,
                    "inference": inference}
        pending = self.l0.l1_pending_decisions("would_be_order")
        accepted = next((row for row in pending
                         if row["idempotency_key"] == decision["payload"]["idempotency_key"]), None)
        if accepted is None:
            return {"state": "replayed_signal", "decision": decision}
        from trading_contracts import OrderIntentV2
        order_intent = OrderIntentV2.model_validate_json(accepted["intent_json"])
        command = self.bridge_store.enqueue(
            config=self.bridge_config,
            idempotency_key=order_intent.idempotency_key,
            action="open_long" if order_intent.delta_units > 0 else "open_short",
            symbol=symbol, volume=abs(float(order_intent.delta_units)),
            stop_loss=float(order_intent.protection.stop_loss_price),
            take_profit=float(order_intent.protection.take_profit_price),
            model_id=self.policy.model_id,
            artifact_sha256=self.policy.artifact_sha256,
            config_sha256=self.manifest["config_sha256"],
            input_sha256=inference["input_sha256"],
        )
        return {"state": "command_queued", "command_id": command["command_id"],
                "inference": inference, "decision": decision}

    def close(self) -> None:
        self.l0.close()
        self.bridge_store.close()


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "lts.mt5.model_runner.v1":
        raise Mt5ModelRunnerError("unsupported MT5 model runner config")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    runner = Mt5ModelRunner(load_config(args.config))
    stopped = False

    def stop(*_args):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopped:
            print(json.dumps(runner.tick(), sort_keys=True, default=str), flush=True)
            if args.once:
                break
            time.sleep(float(runner.config["loop_seconds"]))
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
