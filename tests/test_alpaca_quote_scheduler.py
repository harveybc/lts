"""Regressions for the bounded Alpaca quote scheduler (WP-DATA;
DATA-SOTA-351 corrected).

The scheduler must: record per symbol per tick through the GLOBALLY
idempotent canonical store, stop exactly at the declared bound, be
terminally honest (completed ONLY after the final tick; store crashes
and operator interruption never masquerade as success), validate every
quote before storage with typed rejection reasons, and stay idempotent
across restarts while preserving per-session provenance.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.alpaca_paper_lab import AlpacaPaperOlap  # noqa: E402
from tools.alpaca_quote_scheduler import (SchedulerAborted,  # noqa: E402
                                          run_scheduler, validate_quote)


class FakeStore:
    def __init__(self):
        self.quotes = []
        self.sessions = {}

    def start_session(self, phase, fingerprint, config):
        session_id = f"{phase}-test-{len(self.sessions)}"
        self.sessions[session_id] = {"status": "running",
                                     "config": config}
        return session_id

    def record_quote_canonical(self, session_id, venue, symbol, quote):
        key = (venue, symbol, quote.get("t"))
        is_new = key not in {(v, s, q.get("t"))
                             for _sid, v, s, q in self.quotes}
        self.quotes.append((session_id, venue, symbol, dict(quote)))
        return is_new

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


def run(client, store, **overrides):
    kwargs = dict(client=client, store=store, symbols=["ETH/USD"],
                  interval_seconds=1.0, max_samples=1,
                  max_consecutive_failures=5, sleeper=lambda s: None,
                  log=lambda m: None)
    kwargs.update(overrides)
    return run_scheduler(**kwargs)


# ------------------------------------------------------------- happy path

def test_records_every_symbol_every_tick_and_stops_at_bound():
    store = FakeStore()
    client = FakeClient([{"ETH/USD": QUOTE, "BTC/USD": QUOTE}])
    result = run(client, store, symbols=["ETH/USD", "BTC/USD"],
                 max_samples=3)
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
    result = run(client, store, max_samples=4,
                 max_consecutive_failures=2)
    assert result["status"] == "completed"
    assert result["fetch_failures"] == 2
    assert result["quotes_recorded"] == 2


def test_symbol_miss_is_counted_not_fatal():
    store = FakeStore()
    client = FakeClient([{"ETH/USD": QUOTE}])  # BTC absent
    result = run(client, store, symbols=["ETH/USD", "BTC/USD"],
                 max_samples=2)
    assert result["status"] == "completed"
    assert result["symbol_misses"] == 2
    assert result["quotes_recorded"] == 2


# --------------------------------------------- 351: terminal honesty

def test_consecutive_failures_abort_with_typed_status():
    store = FakeStore()
    client = FakeClient([RuntimeError("down")])
    with pytest.raises(SchedulerAborted, match="consecutive"):
        run(client, store, max_samples=10, max_consecutive_failures=3)
    session = next(iter(store.sessions.values()))
    assert session["status"] == "aborted_consecutive_failures"
    assert session["detail"]["fetch_failures"] == 3


def test_store_write_crash_is_not_a_completed_session():
    """The PRE counterexample: a record_quote crash reached finally and
    the session said 'completed'."""
    class BoomStore(FakeStore):
        def record_quote_canonical(self, *args):
            raise RuntimeError("disk full")
    store = BoomStore()
    client = FakeClient([{"ETH/USD": QUOTE}])
    with pytest.raises(RuntimeError, match="disk full"):
        run(client, store)
    session = next(iter(store.sessions.values()))
    assert session["status"] == "failed_unexpected"


def test_operator_interruption_records_interrupted():
    class InterruptingClient:
        calls = 0

        def latest_crypto_quotes(self, symbols):
            self.calls += 1
            if self.calls == 2:
                raise KeyboardInterrupt
            return {"ETH/USD": QUOTE}
    store = FakeStore()
    result = run(InterruptingClient(), store, max_samples=5)
    assert result["status"] == "interrupted"
    assert result["ticks"] == 1  # only the completed tick counts
    session = next(iter(store.sessions.values()))
    assert session["status"] == "interrupted"


@pytest.mark.parametrize("kwargs, fragment", [
    ({"max_samples": 0}, "max_samples"),
    ({"interval_seconds": 0.0}, "interval_seconds"),
    ({"symbols": []}, "symbol"),
    ({"max_consecutive_failures": 0}, "max_consecutive_failures"),
    ({"max_consecutive_failures": -3}, "max_consecutive_failures"),
], ids=["zero-bound", "zero-interval", "no-symbols", "zero-failures",
        "negative-failures"])
def test_invalid_parameters_refuse(kwargs, fragment):
    with pytest.raises(ValueError, match=fragment):
        run(FakeClient([{}]), FakeStore(), **kwargs)


# --------------------------------------------- 351: quote validation

@pytest.mark.parametrize("quote, reason", [
    ({"ap": 4004.0, "t": "2026-08-26T00:00:00Z"},
     "missing_or_non_numeric_bid_ask"),
    ({"bp": float("nan"), "ap": 4004.0, "t": "2026-08-26T00:00:00Z"},
     "non_finite_bid_ask"),
    ({"bp": float("inf"), "ap": 4004.0, "t": "2026-08-26T00:00:00Z"},
     "non_finite_bid_ask"),
    ({"bp": 0.0, "ap": 4004.0, "t": "2026-08-26T00:00:00Z"},
     "non_positive_bid_ask"),
    ({"bp": 4010.0, "ap": 4004.0, "t": "2026-08-26T00:00:00Z"},
     "crossed_quote"),
    ({"bp": 4000.0, "ap": 4004.0, "bs": -1.0,
      "t": "2026-08-26T00:00:00Z"}, "negative_or_non_finite_size"),
    ({"bp": 4000.0, "ap": 4004.0}, "missing_broker_timestamp"),
    ({"bp": 4000.0, "ap": 4004.0, "t": "yesterday-ish"},
     "malformed_broker_timestamp"),
], ids=["missing-bid", "nan", "inf", "zero-bid", "crossed",
        "negative-size", "no-stamp", "bad-stamp"])
def test_invalid_quotes_reject_with_typed_reason(quote, reason):
    assert validate_quote(quote) == reason
    store = FakeStore()
    client = FakeClient([{"ETH/USD": quote}])
    result = run(client, store)
    assert result["status"] == "completed"
    assert result["quotes_recorded"] == 0
    assert result["rejected_quotes"] == {reason: 1}
    assert store.quotes == []  # never stored


def test_valid_quote_passes_validation():
    assert validate_quote(QUOTE) is None


# ------------------------------------- 351: global idempotency (REAL store)

def test_restart_is_globally_idempotent_with_session_provenance(
        tmp_path):
    """The PRE counterexample: the same (symbol, broker_time) was
    stored twice under two session_ids."""
    store = AlpacaPaperOlap(tmp_path / "lab.sqlite")
    client = FakeClient([{"ETH/USD": QUOTE}])
    first = run(client, store, max_samples=2)
    second = run(FakeClient([{"ETH/USD": QUOTE}]), store,
                 max_samples=1)  # restart replays the same broker_time
    assert first["canonical_new"] == 1
    assert first["canonical_duplicates"] == 1  # same tick replay
    assert second["canonical_new"] == 0
    assert second["canonical_duplicates"] == 1
    canonical = store.connection.execute(
        "SELECT COUNT(*) FROM quote_canonical").fetchone()[0]
    membership = store.connection.execute(
        "SELECT COUNT(DISTINCT session_id) FROM "
        "quote_session_membership").fetchone()[0]
    assert canonical == 1     # ONE observation, ever
    assert membership == 2    # provenance for BOTH runs preserved


def test_clean_continuation_after_interrupted_run(tmp_path):
    store = AlpacaPaperOlap(tmp_path / "lab.sqlite")

    class InterruptingClient:
        calls = 0

        def latest_crypto_quotes(self, symbols):
            self.calls += 1
            if self.calls == 2:
                raise KeyboardInterrupt
            return {"ETH/USD": QUOTE}
    interrupted = run(InterruptingClient(), store, max_samples=5)
    assert interrupted["status"] == "interrupted"
    fresh_quote = dict(QUOTE, t="2026-08-26T00:01:00Z")
    resumed = run(FakeClient([{"ETH/USD": fresh_quote}]), store,
                  max_samples=1)
    assert resumed["status"] == "completed"
    canonical = store.connection.execute(
        "SELECT COUNT(*) FROM quote_canonical").fetchone()[0]
    assert canonical == 2  # old observation + the new one, no dupes
    statuses = [r[0] for r in store.connection.execute(
        "SELECT status FROM lab_sessions ORDER BY started_at").fetchall()]
    assert "interrupted" in statuses and "completed" in statuses
