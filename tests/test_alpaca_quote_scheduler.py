"""Regressions for the bounded Alpaca quote scheduler (WP-DATA).

All tests use injected fakes — no network, no credentials, no sqlite
side effects outside tmp. The scheduler must: record per symbol per
tick, stop exactly at the declared bound, tolerate isolated fetch
failures but abort with a typed error after K consecutive ones, count
symbol misses without crashing, and always finish its lab session with
honest status + counters.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.alpaca_quote_scheduler import (SchedulerAborted,  # noqa: E402
                                          run_scheduler)


class FakeStore:
    def __init__(self):
        self.quotes = []
        self.sessions = {}

    def start_session(self, phase, fingerprint, config):
        session_id = f"{phase}-test"
        self.sessions[session_id] = {"status": "running",
                                     "config": config}
        return session_id

    def record_quote(self, session_id, symbol, quote):
        self.quotes.append((session_id, symbol, dict(quote)))

    def finish_session(self, session_id, status, detail):
        self.sessions[session_id] = {"status": status,
                                     "detail": dict(detail)}


class FakeClient:
    def __init__(self, script):
        """script: list of dict payloads or Exception instances."""
        self.script = list(script)
        self.calls = 0

    def latest_crypto_quotes(self, symbols):
        payload = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(payload, Exception):
            raise payload
        return payload


QUOTE = {"bp": 4000.0, "ap": 4004.0, "bs": 1.0, "as": 2.0,
         "t": "2026-08-26T00:00:00Z"}


def test_records_every_symbol_every_tick_and_stops_at_bound():
    store = FakeStore()
    client = FakeClient([{"ETH/USD": QUOTE, "BTC/USD": QUOTE}])
    result = run_scheduler(
        client=client, store=store, symbols=["ETH/USD", "BTC/USD"],
        interval_seconds=60.0, max_samples=3,
        max_consecutive_failures=5, sleeper=lambda s: None,
        log=lambda m: None)
    assert result["status"] == "completed"
    assert result["ticks"] == 3
    assert result["quotes_recorded"] == 6
    assert client.calls == 3  # exactly the bound, never more
    assert store.sessions[result["session_id"]]["status"] == "completed"


def test_isolated_failures_tolerated_and_counter_resets():
    store = FakeStore()
    client = FakeClient([
        RuntimeError("net"), {"ETH/USD": QUOTE}, RuntimeError("net"),
        {"ETH/USD": QUOTE}])
    result = run_scheduler(
        client=client, store=store, symbols=["ETH/USD"],
        interval_seconds=1.0, max_samples=4,
        max_consecutive_failures=2, sleeper=lambda s: None,
        log=lambda m: None)
    assert result["status"] == "completed"
    assert result["fetch_failures"] == 2
    assert result["quotes_recorded"] == 2


def test_consecutive_failures_abort_with_typed_status():
    store = FakeStore()
    client = FakeClient([RuntimeError("down")])
    with pytest.raises(SchedulerAborted, match="consecutive"):
        run_scheduler(
            client=client, store=store, symbols=["ETH/USD"],
            interval_seconds=1.0, max_samples=10,
            max_consecutive_failures=3, sleeper=lambda s: None,
            log=lambda m: None)
    session = next(iter(store.sessions.values()))
    assert session["status"] == "aborted_consecutive_failures"
    assert session["detail"]["fetch_failures"] == 3


def test_symbol_miss_is_counted_not_fatal():
    store = FakeStore()
    client = FakeClient([{"ETH/USD": QUOTE}])  # BTC absent
    result = run_scheduler(
        client=client, store=store, symbols=["ETH/USD", "BTC/USD"],
        interval_seconds=1.0, max_samples=2,
        max_consecutive_failures=5, sleeper=lambda s: None,
        log=lambda m: None)
    assert result["status"] == "completed"
    assert result["symbol_misses"] == 2
    assert result["quotes_recorded"] == 2


@pytest.mark.parametrize("kwargs, fragment", [
    ({"max_samples": 0}, "max_samples"),
    ({"interval_seconds": 0.0}, "interval_seconds"),
    ({"symbols": []}, "symbol"),
], ids=["zero-bound", "zero-interval", "no-symbols"])
def test_invalid_parameters_refuse(kwargs, fragment):
    defaults = dict(client=FakeClient([{}]), store=FakeStore(),
                    symbols=["ETH/USD"], interval_seconds=1.0,
                    max_samples=1, max_consecutive_failures=1,
                    sleeper=lambda s: None, log=lambda m: None)
    defaults.update(kwargs)
    with pytest.raises(ValueError, match=fragment):
        run_scheduler(**defaults)


def test_session_finishes_even_when_aborted():
    store = FakeStore()
    client = FakeClient([RuntimeError("down")])
    with pytest.raises(SchedulerAborted):
        run_scheduler(
            client=client, store=store, symbols=["ETH/USD"],
            interval_seconds=1.0, max_samples=5,
            max_consecutive_failures=1, sleeper=lambda s: None,
            log=lambda m: None)
    assert all(s["status"] != "running"
               for s in store.sessions.values())
