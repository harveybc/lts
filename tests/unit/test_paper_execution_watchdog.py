from __future__ import annotations

import sqlite3
from pathlib import Path

from tools.paper_execution_watchdog import (
    MonitorStore,
    evaluate,
    process_events,
    read_mt5_snapshot,
)


def _healthy_alpaca(now: float) -> dict:
    return {
        "available": True,
        "status": "complete",
        "ended_at": "2026-07-30T04:00:00+00:00",
        "detail": {
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
        {"available": True, "host": "127.0.0.1", "port": 7497},
        now=now,
        stale_seconds=900,
    )

    assert events == []
    assert discussions == []


def test_evaluate_reports_missing_alpaca_and_offline_ibkr() -> None:
    events, discussions = evaluate(
        {"available": False, "reason": "database_missing"},
        {"available": False, "host": "127.0.0.1", "port": 7497},
        now=1785384300.0,
        stale_seconds=900,
    )

    assert {event["key"] for event in events} == {
        "alpaca_observer_missing",
        "ibkr_paper_offline",
    }
    assert discussions == []


def test_evaluate_reports_unconfigured_oanda() -> None:
    events, discussions = evaluate(
        _healthy_alpaca(1785384300.0),
        {"available": True, "host": "127.0.0.1", "port": 7497},
        {"available": False, "configured": False, "reason": "not_configured"},
        now=1785384300.0,
        stale_seconds=900,
    )

    assert [event["key"] for event in events] == [
        "oanda_practice_not_configured"
    ]
    assert discussions == []


def test_evaluate_reports_stale_disconnected_mt5_with_exposure() -> None:
    events, discussions = evaluate(
        _healthy_alpaca(1785384300.0),
        {"available": True, "host": "127.0.0.1", "port": 7497},
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


def test_read_mt5_snapshot_handles_missing_schema(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite"
    sqlite3.connect(path).close()

    snapshot = read_mt5_snapshot(path)

    assert snapshot["available"] is False
    assert snapshot["reason"] == "schema_unavailable"


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

    assert first == ["IBKR offline\nStart TWS Paper."]
    assert duplicate == []
    assert recovered == ["LTS PAPER RECOVERED\nevent cleared: ibkr_paper_offline"]

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
