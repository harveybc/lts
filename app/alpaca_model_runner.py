"""Continuous model -> L0 risk -> Alpaca Paper native-bracket vertical."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from prediction_provider_mechanics import LiveLinearPolicy, build_closed_bar_features
from trading_contracts import AssetIntent, BrokerCapabilitySnapshot, InstrumentCapability

from app.alpaca_l1 import AlpacaL1Executor, AlpacaL1Profile, AlpacaPaperTradingClient
from app.demo_execution_service import DemoExecutionConfig, DemoExecutionService, ZeroNetworkSink
from app.ibkr_l1_journal import L1ExecutionOlap


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _sha(path: str | Path) -> str:
    return hashlib.sha256(_path(path).read_bytes()).hexdigest()


class AlpacaModelRunnerError(RuntimeError):
    pass


class ModelSessionStore:
    """Persist model ownership and broker-derived starting/ending balances."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS live_model_sessions (
            session_id TEXT PRIMARY KEY,
            venue TEXT NOT NULL,
            account_fingerprint TEXT NOT NULL,
            symbol TEXT NOT NULL,
            model_id TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            started_at TEXT NOT NULL,
            starting_balance REAL NOT NULL,
            starting_equity REAL NOT NULL,
            ended_at TEXT,
            ending_balance REAL,
            ending_equity REAL,
            state TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_model_per_route
        ON live_model_sessions(venue,account_fingerprint,symbol)
        WHERE state='active';
        CREATE TABLE IF NOT EXISTS live_model_inferences (
            input_sha256 TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            last_closed_bar TEXT NOT NULL,
            action TEXT NOT NULL,
            probability_up REAL NOT NULL,
            output_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        """)

    def active(self, venue: str, account: str, symbol: str) -> Optional[dict[str, Any]]:
        row = self.connection.execute(
            "SELECT session_id,model_id,artifact_sha256,config_sha256,"
            "starting_balance,starting_equity FROM live_model_sessions "
            "WHERE venue=? AND account_fingerprint=? AND symbol=? AND state='active'",
            (venue, account, symbol),
        ).fetchone()
        if row is None:
            return None
        return dict(zip(
            ("session_id", "model_id", "artifact_sha256", "config_sha256",
             "starting_balance", "starting_equity"), row
        ))

    def activate(
        self, *, venue: str, account: str, symbol: str, model_id: str,
        artifact_sha256: str, config_sha256: str, balance: float, equity: float,
    ) -> dict[str, Any]:
        current = self.active(venue, account, symbol)
        if (
            current
            and current["model_id"] == model_id
            and current["artifact_sha256"] == artifact_sha256
            and current["config_sha256"] == config_sha256
        ):
            return current
        if current:
            raise AlpacaModelRunnerError("old model session must be drained before activation")
        session_id = "model-session-" + hashlib.sha256(
            (
                f"{venue}|{account}|{symbol}|{model_id}|"
                f"{artifact_sha256}|{config_sha256}"
            ).encode()
        ).hexdigest()[:24]
        self.connection.execute(
            "INSERT OR IGNORE INTO live_model_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, venue, account, symbol, model_id, artifact_sha256,
             config_sha256, _utc_now().isoformat(), balance, equity,
             None, None, None, "active"),
        )
        self.connection.commit()
        return self.active(venue, account, symbol) or {}

    def end(self, session_id: str, *, balance: float, equity: float) -> None:
        self.connection.execute(
            "UPDATE live_model_sessions SET ended_at=?,ending_balance=?,"
            "ending_equity=?,state='closed' WHERE session_id=? AND state='active'",
            (_utc_now().isoformat(), balance, equity, session_id),
        )
        self.connection.commit()

    def record_inference(self, session_id: str, inference: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO live_model_inferences VALUES (?,?,?,?,?,?,?,?)",
            (inference["input_sha256"], session_id, _utc_now().isoformat(),
             inference["last_closed_bar"], inference["action"],
             inference["probability_up"], inference["output_sha256"],
             json.dumps(inference, sort_keys=True)),
        )
        self.connection.commit()


def _bars(client: AlpacaPaperTradingClient, symbol: str, start: str) -> list[dict[str, Any]]:
    pages, token = [], None
    while True:
        payload = client.stock_bars(
            symbol, timeframe="1Day", start=start, feed="iex", page_token=token
        )
        pages.extend(payload.get("bars", []))
        token = payload.get("next_page_token")
        if not token:
            break
    today = _utc_now().date()
    result = []
    for bar in pages:
        timestamp = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
        if timestamp.date() >= today:
            continue
        result.append({
            "time": timestamp.isoformat(), "open": bar["o"], "high": bar["h"],
            "low": bar["l"], "close": bar["c"], "volume": bar["v"],
            "complete": True,
        })
    return result


class AlpacaModelRunner:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        key = os.environ[config["secrets"]["api_key_env"]]
        secret = os.environ[config["secrets"]["api_secret_env"]]
        self.client = AlpacaPaperTradingClient(key, secret)
        self.profile = AlpacaL1Profile.load(_path(config["profile_file"]))
        config["service"]["database_path"] = str(
            _path(config["service"]["database_path"])
        )
        self.store = L1ExecutionOlap(config["service"]["database_path"])
        self.service = DemoExecutionService(
            DemoExecutionConfig.from_dict(config["service"]), self.store,
            ZeroNetworkSink(),
        )
        self.executor = AlpacaL1Executor(self.store, self.client, self.profile)
        self.sessions = ModelSessionStore(self.store._con)
        manifest_path = _path(config["model"]["manifest_file"])
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact_path = _path(self.manifest["artifact_file"])
        if _sha(artifact_path) != self.manifest["artifact_sha256"]:
            raise AlpacaModelRunnerError("selected model artifact hash mismatch")
        if _sha(self.manifest["config_file"]) != self.manifest["config_sha256"]:
            raise AlpacaModelRunnerError("selected model config hash mismatch")
        self.policy = LiveLinearPolicy.load(artifact_path, self.manifest["artifact_sha256"])

    def _account_facts(self) -> tuple[dict[str, Any], str]:
        account = self.client.account()
        fingerprint = self.client.account_fingerprint(account)
        if fingerprint != self.profile.account_fingerprint:
            raise AlpacaModelRunnerError("Alpaca account fingerprint changed")
        return account, fingerprint

    def _capability(self, fingerprint: str, observed_at: datetime) -> BrokerCapabilitySnapshot:
        asset = self.client.asset(self.profile.symbol)
        return BrokerCapabilitySnapshot(
            object_id=f"alpaca-cap-{observed_at.date().isoformat()}", as_of=observed_at,
            producer={"name": "lts.alpaca_model_runner", "version": "0.1.0"},
            trace_id=f"alpaca-cap-{observed_at.date().isoformat()}",
            venue="alpaca_paper", account_fingerprint=fingerprint,
            environment="paper", capability_evidence="live_observed",
            source_artifact_hash="sha256:" + hashlib.sha256(
                json.dumps(asset, sort_keys=True).encode()
            ).hexdigest(),
            source_observed_at=observed_at,
            instruments=[InstrumentCapability(
                instrument=self.profile.symbol,
                tradeable=bool(asset.get("tradable")),
                shortable=bool(asset.get("shortable")), min_units=1.0,
                unit_step=1.0, price_decimals=2, margin_rate=1.0,
                native_stop_loss=True, native_take_profit=True,
                native_bracket=True,
            )],
        )

    def tick(self, *, allow_execution: bool = True) -> dict[str, Any]:
        now = _utc_now()
        account, fingerprint = self._account_facts()
        open_orders = self.client.open_orders()
        positions = [p for p in self.client.positions() if p.get("symbol") == self.profile.symbol]
        current = self.sessions.active("alpaca_paper", fingerprint, self.profile.symbol)
        selected_changed = current is not None and (
            current["model_id"] != self.policy.model_id
            or current["artifact_sha256"] != self.policy.artifact_sha256
            or current["config_sha256"] != self.manifest["config_sha256"]
        )
        if selected_changed:
            for order in open_orders:
                if order.get("symbol") == self.profile.symbol:
                    self.client.cancel_order(str(order["id"]))
            if positions:
                self.client.close_position(self.profile.symbol)
            if open_orders or positions:
                return {"state": "draining_for_model_switch", "model_id": current["model_id"]}
            self.sessions.end(
                current["session_id"], balance=float(account["cash"]),
                equity=float(account["equity"]),
            )
            current = None
        if current is None:
            if open_orders or positions:
                return {"state": "blocked_foreign_exposure"}
            current = self.sessions.activate(
                venue="alpaca_paper", account=fingerprint, symbol=self.profile.symbol,
                model_id=self.policy.model_id,
                artifact_sha256=self.policy.artifact_sha256,
                config_sha256=self.manifest["config_sha256"],
                balance=float(account["cash"]), equity=float(account["equity"]),
            )

        for effect in self.store.nonterminal_effects():
            if effect["kind"] == "alpaca_bracket_entry" and effect["state"] == "acknowledged":
                self.executor.monitor(effect["effect_id"])
        if open_orders or positions:
            return {"state": "monitoring", "model_id": self.policy.model_id,
                    "orders": len(open_orders), "positions": len(positions)}

        bars = _bars(self.client, self.profile.symbol, self.config["data"]["start"])
        observation = build_closed_bar_features(bars[-60:])
        inference = self.policy.predict(observation)
        self.sessions.record_inference(current["session_id"], inference)
        if not allow_execution:
            return {"state": "inference_only", "inference": inference,
                    "orders_submitted": 0}
        if inference["action"] == "hold":
            return {"state": "hold", "inference": inference}
        quote = self.client.latest_stock_quote(self.profile.symbol)
        bid, ask = float(quote["bp"]), float(quote["ap"])
        reference = (bid + ask) / 2.0
        side = 1.0 if inference["action"] == "long" else -1.0
        stop_fraction = float(self.config["strategy"]["stop_fraction"])
        take_fraction = float(self.config["strategy"]["take_profit_fraction"])
        stop = reference * (1.0 - side * stop_fraction)
        take = reference * (1.0 + side * take_fraction)
        bar_start = datetime.fromisoformat(inference["last_closed_bar"])
        decided_at = bar_start + timedelta(hours=16)
        intent = AssetIntent(
            object_id=f"{self.policy.model_id}:{inference['last_closed_bar']}",
            as_of=decided_at, valid_until=decided_at + timedelta(days=7),
            producer={"name": "prediction_provider.live_linear_policy", "version": "0.1.0"},
            trace_id=f"alpaca-{inference['input_sha256'][:16]}",
            config_hash="sha256:" + self.manifest["config_sha256"],
            cell_id=f"equity:SPY@1d:{self.policy.model_id}", asset_id=self.policy.asset_id,
            action="target", target_exposure=side, confidence=max(
                inference["probability_up"], 1.0 - inference["probability_up"]
            ),
            strategy_rel_volume=1.0,
            risk_geometry={"mode": "fixed_price", "stop_price": round(stop, 2),
                           "take_profit_price": round(take, 2)},
            reason_codes=[f"model:{self.policy.model_id}",
                          f"input:{inference['input_sha256']}",
                          "paper_infrastructure_canary"],
            artifact_hash="sha256:" + self.policy.artifact_sha256,
        )
        capability = self._capability(fingerprint, now)
        decision = self.service.process_intent(
            intent, capability, equity=float(account["equity"]),
            reference_price=reference,
            quote_time=datetime.fromisoformat(str(quote["t"]).replace("Z", "+00:00")),
            instrument=self.profile.symbol, now=now,
        )
        executions = self.executor.consume_pending()
        state = "decided" if executions else (
            "replayed_signal" if decision.get("replayed") else "no_execution"
        )
        return {"state": state, "inference": inference,
                "decision": decision, "executions": executions}

    def close(self) -> None:
        self.store.close()


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "lts.alpaca.model_runner.v1":
        raise AlpacaModelRunnerError("unsupported Alpaca model runner config")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--inference-only", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    runner = AlpacaModelRunner(config)
    stopped = False
    def stop(*_args):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopped:
            print(json.dumps(
                runner.tick(allow_execution=not args.inference_only),
                sort_keys=True, default=str,
            ), flush=True)
            if args.once:
                break
            time.sleep(float(config["loop_seconds"]))
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
