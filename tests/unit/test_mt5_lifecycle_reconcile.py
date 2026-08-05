"""AUD-F2-20260804-104 fixtures: MT5 deal/history events reconcile into
accepted/filled/closed L0 facts and release the reservation — duplicate
event, restart, out-of-order event, partial-close, full-close and
foreign-ticket cases, all through accepted idempotent APIs."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.ibkr_l1_journal import L1ExecutionOlap
from app.mt5_model_runner import reconcile_completed_lifecycles

NOW = datetime(2026, 8, 5, 3, 0, 0, tzinfo=timezone.utc)
FP = "c88e492afa0f8d66a3643373"
KEY = "ethusdt-4h-linear-live-v1:2026-08-03T17:00:00Z:2026-08-03T21:00:00+00:00"
PRODUCER = {"name": "lts.mt5_model_runner", "version": "0.1.0"}

_BRIDGE_TABLES = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_fingerprint TEXT,
    received_at TEXT, positions_total INTEGER, orders_total INTEGER);
CREATE TABLE IF NOT EXISTS execution_commands (
    command_id TEXT PRIMARY KEY, idempotency_key TEXT,
    account_fingerprint TEXT, action TEXT, volume REAL, state TEXT,
    result_json TEXT);
CREATE TABLE IF NOT EXISTS trade_events (
    event_id TEXT PRIMARY KEY, account_fingerprint TEXT, event_type TEXT,
    order_ticket TEXT, deal_ticket TEXT, symbol TEXT, volume REAL,
    price REAL, terminal_observed_at TEXT);
"""


@pytest.fixture()
def l0(tmp_path):
    store = L1ExecutionOlap(tmp_path / "bridge.sqlite")
    store._con.executescript(_BRIDGE_TABLES)
    yield store
    store.close()


def _snapshot(l0, *, positions=0, orders=0, at=None):
    l0._con.execute(
        "INSERT INTO account_snapshots (account_fingerprint, received_at,"
        " positions_total, orders_total) VALUES (?,?,?,?)",
        (FP, (at or NOW).isoformat(), positions, orders))


def _command(l0, *, state="succeeded", order_ticket="40217543",
             key_suffix=":retry:5aeea9c"):
    l0._con.execute(
        "INSERT INTO execution_commands VALUES (?,?,?,?,?,?,?)",
        ("mt5-cmd-1", KEY + key_suffix, FP, "open_short", 0.01, state,
         json.dumps({"order_ticket": order_ticket,
                     "deal_ticket": "41053668"})))


def _deal(l0, *, event_id, order_ticket, deal_ticket, volume, price,
          at, symbol="ETHUSD", fingerprint=FP):
    l0._con.execute(
        "INSERT INTO trade_events VALUES (?,?,?,?,?,?,?,?,?)",
        (event_id, fingerprint, "TRADE_TRANSACTION_DEAL_ADD",
         order_ticket, deal_ticket, symbol, volume, price, at))


def seed(l0, *, close_volume=0.01, duplicate=False, out_of_order=False,
         foreign=False):
    l0.reserve("rsv-610092ed3f4cbcc6", KEY, "2026-08-03", 0.0001, 0.01,
               0.01)
    _snapshot(l0)
    _command(l0)
    _deal(l0, event_id="e-entry", order_ticket="40217543",
          deal_ticket="41053668", volume=0.01, price=1856.95,
          at="2026-08-03T23:47:14+00:00")
    close_at = "2026-08-04T12:38:14+00:00"
    _deal(l0, event_id="e-close", order_ticket="40238369",
          deal_ticket="41073597", volume=close_volume, price=1881.0,
          at=close_at)
    if duplicate:
        _deal(l0, event_id="e-close-dup", order_ticket="40238369",
              deal_ticket="41073597", volume=close_volume, price=1881.0,
              at=close_at)
    if out_of_order:
        # A second close slice observed EARLIER in the feed order but with
        # a later timestamp: ordering is restored by observation time.
        _deal(l0, event_id="a-first-row", order_ticket="40238370",
              deal_ticket="41073598", volume=0.0, price=1881.0,
              at="2026-08-04T12:38:15+00:00")
    if foreign:
        _deal(l0, event_id="e-foreign", order_ticket="99999999",
              deal_ticket="88888888", volume=0.02, price=1700.0,
              at="2026-08-04T13:00:00+00:00", symbol="BTCUSD")
        _deal(l0, event_id="e-foreign-acct", order_ticket="77777777",
              deal_ticket="66666666", volume=0.02, price=1700.0,
              at="2026-08-04T13:00:00+00:00",
              fingerprint="deadbeefdeadbeefdeadbeef")


def _run(l0):
    return reconcile_completed_lifecycles(
        l0, account_fingerprint=FP, symbol="ETHUSD", producer=PRODUCER,
        now=NOW)


def test_full_close_reconciles_and_releases(l0):
    seed(l0)
    repaired = _run(l0)
    assert [r["reservation_id"] for r in repaired] == [
        "rsv-610092ed3f4cbcc6"]
    assert repaired[0]["close_deals"] == ["41073597"]
    assert l0.active_reservation_intents() == []
    states = [row[0] for row in l0._con.execute(
        "SELECT state FROM lifecycle_events WHERE order_intent_id=?"
        " ORDER BY seq", ("oi2-rsv-610092ed3f4cbcc6",))]
    assert states == ["accepted", "filled", "closed"]


def test_restart_replay_is_idempotent(l0):
    seed(l0)
    _run(l0)
    reopened = L1ExecutionOlap(l0._con.execute("PRAGMA database_list"
                                               ).fetchone()[2])
    again = reconcile_completed_lifecycles(
        reopened, account_fingerprint=FP, symbol="ETHUSD",
        producer=PRODUCER, now=NOW)
    assert again == []
    count = reopened._con.execute(
        "SELECT COUNT(*) FROM lifecycle_events WHERE order_intent_id=?",
        ("oi2-rsv-610092ed3f4cbcc6",)).fetchone()[0]
    assert count == 3                       # no duplicated close
    reopened.close()


def test_duplicate_close_event_counts_once(l0):
    seed(l0, duplicate=True)
    repaired = _run(l0)
    assert repaired[0]["closed_volume"] == pytest.approx(0.01)


def test_out_of_order_events_are_time_ordered(l0):
    seed(l0, out_of_order=True)
    repaired = _run(l0)
    assert repaired and repaired[0]["close_deals"][0] == "41073597"


def test_partial_close_keeps_reservation(l0):
    seed(l0, close_volume=0.004)
    assert _run(l0) == []
    assert len(l0.active_reservation_intents()) == 1
    assert l0._con.execute(
        "SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 0


def test_foreign_tickets_are_excluded(l0):
    seed(l0, foreign=True)
    repaired = _run(l0)
    assert repaired[0]["close_deals"] == ["41073597"]
    assert repaired[0]["closed_volume"] == pytest.approx(0.01)


def test_nonflat_snapshot_blocks(l0):
    seed(l0)
    _snapshot(l0, positions=1)              # newer snapshot: not flat
    assert _run(l0) == []
    assert len(l0.active_reservation_intents()) == 1


def test_stale_snapshot_blocks(l0):
    l0.reserve("rsv-610092ed3f4cbcc6", KEY, "2026-08-03", 0.0001, 0.01,
               0.01)
    _snapshot(l0, at=datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc))
    _command(l0)
    assert _run(l0) == []


def test_unknown_linkage_skipped(l0):
    l0.reserve("rsv-nolink", "other:key", "2026-08-03", 0.0001, 0.01,
               0.01)
    _snapshot(l0)
    assert _run(l0) == []
    assert len(l0.active_reservation_intents()) == 1
