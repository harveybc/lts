from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tools.paper_execution_watchdog import (
    MonitorStore,
    evaluate,
    process_events,
    read_execution_runtime,
    read_ibkr_snapshot,
    read_mt5_remote_status,
    read_mt5_snapshot,
)


def _healthy_alpaca(now: float) -> dict:
    return {
        "available": True,
        "status": "complete",
        "ended_at": "2026-07-30T04:00:00+00:00",
        "detail": {
            "account_fingerprint": "0123456789abcdef",
            "account_blocked": False,
            "trading_blocked": False,
            "missing_cells": [],
            "quotes_received": [
                "ADA/USD",
                "BTC/USD",
                "DOGE/USD",
                "ETH/USD",
                "SOL/USD",
                "XRP/USD",
            ],
            "open_positions": 0,
            "open_orders": 0,
        },
        "probes": [{"endpoint": "account", "success": True}],
        "history": [],
    }


def test_evaluate_reports_no_event_for_healthy_online_venues() -> None:
    now = 1785384300.0
    events, discussions = evaluate(
        _healthy_alpaca(now),
        {
            "available": True,
            "socket": {
                "available": True,
                "host": "127.0.0.1",
                "port": 7497,
            },
            "latest_complete": {
                "ended_at": "2026-07-30T04:00:00+00:00",
                "open_positions": 0,
                "open_orders": 0,
            },
        },
        now=now,
        stale_seconds=900,
    )

    assert events == []
    assert discussions == []


def test_evaluate_accepts_account_bound_alpaca_execution() -> None:
    now = 1785384300.0
    alpaca = _healthy_alpaca(now)
    alpaca["detail"].update({"open_positions": 0, "open_orders": 1})
    alpaca["execution_runtime"] = {
        "available": True,
        "venue": "alpaca_paper",
        "environment": "paper",
        "read_only": False,
        "account_binding_verified": True,
        "account_fingerprint": alpaca["detail"]["account_fingerprint"],
        "instrument": "SPY",
        "model_id": "spy-daily-linear-live-v1",
        "selection_error": None,
        "state": "monitoring",
        "positions": 0,
        "orders": 1,
    }
    events, _ = evaluate(
        alpaca,
        {
            "available": True,
            "socket": {"available": True},
            "latest_complete": {
                "ended_at": "2026-07-30T04:00:00+00:00",
                "open_positions": 0,
                "open_orders": 0,
            },
        },
        now=now,
        stale_seconds=900,
    )

    assert events == []


def test_evaluate_rejects_unbound_alpaca_execution() -> None:
    now = 1785384300.0
    alpaca = _healthy_alpaca(now)
    alpaca["detail"]["open_orders"] = 1
    alpaca["execution_runtime"] = {"available": True, "orders": 1}

    events, _ = evaluate(
        alpaca,
        {
            "available": True,
            "socket": {"available": True},
            "latest_complete": {
                "ended_at": "2026-07-30T04:00:00+00:00",
                "open_positions": 0,
                "open_orders": 0,
            },
        },
        now=now,
        stale_seconds=900,
    )

    assert [item["key"] for item in events] == ["alpaca_unexpected_exposure"]


def test_evaluate_accepts_reconciled_ibkr_model_exposure() -> None:
    now = 1785384300.0
    events, _ = evaluate(
        _healthy_alpaca(now),
        {
            "available": True,
            "socket": {"available": True},
            "latest_complete": {
                "ended_at": "2026-07-30T04:00:00+00:00",
                "open_positions": 1,
                "open_orders": 2,
            },
            "execution_runtime": {
                "available": True,
                "venue": "ibkr_paper",
                "environment": "paper",
                "read_only": False,
                "account_binding_verified": True,
                "account_fingerprint": "0123456789abcdef",
                "instrument": "USD.CAD",
                "model_id": "usdcad-4h-linear-live-v1",
                "selection_error": None,
                "state": "monitoring",
                "position": -25000,
                "orders": 2,
                "l1": {"fills": [{"result": {"position_reconciled": True}}]},
            },
        },
        now=now,
        stale_seconds=900,
    )

    assert events == []


def test_evaluate_rejects_ibkr_exposure_without_reconciled_fill() -> None:
    now = 1785384300.0
    events, _ = evaluate(
        _healthy_alpaca(now),
        {
            "available": True,
            "socket": {"available": True},
            "latest_complete": {
                "ended_at": "2026-07-30T04:00:00+00:00",
                "open_positions": 1,
                "open_orders": 2,
            },
            "execution_runtime": {
                "available": True,
                "venue": "ibkr_paper",
                "environment": "paper",
                "read_only": False,
                "account_binding_verified": True,
                "account_fingerprint": "0123456789abcdef",
                "instrument": "USD.CAD",
                "model_id": "usdcad-4h-linear-live-v1",
                "selection_error": None,
                "state": "monitoring",
                "position": -25000,
                "orders": 2,
                "l1": {"fills": []},
            },
        },
        now=now,
        stale_seconds=900,
    )

    assert [item["key"] for item in events] == ["ibkr_unexpected_exposure"]


def test_read_execution_runtime_requires_fresh_expected_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps(
            {
                "schema": "lts.ibkr.model_runner.heartbeat.v1",
                "observed_at": "2026-07-30T04:00:00+00:00",
                "state": "monitoring",
            }
        ),
        encoding="utf-8",
    )

    fresh = read_execution_runtime(
        path,
        "lts.ibkr.model_runner.heartbeat.v1",
        now=1785384300.0,
        stale_seconds=900,
    )
    stale = read_execution_runtime(
        path,
        "lts.ibkr.model_runner.heartbeat.v1",
        now=1785385301.0,
        stale_seconds=900,
    )

    assert fresh["available"] is True
    assert stale["available"] is False
    assert stale["reason"] == "heartbeat_stale"


def test_evaluate_reports_missing_alpaca_and_offline_ibkr() -> None:
    events, discussions = evaluate(
        {"available": False, "reason": "database_missing"},
        {
            "available": False,
            "reason": "database_missing",
            "socket": {
                "available": False,
                "host": "127.0.0.1",
                "port": 7497,
            },
        },
        now=1785384300.0,
        stale_seconds=900,
    )

    assert {event["key"] for event in events} == {
        "alpaca_observer_missing",
        "ibkr_paper_offline",
        "ibkr_observer_missing",
    }
    assert discussions == []


def test_evaluate_reports_unconfigured_oanda() -> None:
    events, discussions = evaluate(
        _healthy_alpaca(1785384300.0),
        {
            "available": True,
            "socket": {"available": True},
            "latest_complete": {
                "ended_at": "2026-07-30T04:00:00+00:00",
                "open_positions": 0,
                "open_orders": 0,
            },
        },
        {"available": False, "configured": False, "reason": "not_configured"},
        now=1785384300.0,
        stale_seconds=900,
        oanda_rest_required=True,
    )

    assert [event["key"] for event in events] == [
        "oanda_practice_not_configured"
    ]
    assert discussions == []


def test_evaluate_ignores_optional_unconfigured_oanda() -> None:
    events, discussions = evaluate(
        _healthy_alpaca(1785384300.0),
        {
            "available": True,
            "socket": {"available": True},
            "latest_complete": {
                "ended_at": "2026-07-30T04:00:00+00:00",
                "open_positions": 0,
                "open_orders": 0,
            },
        },
        {"available": False, "configured": False, "reason": "not_configured"},
        now=1785384300.0,
        stale_seconds=900,
    )

    assert events == []
    assert discussions == []


def test_evaluate_reports_stale_disconnected_mt5_with_exposure() -> None:
    events, discussions = evaluate(
        _healthy_alpaca(1785384300.0),
        {
            "available": True,
            "socket": {"available": True},
            "latest_complete": {
                "ended_at": "2026-07-30T04:00:00+00:00",
                "open_positions": 0,
                "open_orders": 0,
            },
        },
        None,
        {
            "available": True,
            "heartbeat": {
                "connected": False,
                "received_at": "2026-07-30T03:00:00+00:00",
            },
            "latest_snapshot": {
                "positions_total": 1,
                "orders_total": 2,
            },
        },
        now=1785384300.0,
        stale_seconds=900,
    )

    assert {event["key"] for event in events} == {
        "mt5_bridge_stale",
        "mt5_terminal_disconnected",
        "mt5_unexpected_exposure",
    }
    assert discussions == []


def test_evaluate_accepts_reconciled_writable_mt5_exposure() -> None:
    events, discussions = evaluate(
        _healthy_alpaca(1785384300.0),
        {
            "available": True,
            "socket": {"available": True},
            "latest_complete": {
                "ended_at": "2026-07-30T04:00:00+00:00",
                "open_positions": 0,
                "open_orders": 0,
            },
        },
        None,
        {
            "available": True,
            "read_only": False,
            "execution_enabled": True,
            "heartbeat": {
                "connected": True,
                "received_at": "2026-07-30T04:00:00+00:00",
            },
            "latest_snapshot": {"positions_total": 1, "orders_total": 0},
            "exposure_reconciliation": {
                "available": True,
                "positions_total": 1,
                "orders_total": 0,
                "all_authorized": True,
            },
        },
        now=1785384300.0,
        stale_seconds=900,
    )

    assert events == []
    assert discussions == []


def test_evaluate_rejects_unreconciled_writable_mt5_exposure() -> None:
    events, _ = evaluate(
        _healthy_alpaca(1785384300.0),
        {
            "available": True,
            "socket": {"available": True},
            "latest_complete": {
                "ended_at": "2026-07-30T04:00:00+00:00",
                "open_positions": 0,
                "open_orders": 0,
            },
        },
        None,
        {
            "available": True,
            "read_only": False,
            "execution_enabled": True,
            "heartbeat": {
                "connected": True,
                "received_at": "2026-07-30T04:00:00+00:00",
            },
            "latest_snapshot": {"positions_total": 1, "orders_total": 0},
            "exposure_reconciliation": {
                "available": True,
                "positions_total": 1,
                "orders_total": 0,
                "all_authorized": False,
            },
        },
        now=1785384300.0,
        stale_seconds=900,
    )

    assert [event["key"] for event in events] == ["mt5_unexpected_exposure"]


def test_read_mt5_snapshot_handles_missing_schema(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite"
    sqlite3.connect(path).close()

    snapshot = read_mt5_snapshot(path)

    assert snapshot["available"] is False
    assert snapshot["reason"] == "schema_unavailable"


def test_read_mt5_remote_status_accepts_fleet_safe_contract(monkeypatch) -> None:
    payload = {
        "schema": "lts.mt5.operational_status.v1",
        "available": True,
        "reason": None,
        "heartbeat": {
            "connected": True,
            "received_at": "2026-08-01T12:00:00+00:00",
        },
        "latest_snapshot": {
            "positions_total": 0,
            "orders_total": 0,
            "symbols_total": 6,
        },
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        "tools.paper_execution_watchdog.urllib.request.urlopen",
        lambda request, timeout: Response(),
    )

    assert read_mt5_remote_status("http://dragon:8766/v1/status") == payload


def test_read_ibkr_snapshot_requires_completed_authenticated_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "ibkr.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE lab_sessions (
                session_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                detail_json TEXT NOT NULL
            );
            CREATE TABLE reconciliation_snapshots (
                session_id TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                open_positions INTEGER NOT NULL,
                open_orders INTEGER NOT NULL
            );
            INSERT INTO lab_sessions VALUES (
                'session-1','complete','2026-07-30T03:59:00+00:00',
                '2026-07-30T04:00:00+00:00','{}'
            );
            INSERT INTO reconciliation_snapshots VALUES (
                'session-1','2026-07-30T04:00:00+00:00',0,0
            );
            """
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(
        "tools.paper_execution_watchdog.read_ibkr_socket",
        lambda host, port: {
            "available": True,
            "host": host,
            "port": port,
        },
    )

    snapshot = read_ibkr_snapshot(path, "127.0.0.1", 7497)

    assert snapshot["available"] is True
    assert snapshot["complete_sessions"] == 1
    assert snapshot["latest_complete"]["open_positions"] == 0


def test_read_ibkr_snapshot_rejects_complete_session_without_reconciliation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "ibkr.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE lab_sessions (
                session_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                detail_json TEXT NOT NULL
            );
            CREATE TABLE reconciliation_snapshots (
                session_id TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                open_positions INTEGER NOT NULL,
                open_orders INTEGER NOT NULL
            );
            INSERT INTO lab_sessions VALUES (
                'session-1','complete','2026-07-30T03:59:00+00:00',
                '2026-07-30T04:00:00+00:00','{}'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(
        "tools.paper_execution_watchdog.read_ibkr_socket",
        lambda host, port: {
            "available": True,
            "host": host,
            "port": port,
        },
    )

    snapshot = read_ibkr_snapshot(path, "127.0.0.1", 7497)

    assert snapshot["available"] is False
    assert snapshot["reason"] == "no_reconciled_complete_sessions"


def test_evaluate_reports_reachable_but_stale_ibkr_observer() -> None:
    events, _ = evaluate(
        _healthy_alpaca(1785384300.0),
        {
            "available": True,
            "socket": {"available": True},
            "latest_complete": {
                "ended_at": "2026-07-30T03:00:00+00:00",
                "open_positions": 0,
                "open_orders": 0,
            },
        },
        now=1785384300.0,
        stale_seconds=900,
    )

    assert [event["key"] for event in events] == ["ibkr_observer_stale"]


def test_process_events_deduplicates_and_records_recovery(tmp_path: Path) -> None:
    store_path = tmp_path / "monitor.sqlite"
    store = MonitorStore(store_path)
    state: dict = {}
    event = {
        "key": "ibkr_paper_offline",
        "title": "IBKR offline",
        "detail": "Start TWS Paper.",
        "severity": "warning",
        "category": "operations",
        "discussion": False,
    }
    try:
        first = process_events(
            [event],
            state,
            store,
            now=100.0,
            repeat_seconds=3600.0,
        )
        duplicate = process_events(
            [event],
            state,
            store,
            now=200.0,
            repeat_seconds=3600.0,
        )
        recovered = process_events(
            [],
            state,
            store,
            now=300.0,
            repeat_seconds=3600.0,
        )
    finally:
        store.close()

    assert [(e["kind"], e["title"], e["detail"]) for e in first] == [
        ("observe", "IBKR offline", "Start TWS Paper.")
    ]
    assert duplicate == []
    assert [(e["kind"], e["key"]) for e in recovered] == [
        ("recover", "ibkr_paper_offline")
    ]

    connection = sqlite3.connect(store_path)
    try:
        transitions = [
            row[0]
            for row in connection.execute(
                "SELECT transition FROM monitor_events ORDER BY observed_at"
            )
        ]
    finally:
        connection.close()
    assert transitions == ["activated", "recovered"]


def test_ledger_severity_contract() -> None:
    from tools.paper_execution_watchdog import ledger_severity

    assert ledger_severity("capital_demo_unexpected_exposure", "critical") == "P0"
    assert ledger_severity("oanda_unexpected_exposure", "critical") == "P0"
    assert (
        ledger_severity("multi_venue_shadow_order_violation", "critical")
        == "P0"
    )
    assert ledger_severity("ibkr_observer_stale", "critical") == "P1"
    assert ledger_severity("anything", "warning") == "P2"
    assert ledger_severity("anything", "info") == "P3"


def test_dynamic_rush_key_uses_stable_event_code_and_object() -> None:
    from tools.paper_execution_watchdog import incident_identity

    assert incident_identity("rush_1h:ADA/USD:496484") == (
        "rush_1h", "ADA/USD:496484"
    )
    assert incident_identity("rush_4h:ETH/USD:123") == (
        "rush_4h", "ETH/USD:123"
    )
    assert incident_identity("ibkr_observer_stale") == (
        "ibkr_observer_stale", "-"
    )


def test_emit_to_incident_ledger_builds_cli_calls(tmp_path: Path) -> None:
    from tools.paper_execution_watchdog import emit_to_incident_ledger

    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stderr = ""

    def fake_runner(command, **_kwargs):
        calls.append(command)
        return _Result()

    failures = emit_to_incident_ledger(
        [
            {
                "kind": "observe",
                "key": "oanda_unexpected_exposure",
                "title": "foreign exposure",
                "detail": "unauthorized ticket",
                "severity": "critical",
                "category": "execution",
            },
            {
                "kind": "recover",
                "key": "ibkr_observer_stale",
                "title": "LTS PAPER RECOVERED",
                "detail": "event cleared: ibkr_observer_stale",
                "severity": "info",
                "category": "operations",
            },
        ],
        repo=tmp_path,
        config=tmp_path / "config.json",
        machine="omega",
        runner=fake_runner,
    )
    assert failures == 0
    assert len(calls) == 2
    observe_call = calls[0]
    assert "observe" in observe_call
    assert observe_call[observe_call.index("--severity") + 1] == "P0"
    assert "recover" in calls[1]


def test_emit_dynamic_rush_identity_is_ledger_safe(tmp_path: Path) -> None:
    from tools.paper_execution_watchdog import emit_to_incident_ledger

    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stderr = ""

    def fake_runner(command, **_kwargs):
        calls.append(command)
        return _Result()

    failures = emit_to_incident_ledger(
        [{
            "kind": "observe",
            "key": "rush_1h:ADA/USD:496484",
            "title": "one-hour move",
            "detail": "research discussion only",
            "severity": "info",
            "category": "research",
        }],
        repo=tmp_path,
        config=tmp_path / "config.json",
        machine="omega",
        runner=fake_runner,
    )
    assert failures == 0
    command = calls[0]
    assert command[command.index("--event-code") + 1] == "rush_1h"
    assert command[command.index("--object") + 1] == "ADA/USD:496484"


def test_emit_failures_are_counted_not_raised(tmp_path: Path) -> None:
    from tools.paper_execution_watchdog import emit_to_incident_ledger

    def broken_runner(command, **_kwargs):
        raise OSError("no interpreter")

    failures = emit_to_incident_ledger(
        [
            {
                "kind": "observe",
                "key": "k",
                "title": "t",
                "detail": "d",
                "severity": "warning",
                "category": "c",
            }
        ],
        repo=tmp_path,
        config=tmp_path / "config.json",
        machine="omega",
        runner=broken_runner,
    )
    assert failures == 1
