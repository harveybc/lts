"""Continuous L0 demo-execution runner (finding 048; doc 29 §7).

One deterministic loop: read the freshest live demo quote observation from
the Alpaca Paper lab OLAP (read-only), derive the bar-clock observation,
ask the provider-owned mechanics policy for a labeled ``AssetIntent``, and
hand it to the LTS demo execution service — which plans a protected
would-be order through the zero-network sink and persists every fact.

Structural guarantees:

- read-only against every live source (SQLite opened with ``mode=ro``);
- the only "order" surface is the zero-network sink: no socket, no
  credential, no venue endpoint exists anywhere in this process;
- the IBKR capability snapshot is a ``synthetic_fixture`` with a synthetic
  fingerprint (ruling R3): it drives mechanics only and is mechanically
  excluded from readiness claims;
- one decision per (cell, bar): restarts and overlapping runs replay the
  recorded decision instead of duplicating it;
- every loop writes a heartbeat fact so the watchdog/status contract can
  prove the process is advancing, not merely alive.

Usage:

    python -m app.demo_execution_runner --config <resolved.json> [--once]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from trading_contracts import BrokerCapabilitySnapshot, InstrumentCapability

from app.demo_execution_service import (
    DemoExecutionConfig,
    DemoExecutionError,
    DemoExecutionOlap,
    DemoExecutionService,
    ZeroNetworkSink,
)
from prediction_provider_mechanics import MechanicsPolicy, MechanicsPolicyConfig


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunnerError(RuntimeError):
    pass


class QuoteSource:
    """Read-only freshest-quote reader over a lab OLAP."""

    def __init__(self, database_path: str, symbol: str, max_age_seconds: float):
        self.database_path = database_path
        self.symbol = symbol
        self.max_age_seconds = max_age_seconds

    def latest(self, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
        try:
            con = sqlite3.connect(
                f"file:{self.database_path}?mode=ro", uri=True, timeout=5.0
            )
            row = con.execute(
                "SELECT mid, bid, ask, observed_at, quote_json "
                "FROM quote_observations WHERE symbol=? "
                "ORDER BY observed_at DESC LIMIT 1",
                (self.symbol,),
            ).fetchone()
            con.close()
        except sqlite3.Error:
            return None
        if row is None or row[0] is None:
            return None
        observed_at = datetime.fromisoformat(str(row[3]))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        age = ((now or _utc_now()) - observed_at).total_seconds()
        return {
            "mid": float(row[0]),
            "bid": row[1],
            "ask": row[2],
            "observed_at": observed_at,
            "age_seconds": age,
            "stale": age > self.max_age_seconds,
            # Finding 058: a quote from the future is corrupt evidence, not
            # fresh evidence; 30 s of clock skew is the only tolerance.
            "future": age < -30.0,
            "quote_hash": "sha256:"
            + hashlib.sha256(str(row[4]).encode()).hexdigest(),
        }


def load_runner_config(path: str | Path) -> dict[str, Any]:
    with open(path) as handle:
        config = json.load(handle)
    required = [
        "service", "policy", "quote_source", "capability_fixture",
        "bar_seconds", "loop_seconds", "heartbeat_path",
    ]
    missing = [key for key in required if key not in config]
    if missing:
        raise RunnerError(f"runner config missing keys: {missing}")
    return config


def build_capability(fixture: dict[str, Any], now: datetime) -> BrokerCapabilitySnapshot:
    """Materialize the synthetic IBKR capability fixture (ruling R3)."""
    if fixture.get("capability_evidence") != "synthetic_fixture":
        raise RunnerError(
            "L0 runner accepts only synthetic_fixture capability evidence; "
            "live_observed claims require the real venue observer"
        )
    return BrokerCapabilitySnapshot(
        object_id=f"cap-l0-{now.date().isoformat()}",
        as_of=now,
        producer={"name": "lts.demo_execution_runner", "version": "0.1.0"},
        trace_id="l0-capability-fixture",
        venue=fixture["venue"],
        account_fingerprint=fixture["account_fingerprint"],
        environment=fixture["environment"],
        capability_evidence="synthetic_fixture",
        source_artifact_hash=fixture["source_artifact_hash"],
        source_observed_at=datetime.fromisoformat(fixture["source_observed_at"]),
        instruments=[
            InstrumentCapability(**entry) for entry in fixture["instruments"]
        ],
    )


def bar_time(now: datetime, bar_seconds: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % bar_seconds, tz=timezone.utc)


class SyntheticLifecycleDriver:
    """Finding 056: L0 has no broker, so nothing resolves a would-be order.

    This driver generates a deterministic, clearly-labeled synthetic
    lifecycle for every pending would-be order — accept, then fill with
    confirmed protection, then position close after a hold period — so the
    running vertical continuously exercises reservations, exposures,
    conservation and release instead of saturating on one pending entry.
    Zero network; every generated report says so in its producer identity.
    """

    PRODUCER = {"name": "lts.l0_synthetic_lifecycle_driver", "version": "0.1.0"}

    def __init__(self, service: DemoExecutionService, hold_bars: int,
                 bar_seconds: int):
        self.service = service
        self.hold_bars = hold_bars
        self.bar_seconds = bar_seconds

    def _pending_orders(self) -> list[dict[str, Any]]:
        rows = self.service.olap._con.execute(
            "SELECT idempotency_key, intent_json FROM decisions "
            "WHERE outcome='would_be_order'"
        ).fetchall()
        orders = []
        for idem, intent_json in rows:
            intent = json.loads(intent_json)
            orders.append({
                "order_intent_id": intent["object_id"],
                "delta_units": intent["delta_units"],
                "protection": intent["protection"],
                "reservation_id": intent["risk"]["reservation_id"],
                "as_of": intent["as_of"],
            })
        return orders

    def advance(self, now: datetime) -> list[dict[str, Any]]:
        advanced = []
        for order in self._pending_orders():
            state = self.service.olap.last_state(order["order_intent_id"])
            units = abs(order["delta_units"])
            legs = [
                {"leg": "stop_loss", "broker_confirmed": True,
                 "covered_units": units},
                {"leg": "take_profit", "broker_confirmed": True,
                 "covered_units": units},
            ]
            try:
                if state == "requested":
                    self._apply(order, "accepted", "requested", 0.0, [], now)
                    advanced.append({"order": order["order_intent_id"],
                                     "to": "accepted"})
                elif state == "accepted":
                    self._apply(order, "filled", "accepted", units, legs, now)
                    advanced.append({"order": order["order_intent_id"],
                                     "to": "filled"})
                elif state == "filled":
                    exposure_state = self.service.olap.exposure_state(
                        f"exp-{order['order_intent_id']}"
                    )
                    if exposure_state != "open":
                        continue  # already closed; a settled past stays quiet
                    opened = datetime.fromisoformat(order["as_of"])
                    held = (now - opened).total_seconds()
                    if held >= self.hold_bars * self.bar_seconds:
                        self.service.apply_position_close(
                            order["order_intent_id"]
                        )
                        advanced.append({"order": order["order_intent_id"],
                                         "to": "position_closed"})
            except DemoExecutionError as error:
                advanced.append({"order": order["order_intent_id"],
                                 "error": str(error)})
        return advanced

    def _apply(self, order, state, previous, filled, legs, now) -> None:
        from trading_contracts import ExecutionReportV2, ProtectionLegState

        self.service.apply_execution_event(ExecutionReportV2(
            object_id=f"er-syn-{order['order_intent_id']}-{state}",
            as_of=now,
            producer=self.PRODUCER,
            trace_id="l0-synthetic-lifecycle",
            order_intent_id=order["order_intent_id"],
            attempt_id=f"attempt-{order['reservation_id']}",
            bracket_role="parent",
            state=state,
            previous_state=previous,
            requested_units=order["delta_units"],
            filled_units=filled,
            protection_legs=[ProtectionLegState(**leg) for leg in legs],
        ))


class DemoExecutionRunner:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.service = DemoExecutionService(
            DemoExecutionConfig.from_dict(config["service"]),
            DemoExecutionOlap(config["service"]["database_path"]),
            ZeroNetworkSink(),
        )
        self.policy = MechanicsPolicy(
            MechanicsPolicyConfig.from_dict(config["policy"])
        )
        self.quotes = QuoteSource(
            config["quote_source"]["database_path"],
            config["quote_source"]["symbol"],
            float(config["quote_source"]["max_age_seconds"]),
        )
        self.instrument = config["quote_source"]["instrument"]
        self.equity = float(config["service"].get("synthetic_equity", 100_000.0))
        driver_config = config.get("lifecycle_driver", {})
        self.driver = (
            SyntheticLifecycleDriver(
                self.service,
                int(driver_config.get("hold_bars", 2)),
                int(config["bar_seconds"]),
            )
            if driver_config.get("enabled", False)
            else None
        )
        self._stop = False
        # Finding 055: journaled command effects resume at startup.
        self.resumed_effects = self.service.resume_pending_effects()

    def request_stop(self, *_args) -> None:
        self._stop = True

    def _alerts(self, quote, outcome) -> list[str]:
        """Automatic L0 health alerts (finding 056 acceptance demand)."""
        alerts = []
        if quote is None:
            alerts.append("quote_source_unavailable")
        elif quote.get("future"):
            alerts.append("quote_future_timestamp")
        elif quote.get("stale"):
            alerts.append("quote_stale")
        halt = self.service.olap.get_state("halt", "none")
        if halt != "none":
            alerts.append(f"halted:{halt}")
        unreconciled = self.service.olap.unreconciled()
        if unreconciled:
            alerts.append(f"unreconciled_orders:{len(unreconciled)}")
        if outcome.get("reason") and "cap" in str(outcome.get("reason")):
            alerts.append("cap_saturation")
        if self.service.olap.get_state("effects_due", ""):
            alerts.append("command_effects_pending")
        return alerts

    def tick(self, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or _utc_now()
        bar = bar_time(now, int(self.config["bar_seconds"]))
        # Settle the past before deciding the present (finding 056): the
        # synthetic lifecycle advances first so closed positions free their
        # capacity for this bar's decision instead of one bar later.
        lifecycle_advanced = self.driver.advance(now) if self.driver else []
        quote = self.quotes.latest(now)
        outcome: dict[str, Any]
        if quote is None:
            outcome = {"outcome": "no_quote_available"}
        elif quote["future"]:
            outcome = {
                "outcome": "quote_invalid_future_timestamp",
                "age_seconds": quote["age_seconds"],
            }
        elif quote["stale"]:
            outcome = {
                "outcome": "quote_stale",
                "age_seconds": quote["age_seconds"],
            }
        else:
            intent = self.policy.decide({
                "bar_time": bar,
                "reference_price": quote["mid"],
                "quote_hash": quote["quote_hash"],
            })
            capability = build_capability(
                self.config["capability_fixture"], now
            )
            outcome = self.service.process_intent(
                intent,
                capability,
                equity=self.equity,
                reference_price=quote["mid"],
                quote_time=quote["observed_at"],
                instrument=self.instrument,
                now=now,
            )
        heartbeat = {
            "schema": "lts.demo_execution_runner.heartbeat.v1",
            "at": now.isoformat(),
            "bar": bar.isoformat(),
            "symbol": self.quotes.symbol,
            "outcome": outcome.get("outcome"),
            "reason": outcome.get("reason"),
            "replayed": outcome.get("replayed", False),
            "would_be_orders_session": self.service.sink.would_be_orders,
            "network_submissions_session": self.service.sink.network_submissions,
            "capability_evidence": "synthetic_fixture",
            "halt_state": self.service.olap.get_state("halt", "none"),
            "lifecycle_advanced": lifecycle_advanced,
            "alerts": self._alerts(quote, outcome),
        }
        heartbeat_path = Path(self.config["heartbeat_path"])
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = heartbeat_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(heartbeat, indent=1, sort_keys=True))
        tmp.replace(heartbeat_path)
        return heartbeat

    def run(self, once: bool = False) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        interval = float(self.config["loop_seconds"])
        while True:
            try:
                heartbeat = self.tick()
                print(json.dumps(heartbeat, sort_keys=True), flush=True)
            except (DemoExecutionError, RunnerError) as error:
                print(json.dumps({"error": str(error)}), flush=True)
            if once or self._stop:
                return 0
            deadline = time.monotonic() + interval
            while not self._stop and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))
            if self._stop:
                return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    runner = DemoExecutionRunner(load_runner_config(args.config))
    return runner.run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
