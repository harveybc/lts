"""IBKR Paper L1 canary runner: disabled by default, fail closed, journal-first.

Milestone E of the 063-068 correction order. One continuous loop that:

1. writes a deterministic heartbeat every tick (the accepted L0 runner
   idiom: atomic tmp+replace JSON), even when disabled or degraded;
2. resumes crash-interrupted effects through exact re-acknowledgement
   before doing anything else;
3. consumes accepted L0 decisions through the L1 outbox with a fresh
   read-only quote; quote problems defer, they never destroy a decision;
4. syncs direct parent-fill facts into the L0 lifecycle ledger; and
5. derives owner-facing alerts and Telegram-forwardable event facts from
   the durable ledger only — never from in-memory guesses. Delivery is
   owned by the deployed watchdog; this runner holds no token.

Fail-closed properties:

- ``enabled: false`` (the default in every shipped config) produces a
  heartbeat-only loop; the broker client factory is NEVER invoked;
- no TWS-backed ``IbkrClientProtocol`` implementation exists until
  Milestone F: the default factory refuses deterministically, the refusal
  becomes an alert, and the loop keeps heartbeating;
- rollback is ``systemctl --user disable --now lts-ibkr-l1-canary`` plus
  the owner hold/kill command through the accepted L0 path; the runner
  honors the halt state on the next tick.
"""
from __future__ import annotations

import argparse
import json
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.demo_execution_runner import QuoteSource
from app.demo_execution_service import (
    DemoExecutionConfig,
    DemoExecutionService,
    ZeroNetworkSink,
)
from app.ibkr_l1_adapter import L1ExecutionError, L1Profile
from app.ibkr_l1_broker import IbkrClientProtocol
from app.ibkr_l1_capability import CapabilityGate
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_l1_outbox import L1OutboxConsumer

HEARTBEAT_SCHEMA = "lts.ibkr_l1_runner.heartbeat.v1"

_REQUIRED_CONFIG = (
    "enabled", "service", "profile_path", "quote_database_path",
    "quote_symbol", "quote_max_age_seconds", "loop_seconds",
    "heartbeat_path", "price_decimals", "quantity_decimals",
)
_OPTIONAL_CONFIG = ("capability_store_dir", "max_decision_age_seconds")


class L1RunnerError(RuntimeError):
    pass


def load_l1_runner_config(path: str | Path) -> dict[str, Any]:
    with open(path) as handle:
        config = json.load(handle)
    missing = [key for key in _REQUIRED_CONFIG if key not in config]
    if missing:
        raise L1RunnerError(f"L1 runner config missing keys: {missing}")
    unknown = sorted(
        set(config) - set(_REQUIRED_CONFIG) - set(_OPTIONAL_CONFIG)
    )
    if unknown:
        raise L1RunnerError(f"L1 runner config has unknown keys: {unknown}")
    if not isinstance(config["enabled"], bool):
        raise L1RunnerError("L1 runner config 'enabled' must be a boolean")
    return config


def default_client_factory(profile: L1Profile) -> IbkrClientProtocol:
    """No TWS-backed client exists until Milestone F. Refuse loudly."""
    raise L1ExecutionError(
        "no TWS-backed IbkrClientProtocol implementation exists yet "
        "(Milestone F); the runner fails closed without a client"
    )


class IbkrL1Runner:
    """Continuous, disabled-by-default driver of the L1 outbox consumer."""

    def __init__(
        self,
        config: dict[str, Any],
        client_factory: Optional[
            Callable[[L1Profile], IbkrClientProtocol]
        ] = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory or default_client_factory
        self._stop = False
        self._runtime: Optional[dict[str, Any]] = None

    def request_stop(self, *_args: Any) -> None:
        self._stop = True

    # -- lazy runtime construction (only when enabled) ---------------------
    def _runtime_or_none(self, alerts: list[str]) -> Optional[dict[str, Any]]:
        if self._runtime is not None:
            return self._runtime
        try:
            profile = L1Profile.load(self.config["profile_path"])
            client = self._client_factory(profile)
        except Exception as error:  # noqa: BLE001 — degraded, not fatal
            alerts.append(f"client_unavailable:{type(error).__name__}")
            return None
        service_config = DemoExecutionConfig.from_dict(self.config["service"])
        olap = L1ExecutionOlap(service_config.database_path)
        service = DemoExecutionService(service_config, olap, ZeroNetworkSink())
        store = self.config.get("capability_store_dir")
        consumer = L1OutboxConsumer(
            service, olap, client, profile,
            CapabilityGate(Path(store) if store else None),
            price_decimals=int(self.config["price_decimals"]),
            quantity_decimals=int(self.config["quantity_decimals"]),
            max_decision_age_seconds=float(
                self.config.get("max_decision_age_seconds", 300.0)
            ),
        )
        self._runtime = {
            "profile": profile,
            "client": client,
            "olap": olap,
            "service": service,
            "consumer": consumer,
        }
        return self._runtime

    # -- one tick ----------------------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        alerts: list[str] = []
        events: list[str] = []
        heartbeat: dict[str, Any] = {
            "schema": HEARTBEAT_SCHEMA,
            "generated_at": now.isoformat(),
            "enabled": self.config["enabled"],
            "orders_submitted_by_this_runner": None,
        }

        if not self.config["enabled"]:
            heartbeat.update({
                "state": "disabled",
                "alerts": [],
                "events": [],
            })
            self._write_heartbeat(heartbeat)
            return heartbeat

        runtime = self._runtime_or_none(alerts)
        if runtime is None:
            heartbeat.update({
                "state": "degraded_no_client",
                "alerts": alerts,
                "events": [],
            })
            self._write_heartbeat(heartbeat)
            return heartbeat

        olap: L1ExecutionOlap = runtime["olap"]
        consumer: L1OutboxConsumer = runtime["consumer"]

        # 1. crash recovery first: classify and re-acknowledge from facts
        for outcome in consumer.resume(now=now):
            if outcome.get("reacknowledged") is not None:
                events.append(
                    f"l1_effect_resumed:{outcome['effect_id']}:"
                    f"{outcome['state']}"
                )

        # 2. fresh read-only quote; problems defer, never destroy
        quote_fact = QuoteSource(
            self.config["quote_database_path"],
            self.config["quote_symbol"],
            float(self.config["quote_max_age_seconds"]),
        ).latest(now)
        quote_payload: Optional[dict[str, Any]] = None
        if quote_fact is None:
            alerts.append("quote_source_unavailable")
        elif quote_fact["future"]:
            alerts.append("quote_future_timestamp")
        elif quote_fact["stale"]:
            alerts.append(f"quote_stale:{quote_fact['age_seconds']:.1f}s")
        elif quote_fact["bid"] is None or quote_fact["ask"] is None:
            alerts.append("quote_missing_bid_ask")
        else:
            quote_payload = {
                "bid": float(quote_fact["bid"]),
                "ask": float(quote_fact["ask"]),
                "time": quote_fact["observed_at"],
            }

        # 3. entries, 4. fills, 5. flattens — all durably journaled
        entries = consumer.consume_entries(quote=quote_payload, now=now)
        for result in entries:
            if result["state"] == "deferred":
                alerts.append(f"entry_deferred:{result['reason']}")
            elif result["state"] == "terminal_rejected":
                alerts.append(f"entry_rejected:{result['reason']}")
            else:
                events.append(
                    f"l1_entry:{result.get('effect_id')}:{result['state']}"
                )
        fills = []
        for effect in olap.nonterminal_effects():
            if (
                effect["kind"] == "bracket_entry"
                and effect["state"] == "acknowledged"
            ):
                applied = consumer.sync_parent_fill(
                    effect["effect_id"], now=now
                )
                if applied is not None:
                    fills.append(effect["effect_id"])
                    events.append(f"l1_parent_filled:{effect['effect_id']}")
        flattens = consumer.consume_flattens(now=now)
        for result in flattens:
            events.append(
                f"l1_flatten:{result['idempotency_key']}:{result['state']}"
            )

        # 6. deterministic ledger-derived status
        halt = olap.get_state("halt", "none")
        if halt != "none":
            alerts.append(f"halted:{halt}")
        unreconciled = olap.unreconciled()
        if unreconciled:
            alerts.append(f"unreconciled_orders:{len(unreconciled)}")
        state_counts = olap.l1_effect_state_counts()
        requiring = sum(
            state_counts.get(state, 0)
            for state in ("effect_unknown", "recovering")
        )
        if requiring:
            alerts.append(f"effects_requiring_reconciliation:{requiring}")
        if olap.get_state("effects_due", ""):
            alerts.append("command_effects_pending")
        fact_counts = olap.l1_broker_fact_counts()

        heartbeat.update({
            "state": "active",
            "halt": halt,
            "quote": None if quote_fact is None else {
                "age_seconds": quote_fact["age_seconds"],
                "stale": quote_fact["stale"],
                "future": quote_fact["future"],
                "quote_hash": quote_fact["quote_hash"],
            },
            "effect_state_counts": state_counts,
            "broker_fact_counts": fact_counts,
            "entry_budget_used": olap.l1_entry_count(),
            "tick": {
                "entries": [r["state"] for r in entries],
                "fills": fills,
                "flattens": [r["state"] for r in flattens],
            },
            "alerts": alerts,
            "events": events,
            "orders_submitted_by_this_runner": fact_counts.get(
                "call_result", 0
            ),
        })
        self._write_heartbeat(heartbeat)
        return heartbeat

    def _write_heartbeat(self, heartbeat: dict[str, Any]) -> None:
        path = Path(self.config["heartbeat_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(heartbeat, indent=1, sort_keys=True,
                                  default=str))
        tmp.replace(path)

    # -- the loop ----------------------------------------------------------
    def run(self, once: bool = False) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        while True:
            self.tick()
            if once or self._stop:
                return 0
            deadline = time.monotonic() + float(self.config["loop_seconds"])
            while not self._stop and time.monotonic() < deadline:
                time.sleep(0.25)
            if self._stop:
                return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    runner = IbkrL1Runner(load_l1_runner_config(args.config))
    return runner.run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
