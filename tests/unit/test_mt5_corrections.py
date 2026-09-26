"""Three corrections to the MT5 unknown-outcome work, 2026-09-26.

The forensics of the five `failed` MT5 commands corrected the ruling itself and
found two further defects. These are the tests for all three.

**Correction 1 — reconciliation must admit retained records, not only a live
query.** The ruling let an `effect_unknown` leave that state only through a live
read-side broker query. With the terminal silent that is unsatisfiable, so an
unknown effect would stay unknown forever and its route blocked forever. The
second admissible source is the lane's own retained `account_snapshots` and
`trade_events` — under conditions strict enough that the absence of data can
never be read as the absence of an order. Every condition here has a test that
REFUSES, including the one the single real unknown effect in this fleet fails: a
424.7-second hole inside its own window, on the terminal's own clock.

**Correction 2 — an unreachable branch.** `exposure_reconciliation` retired a
closed position's ticket on `action == "close_position"`, a verb the command
vocabulary has never contained (it is `close`). The branch had never executed.
`test_a_closed_position_stops_being_authorized` fails on the old spelling.

**Correction 3 — a scheduling defect, separate from the store defect.** Zero of
47 commands created outside 21:00-22:59 UTC ever failed; five of the six created
inside it did, four of them `MARKET_CLOSED` at the venue's daily rollover, into
which the runner's four-hour boundary lands deterministically. The break is
derived from configuration or from the observed record — never hardcoded — and a
boundary inside it defers by name. `test_the_runner_defers_inside_a_venue_break`
fails on the old code, which had no gate and queued the command.

Offline by construction: sockets are booby-trapped, no credential is read, no
terminal or EA is contacted, no order is placed, and no real account, host or
ticket is named.
"""
from __future__ import annotations

import ast
import hashlib
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prediction_provider_mechanics import FEATURE_NAMES

from app.mt5_bridge_lab import Mt5BridgeError, SnapshotPayload
from app.mt5_execution_bridge import (
    ExecutionResultPayload,
    Mt5ExecutionConfig,
    Mt5ExecutionStore,
    _ACTIONS,
    _CLOSE_ACTIONS,
    _OPEN_ACTIONS,
    read_retained_record_window,
    read_venue_break_command_rows,
)
from app.mt5_model_runner import Mt5ModelRunner
from app.mt5_unknown_outcome import (
    CLOSED_BY_END_OF_RECORD,
    CLOSED_BY_NEXT_COMMAND,
    EVENT_RECONCILIATION,
    EVIDENCE_READ_SIDE_OBSERVED_NO_ORDER,
    EVIDENCE_RETAINED_RECORDS_OBSERVED_NO_ORDER,
    MUTATING_CALL_SITES,
    QUERY_RETAINED_RECORDS,
    READ_SIDE_QUERIES,
    RECONCILIATION_EVIDENCE,
    SOURCE_LIVE_BROKER_QUERY,
    SOURCE_RETAINED_RECORDS,
    STATE_EFFECT_UNKNOWN,
    STATE_FAILED,
    ReconciliationInconclusive,
    RetainedRecordWindow,
    observation_from_retained_records,
    outcome_from_observation,
    reconciliation_source_for,
)
from app.mt5_venue_break import (
    MAX_BREAK_MINUTES,
    SOURCE_CONFIGURED,
    SOURCE_OBSERVED_RECORD,
    VENUE_BREAK_UNDECLARED,
    MARKET_CLOSED_RETCODES,
    REFUSAL_VENUE_BREAK,
    STATE_VENUE_BREAK_DEFERRED,
    VenueBreak,
    VenueBreakMisdeclared,
    VenueBreakUndeclarable,
    deferral_for,
    minute_of_day,
    venue_break_from_observed_record,
    venue_breaks_from_config,
)

ACCOUNT = "0123456789abcdef01234567"
ETH = "ETHUSD"
CAD = "USDCAD"
#: TRADE_RETCODE_TIMEOUT: the send whose effect nobody observed.
TIMEOUT = 10012
#: TRADE_RETCODE_MARKET_CLOSED: proof the venue placed nothing.
MARKET_CLOSED = 10018
#: TRADE_RETCODE_DONE.
DONE = 10009
BUDGET_SECONDS = 180.0


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*_args, **_kwargs):
        raise AssertionError("network operation attempted in an offline test")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


# ===================================================== helpers, not fixtures

def _config(tmp_path, *, budget=2):
    return Mt5ExecutionConfig(
        database_path=tmp_path / "mt5.sqlite", secret_env="SECRET",
        bind_host="127.0.0.1", port=8766, max_clock_skew_seconds=90,
        nonce_retention_seconds=900, stale_heartbeat_seconds=int(BUDGET_SECONDS),
        account_fingerprint=ACCOUNT, allowed_symbols=(ETH, CAD),
        symbol_magics={ETH: 26080301, CAD: 26080302},
        require_route_identity=False, max_volume=0.1,
        max_open_commands_per_day=budget, delivery_retry_seconds=30,
    )


def _store(tmp_path, **kwargs):
    config = _config(tmp_path, **kwargs)
    return config, Mt5ExecutionStore(config.database_path)


def _enqueue(store, config, *, symbol=ETH, key, action="open_long"):
    return store.enqueue(
        config=config, idempotency_key=key, action=action, symbol=symbol,
        volume=0.05 if action in _OPEN_ACTIONS else 0,
        stop_loss=1.0 if action in _OPEN_ACTIONS else 0,
        take_profit=2.0 if action in _OPEN_ACTIONS else 0,
        model_id="m1", artifact_sha256="a" * 64, config_sha256="b" * 64,
        input_sha256="c" * 64)


def _complete(store, command_id, retcode, *, success=False, ticket="0"):
    return store.complete(ExecutionResultPayload.model_validate({
        "schema": "lts.mt5.execution_result.v1",
        "command_id": command_id, "account_fingerprint": ACCOUNT,
        "success": success, "result_code": retcode,
        "order_ticket": ticket, "deal_ticket": ticket,
        "message": "fixture only",
        "observed_at": datetime.now(timezone.utc),
    }))


def _observation_row(row_id, moment, *, positions=0, orders=0):
    return {"row_id": row_id, "observed_at": moment.isoformat(),
            "positions_total": positions, "orders_total": orders}


def _clean_window(*, start=None, cadence=60.0, count=12, **over):
    """A window whose retained record is continuous and flat throughout."""
    start = start or datetime(2026, 8, 3, 23, 30, tzinfo=timezone.utc)
    inside = [_observation_row(3837 + index,
                              start + timedelta(seconds=cadence * (index + 1)))
              for index in range(count)]
    end = start + timedelta(seconds=cadence * (count + 1))
    kwargs = {
        "command_id": "mt5-clean",
        "window_start": start,
        "window_end": end,
        "closed_by": CLOSED_BY_NEXT_COMMAND,
        "observations": inside,
        "trade_events": [],
        "max_gap_seconds": BUDGET_SECONDS,
        "boundary_before": _observation_row(3836, start - timedelta(seconds=1)),
        # The closing observation carries the NEXT command's fill: coverage, not
        # flatness, is what it is there to prove.
        "boundary_after": _observation_row(3849, end + timedelta(seconds=14),
                                           positions=1),
    }
    kwargs.update(over)
    return RetainedRecordWindow(**kwargs)


def _real_shaped_window():
    """The shape of the ONE real unknown effect: 12 flat observations that are
    id-contiguous but hold a 424.7-second hole on the terminal's own clock."""
    base = datetime(2026, 8, 3, 23, 30, 24, 951197, tzinfo=timezone.utc)
    stamps = [
        "2026-08-03T23:30:24.965369+00:00", "2026-08-03T23:31:09.924070+00:00",
        "2026-08-03T23:32:09.926377+00:00", "2026-08-03T23:33:09.921732+00:00",
        "2026-08-03T23:34:09.928540+00:00", "2026-08-03T23:35:09.921652+00:00",
        "2026-08-03T23:36:09.911453+00:00", "2026-08-03T23:37:09.926598+00:00",
        "2026-08-03T23:38:09.919607+00:00", "2026-08-03T23:39:09.928505+00:00",
        # ---- 424.7 seconds in which nothing was observed at all ----
        "2026-08-03T23:46:14.636174+00:00", "2026-08-03T23:46:59.626585+00:00",
    ]
    return RetainedRecordWindow(
        command_id="mt5-real-shape",
        window_start=base,
        window_end=datetime(2026, 8, 3, 23, 47, 0, 173, tzinfo=timezone.utc),
        closed_by=CLOSED_BY_NEXT_COMMAND,
        observations=[{"row_id": 3837 + index, "observed_at": stamp,
                       "positions_total": 0, "orders_total": 0}
                      for index, stamp in enumerate(stamps)],
        trade_events=[],
        max_gap_seconds=BUDGET_SECONDS,
        boundary_before={"row_id": 3836,
                         "observed_at": "2026-08-03T23:30:24.922121+00:00",
                         "positions_total": 0, "orders_total": 0},
        boundary_after={"row_id": 3849,
                        "observed_at": "2026-08-03T23:47:14.765291+00:00",
                        "positions_total": 1, "orders_total": 0},
    )


# ============================== Correction 1: the second admissible source

def test_the_retained_record_is_a_declared_read_side_source():
    assert QUERY_RETAINED_RECORDS in READ_SIDE_QUERIES
    assert QUERY_RETAINED_RECORDS not in MUTATING_CALL_SITES
    assert reconciliation_source_for(QUERY_RETAINED_RECORDS) == (
        SOURCE_RETAINED_RECORDS)
    assert reconciliation_source_for("MetaTrader5.positions_get") == (
        SOURCE_LIVE_BROKER_QUERY)
    assert EVIDENCE_RETAINED_RECORDS_OBSERVED_NO_ORDER in RECONCILIATION_EVIDENCE


def test_a_continuous_flat_window_is_a_positive_no_order_observation():
    """The correction's whole point: the records CAN settle it, when they are
    complete. The exit names its source and the rows it rests on."""
    observation = observation_from_retained_records(_clean_window())
    assert observation.order_exists is False
    assert observation.query == QUERY_RETAINED_RECORDS
    detail = dict(observation.detail)
    assert detail["reconciliation_source"] == SOURCE_RETAINED_RECORDS
    assert detail["source_tables"] == ["account_snapshots", "trade_events"]
    assert detail["flat_observations"] == 13
    assert detail["observation_row_ids"][0] == 3836
    assert detail["trade_events"] == 0
    assert detail["max_gap_budget_seconds"] == BUDGET_SECONDS
    assert detail["max_observed_gap_seconds"] <= BUDGET_SECONDS

    outcome = outcome_from_observation(observation)
    assert outcome.state == STATE_FAILED
    assert outcome.evidence == EVIDENCE_RETAINED_RECORDS_OBSERVED_NO_ORDER
    assert outcome.event_kind == EVENT_RECONCILIATION
    assert outcome.consumes_budget_slot is False
    assert outcome.observed_at is not None
    fact = outcome.as_fact()
    assert fact["detail"]["reconciliation_source"] == SOURCE_RETAINED_RECORDS
    assert fact["detail"]["window_closed_by"] == CLOSED_BY_NEXT_COMMAND


def test_the_one_real_unknown_effect_refuses_on_the_gap_in_its_own_window():
    """THE test for the correction's strictness, and the finding it produced.

    The real window holds 12 flat, id-contiguous observations and an empty
    trade-event log — and a 424.7-second hole in the middle in which the account
    was not observed at all. Against the lane's OWN declared budget
    (`stale_heartbeat_seconds`) the retained record refuses to settle it.
    """
    with pytest.raises(ReconciliationInconclusive) as refusal:
        observation_from_retained_records(_real_shaped_window())
    message = str(refusal.value)
    assert "424.7s gap" in message
    assert "absence of data is not absence of an order" in message


def test_a_wider_budget_is_the_only_thing_that_would_admit_it():
    """Stated as a test so nobody has to take it on trust: the refusal is about
    the DECLARED budget, and no tolerance this lane declares is that wide."""
    window = _real_shaped_window()
    admitted = observation_from_retained_records(
        RetainedRecordWindow(
            command_id=window.command_id, window_start=window.window_start,
            window_end=window.window_end, closed_by=window.closed_by,
            observations=window.observations, trade_events=window.trade_events,
            max_gap_seconds=425.0, boundary_before=window.boundary_before,
            boundary_after=window.boundary_after))
    assert admitted.order_exists is False
    assert dict(admitted.detail)["max_observed_gap_seconds"] > 424.0
    # Every tolerance the deployed configuration declares is far below that.
    for declared in (90, 120, 180):
        assert declared < 424.7


def test_an_uncovered_start_refuses():
    with pytest.raises(ReconciliationInconclusive) as refusal:
        observation_from_retained_records(_clean_window(boundary_before=None))
    assert "start" in str(refusal.value)


def test_an_observation_after_the_completion_does_not_cover_the_start():
    start = datetime(2026, 8, 3, 23, 30, tzinfo=timezone.utc)
    with pytest.raises(ReconciliationInconclusive):
        observation_from_retained_records(_clean_window(
            start=start,
            boundary_before=_observation_row(3836, start + timedelta(seconds=5))))


def test_an_uncovered_end_refuses():
    with pytest.raises(ReconciliationInconclusive) as refusal:
        observation_from_retained_records(_clean_window(boundary_after=None))
    assert "end" in str(refusal.value)


def test_exposure_inside_the_window_refuses_rather_than_being_attributed():
    window = _clean_window()
    poisoned = list(window.observations)
    poisoned[5] = {**poisoned[5], "positions_total": 1}
    with pytest.raises(ReconciliationInconclusive) as refusal:
        observation_from_retained_records(_clean_window(observations=poisoned))
    assert "live" in str(refusal.value)


def test_a_received_transaction_refuses_outright():
    with pytest.raises(ReconciliationInconclusive) as refusal:
        observation_from_retained_records(_clean_window(trade_events=[
            {"event_id": "e1", "event_type": "TRADE_TRANSACTION_ORDER_ADD",
             "observed_at": "2026-08-03T23:35:00+00:00"}]))
    assert "transaction" in str(refusal.value)


def test_a_missing_count_is_never_read_as_zero():
    window = _clean_window()
    blind = list(window.observations)
    blind[3] = {**blind[3], "orders_total": None}
    with pytest.raises(ReconciliationInconclusive) as refusal:
        observation_from_retained_records(_clean_window(observations=blind))
    assert "not a count of zero" in str(refusal.value)


def test_a_window_that_reaches_the_present_refuses_on_a_stale_record():
    """The bridge going silent is exactly the case that must refuse: a record
    whose newest observation is 18 days old cannot settle a window that runs to
    now."""
    start = datetime(2026, 8, 3, 23, 30, tzinfo=timezone.utc)
    window = _clean_window(start=start, closed_by=CLOSED_BY_END_OF_RECORD,
                           boundary_after=None)
    with pytest.raises(ReconciliationInconclusive) as refusal:
        observation_from_retained_records(
            window, now=start + timedelta(days=18))
    assert "stale record" in str(refusal.value)


def test_a_window_that_reaches_a_fresh_present_is_admitted():
    start = datetime(2026, 8, 3, 23, 30, tzinfo=timezone.utc)
    window = _clean_window(start=start, closed_by=CLOSED_BY_END_OF_RECORD,
                           boundary_after=None)
    observation = observation_from_retained_records(
        window, now=start + timedelta(seconds=60 * 13 + 30))
    assert observation.order_exists is False
    assert dict(observation.detail)["window_closed_by"] == (
        CLOSED_BY_END_OF_RECORD)


def test_a_window_reaching_the_present_needs_a_reference_time():
    window = _clean_window(closed_by=CLOSED_BY_END_OF_RECORD,
                           boundary_after=None)
    with pytest.raises(ReconciliationInconclusive) as refusal:
        observation_from_retained_records(window)
    assert "reference time" in str(refusal.value)


def test_the_retained_record_may_never_report_that_an_order_exists():
    """It can witness that nothing is there. It cannot attribute a position it
    DOES see to one command rather than another."""
    from app.mt5_unknown_outcome import OrderObservation

    seen = OrderObservation(
        query=QUERY_RETAINED_RECORDS,
        observed_at=datetime(2026, 8, 3, 23, 47, tzinfo=timezone.utc),
        order_exists=True)
    with pytest.raises(ReconciliationInconclusive) as refusal:
        outcome_from_observation(seen)
    assert "never by attribution" in str(refusal.value)


def test_a_live_query_keeps_its_own_evidence_token():
    from app.mt5_unknown_outcome import OrderObservation

    outcome = outcome_from_observation(OrderObservation(
        query="MetaTrader5.history_orders_get",
        observed_at=datetime(2026, 8, 3, 23, 47, tzinfo=timezone.utc),
        order_exists=False))
    assert outcome.evidence == EVIDENCE_READ_SIDE_OBSERVED_NO_ORDER
    assert dict(outcome.detail)["reconciliation_source"] == (
        SOURCE_LIVE_BROKER_QUERY)


def test_a_zero_or_negative_budget_is_not_a_budget():
    with pytest.raises(ValueError):
        _clean_window(max_gap_seconds=0)


# --------------------------------------------- the same thing, via the store

def _snapshot(store, moment, *, positions=(), orders=()):
    payload = {
        "schema": "lts.mt5.snapshot.v1", "account_fingerprint": ACCOUNT,
        "observed_at": moment, "currency": "USD", "balance": 10000,
        "equity": 10000, "margin": 0, "free_margin": 10000,
        "positions": list(positions), "orders": list(orders),
        "bars": [], "symbols": [],
    }
    store.record_snapshot(SnapshotPayload.model_validate(payload))
    store.connection.execute(
        "UPDATE account_snapshots SET received_at=? WHERE id="
        "(SELECT MAX(id) FROM account_snapshots)", (moment.isoformat(),))
    store.connection.commit()


def test_the_store_builds_the_window_and_the_slot_is_released(tmp_path):
    """End to end on a real SQLite file: an unknown effect holds its slot and
    blocks its route, the retained record settles it, and both are released —
    without a single broker call."""
    config, store = _store(tmp_path, budget=1)
    try:
        first = _enqueue(store, config, key="k1")
        _complete(store, first["command_id"], TIMEOUT)
        assert store.effective_state(first["command_id"]) == (
            STATE_EFFECT_UNKNOWN)
        day = f"{datetime.now(timezone.utc).date().isoformat()}T00:00:00+00:00"
        assert store.daily_entry_slots_consumed(day_start=day) == 1
        with pytest.raises(Mt5BridgeError, match="never been observed"):
            _enqueue(store, config, key="k2")

        completed = store.connection.execute(
            "SELECT completed_at FROM execution_commands WHERE command_id=?",
            (first["command_id"],)).fetchone()[0]
        opened = datetime.fromisoformat(completed)
        # coverage at the window's start, then four continuous observations
        _snapshot(store, opened - timedelta(seconds=1))
        for index in range(4):
            _snapshot(store, opened + timedelta(seconds=30 * (index + 1)))
        window = store.retained_record_window(
            command_id=first["command_id"], account_fingerprint=ACCOUNT,
            max_gap_seconds=BUDGET_SECONDS)
        assert window.closed_by == CLOSED_BY_END_OF_RECORD
        observation = observation_from_retained_records(
            window, now=opened + timedelta(seconds=150))
        result = store.reconcile_unknown_effect(
            command_id=first["command_id"], account_fingerprint=ACCOUNT,
            observation=observation)
        assert result["state"] == STATE_FAILED
        assert result["evidence"] == EVIDENCE_RETAINED_RECORDS_OBSERVED_NO_ORDER
        assert result["consumes_budget_slot"] is False
        assert result["query"] == QUERY_RETAINED_RECORDS
        assert store.daily_entry_slots_consumed(day_start=day) == 0
        # The unknown record is kept: the exit is an APPENDED event.
        history = store.outcome_history(first["command_id"])
        assert [row["outcome"] for row in history] == [
            STATE_EFFECT_UNKNOWN, STATE_FAILED]
        assert history[-1]["event_kind"] == EVENT_RECONCILIATION
        appended = json.loads(history[-1]["outcome_json"])
        assert appended["detail"]["reconciliation_source"] == (
            SOURCE_RETAINED_RECORDS)
        assert appended["detail"]["observation_row_ids"]
        # and the route is free again
        _enqueue(store, config, key="k3")
    finally:
        store.connection.close()


def test_a_refusal_keeps_the_state_and_the_slot(tmp_path):
    config, store = _store(tmp_path, budget=1)
    try:
        first = _enqueue(store, config, key="k1")
        _complete(store, first["command_id"], TIMEOUT)
        completed = store.connection.execute(
            "SELECT completed_at FROM execution_commands WHERE command_id=?",
            (first["command_id"],)).fetchone()[0]
        opened = datetime.fromisoformat(completed)
        # Coverage at the start, one observation, then a hole far wider than
        # the declared budget.
        _snapshot(store, opened - timedelta(seconds=1))
        _snapshot(store, opened + timedelta(seconds=10))
        _snapshot(store, opened + timedelta(seconds=10 + 3 * BUDGET_SECONDS))
        window = store.retained_record_window(
            command_id=first["command_id"], account_fingerprint=ACCOUNT,
            max_gap_seconds=BUDGET_SECONDS)
        with pytest.raises(ReconciliationInconclusive):
            observation_from_retained_records(
                window, now=opened + timedelta(seconds=10 + 3 * BUDGET_SECONDS))
        day = f"{datetime.now(timezone.utc).date().isoformat()}T00:00:00+00:00"
        assert store.effective_state(first["command_id"]) == (
            STATE_EFFECT_UNKNOWN)
        assert store.daily_entry_slots_consumed(day_start=day) == 1
        assert len(store.outcome_history(first["command_id"])) == 1
        with pytest.raises(Mt5BridgeError, match="never been observed"):
            _enqueue(store, config, key="k2")
    finally:
        store.connection.close()


def test_the_window_can_be_read_from_a_read_only_connection(tmp_path):
    """The report path must work against a live store it cannot write, and
    against a database written by pre-fix code with no outcome ledger at all."""
    import sqlite3

    config, store = _store(tmp_path, budget=2)
    try:
        first = _enqueue(store, config, key="k1")
        _complete(store, first["command_id"], TIMEOUT)
        completed = store.connection.execute(
            "SELECT completed_at FROM execution_commands WHERE command_id=?",
            (first["command_id"],)).fetchone()[0]
        _snapshot(store, datetime.fromisoformat(completed) - timedelta(
            seconds=1))
        _snapshot(store, datetime.fromisoformat(completed) + timedelta(
            seconds=30))
    finally:
        store.connection.close()
    read_only = sqlite3.connect(
        f"file:{config.database_path}?mode=ro", uri=True)
    try:
        window = read_retained_record_window(
            read_only, command_id=first["command_id"],
            account_fingerprint=ACCOUNT, max_gap_seconds=BUDGET_SECONDS)
        assert window.command_id == first["command_id"]
        assert window.boundary_before is not None
        assert read_venue_break_command_rows(read_only)
    finally:
        read_only.close()


# ================================ Correction 2: the unreachable close branch

def test_the_action_vocabulary_is_partitioned_and_close_is_spelled_once():
    assert _ACTIONS == _OPEN_ACTIONS | _CLOSE_ACTIONS
    assert _CLOSE_ACTIONS == {"close"}
    assert "close_position" not in _ACTIONS


def test_no_mt5_path_branches_on_a_verb_the_vocabulary_lacks():
    source = (Path(__file__).resolve().parents[2]
              / "app" / "mt5_execution_bridge.py").read_text(encoding="utf-8")
    constants = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "close_position" not in constants


def test_a_closed_position_stops_being_authorized(tmp_path):
    """Fails on the old spelling.

    A succeeded `close` must retire the ticket it flattened from the authorized
    set. With the old `action == "close_position"` branch the close was never
    seen, so a position the lane had already closed went on being reported as
    authorized exposure.
    """
    config, store = _store(tmp_path, budget=4)
    try:
        entry = _enqueue(store, config, key="entry")
        _complete(store, entry["command_id"], DONE, success=True,
                  ticket="99001122")
        position = {"ticket": "99001122", "symbol": ETH, "side": "long",
                    "volume": 0.05, "price_open": 1900.0, "stop_loss": 1.0,
                    "take_profit": 2.0, "profit": 0.0,
                    "time_open_unix": 1785000000}
        _snapshot(store, datetime.now(timezone.utc), positions=[position])
        before = store.exposure_reconciliation()
        assert before["authorized_positions"] == 1
        assert before["all_authorized"] is True

        closing = _enqueue(store, config, key="exit", action="close")
        _complete(store, closing["command_id"], DONE, success=True,
                  ticket="99001133")
        # The same ticket is still reported open one snapshot later. It is no
        # longer authorized by anything: the command that authorized it has been
        # closed.
        _snapshot(store, datetime.now(timezone.utc) + timedelta(seconds=1),
                  positions=[position])
        after = store.exposure_reconciliation()
        assert after["authorized_positions"] == 0
        assert after["unexpected_positions"] == 1
        assert after["all_authorized"] is False
    finally:
        store.connection.close()


# ================================= Correction 3: the scheduling repair

#: The four real `MARKET_CLOSED` refusals and two of the successes, as the
#: retained record holds them. No account, host or ticket is named.
_OBSERVED_ROWS = [
    {"created_at": "2026-08-18T21:00:48.252679+00:00", "state": "failed",
     "result_code": MARKET_CLOSED},
    {"created_at": "2026-08-25T21:00:58.085909+00:00", "state": "failed",
     "result_code": MARKET_CLOSED},
    {"created_at": "2026-08-31T21:00:58.787558+00:00", "state": "failed",
     "result_code": MARKET_CLOSED},
    {"created_at": "2026-09-01T21:01:00.097100+00:00", "state": "failed",
     "result_code": MARKET_CLOSED},
    {"created_at": "2026-08-19T01:00:49.000000+00:00", "state": "succeeded",
     "result_code": DONE},
    {"created_at": "2026-08-20T21:30:00.000000+00:00", "state": "succeeded",
     "result_code": DONE},
]


def test_the_market_closed_codes_are_derived_from_the_committed_table():
    assert MARKET_CLOSED in MARKET_CLOSED_RETCODES
    assert TIMEOUT not in MARKET_CLOSED_RETCODES


def test_the_break_is_derived_from_the_venues_own_refusals():
    window = venue_break_from_observed_record(_OBSERVED_ROWS)
    assert window.source == SOURCE_OBSERVED_RECORD
    fact = window.as_fact()
    assert fact["start_utc"] == "21:00"
    assert fact["resume_utc"] == "21:30"
    assert fact["evidence"]["refusal_rows"] == 4
    assert len(fact["evidence"]["refusal_dates_utc"]) == 4
    assert fact["evidence"]["resume_minute_observed_succeeding"] == 21 * 60 + 30


def test_one_days_refusal_is_an_incident_not_a_schedule():
    with pytest.raises(VenueBreakUndeclarable, match="distinct dates"):
        venue_break_from_observed_record(_OBSERVED_ROWS[:1] + _OBSERVED_ROWS[4:])


def test_a_success_inside_the_window_proves_it_is_not_a_break():
    rows = _OBSERVED_ROWS + [
        {"created_at": "2026-08-27T21:00:30.000000+00:00",
         "state": "succeeded", "result_code": DONE}]
    with pytest.raises(VenueBreakUndeclarable, match="not a venue break"):
        venue_break_from_observed_record(rows)


def test_a_record_that_never_shows_the_venue_reopening_refuses():
    rows = [row for row in _OBSERVED_ROWS
            if row["state"] != "succeeded"] + [
        {"created_at": "2026-08-19T01:00:49.000000+00:00",
         "state": "succeeded", "result_code": DONE}]
    with pytest.raises(VenueBreakUndeclarable, match="when the venue reopened"):
        venue_break_from_observed_record(rows)


def test_a_record_with_no_refusal_derives_nothing_and_guesses_nothing():
    with pytest.raises(VenueBreakUndeclarable, match="no market_closed"):
        venue_break_from_observed_record(_OBSERVED_ROWS[4:])


def test_a_configured_break_is_authoritative_and_strictly_parsed():
    breaks = venue_breaks_from_config(
        {"venue_breaks": [{"start_utc": "21:00", "resume_utc": "21:05",
                           "label": "daily rollover"}]})
    assert len(breaks) == 1
    assert breaks[0].source == SOURCE_CONFIGURED
    assert breaks[0].duration_minutes == 5
    assert breaks[0].as_fact()["evidence"]["label"] == "daily rollover"
    assert venue_breaks_from_config({}) == ()
    for broken in (
        {"venue_breaks": "21:00-21:05"},
        {"venue_breaks": [{"start_utc": "21:00"}]},
        {"venue_breaks": [{"start_utc": "9:00", "resume_utc": "21:05"}]},
        {"venue_breaks": [{"start_utc": "25:00", "resume_utc": "25:05"}]},
        {"venue_breaks": [{"start_utc": "21:00", "resume_utc": "21:00"}]},
        {"venue_breaks": [{"start_utc": "23:50", "resume_utc": "00:10"}]},
        {"venue_breaks": [{"start_utc": "21:00", "resume_utc": "21:05",
                           "pad_minutes": 3}]},
    ):
        with pytest.raises(VenueBreakMisdeclared):
            venue_breaks_from_config(broken)


def test_an_implausibly_long_break_is_refused():
    with pytest.raises(VenueBreakMisdeclared, match="ceiling"):
        VenueBreak(start_minute=0, resume_minute=MAX_BREAK_MINUTES + 1,
                   source=SOURCE_CONFIGURED)


def test_a_break_is_a_statement_about_the_utc_clock():
    window = VenueBreak(start_minute=21 * 60, resume_minute=21 * 60 + 30,
                        source=SOURCE_CONFIGURED)
    inside = datetime(2026, 9, 26, 21, 0, 48, tzinfo=timezone.utc)
    assert window.contains(inside)
    assert not window.contains(inside + timedelta(minutes=30))
    assert window.resumes_at(inside) == datetime(
        2026, 9, 26, 21, 30, tzinfo=timezone.utc)
    # An hour before the break, the next resume is still today's.
    assert window.resumes_at(inside - timedelta(hours=1)).day == 26
    # After it, the next one is tomorrow's.
    assert window.resumes_at(inside + timedelta(hours=1)).day == 27
    with pytest.raises(ValueError):
        minute_of_day(datetime(2026, 9, 26, 21, 0))


def test_a_deferral_names_the_refusal_the_break_and_when_it_resumes():
    window = venue_break_from_observed_record(_OBSERVED_ROWS)
    inside = datetime(2026, 9, 26, 21, 0, 48, tzinfo=timezone.utc)
    deferral = deferral_for([window], inside, boundary="2026-09-26T21:00:00Z")
    assert deferral["state"] == STATE_VENUE_BREAK_DEFERRED
    assert deferral["refusal"] == REFUSAL_VENUE_BREAK
    assert deferral["commands_queued"] == 0
    assert deferral["resumes_at"] == "2026-09-26T21:30:00+00:00"
    assert deferral["venue_break"]["source"] == SOURCE_OBSERVED_RECORD
    assert deferral["venue_break"]["evidence"]["refusal_rows"] == 4
    assert deferral_for([window], inside + timedelta(minutes=30)) is None
    assert deferral_for([], inside) is None


def test_no_venue_break_decision_is_ever_made_by_reading_a_message():
    """The same AST guard the outcome vocabulary keeps."""
    source = (Path(__file__).resolve().parents[2]
              / "app" / "mt5_venue_break.py").read_text(encoding="utf-8")
    forbidden = {"lower", "upper", "startswith", "endswith", "find", "search",
                 "match", "split", "strip", "casefold"}
    called = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & forbidden, sorted(called & forbidden)


# ------------------------------------------------- the runner, end to end

def _runner_fixture(tmp_path, *, extra_config=None):
    bridge_path = tmp_path / "bridge.json"
    database_path = tmp_path / "mt5.sqlite"

    def _json(path, value):
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    _json(bridge_path, {
        "schema": "lts.mt5.execution_bridge_config.v2",
        "environment": "demo", "execution_enabled": True,
        "database_path": str(database_path), "secret_env": "SECRET",
        "bind_host": "127.0.0.1", "port": 8766,
        "stale_heartbeat_seconds": int(BUDGET_SECONDS),
        "account_fingerprint": ACCOUNT, "allowed_symbols": [ETH],
        "max_volume": 0.01, "max_open_commands_per_day": 4,
    })
    artifact_path = tmp_path / "model.json"
    artifact_sha = _json(artifact_path, {
        "schema": "prediction_provider.live_linear_policy.v1",
        "model_id": "eth-test-v1", "asset_id": "crypto:ETHUSD",
        "timeframe": "4h", "feature_names": list(FEATURE_NAMES),
        "means": [0.0] * len(FEATURE_NAMES),
        "scales": [1.0] * len(FEATURE_NAMES),
        "coefficients": [0.0] * len(FEATURE_NAMES),
        "intercept": 10.0, "probability_threshold": 0.5,
    })
    training_config = tmp_path / "training.json"
    config_sha = _json(training_config, {"model": "test"})
    manifest_path = tmp_path / "manifest.json"
    _json(manifest_path, {
        "schema": "prediction_provider.live_linear_manifest.v1",
        "model_id": "eth-test-v1", "asset_id": "crypto:ETHUSD",
        "timeframe": "4h", "artifact_file": str(artifact_path),
        "artifact_sha256": artifact_sha, "config_file": str(training_config),
        "config_sha256": config_sha, "research_validated": True,
        "live_inference_eligible": False, "live_execution_eligible": False,
    })
    now = datetime.now(timezone.utc).replace(microsecond=0)
    bars = []
    for index in range(60):
        close = 1900.0 + index
        bars.append({
            "symbol": ETH, "timeframe": "4h",
            "time": (now - timedelta(hours=4 * (60 - index))).isoformat(),
            "open": close - 1, "high": close + 2, "low": close - 2,
            "close": close, "volume": 1000 + index,
        })
    store = Mt5ExecutionStore(database_path)
    store.record_snapshot(SnapshotPayload.model_validate({
        "schema": "lts.mt5.snapshot.v1", "account_fingerprint": ACCOUNT,
        "observed_at": now, "currency": "USD", "balance": 10000,
        "equity": 10000, "margin": 0, "free_margin": 10000,
        "positions": [], "orders": [], "bars": bars,
        "symbols": [{
            "symbol": ETH, "bid": 1958.0, "ask": 1960.0, "point": 0.01,
            "volume_min": 0.01, "volume_max": 65, "volume_step": 0.01,
            "trade_mode": 4, "observed_at": now,
        }],
    }))
    store.close()
    config = {
        "schema": "lts.mt5.model_runner.v1",
        "bridge_config_file": str(bridge_path),
        "model": {
            "manifest_file": str(manifest_path),
            "expected_asset_id": "crypto:ETHUSD",
            "expected_timeframe": "4h",
            "execution_tier": "demo_research_canary",
        },
        "route": {"symbol": ETH, "timeframe": "4h"},
        "strategy": {"stop_fraction": 0.01, "take_profit_fraction": 0.02},
        "snapshot_max_age_seconds": 120, "loop_seconds": 15,
        "service": {
            "venue": "mt5_demo", "account_fingerprint": ACCOUNT,
            "environment": "demo", "database_path": str(database_path),
            "risk_fraction_at_stop": 0.00002, "max_overshoot_ratio": 0.5,
            "gross_notional_fraction_max": 0.003,
            "margin_fraction_max": 0.003,
            "daily_loss_budget_fraction": 0.00008,
            "max_concurrent_positions": 1, "signal_max_age_seconds": 28800,
            "owner_issuer_allowlist": ["owner"], "command_phrases": {},
            "asset_instrument_bindings": {"crypto:ETHUSD": ETH},
        },
    }
    config.update(extra_config or {})
    return config, database_path


def test_the_runner_defers_inside_a_venue_break(tmp_path):
    """Fails on the old code, which had no gate and queued the command.

    The break is declared to cover this instant, so the boundary the runner is
    about to act on falls inside it. Nothing is queued, the deferral is named,
    and the decision is not lost: the closed bar is still the closed bar when
    the venue reopens.
    """
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=1)).replace(second=0, microsecond=0)
    resume = start + timedelta(minutes=3)
    if start.date() != resume.date():          # never straddle UTC midnight
        pytest.skip("a break declared across UTC midnight is two breaks")
    config, database_path = _runner_fixture(tmp_path, extra_config={
        "venue_breaks": [{
            "start_utc": "%02d:%02d" % (start.hour, start.minute),
            "resume_utc": "%02d:%02d" % (resume.hour, resume.minute),
            "label": "test rollover"}]})
    runner = Mt5ModelRunner(config)
    try:
        assert runner.venue_break_reason == SOURCE_CONFIGURED
        result = runner.tick()
        assert result["state"] == STATE_VENUE_BREAK_DEFERRED
        assert result["refusal"] == REFUSAL_VENUE_BREAK
        assert result["commands_queued"] == 0
        assert result["venue_break"]["source"] == SOURCE_CONFIGURED
        assert runner.bridge_store.connection.execute(
            "SELECT COUNT(*) FROM execution_commands").fetchone()[0] == 0
        # The heartbeat carries the deferral, so an operator can see why.
        runner.write_heartbeat(result)
    finally:
        runner.close()


def test_the_runner_issues_normally_when_no_break_is_declarable(tmp_path):
    """The mirror, so the repair cannot be "block everything": an empty record
    derives no break, the absence is recorded by name, and the command is
    queued exactly as before."""
    config, _ = _runner_fixture(tmp_path)
    runner = Mt5ModelRunner(config)
    try:
        assert VENUE_BREAK_UNDECLARED in runner.venue_break_reason
        assert runner.venue_breaks == ()
        result = runner.tick()
        assert result["state"] == "command_queued"
    finally:
        runner.close()


def test_the_runner_derives_the_break_from_its_own_store(tmp_path):
    """With no declaration, the break comes from the venue's own refusals in
    this store — and then the runner defers or proceeds on that."""
    config, database_path = _runner_fixture(tmp_path)
    store = Mt5ExecutionStore(database_path)
    try:
        store.connection.executemany(
            "INSERT INTO execution_commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?)",
            [(f"mt5-hist{index}", f"key-{index}", ACCOUNT, action, ETH, 0.01,
              1.0, 2.0, "m1", "a" * 64, "b" * 64, "c" * 64, state, created,
              created, created,
              json.dumps({"success": state == "succeeded",
                          "result_code": code}))
             for index, (action, state, created, code) in enumerate([
                 ("open_short", "failed", "2026-08-18T21:00:48+00:00",
                  MARKET_CLOSED),
                 ("open_short", "failed", "2026-08-25T21:00:58+00:00",
                  MARKET_CLOSED),
                 ("open_long", "succeeded", "2026-08-19T21:30:00+00:00", DONE),
             ])])
        store.connection.commit()
    finally:
        store.close()
    runner = Mt5ModelRunner(config)
    try:
        assert runner.venue_break_reason == SOURCE_OBSERVED_RECORD
        assert len(runner.venue_breaks) == 1
        derived = runner.venue_breaks[0]
        assert derived.as_fact()["start_utc"] == "21:00"
        assert derived.as_fact()["resume_utc"] == "21:30"
    finally:
        runner.close()
