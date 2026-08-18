"""Continuous model -> L0 risk -> Alpaca Paper native-bracket vertical."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from prediction_provider_mechanics import build_closed_bar_features
from trading_contracts import AssetIntent, BrokerCapabilitySnapshot, InstrumentCapability

from app.alpaca_l1 import AlpacaL1Executor, AlpacaL1Profile, AlpacaPaperTradingClient
from app.demo_execution_service import DemoExecutionConfig, DemoExecutionService, ZeroNetworkSink
from app.ibkr_l1_journal import L1ExecutionOlap
from app.live_model_selection import LiveModelSelectionError, SelectedLinearPolicy
from app.model_position_control import decide_position_control, model_close_consumed_bar
from app.model_runner_heartbeat import (
    linear_model_identity,
    write_runner_heartbeat,
)
from app.runner_retry_taxonomy import classify_runner_exception


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


class AlpacaModelRunnerError(RuntimeError):
    pass


def signed_position_quantity(position: Mapping[str, Any]) -> float:
    """Return one signed quantity from Alpaca's redundant qty/side facts."""
    quantity = float(position.get("qty", 0.0))
    if quantity < 0.0:
        return quantity
    if str(position.get("side", "long")).lower() == "short":
        return -quantity
    return quantity


def equity_model_close_allowed(clock: Mapping[str, Any]) -> bool:
    """Market closes are executable only while the regular session is open."""
    return bool(clock.get("is_open"))


def l0_retry_suffix(store, model_id: str, last_closed_bar: str,
                    reconciliations: list) -> str:
    """Exactly-once retry gate for L0-reconciliation repairs.

    A signal may be retried only when it never reached the broker: if ANY
    effect (any state, terminal included) exists for this bar's identity,
    the signal is satisfied and reconciliation must not mint a new one.
    The retry identity is deterministic per bar, so repeated repairs
    collapse into one replayed decision. A per-repair hashed suffix let
    every reconciliation mint a fresh idempotency key, producing four
    duplicate SPY lifecycles from one signal on 2026-08-04.
    """
    if not reconciliations:
        return ""
    if store.effect_exists_with_key_prefix(f"{model_id}:{last_closed_bar}"):
        return ""
    return ":l0-retry-1"


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
        score = inference.get("probability_up", inference.get("raw_action"))
        if score is None:
            raise AlpacaModelRunnerError("model inference lacks a numeric score")
        output_sha256 = inference.get("output_sha256")
        if not output_sha256:
            output_sha256 = hashlib.sha256(json.dumps(
                {"action": inference.get("action"), "score": score},
                sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
        self.connection.execute(
            "INSERT OR IGNORE INTO live_model_inferences VALUES (?,?,?,?,?,?,?,?)",
            (inference["input_sha256"], session_id, _utc_now().isoformat(),
             inference["last_closed_bar"], inference["action"],
             float(score), output_sha256,
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
        Path(config["service"]["database_path"]).parent.mkdir(
            parents=True, exist_ok=True
        )
        self.store = L1ExecutionOlap(config["service"]["database_path"])
        self.service = DemoExecutionService(
            DemoExecutionConfig.from_dict(config["service"]), self.store,
            ZeroNetworkSink(),
        )
        self.executor = AlpacaL1Executor(
            self.store, self.client, self.profile, self.service
        )
        self.sessions = ModelSessionStore(self.store._con)
        self.selector = SelectedLinearPolicy(
            manifest_file=config["model"]["manifest_file"],
            expected_asset_id=config["model"]["expected_asset_id"],
            expected_timeframe=config["model"]["expected_timeframe"],
            execution_tier=config["model"]["execution_tier"],
        )
        self.manifest = self.selector.manifest
        self.policy = self.selector.policy

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
        selection_error = None
        try:
            if self.selector.refresh():
                self.manifest = self.selector.manifest
                self.policy = self.selector.policy
        except LiveModelSelectionError as exc:
            selection_error = str(exc)
        account, fingerprint = self._account_facts()
        open_orders = self.client.open_orders()
        positions = [p for p in self.client.positions() if p.get("symbol") == self.profile.symbol]
        reconciliations = self.executor.reconcile_terminal_effects()
        orphan_releases = self.executor.reconcile_orphan_reservations(
            route_flat=not open_orders and not positions)
        current = self.sessions.active("alpaca_paper", fingerprint, self.profile.symbol)
        selected_changed = current is not None and (
            current["model_id"] != self.policy.model_id
            or current["artifact_sha256"] != self.policy.artifact_sha256
            or current["config_sha256"] != self.manifest["config_sha256"]
        )
        if selected_changed:
            # Order C3: succession drains through the journaled lifecycle
            # (owned effects only) — the raw cancel/close bypass is gone.
            self.executor.drain_for_succession(
                reason=f"model_switch:{self.policy.model_id}")
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
            if selection_error:
                return {"state": "selection_refused", "reason": selection_error,
                        "orders_submitted": 0}
            current = self.sessions.activate(
                venue="alpaca_paper", account=fingerprint, symbol=self.profile.symbol,
                model_id=self.policy.model_id,
                artifact_sha256=self.policy.artifact_sha256,
                config_sha256=self.manifest["config_sha256"],
                balance=float(account["cash"]), equity=float(account["equity"]),
            )

        for effect in self.store.nonterminal_effects():
            if (
                effect["kind"] == "alpaca_bracket_entry"
                and effect["state"] in {"acknowledged", "recovering"}
            ):
                self.executor.monitor(effect["effect_id"])
        if selection_error:
            if open_orders or positions:
                return {"state": "monitoring", "model_id": self.policy.model_id,
                        "orders": len(open_orders), "positions": len(positions),
                        "selection_error": selection_error}
            return {"state": "selection_refused", "reason": selection_error,
                    "orders_submitted": 0}

        bars = _bars(self.client, self.profile.symbol, self.config["data"]["start"])
        observation = build_closed_bar_features(bars[-60:])
        inference = self.policy.predict(observation)
        self.sessions.record_inference(current["session_id"], inference)
        if not allow_execution:
            self._record_due_bar(inference, outcome="inference_only",
                                 reason="execution_disabled")
            return {"state": "inference_only", "inference": inference,
                    "orders_submitted": 0}
        exposure = sum(signed_position_quantity(position) for position in positions)
        control = decide_position_control(
            inference["action"], current_exposure=exposure,
            pending_entry=bool(open_orders and not positions),
        )
        if control.disposition == "close" and (open_orders or positions):
            clock = self.client.clock()
            if not equity_model_close_allowed(clock):
                self._record_due_bar(
                    inference,
                    outcome="model_close_deferred",
                    reason="equity_market_closed_protection_preserved",
                )
                return {
                    "state": "model_close_deferred_market_closed",
                    "inference": inference,
                    "control": control.reason,
                    "orders": len(open_orders),
                    "positions": len(positions),
                    "protection_preserved": True,
                }
            drained = self.executor.drain_for_model_signal(control.reason)
            self._record_due_bar(
                inference, outcome="model_close_requested", reason=control.reason
            )
            return {
                "state": "closing_on_model_signal", "inference": inference,
                "control": control.reason, "drained": drained,
                "orders": len(open_orders), "positions": len(positions),
            }
        if open_orders or positions:
            self._record_due_bar(
                inference, outcome="monitoring_open_exposure",
                reason=control.reason,
            )
            return {"state": "monitoring", "model_id": self.policy.model_id,
                    "orders": len(open_orders), "positions": len(positions),
                    "inference": inference, "control": control.reason,
                    "selection_error": selection_error}
        if control.disposition == "hold":
            self._record_due_bar(inference, outcome="hold",
                                 reason=control.reason)
            return {"state": "hold", "inference": inference}
        if model_close_consumed_bar(
            self.store.due_bar_decisions(
                venue="alpaca_paper", since=inference["last_closed_bar"]
            ),
            venue="alpaca_paper",
            model_id=self.policy.model_id,
            timeframe=self.policy.timeframe,
            bar_close=inference["last_closed_bar"],
        ):
            return {
                "state": "model_close_bar_consumed",
                "inference": inference,
                "orders_submitted": 0,
            }
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
        retry_suffix = l0_retry_suffix(
            self.store, self.policy.model_id, inference["last_closed_bar"],
            reconciliations,
        )
        intent = AssetIntent(
            object_id=(
                f"{self.policy.model_id}:{inference['last_closed_bar']}"
                f"{retry_suffix}"
            ),
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
                          "paper_infrastructure_canary"] + (
                              ["retry_after_l0_terminal_reconciliation"]
                              if retry_suffix else []
                          ),
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
        self._record_due_bar(
            inference, outcome=str(decision.get("outcome")),
            reason=decision.get("reason"),
            quote={"bid": bid, "ask": ask},
            risk={"stop_price": round(stop, 2),
                  "take_profit_price": round(take, 2),
                  "target_exposure": side},
            effect_id=next(
                (item.get("effect_id") for item in executions
                 if isinstance(item, dict) and item.get("effect_id")),
                None),
        )
        return {"state": state, "inference": inference,
                "decision": decision, "executions": executions}

    def _record_due_bar(self, inference, *, outcome, reason=None,
                        quote=None, risk=None, effect_id=None):
        """Order C1: one normalized decision fact per due closed bar —
        HOLDs and refusals included. Never raises into the tick."""
        if not inference or not inference.get("last_closed_bar"):
            return
        try:
            self.store.record_due_bar_decision({
                "venue": "alpaca_paper",
                "account_fingerprint": self.profile.account_fingerprint,
                "asset_id": self.policy.asset_id,
                "instrument": self.profile.symbol,
                "timeframe": self.config["model"]["expected_timeframe"],
                "bar_close": inference["last_closed_bar"],
                "decided_at": _utc_now().isoformat(),
                "feature_cutoff": inference["last_closed_bar"],
                "input_sha256": inference["input_sha256"],
                "config_sha256": self.manifest["config_sha256"],
                "model_id": self.policy.model_id,
                "artifact_sha256": self.policy.artifact_sha256,
                "manifest_sha256": self.manifest.get("manifest_sha256"),
                "action": inference["action"],
                "score": inference.get("probability_up"),
                "outcome": outcome, "reason": reason,
                "risk_envelope": risk, "quote": quote,
                "decision_id":
                    f"{self.policy.model_id}:{inference['last_closed_bar']}",
                "effect_or_command_id": effect_id,
            })
        except Exception as exc:
            print(json.dumps({"due_bar_fact_error": str(exc)[:160]}),
                  flush=True)

    def close(self) -> None:
        self.store.close()

    def write_heartbeat(self, payload: dict[str, Any]) -> None:
        runtime = {
            **payload,
            **linear_model_identity(self.selector),
            "venue": "alpaca_paper",
            "environment": "paper",
            "read_only": False,
            "account_binding_verified": True,
            "account_fingerprint": self.profile.account_fingerprint,
            "instrument": self.profile.symbol,
        }
        write_runner_heartbeat(
            self.config.get(
                "heartbeat_path", "~/.local/state/lts/alpaca-model-runner-heartbeat.json"
            ),
            schema="lts.alpaca.model_runner.heartbeat.v1",
            payload=runtime,
        )


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
    stopped = threading.Event()
    def stop(*_args):
        stopped.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopped.is_set():
            try:
                result = runner.tick(allow_execution=not args.inference_only)
                runner.write_heartbeat(result)
                print(json.dumps(result, sort_keys=True, default=str), flush=True)
            except Exception as exc:
                # 103: transient connection/session failures keep the
                # normal cadence; anything else is fatal — an advancing
                # degraded heartbeat on the slow fatal cadence so the
                # condition pages immediately and is never mislabeled as
                # connectivity (091 keeps the service alive either way;
                # ledger idempotency makes the next tick safe).
                kind = classify_runner_exception(exc)
                runner.write_heartbeat({
                    "state": "degraded_error",
                    "phase": "connect" if kind == "transient" else "fatal",
                    "error": f"{type(exc).__name__}: {exc}",
                    "orders_submitted": None,
                })
                if args.once:
                    raise
                if kind == "fatal":
                    stopped.wait(float(
                        config.get("fatal_retry_seconds", 3600.0)))
                    continue
            if args.once:
                break
            stopped.wait(float(config["loop_seconds"]))
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
