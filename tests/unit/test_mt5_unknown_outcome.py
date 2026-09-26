"""The third MT5 outcome: an effect that was never observed.

Owner grant of 2026-09-26. The defect these tests close is composed of two
lines that were each defensible on their own:

    Mt5ExecutionStore.complete:  state = "succeeded" if payload.success else "failed"
    Mt5ExecutionStore.enqueue:   ... AND state!='failed'   # the daily budget

Together they say that a send which timed out did not happen and that the daily
entry slot it was holding is free. The position may exist. At the budget
boundary that lets a second entry through.

``test_an_unknown_effect_holds_its_daily_entry_slot`` is the test that fails on
the old behaviour: with the collapsing line in place the timed-out command is
recorded ``failed``, the budget predicate skips it and the second entry is
ADMITTED instead of refused.

Offline by construction: sockets are booby-trapped module-wide, no credential is
read, no terminal or EA is contacted, no order is placed and no real account is
named. Every broker fact is a real object built in the test and handed to the
real classifier or the real store on a temporary SQLite file.
"""
from __future__ import annotations

import ast
import json
import socket
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.broker_refusal import OPERATION_MUTATING, OPERATION_READ
from app.mt5_bridge_lab import Mt5BridgeError
from app.mt5_execution_bridge import (
    ExecutionResultPayload,
    Mt5ExecutionConfig,
    Mt5ExecutionStore,
)
from app.mt5_model_runner import durable_command_heartbeat
from app.mt5_policy_risk import (
    ACTION_OPEN_LONG,
    RULE_EFFECT_UNKNOWN_OUTSTANDING,
    OrderPolicy,
    OrderRequest,
    PolicyRefusalError,
    PositionState,
    RiskMandate,
    evaluate_order,
)
from app.mt5_unknown_outcome import (
    BUDGET_RELEASING_STATES,
    EVENT_EA_RESULT,
    EVENT_MIGRATION_CORRECTION,
    EVENT_RECONCILIATION,
    EVIDENCE_DELIVERED_WITHOUT_A_RESULT,
    EVIDENCE_NEVER_DELIVERED_TO_THE_EA,
    EVIDENCE_NO_MACHINE_CODE_REPORTED,
    EVIDENCE_READ_SIDE_OBSERVED_NO_ORDER,
    EVIDENCE_READ_SIDE_OBSERVED_ORDER,
    EVIDENCE_RECORD_CANNOT_SUPPORT_EITHER_READING,
    EVIDENCE_RESULT_CONTRADICTS_ITSELF,
    EVIDENCE_RETCODE_LEAVES_OUTCOME_UNPROVEN,
    EVIDENCE_RETCODE_PROVES_NO_ORDER,
    EVIDENCE_VENUE_REPORTED_SUCCESS,
    MUTATING_CALL_SITES,
    READ_SIDE_QUERIES,
    STATE_EFFECT_UNKNOWN,
    STATE_FAILED,
    STATE_PENDING,
    STATE_SUCCEEDED,
    HistoricalRowNotAFailure,
    Outcome,
    OrderObservation,
    ReconciliationInconclusive,
    ReconciliationIsNotARead,
    consumes_budget_slot,
    outcome_for_execution_result,
    outcome_for_historical_row,
    outcome_from_observation,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools import mt5_unknown_outcome_migration as migration  # noqa: E402

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)

#: A synthetic hex-digest fingerprint. Not an account identifier.
FINGERPRINT = "0123456789abcdef01234567"
ETH = "ETHUSD"
CAD = "USDCAD"

#: TRADE_RETCODE_TIMEOUT: the terminal cancelled the REQUEST and never learned
#: what the trade server did with it.
TIMEOUT = 10012
#: TRADE_RETCODE_INVALID_VOLUME: the server refused before placing anything.
REJECTED = 10014
#: TRADE_RETCODE_MARKET_CLOSED: also a proof that nothing was placed.
MARKET_CLOSED = 10018
#: TRADE_RETCODE_DONE.
DONE = 10009


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted in an offline test")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


def _config(tmp_path, *, budget=1):
    """A dual-symbol Demo mandate. Two routes are needed to measure the
    ACCOUNT-WIDE daily budget without the per-route rule answering first."""
    return Mt5ExecutionConfig(
        database_path=tmp_path / "mt5.sqlite", secret_env="SECRET",
        bind_host="127.0.0.1", port=8766, max_clock_skew_seconds=90,
        nonce_retention_seconds=900, stale_heartbeat_seconds=180,
        account_fingerprint=FINGERPRINT, allowed_symbols=(ETH, CAD),
        symbol_magics={ETH: 26080301, CAD: 26080302},
        require_route_identity=False, max_volume=0.1,
        max_open_commands_per_day=budget, delivery_retry_seconds=30,
    )


def _store(tmp_path, **kwargs):
    config = _config(tmp_path, **kwargs)
    return config, Mt5ExecutionStore(config.database_path)


def _enqueue(store, config, *, symbol, key):
    return store.enqueue(
        config=config, idempotency_key=key, action=ACTION_OPEN_LONG,
        symbol=symbol, volume=0.05, stop_loss=1.0, take_profit=2.0,
        model_id="m1", artifact_sha256="a" * 64, config_sha256="b" * 64,
        input_sha256="c" * 64)


def _result(command_id, retcode, message="", *, success=False):
    return ExecutionResultPayload.model_validate({
        "schema": "lts.mt5.execution_result.v1",
        "command_id": command_id, "account_fingerprint": FINGERPRINT,
        "success": success, "result_code": retcode, "message": message,
        "observed_at": NOW,
    })


def _observation(*, order_exists, query="MetaTrader5.history_orders_get",
                 **over):
    kwargs = {"query": query, "observed_at": NOW + timedelta(minutes=3),
              "order_exists": order_exists}
    kwargs.update(over)
    return OrderObservation(**kwargs)


# ============================================ the budget, and the inversion

def test_an_unknown_effect_holds_its_daily_entry_slot(tmp_path):
    """THE test. It fails on the old behaviour.

    Budget of one. The ETHUSD entry times out, so whether a position exists is
    unknown. Under the collapsing line that command was ``failed``, the budget
    predicate ``state != 'failed'`` skipped it, and the USDCAD entry below was
    ADMITTED — a second position at the budget boundary. An unknown effect must
    hold the slot, because releasing it requires proof that no order exists.
    """
    config, store = _store(tmp_path, budget=1)
    try:
        first = _enqueue(store, config, symbol=ETH, key="decision:eth")
        completed = store.complete(_result(first["command_id"], TIMEOUT,
                                           "request canceled by timeout"))
        assert completed["state"] == STATE_EFFECT_UNKNOWN
        assert completed["consumes_budget_slot"] is True

        assert store.daily_entry_slots_consumed(
            day_start="2026-01-01T00:00:00+00:00") == 1
        with pytest.raises(Mt5BridgeError, match="budget is exhausted"):
            _enqueue(store, config, symbol=CAD, key="decision:cad")
    finally:
        store.connection.close()


def test_a_proven_rejection_still_releases_the_slot(tmp_path):
    """The mirror, so the fix is not "block everything". An invalid volume is
    the venue proving it placed nothing: that slot was never consumed."""
    config, store = _store(tmp_path, budget=1)
    try:
        first = _enqueue(store, config, symbol=ETH, key="decision:eth")
        completed = store.complete(_result(first["command_id"], REJECTED,
                                           "invalid volume"))
        assert completed["state"] == STATE_FAILED
        assert completed["evidence"] == EVIDENCE_RETCODE_PROVES_NO_ORDER
        assert completed["consumes_budget_slot"] is False
        assert store.daily_entry_slots_consumed(
            day_start="2026-01-01T00:00:00+00:00") == 0
        admitted = _enqueue(store, config, symbol=CAD, key="decision:cad")
        assert admitted["state"] == STATE_PENDING
    finally:
        store.connection.close()


def test_a_succeeded_entry_never_releases_its_slot(tmp_path):
    config, store = _store(tmp_path, budget=1)
    try:
        first = _enqueue(store, config, symbol=ETH, key="decision:eth")
        assert store.complete(
            _result(first["command_id"], DONE, "done", success=True)
        )["state"] == STATE_SUCCEEDED
        with pytest.raises(Mt5BridgeError, match="budget is exhausted"):
            _enqueue(store, config, symbol=CAD, key="decision:cad")
    finally:
        store.connection.close()


def test_the_slot_predicate_denies_by_default():
    """The direction of the predicate IS the fix. The question is not "do we
    know it failed?" but "can we prove no order exists?", so anything
    unrecognised holds the slot."""
    assert BUDGET_RELEASING_STATES == {STATE_FAILED}
    assert consumes_budget_slot(STATE_EFFECT_UNKNOWN) is True
    assert consumes_budget_slot(STATE_SUCCEEDED) is True
    assert consumes_budget_slot(STATE_PENDING) is True
    assert consumes_budget_slot("a_state_from_a_later_release") is True
    assert consumes_budget_slot(None) is True
    assert consumes_budget_slot(STATE_FAILED) is False


def test_unknown_is_a_stored_value_of_its_own_and_not_a_flag_on_failed(tmp_path):
    config, store = _store(tmp_path, budget=3)
    try:
        command = _enqueue(store, config, symbol=ETH, key="decision:eth")
        store.complete(_result(command["command_id"], TIMEOUT, "timeout"))
        stored = store.connection.execute(
            "SELECT state FROM execution_commands WHERE command_id=?",
            (command["command_id"],)).fetchone()[0]
        assert stored == STATE_EFFECT_UNKNOWN
        assert stored not in {STATE_FAILED, STATE_SUCCEEDED}
        assert store.command_counts() == {STATE_EFFECT_UNKNOWN: 1}
        assert store.effective_state(command["command_id"]) == (
            STATE_EFFECT_UNKNOWN)
    finally:
        store.connection.close()


def test_an_unknown_effect_blocks_new_risk_on_its_own_route(tmp_path):
    config, store = _store(tmp_path, budget=5)
    try:
        command = _enqueue(store, config, symbol=ETH, key="decision:eth")
        store.complete(_result(command["command_id"], TIMEOUT, "timeout"))
        with pytest.raises(Mt5BridgeError, match="never been observed"):
            _enqueue(store, config, symbol=ETH, key="decision:eth:2")
    finally:
        store.connection.close()


# =============================================== reconciliation is the only exit

def test_only_a_read_side_observation_of_absence_releases_the_slot(tmp_path):
    config, store = _store(tmp_path, budget=1)
    try:
        command = _enqueue(store, config, symbol=ETH, key="decision:eth")
        store.complete(_result(command["command_id"], TIMEOUT, "timeout"))
        with pytest.raises(Mt5BridgeError, match="budget is exhausted"):
            _enqueue(store, config, symbol=CAD, key="decision:cad")

        exit_ = store.reconcile_unknown_effect(
            command_id=command["command_id"],
            account_fingerprint=FINGERPRINT,
            observation=_observation(order_exists=False))
        assert exit_["previous_state"] == STATE_EFFECT_UNKNOWN
        assert exit_["state"] == STATE_FAILED
        assert exit_["evidence"] == EVIDENCE_READ_SIDE_OBSERVED_NO_ORDER
        assert exit_["consumes_budget_slot"] is False

        assert store.effective_state(command["command_id"]) == STATE_FAILED
        assert _enqueue(store, config, symbol=CAD,
                        key="decision:cad")["state"] == STATE_PENDING
    finally:
        store.connection.close()


def test_a_read_side_observation_of_the_order_makes_it_a_success(tmp_path):
    config, store = _store(tmp_path, budget=1)
    try:
        command = _enqueue(store, config, symbol=ETH, key="decision:eth")
        store.complete(_result(command["command_id"], TIMEOUT, "timeout"))
        exit_ = store.reconcile_unknown_effect(
            command_id=command["command_id"],
            account_fingerprint=FINGERPRINT,
            observation=_observation(order_exists=True,
                                     broker_reference="40217543"))
        assert exit_["state"] == STATE_SUCCEEDED
        assert exit_["evidence"] == EVIDENCE_READ_SIDE_OBSERVED_ORDER
        # the slot stays spent: the order exists
        with pytest.raises(Mt5BridgeError, match="budget is exhausted"):
            _enqueue(store, config, symbol=CAD, key="decision:cad")
    finally:
        store.connection.close()


def test_an_unanswered_query_leaves_the_command_unknown(tmp_path):
    """The absence of a confirmation is not an observation of absence."""
    config, store = _store(tmp_path, budget=1)
    try:
        command = _enqueue(store, config, symbol=ETH, key="decision:eth")
        store.complete(_result(command["command_id"], TIMEOUT, "timeout"))
        with pytest.raises(ReconciliationInconclusive):
            store.reconcile_unknown_effect(
                command_id=command["command_id"],
                account_fingerprint=FINGERPRINT,
                observation=_observation(order_exists=None))
        assert store.effective_state(command["command_id"]) == (
            STATE_EFFECT_UNKNOWN)
        assert len(store.outcome_history(command["command_id"])) == 1
        with pytest.raises(Mt5BridgeError, match="budget is exhausted"):
            _enqueue(store, config, symbol=CAD, key="decision:cad")
    finally:
        store.connection.close()


@pytest.mark.parametrize("query", sorted(MUTATING_CALL_SITES))
def test_a_mutating_call_site_can_never_witness_its_own_effect(query):
    with pytest.raises(ReconciliationIsNotARead):
        _observation(order_exists=False, query=query)


def test_a_reconciliation_declared_mutating_refuses_to_exist():
    with pytest.raises(ReconciliationIsNotARead):
        _observation(order_exists=False, operation=OPERATION_MUTATING)


def test_an_undeclared_query_is_not_an_observation():
    with pytest.raises(ReconciliationIsNotARead):
        _observation(order_exists=False, query="the operator looked at the app")


@pytest.mark.parametrize("query", sorted(READ_SIDE_QUERIES))
def test_every_declared_read_query_can_reconcile(query):
    outcome = outcome_from_observation(
        _observation(order_exists=False, query=query))
    assert outcome.state == STATE_FAILED
    assert outcome.event_kind == EVENT_RECONCILIATION


def test_an_observation_without_a_real_timestamp_refuses():
    with pytest.raises(ValueError, match="timezone"):
        _observation(order_exists=True,
                     observed_at=datetime(2026, 9, 26, 12, 0))
    with pytest.raises(ValueError, match="when it was observed"):
        _observation(order_exists=True, observed_at="2026-09-26T12:00:00+00:00")


def test_a_retry_is_not_an_exit_from_unknown(tmp_path):
    """A second result for a terminal command is a duplicate or a collision; it
    is never a relabelling. Nothing but a read-side observation moves the
    state."""
    config, store = _store(tmp_path, budget=3)
    try:
        command = _enqueue(store, config, symbol=ETH, key="decision:eth")
        first = _result(command["command_id"], TIMEOUT, "timeout")
        store.complete(first)
        again = store.complete(first)
        assert again["duplicate"] is True
        assert again["state"] == STATE_EFFECT_UNKNOWN
        with pytest.raises(Mt5BridgeError, match="identity collision"):
            store.complete(_result(command["command_id"], DONE, "done",
                                   success=True))
        assert store.effective_state(command["command_id"]) == (
            STATE_EFFECT_UNKNOWN)
    finally:
        store.connection.close()


def test_only_an_unknown_effect_can_be_reconciled(tmp_path):
    config, store = _store(tmp_path, budget=3)
    try:
        command = _enqueue(store, config, symbol=ETH, key="decision:eth")
        store.complete(_result(command["command_id"], REJECTED, "invalid"))
        with pytest.raises(Mt5BridgeError, match="effect is unknown"):
            store.reconcile_unknown_effect(
                command_id=command["command_id"],
                account_fingerprint=FINGERPRINT,
                observation=_observation(order_exists=True))
        with pytest.raises(Mt5BridgeError, match="Unknown MT5 command"):
            store.reconcile_unknown_effect(
                command_id="mt5-does-not-exist",
                account_fingerprint=FINGERPRINT,
                observation=_observation(order_exists=True))
    finally:
        store.connection.close()


def test_the_exit_is_a_separate_appended_event_and_rewrites_nothing(tmp_path):
    config, store = _store(tmp_path, budget=3)
    try:
        command = _enqueue(store, config, symbol=ETH, key="decision:eth")
        store.complete(_result(command["command_id"], TIMEOUT, "timeout"))
        store.reconcile_unknown_effect(
            command_id=command["command_id"],
            account_fingerprint=FINGERPRINT,
            observation=_observation(order_exists=False))

        history = store.outcome_history(command["command_id"])
        assert [row["outcome"] for row in history] == [STATE_EFFECT_UNKNOWN,
                                                       STATE_FAILED]
        assert [row["event_kind"] for row in history] == [EVENT_EA_RESULT,
                                                          EVENT_RECONCILIATION]
        # the unknown record is untouched and still says it consumed the slot
        assert history[0]["consumes_budget_slot"] == 1
        assert history[0]["observed_at"] is None
        # the exit carries its OWN observation timestamp, distinct from the
        # moment it was recorded
        assert history[1]["observed_at"] == (NOW + timedelta(minutes=3)).isoformat()
        assert history[1]["recorded_at"] != history[1]["observed_at"]
        assert history[1]["supersedes_state"] == STATE_EFFECT_UNKNOWN
        # and the original row was never rewritten
        assert store.connection.execute(
            "SELECT state FROM execution_commands WHERE command_id=?",
            (command["command_id"],)).fetchone()[0] == STATE_EFFECT_UNKNOWN
    finally:
        store.connection.close()


# ==================================================== the classifier itself

@pytest.mark.parametrize("retcode,state,evidence", [
    (TIMEOUT, STATE_EFFECT_UNKNOWN, EVIDENCE_RETCODE_LEAVES_OUTCOME_UNPROVEN),
    (10011, STATE_EFFECT_UNKNOWN, EVIDENCE_RETCODE_LEAVES_OUTCOME_UNPROVEN),
    (10028, STATE_EFFECT_UNKNOWN, EVIDENCE_RETCODE_LEAVES_OUTCOME_UNPROVEN),
    (10031, STATE_EFFECT_UNKNOWN, EVIDENCE_RETCODE_LEAVES_OUTCOME_UNPROVEN),
    (REJECTED, STATE_FAILED, EVIDENCE_RETCODE_PROVES_NO_ORDER),
    (MARKET_CLOSED, STATE_FAILED, EVIDENCE_RETCODE_PROVES_NO_ORDER),
    (10017, STATE_FAILED, EVIDENCE_RETCODE_PROVES_NO_ORDER),
    (0, STATE_EFFECT_UNKNOWN, EVIDENCE_NO_MACHINE_CODE_REPORTED),
])
def test_a_result_is_typed_into_three_states_from_its_code(retcode, state,
                                                           evidence):
    outcome = outcome_for_execution_result(
        {"success": False, "result_code": retcode, "message": "whatever"})
    assert (outcome.state, outcome.evidence) == (state, evidence)


def test_a_successful_result_is_a_success():
    outcome = outcome_for_execution_result(
        {"success": True, "result_code": DONE, "message": "done"})
    assert outcome.state == STATE_SUCCEEDED
    assert outcome.evidence == EVIDENCE_VENUE_REPORTED_SUCCESS


def test_a_self_contradicting_result_is_unknown_and_not_resolved():
    outcome = outcome_for_execution_result(
        {"success": False, "result_code": DONE, "message": "done"})
    assert outcome.state == STATE_EFFECT_UNKNOWN
    assert outcome.evidence == EVIDENCE_RESULT_CONTRADICTS_ITSELF


def test_an_outcome_and_its_evidence_can_never_disagree():
    with pytest.raises(ValueError, match="must agree"):
        Outcome(state=STATE_FAILED,
                evidence=EVIDENCE_RETCODE_LEAVES_OUTCOME_UNPROVEN,
                event_kind=EVENT_EA_RESULT)
    with pytest.raises(ValueError, match="must agree"):
        Outcome(state=STATE_EFFECT_UNKNOWN,
                evidence=EVIDENCE_RETCODE_PROVES_NO_ORDER,
                event_kind=EVENT_EA_RESULT)


def test_a_read_side_evidence_can_only_be_claimed_by_a_reconciliation():
    with pytest.raises(ValueError, match="reconciliation"):
        Outcome(state=STATE_FAILED,
                evidence=EVIDENCE_READ_SIDE_OBSERVED_NO_ORDER,
                event_kind=EVENT_EA_RESULT, observed_at=NOW)


def test_a_reconciliation_without_an_observation_time_refuses_to_exist():
    with pytest.raises(ValueError, match="observed at"):
        Outcome(state=STATE_FAILED,
                evidence=EVIDENCE_READ_SIDE_OBSERVED_NO_ORDER,
                event_kind=EVENT_RECONCILIATION)


# ================================================= the policy interfaces

def test_the_policy_interface_is_fed_the_corrected_counts(tmp_path):
    """``mt5_policy_risk`` already refuses new risk while an unknown effect is
    outstanding. What it could not do before is get an honest number."""
    config, store = _store(tmp_path, budget=3)
    try:
        command = _enqueue(store, config, symbol=ETH, key="decision:eth")
        store.complete(_result(command["command_id"], TIMEOUT, "timeout"))
        inputs = store.policy_inputs(config, symbol=CAD, now=NOW)
        assert inputs == {"entries_today": 1, "unknown_effects": 1,
                          "unresolved_commands_on_route": 0}

        with pytest.raises(PolicyRefusalError) as raised:
            evaluate_order(
                OrderRequest(
                    client_order_id="cid-2", account_fingerprint=FINGERPRINT,
                    symbol=CAD, action=ACTION_OPEN_LONG, volume=0.05,
                    decided_at=NOW - timedelta(seconds=5), model_id="m1",
                    artifact_sha256="a" * 64, stop_loss=1.0, take_profit=2.0),
                policy=OrderPolicy(account_fingerprint=FINGERPRINT,
                                   allowed_symbols=frozenset({ETH, CAD}),
                                   max_decision_age_seconds=60.0),
                mandate=RiskMandate(max_volume=0.1,
                                    max_open_positions_per_symbol=1,
                                    max_open_commands_per_day=3,
                                    max_daily_loss=25.0),
                now=NOW, position=PositionState.known(0.0),
                already_submitted=lambda _cid: False,
                unknown_effects=inputs["unknown_effects"],
                entries_today=inputs["entries_today"])
        assert raised.value.refusal.rule == RULE_EFFECT_UNKNOWN_OUTSTANDING
    finally:
        store.connection.close()


def test_the_model_runner_reads_an_unknown_effect_and_never_calls_it_flat(tmp_path):
    config, store = _store(tmp_path, budget=3)
    try:
        command = _enqueue(store, config, symbol=ETH, key="decision:eth")
        store.complete(_result(command["command_id"], TIMEOUT, "timeout"))
        beat = durable_command_heartbeat(
            store, account_fingerprint=FINGERPRINT,
            idempotency_key="decision:eth",
            snapshot_received_at=(NOW + timedelta(minutes=5)).isoformat(),
            positions_total=0, orders_total=0)
        assert beat["state"] == "command_effect_unknown"
        assert beat["reconciliation_required"] is True
        assert beat["command_state"] == STATE_EFFECT_UNKNOWN
        assert beat["outcome_evidence"] == (
            EVIDENCE_RETCODE_LEAVES_OUTCOME_UNPROVEN)
    finally:
        store.connection.close()


def test_the_runner_follows_a_reconciliation_rather_than_the_column(tmp_path):
    config, store = _store(tmp_path, budget=3)
    try:
        command = _enqueue(store, config, symbol=ETH, key="decision:eth")
        store.complete(_result(command["command_id"], TIMEOUT, "timeout"))
        store.reconcile_unknown_effect(
            command_id=command["command_id"],
            account_fingerprint=FINGERPRINT,
            observation=_observation(order_exists=False))
        beat = durable_command_heartbeat(
            store, account_fingerprint=FINGERPRINT,
            idempotency_key="decision:eth",
            snapshot_received_at=(NOW + timedelta(minutes=5)).isoformat(),
            positions_total=0, orders_total=0)
        assert beat["state"] == "command_failed"
    finally:
        store.connection.close()


def test_the_status_report_names_every_unreconciled_effect(tmp_path):
    config, store = _store(tmp_path, budget=3)
    try:
        command = _enqueue(store, config, symbol=ETH, key="decision:eth")
        store.complete(_result(command["command_id"], TIMEOUT, "timeout"))
        unknown = store.unreconciled_unknown_effects(FINGERPRINT)
        assert [row["command_id"] for row in unknown] == [command["command_id"]]
        assert unknown[0]["evidence"] == EVIDENCE_RETCODE_LEAVES_OUTCOME_UNPROVEN
        assert store.unreconciled_unknown_effects("ffffffffffffffff") == []
    finally:
        store.connection.close()


# ======================================================== the migration

#: (idempotency_key, state, delivered_at, result_json) for one historical row,
#: written exactly as the old service would have written it.
_DAY = "2026-09-20T09:00:00+00:00"
_DELIVERED = "2026-09-20T09:00:05+00:00"


def _historical(store, key, state, delivered_at, result_json,
                symbol=ETH, action=ACTION_OPEN_LONG):
    command_id = "mt5-" + key
    store.connection.execute(
        "INSERT INTO execution_commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (command_id, key, FINGERPRINT, action, symbol, 0.05, 1.0, 2.0, "m1",
         "a" * 64, "b" * 64, "c" * 64, state, _DAY, delivered_at,
         None if state in {"pending", "delivered"} else _DELIVERED,
         result_json),
    )
    store.connection.commit()
    return command_id


def _payload_json(**over):
    value = {"schema": "lts.mt5.execution_result.v1", "success": False,
             "result_code": TIMEOUT, "message": "request canceled by timeout"}
    value.update(over)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _corpus(tmp_path):
    """Nine historical rows, one per shape the old service could leave."""
    config, store = _store(tmp_path, budget=24)
    ids = {
        "timeout": _historical(store, "h-timeout", STATE_FAILED, _DELIVERED,
                               _payload_json()),
        "closed": _historical(store, "h-closed", STATE_FAILED, _DELIVERED,
                              _payload_json(result_code=MARKET_CLOSED,
                                            message="market closed")),
        "never_delivered": _historical(store, "h-undelivered", STATE_FAILED,
                                       None, None),
        "delivered_no_result": _historical(store, "h-noresult", STATE_FAILED,
                                           _DELIVERED, None),
        "broken_json": _historical(store, "h-broken", STATE_FAILED, _DELIVERED,
                                   '{"success": fals'),
        "contradiction": _historical(store, "h-contra", STATE_FAILED,
                                     _DELIVERED,
                                     _payload_json(success=True,
                                                   result_code=DONE)),
        "no_code": _historical(store, "h-nocode", STATE_FAILED, _DELIVERED,
                               _payload_json(result_code=0)),
        "succeeded": _historical(store, "h-ok", STATE_SUCCEEDED, _DELIVERED,
                                 _payload_json(success=True,
                                               result_code=DONE)),
        "pending": _historical(store, "h-pending", STATE_PENDING, None, None),
    }
    return config, store, ids


def test_the_migration_reads_every_historical_shape(tmp_path):
    config, store, ids = _corpus(tmp_path)
    try:
        verdicts = {v.command_id: v for v in migration.plan(store.connection)}
        expected = {
            ids["timeout"]: (STATE_EFFECT_UNKNOWN,
                             EVIDENCE_RETCODE_LEAVES_OUTCOME_UNPROVEN,
                             migration.ACTION_RELABEL_UNKNOWN),
            ids["closed"]: (STATE_FAILED, EVIDENCE_RETCODE_PROVES_NO_ORDER,
                            migration.ACTION_CONFIRM_FAILED),
            ids["never_delivered"]: (STATE_FAILED,
                                     EVIDENCE_NEVER_DELIVERED_TO_THE_EA,
                                     migration.ACTION_CONFIRM_FAILED),
            ids["delivered_no_result"]: (STATE_EFFECT_UNKNOWN,
                                         EVIDENCE_DELIVERED_WITHOUT_A_RESULT,
                                         migration.ACTION_RELABEL_UNKNOWN),
            ids["broken_json"]: (
                STATE_EFFECT_UNKNOWN,
                EVIDENCE_RECORD_CANNOT_SUPPORT_EITHER_READING,
                migration.ACTION_RELABEL_UNKNOWN),
            ids["contradiction"]: (STATE_EFFECT_UNKNOWN,
                                   EVIDENCE_RESULT_CONTRADICTS_ITSELF,
                                   migration.ACTION_RELABEL_UNKNOWN),
            ids["no_code"]: (STATE_EFFECT_UNKNOWN,
                             EVIDENCE_NO_MACHINE_CODE_REPORTED,
                             migration.ACTION_RELABEL_UNKNOWN),
        }
        for command_id, (state, evidence, action) in expected.items():
            verdict = verdicts[command_id]
            assert (verdict.proposed_state, verdict.evidence,
                    verdict.migration_action) == (state, evidence, action)
        assert verdicts[ids["succeeded"]].migration_action == (
            migration.ACTION_OUT_OF_SCOPE)
        assert verdicts[ids["pending"]].migration_action == (
            migration.ACTION_OUT_OF_SCOPE)
    finally:
        store.connection.close()


def test_the_dry_run_changes_nothing(tmp_path, capsys):
    config, store, ids = _corpus(tmp_path)
    before = store.command_counts()
    store.connection.close()

    report = tmp_path / "dry.json"
    assert migration.main(["--database", str(config.database_path),
                           "--json-report", str(report)]) == 0
    printed = capsys.readouterr().out
    assert "DRY RUN (nothing written)" in printed
    assert "WOULD be appended" in printed

    check = Mt5ExecutionStore(config.database_path)
    try:
        assert check.command_counts() == before
        assert check.connection.execute(
            "SELECT COUNT(*) FROM execution_command_outcomes"
        ).fetchone()[0] == 0
    finally:
        check.connection.close()
    published = json.loads(report.read_text(encoding="utf-8"))
    assert published["mode"] == "dry_run"
    assert published["counts"]["by_migration_action"][
        migration.ACTION_RELABEL_UNKNOWN] == 5
    assert published["counts"]["entry_budget_slots_reclaimed"] == 5


def test_the_migration_is_idempotent_and_append_only(tmp_path, capsys):
    config, store, ids = _corpus(tmp_path)
    store.connection.close()
    args = ["--database", str(config.database_path), "--apply",
            "--attest-bridge-stopped"]

    assert migration.main(args) == 0
    assert "appended 5 correcting record(s)" in capsys.readouterr().out

    check = Mt5ExecutionStore(config.database_path)
    try:
        assert check.effective_state(ids["timeout"]) == STATE_EFFECT_UNKNOWN
        assert check.effective_state(ids["closed"]) == STATE_FAILED
        assert check.effective_state(ids["never_delivered"]) == STATE_FAILED
        assert check.effective_state(ids["delivered_no_result"]) == (
            STATE_EFFECT_UNKNOWN)
        # nothing was rewritten: the old label is still in the column
        assert check.connection.execute(
            "SELECT state FROM execution_commands WHERE command_id=?",
            (ids["timeout"],)).fetchone()[0] == STATE_FAILED
        # and the correcting record says WHY, in the row itself
        broken = check.outcome_history(ids["broken_json"])
        assert len(broken) == 1
        assert broken[0]["evidence"] == (
            EVIDENCE_RECORD_CANNOT_SUPPORT_EITHER_READING)
        assert broken[0]["event_kind"] == EVENT_MIGRATION_CORRECTION
        detail = json.loads(broken[0]["outcome_json"])["detail"]
        assert "not readable as JSON" in detail["reading"]
        assert "not evidence that nothing was sent" in detail["ruling"]
        rows_after_first = check.connection.execute(
            "SELECT COUNT(*) FROM execution_command_outcomes").fetchone()[0]
    finally:
        check.connection.close()

    assert migration.main(args) == 0
    second = capsys.readouterr().out
    assert "appended 0 correcting record(s)" in second
    assert "No correction is required" in second
    again = Mt5ExecutionStore(config.database_path)
    try:
        assert again.connection.execute(
            "SELECT COUNT(*) FROM execution_command_outcomes"
        ).fetchone()[0] == rows_after_first
        verdicts = migration.plan(again.connection)
        actions = {v.migration_action for v in verdicts}
        assert migration.ACTION_RELABEL_UNKNOWN not in actions
    finally:
        again.connection.close()


def test_the_migrated_unknowns_hold_their_budget_slots(tmp_path):
    """The point of the migration: five historical rows that were freeing a
    slot each now hold one."""
    config, store, ids = _corpus(tmp_path)
    try:
        before = store.daily_entry_slots_consumed(day_start=_DAY)
    finally:
        store.connection.close()
    assert migration.main(["--database", str(config.database_path), "--apply",
                           "--attest-bridge-stopped"]) == 0
    after_store = Mt5ExecutionStore(config.database_path)
    try:
        after = after_store.daily_entry_slots_consumed(day_start=_DAY)
    finally:
        after_store.connection.close()
    # only the succeeded and the pending row held a slot: every ambiguous
    # row was recorded ``failed`` and was therefore freeing one
    assert before == 2
    assert after == before + 5


def test_apply_requires_the_operator_attestation(tmp_path):
    config, store, _ids = _corpus(tmp_path)
    store.connection.close()
    with pytest.raises(SystemExit):
        migration.main(["--database", str(config.database_path), "--apply"])


def test_the_migration_refuses_a_row_it_has_no_ruling_about():
    with pytest.raises(HistoricalRowNotAFailure):
        outcome_for_historical_row({"state": STATE_SUCCEEDED,
                                    "result_json": None})


def test_the_dry_run_opens_the_database_read_only(tmp_path, monkeypatch):
    """Belt and braces: the dry path is proved read-only by making every write
    on its connection fail."""
    config, store, _ids = _corpus(tmp_path)
    store.connection.close()
    real_connect = sqlite3.connect
    seen: list[str] = []

    def _connect(target, *args, **kwargs):
        seen.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(migration.sqlite3, "connect", _connect)
    assert migration.main(["--database", str(config.database_path)]) == 0
    assert seen and all("mode=ro" in target for target in seen)


# ================================================= discipline of the module

def _imports_of(relative):
    source = Path(__file__).resolve().parents[2] / relative
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    return imported


def test_the_outcome_module_imports_nothing_that_can_reach_a_network():
    assert _imports_of("app/mt5_unknown_outcome.py") == {
        "__future__", "json", "dataclasses", "datetime", "typing",
        "app.broker_refusal", "app.mt5_refusal",
    }


@pytest.mark.parametrize("relative", [
    "app/mt5_unknown_outcome.py",
    "tools/mt5_unknown_outcome_migration.py",
])
def test_no_outcome_is_ever_decided_by_reading_a_message(relative):
    """The same AST guard ``broker_refusal`` and ``mt5_refusal`` keep. Every
    verdict here comes from a machine code, a boolean the venue set, the
    presence of a structured field or the call site it came from."""
    source = Path(__file__).resolve().parents[2] / relative
    tree = ast.parse(source.read_text(encoding="utf-8"))
    forbidden = {"lower", "upper", "startswith", "endswith", "find", "search",
                 "match", "split", "strip", "casefold"}
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & forbidden, sorted(called & forbidden)


def test_every_state_and_evidence_pair_is_declared():
    """No classification path may invent a state or an evidence token."""
    for retcode in (TIMEOUT, REJECTED, MARKET_CLOSED, 0, 19999):
        outcome = outcome_for_execution_result(
            {"success": False, "result_code": retcode, "message": ""})
        assert outcome.state in {STATE_FAILED, STATE_EFFECT_UNKNOWN}
        assert isinstance(outcome.as_fact()["consumes_budget_slot"], bool)
