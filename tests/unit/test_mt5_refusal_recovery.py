"""RP149: the MT5 Demo lane's refusal, recovery, policy and risk contract.

No socket is opened, no credential is read, no terminal or EA is contacted and
no order is placed. Sockets are booby-trapped module-wide, and every case is a
real object handed to the real classifier — a retcode the terminal would return,
a ``last_error`` pair, or a result payload of the bridge's own declared shape.
The one place a live component appears is ``Mt5ExecutionStore``, driven against a
temporary SQLite file, and it is used to pin what the service does TODAY, not to
change it.

What is being pinned:

* every refusal is TYPED and NAMED, and a venue refusal carries the venue's own
  words verbatim while a refusal of OURS carries our own declared rule;
* every recovery is DECLARED: reconcile an unknown effect, resume from the
  persisted client order id, retry a READ with its bound, or refuse;
* an unknown state is never assumed flat, and it blocks further orders;
* a duplicate client order id never places a second order;
* an entitlement failure is never retried as if it were transient;
* the SAME failure classifies differently on a read and on a mutating call, and
  a mutating call is never retried;
* classification never reads a message: the same structured facts under
  adversarial prose give an identical verdict, enforced by an AST test as well.
"""
from __future__ import annotations

import ast
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.broker_refusal import (
    KIND_AUTHENTICATION_FAILURE,
    KIND_DUPLICATE_CLIENT_ORDER_ID,
    KIND_INSTRUMENT_NOT_PERMITTED,
    KIND_INSUFFICIENT_ENTITLEMENT,
    KIND_MARKET_CLOSED,
    KIND_STALE_QUOTE,
    KIND_TRANSPORT_FAILURE,
    KIND_VENUE_REJECTION,
    KINDS,
    OPERATION_MUTATING,
    OPERATION_READ,
    RECOVERY_RECONCILE_UNKNOWN_FILL,
    RECOVERY_REFUSE_TO_PROCEED,
    RECOVERY_RESUME_FROM_PERSISTED_CLIENT_ORDER_ID,
    RECOVERY_RETRY_WITH_BOUND,
    BrokerRefusal,
)
from app.mt5_bridge_lab import Mt5BridgeError
from app.mt5_execution_bridge import (
    ExecutionResultPayload,
    Mt5ExecutionConfig,
    Mt5ExecutionStore,
)
from app.mt5_unknown_outcome import OrderObservation
from app.mt5_policy_risk import (
    ACTION_CLOSE,
    ACTION_OPEN_LONG,
    ACTION_OPEN_SHORT,
    POLICY_RULES,
    RISK_RULES,
    RULE_ACCOUNT_NOT_THE_MANDATED_ONE,
    RULE_ACTION_NOT_DECLARED,
    RULE_CLIENT_ORDER_ID_MISSING,
    RULE_DAILY_ENTRY_BUDGET_EXHAUSTED,
    RULE_DAILY_LOSS_LIMIT_REACHED,
    RULE_DECISION_STALE,
    RULE_DUPLICATE_CLIENT_ORDER_ID,
    RULE_EFFECT_UNKNOWN_OUTSTANDING,
    RULE_MODEL_EVIDENCE_MISSING,
    RULE_OPEN_POSITIONS_AT_CAP,
    RULE_POSITION_STATE_UNKNOWN,
    RULE_PROTECTIVE_BRACKET_INVERTED,
    RULE_PROTECTIVE_BRACKET_MISSING,
    RULE_SYMBOL_OUTSIDE_MANDATE,
    RULE_UNRESOLVED_COMMAND_ON_ROUTE,
    RULE_VOLUME_ABOVE_CAP,
    RULE_VOLUME_NOT_POSITIVE,
    RULES,
    MandateGrant,
    OrderPolicy,
    OrderRequest,
    PolicyRefusal,
    PolicyRefusalError,
    PositionState,
    RiskMandate,
    evaluate_order,
)
from app.mt5_refusal import (
    MT5_ENTITLEMENT_RETCODES,
    MT5_RETCODE_KINDS,
    MT5_SUCCESS_RETCODES,
    MT5_TERMINAL_ERROR_KINDS,
    MT5_UNPROVEN_ON_A_MUTATING_CALL,
    NO_CODE_REPORTED,
    VENUE,
    Mt5ContradictoryResult,
    Mt5SuccessIsNotARefusal,
    classify_mt5_retcode,
    classify_mt5_terminal_error,
    classify_order_send_returned_nothing,
    command_state_for,
    refusal_for_execution_result,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)

#: A synthetic fingerprint. It is a hex digest shape, not an account identifier,
#: and no real account is named anywhere in this file.
FINGERPRINT = "a1b2c3d4e5f60718"
SYMBOL = "EURUSD"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted in an offline test")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


# ============================================================ the retcodes

def test_every_mapped_retcode_types_to_a_declared_kind():
    assert set(MT5_RETCODE_KINDS.values()) <= KINDS
    assert set(MT5_TERMINAL_ERROR_KINDS.values()) <= KINDS
    assert not MT5_SUCCESS_RETCODES & set(MT5_RETCODE_KINDS), \
        "a success code must not appear in the refusal table at all"


@pytest.mark.parametrize("retcode,kind", [
    (10018, KIND_MARKET_CLOSED),          # MARKET_CLOSED
    (10017, KIND_INSUFFICIENT_ENTITLEMENT),   # TRADE_DISABLED
    (10032, KIND_INSUFFICIENT_ENTITLEMENT),   # ONLY_REAL
    (10014, KIND_VENUE_REJECTION),        # INVALID_VOLUME
    (10019, KIND_VENUE_REJECTION),        # NO_MONEY
    (10004, KIND_STALE_QUOTE),            # REQUOTE
    (10020, KIND_STALE_QUOTE),            # PRICE_CHANGED
    (10042, KIND_INSTRUMENT_NOT_PERMITTED),   # LONG_ONLY
    (10046, KIND_INSTRUMENT_NOT_PERMITTED),   # HEDGE_PROHIBITED
    (10039, KIND_DUPLICATE_CLIENT_ORDER_ID),  # CLOSE_ORDER_EXIST
    (10012, KIND_TRANSPORT_FAILURE),      # TIMEOUT
    (10031, KIND_TRANSPORT_FAILURE),      # CONNECTION
])
def test_a_retcode_is_typed_from_the_integer_alone(retcode, kind):
    refusal = classify_mt5_retcode(retcode=retcode, venue_message="anything at all")
    assert isinstance(refusal, BrokerRefusal)
    assert refusal.kind == kind
    assert refusal.venue == VENUE
    assert refusal.venue_code == retcode
    assert refusal.venue_reason == "anything at all", "the venue's words survive verbatim"


def test_an_unlisted_retcode_is_a_fail_closed_rejection():
    refusal = classify_mt5_retcode(retcode=19999, venue_message="a code nobody mapped")
    assert refusal.kind == KIND_VENUE_REJECTION
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED


@pytest.mark.parametrize("retcode", sorted(MT5_SUCCESS_RETCODES))
def test_a_success_code_is_never_typed_as_a_refusal(retcode):
    with pytest.raises(Mt5SuccessIsNotARefusal):
        classify_mt5_retcode(retcode=retcode, venue_message="done")


def test_a_refusal_with_no_message_still_names_something():
    refusal = classify_mt5_retcode(retcode=10018, venue_message="")
    assert refusal.venue_reason == "TRADE_RETCODE 10018"


# ------------------------------------------ the read versus mutating axis

@pytest.mark.parametrize("retcode", sorted(MT5_UNPROVEN_ON_A_MUTATING_CALL))
def test_the_same_code_retries_a_read_and_is_reconciled_on_a_mutating_call(retcode):
    read = classify_mt5_retcode(retcode=retcode, venue_message="no connection",
                                operation=OPERATION_READ)
    assert read.recovery == RECOVERY_RETRY_WITH_BOUND
    assert read.retry_bound is not None and read.state_is_unknown is False

    write = classify_mt5_retcode(retcode=retcode, venue_message="no connection",
                                 operation=OPERATION_MUTATING)
    assert write.recovery == RECOVERY_RECONCILE_UNKNOWN_FILL
    assert write.state_is_unknown is True
    assert write.blocks_new_orders is True
    assert write.is_transient is False


def test_a_timed_out_order_is_never_recorded_as_a_failure_that_placed_nothing():
    """TRADE_RETCODE_TIMEOUT is the case the whole lane turns on: the terminal
    cancelled the REQUEST, and whether the server had already acted is unknown."""
    refusal = classify_mt5_retcode(retcode=10012, venue_message="request canceled by timeout",
                                   operation=OPERATION_MUTATING)
    assert command_state_for(refusal) == "effect_unknown"
    rejected = classify_mt5_retcode(retcode=10014, venue_message="invalid volume",
                                    operation=OPERATION_MUTATING)
    assert command_state_for(rejected) == "failed", "a rejection PROVES nothing was placed"
    assert rejected.state_is_unknown is False


@pytest.mark.parametrize("retcode", [10004, 10020, 10021])
def test_a_requote_defers_a_read_and_refuses_a_submission(retcode):
    """A quote-side refusal PROVES the order was not executed at the price asked
    for, so there is nothing to reconcile -- and a submission is still never
    retried, because the decision has to be re-made against the new price."""
    read = classify_mt5_retcode(retcode=retcode, venue_message="requote",
                                operation=OPERATION_READ)
    assert read.kind == KIND_STALE_QUOTE
    assert read.recovery == RECOVERY_RETRY_WITH_BOUND and read.retry_bound is not None

    write = classify_mt5_retcode(retcode=retcode, venue_message="requote",
                                 operation=OPERATION_MUTATING)
    assert write.kind == KIND_STALE_QUOTE
    assert write.recovery == RECOVERY_REFUSE_TO_PROCEED
    assert write.state_is_unknown is False, "the price was refused; nothing was placed"
    assert write.blocks_new_orders is True
    assert write.venue_code == retcode


def test_a_mutating_refusal_that_proves_nothing_was_placed_still_blocks_new_orders():
    refusal = classify_mt5_retcode(retcode=10019, venue_message="not enough money",
                                   operation=OPERATION_MUTATING)
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED
    assert refusal.blocks_new_orders is True


# ------------------------------------------------------- entitlement never retried

@pytest.mark.parametrize("retcode", sorted(MT5_ENTITLEMENT_RETCODES))
@pytest.mark.parametrize("operation", [OPERATION_READ, OPERATION_MUTATING])
def test_an_entitlement_refusal_is_never_retried_on_either_axis(retcode, operation):
    refusal = classify_mt5_retcode(retcode=retcode, venue_message="autotrading disabled",
                                   operation=operation)
    assert refusal.kind == KIND_INSUFFICIENT_ENTITLEMENT
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED
    assert refusal.retry_bound is None
    assert refusal.is_transient is False


def test_the_only_real_accounts_refusal_is_an_entitlement_and_not_an_invitation():
    """TRADE_RETCODE_ONLY_REAL says the operation needs a live account. The
    refusal is terminal: this lane has no live counterpart to escalate to."""
    refusal = classify_mt5_retcode(retcode=10032,
                                   venue_message="operation allowed only for live accounts",
                                   operation=OPERATION_MUTATING)
    assert refusal.kind == KIND_INSUFFICIENT_ENTITLEMENT
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED
    assert refusal.venue == VENUE == "mt5_demo"


# ------------------------------------------------- the terminal's own errors

@pytest.mark.parametrize("code,kind", [
    (-6, KIND_AUTHENTICATION_FAILURE),        # RES_E_AUTH_FAILED
    (-8, KIND_INSUFFICIENT_ENTITLEMENT),      # RES_E_AUTO_TRADING_DISABLED
    (-2, KIND_VENUE_REJECTION),               # RES_E_INVALID_PARAMS
    (-10001, KIND_TRANSPORT_FAILURE),         # RES_E_INTERNAL_FAIL_SEND
    (-10002, KIND_TRANSPORT_FAILURE),         # RES_E_INTERNAL_FAIL_RECEIVE
    (-10005, KIND_TRANSPORT_FAILURE),         # RES_E_INTERNAL_FAIL_TIMEOUT
])
def test_a_last_error_pair_is_typed_from_its_code(code, kind):
    refusal = classify_mt5_terminal_error(error_code=code, error_text="terminal said so")
    assert refusal.kind == kind
    assert refusal.venue_code == code
    assert refusal.venue_reason == "terminal said so"
    assert refusal.detail["source"] == "MetaTrader5.last_error"


def test_res_s_ok_is_not_a_refusal():
    with pytest.raises(Mt5SuccessIsNotARefusal):
        classify_mt5_terminal_error(error_code=1, error_text="ok")


def test_an_unlisted_terminal_error_is_fail_closed():
    refusal = classify_mt5_terminal_error(error_code=-424242, error_text="?")
    assert refusal.kind == KIND_VENUE_REJECTION


def test_order_send_returning_nothing_is_always_unknown_and_always_blocks():
    refusal = classify_order_send_returned_nothing(client_order_id="cid-1")
    assert refusal.operation == OPERATION_MUTATING
    assert refusal.state_is_unknown is True
    assert refusal.blocks_new_orders is True
    assert refusal.recovery == RECOVERY_RECONCILE_UNKNOWN_FILL
    assert refusal.venue_reason == NO_CODE_REPORTED
    assert refusal.client_order_id == "cid-1"


def test_order_send_returning_nothing_with_a_last_error_uses_that_code():
    refusal = classify_order_send_returned_nothing(error_code=-10002,
                                                   error_text="receive failed")
    assert refusal.venue_code == -10002
    assert refusal.state_is_unknown is True
    assert refusal.venue_reason == "receive failed"


def test_an_authentication_failure_on_a_mutating_call_is_not_reconciled_as_unknown():
    """It never reached the trade server, so there is nothing to reconcile — and
    it is still not retried."""
    refusal = classify_order_send_returned_nothing(error_code=-6, error_text="auth failed")
    assert refusal.kind == KIND_AUTHENTICATION_FAILURE
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED
    assert refusal.state_is_unknown is False
    assert refusal.blocks_new_orders is True


# ------------------------------------------- the bridge's own result payload

def _result(**over):
    payload = {"schema": "lts.mt5.execution_result.v1", "command_id": "c" * 20,
               "account_fingerprint": FINGERPRINT, "success": False,
               "result_code": 10018, "order_ticket": "", "deal_ticket": "",
               "message": "market closed"}
    payload.update(over)
    return payload


def test_a_failed_execution_result_is_typed_from_its_result_code():
    refusal = refusal_for_execution_result(_result())
    assert refusal.kind == KIND_MARKET_CLOSED
    assert refusal.venue_reason == "market closed"
    assert refusal.detail["source"] == "lts.mt5.execution_result.v1"


def test_a_successful_execution_result_is_not_a_refusal():
    with pytest.raises(Mt5SuccessIsNotARefusal):
        refusal_for_execution_result(_result(success=True, result_code=10009))


@pytest.mark.parametrize("retcode", sorted(MT5_SUCCESS_RETCODES))
def test_a_failure_carrying_a_success_code_is_a_contradiction_and_is_not_resolved(retcode):
    with pytest.raises(Mt5ContradictoryResult, match="contradiction is reported"):
        refusal_for_execution_result(_result(success=False, result_code=retcode))


@pytest.mark.parametrize("value", [None, 0])
def test_a_failed_result_with_no_code_is_unknown_and_never_a_rejection(value):
    refusal = refusal_for_execution_result(_result(result_code=value, message=""))
    assert refusal.kind == KIND_TRANSPORT_FAILURE
    assert refusal.state_is_unknown is True
    assert refusal.blocks_new_orders is True
    assert refusal.venue_reason == NO_CODE_REPORTED


@pytest.mark.parametrize("value", [None, "false", 1, 0])
def test_a_result_that_does_not_state_success_as_a_boolean_is_refused(value):
    with pytest.raises(Mt5ContradictoryResult):
        refusal_for_execution_result(_result(success=value))


def test_a_real_payload_object_of_the_bridges_own_shape_classifies():
    """Not a dict this test invented: the bridge's own pydantic model, dumped the
    way ``Mt5ExecutionStore.complete`` dumps it."""
    payload = ExecutionResultPayload(**{
        "schema": "lts.mt5.execution_result.v1", "command_id": "c" * 20,
        "account_fingerprint": FINGERPRINT, "success": False, "result_code": 10017,
        "message": "trade is disabled", "observed_at": NOW})
    refusal = refusal_for_execution_result(payload.model_dump(by_alias=True, mode="json"))
    assert refusal.kind == KIND_INSUFFICIENT_ENTITLEMENT
    assert refusal.venue_reason == "trade is disabled"
    assert refusal.is_transient is False


# ------------- what the service does TODAY, recorded rather than changed

def _bridge_config(tmp_path):
    return Mt5ExecutionConfig(
        database_path=tmp_path / "mt5.sqlite", secret_env="LTS_TEST_MT5_SECRET",
        bind_host="127.0.0.1", port=8766, max_clock_skew_seconds=90,
        nonce_retention_seconds=900, stale_heartbeat_seconds=180,
        account_fingerprint=FINGERPRINT, allowed_symbols=(SYMBOL,),
        symbol_magics={}, require_route_identity=False, max_volume=0.1,
        max_open_commands_per_day=3, delivery_retry_seconds=30)


def _payload(command_id, result_code, message):
    return ExecutionResultPayload(**{
        "schema": "lts.mt5.execution_result.v1", "command_id": command_id,
        "account_fingerprint": FINGERPRINT, "success": False,
        "result_code": result_code, "message": message, "observed_at": NOW})


def _enqueue(store, config, key):
    return store.enqueue(
        config=config, idempotency_key=key, action=ACTION_OPEN_LONG, symbol=SYMBOL,
        volume=0.05, stop_loss=1.0, take_profit=2.0, model_id="m1",
        artifact_sha256="a" * 64, config_sha256="b" * 64, input_sha256="c" * 64)


def test_the_bridge_no_longer_collapses_a_timeout_and_a_closed_market(tmp_path):
    """The gap this lane's classifier filled, now closed in the service.

    RP149 recorded the defect here and could not fix it: ``complete`` wrote
    ``state = "succeeded" if payload.success else "failed"``, so a timeout and a
    closed market landed in ONE state and the timed-out command stopped counting
    against the daily entry budget. The owner grant of 2026-09-26 made the
    classifier's verdict the state the store writes, so the two payloads now
    part company in the store exactly as they always did in the classifier.
    """
    config = _bridge_config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    try:
        timed_out = _enqueue(store, config, "decision-timeout")
        first = store.complete(_payload(timed_out["command_id"], 10012,
                                        "request canceled by timeout"))
        assert first["state"] == "effect_unknown"
        # The route is now blocked: the position may exist. The only exit is a
        # read-side observation, so the second decision cannot even be queued
        # until the terminal has been ASKED what it did.
        with pytest.raises(Mt5BridgeError, match="never been observed"):
            _enqueue(store, config, "decision-closed")
        store.reconcile_unknown_effect(
            command_id=timed_out["command_id"],
            account_fingerprint=FINGERPRINT,
            observation=OrderObservation(
                query="MetaTrader5.history_orders_get",
                observed_at=NOW + timedelta(minutes=2), order_exists=False))

        closed = _enqueue(store, config, "decision-closed")
        second = store.complete(_payload(closed["command_id"], 10018, "market is closed"))
        assert second["state"] == "failed", "a closed market PROVES nothing was placed"
        assert store.command_counts() == {"failed": 2}, \
            "the timeout only became a failure because the broker was asked"
    finally:
        store.connection.close()

    unknown = refusal_for_execution_result(_result(result_code=10012,
                                                  message="request canceled by timeout"))
    proven = refusal_for_execution_result(_result(result_code=10018,
                                                 message="market is closed"))
    assert command_state_for(unknown) != command_state_for(proven)
    assert (command_state_for(unknown), command_state_for(proven)) == ("effect_unknown", "failed")


def test_the_bridge_today_never_places_a_second_order_for_one_decision(tmp_path):
    """The one half of the duplicate rule the service already keeps: the same
    idempotency key replays the persisted command instead of enqueuing again."""
    config = _bridge_config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    try:
        first = _enqueue(store, config, "decision-1")
        replay = _enqueue(store, config, "decision-1")
        assert replay.get("replayed") is True
        assert replay["command_id"] == first["command_id"]
        counts = store.command_counts()
        assert counts == {"pending": 1}, "one command exists, not two"
    finally:
        store.connection.close()


# ================================================= the policy interface

def _policy(**over):
    kwargs = {"account_fingerprint": FINGERPRINT, "allowed_symbols": frozenset({SYMBOL}),
              "max_decision_age_seconds": 60.0}
    kwargs.update(over)
    return OrderPolicy(**kwargs)


def _mandate(**over):
    kwargs = {"max_volume": 0.1, "max_open_positions_per_symbol": 1,
              "max_open_commands_per_day": 3, "max_daily_loss": 25.0}
    kwargs.update(over)
    return RiskMandate(**kwargs)


def _request(**over):
    kwargs = {"client_order_id": "cid-1", "account_fingerprint": FINGERPRINT,
              "symbol": SYMBOL, "action": ACTION_OPEN_LONG, "volume": 0.05,
              "decided_at": NOW - timedelta(seconds=5), "model_id": "m1",
              "artifact_sha256": "a" * 64, "stop_loss": 1.0, "take_profit": 2.0}
    kwargs.update(over)
    return OrderRequest(**kwargs)


def _never_submitted(_client_order_id):
    return False


def test_a_policy_cannot_be_constructed_for_anything_but_a_demo_account():
    for kind in ("live", "real", "", "DEMO", "paper"):
        with pytest.raises(ValueError, match="Demo-only"):
            _policy(account_kind=kind)


def test_a_mandate_with_no_symbols_or_no_bound_refuses_to_exist():
    with pytest.raises(ValueError, match="admits nothing"):
        _policy(allowed_symbols=frozenset())
    with pytest.raises(ValueError, match="positive"):
        _policy(max_decision_age_seconds=0)


def test_a_well_formed_order_is_admitted_and_granted():
    admitted = _policy().admit(_request(), now=NOW, already_submitted=_never_submitted)
    assert admitted.request.client_order_id == "cid-1"
    grant = _mandate().evaluate(_request(), position=PositionState.known(0.0))
    assert isinstance(grant, MandateGrant)
    assert grant.volume_headroom == pytest.approx(0.05)
    assert grant.entries_remaining == 3


@pytest.mark.parametrize("over,rule", [
    ({"account_fingerprint": "f" * 16}, RULE_ACCOUNT_NOT_THE_MANDATED_ONE),
    ({"action": "hedge"}, RULE_ACTION_NOT_DECLARED),
    ({"symbol": "GBPJPY"}, RULE_SYMBOL_OUTSIDE_MANDATE),
    ({"client_order_id": ""}, RULE_CLIENT_ORDER_ID_MISSING),
    ({"model_id": ""}, RULE_MODEL_EVIDENCE_MISSING),
    ({"artifact_sha256": ""}, RULE_MODEL_EVIDENCE_MISSING),
    ({"stop_loss": None}, RULE_PROTECTIVE_BRACKET_MISSING),
    ({"take_profit": None}, RULE_PROTECTIVE_BRACKET_MISSING),
    ({"stop_loss": 2.0, "take_profit": 1.0}, RULE_PROTECTIVE_BRACKET_INVERTED),
    ({"decided_at": NOW - timedelta(seconds=61)}, RULE_DECISION_STALE),
])
def test_each_policy_rule_refuses_by_name(over, rule):
    with pytest.raises(PolicyRefusalError) as caught:
        _policy().admit(_request(**over), now=NOW, already_submitted=_never_submitted)
    refusal = caught.value.refusal
    assert refusal.rule == rule
    assert refusal.interface == "policy"
    assert refusal.operation == OPERATION_MUTATING
    assert refusal.blocks_new_orders is True
    assert refusal.is_transient is False


def test_a_short_orders_bracket_geometry_is_the_mirror_and_not_the_same():
    short = _request(action=ACTION_OPEN_SHORT, stop_loss=2.0, take_profit=1.0)
    assert _policy().admit(short, now=NOW, already_submitted=_never_submitted)
    with pytest.raises(PolicyRefusalError) as caught:
        _policy().admit(_request(action=ACTION_OPEN_SHORT, stop_loss=1.0, take_profit=2.0),
                        now=NOW, already_submitted=_never_submitted)
    assert caught.value.refusal.rule == RULE_PROTECTIVE_BRACKET_INVERTED


def test_a_stale_decision_carries_the_age_it_was_measured_at_and_the_bound():
    with pytest.raises(PolicyRefusalError) as caught:
        _policy().admit(_request(decided_at=NOW - timedelta(seconds=120)), now=NOW,
                        already_submitted=_never_submitted)
    refusal = caught.value.refusal
    assert refusal.measured == pytest.approx(120.0)
    assert refusal.limit == pytest.approx(60.0)


@pytest.mark.parametrize("stamp", [datetime(2026, 9, 26, 12, 0), None])
def test_an_age_from_a_naive_or_absent_timestamp_is_a_caller_fault_not_a_rule(stamp):
    with pytest.raises(ValueError, match="aware datetime"):
        _policy().admit(_request(decided_at=stamp), now=NOW,
                        already_submitted=_never_submitted)


# ------------------------------------------------ the duplicate order id rule

def test_a_duplicate_client_order_id_resumes_and_never_places_a_second_order():
    with pytest.raises(PolicyRefusalError) as caught:
        _policy().admit(_request(), now=NOW, already_submitted=lambda _cid: True)
    refusal = caught.value.refusal
    assert refusal.rule == RULE_DUPLICATE_CLIENT_ORDER_ID
    assert refusal.recovery == RECOVERY_RESUME_FROM_PERSISTED_CLIENT_ORDER_ID
    assert refusal.client_order_id == "cid-1"
    assert "placing a second order is not an option" in refusal.reason


def test_an_unanswerable_duplicate_question_is_refused_rather_than_answered_no():
    """Without the journal predicate an opening order cannot know whether its id
    is already live, and an unknown answer is not "no"."""
    with pytest.raises(PolicyRefusalError) as caught:
        _policy().admit(_request(), now=NOW, already_submitted=None)
    assert caught.value.refusal.rule == RULE_DUPLICATE_CLIENT_ORDER_ID
    assert "unanswerable" in caught.value.refusal.reason


def test_a_closing_order_does_not_need_the_journal_because_it_reduces_risk():
    close = _request(action=ACTION_CLOSE, volume=0.0, model_id="", artifact_sha256="",
                     stop_loss=None, take_profit=None)
    assert _policy().admit(close, now=NOW, already_submitted=None)


def test_no_recovery_in_this_interface_can_ever_mean_submit_again():
    with pytest.raises(ValueError, match="may not declare recovery"):
        PolicyRefusal(rule=RULE_DUPLICATE_CLIENT_ORDER_ID, reason="x",
                      recovery=RECOVERY_RETRY_WITH_BOUND, interface="policy")


# -------------------------------------------- an unknown state is not flat

def test_an_outstanding_unknown_effect_blocks_new_risk():
    with pytest.raises(PolicyRefusalError) as caught:
        _policy().admit(_request(), now=NOW, already_submitted=_never_submitted,
                        unknown_effects=1)
    refusal = caught.value.refusal
    assert refusal.rule == RULE_EFFECT_UNKNOWN_OUTSTANDING
    assert refusal.state_is_unknown is True
    assert refusal.recovery == RECOVERY_RECONCILE_UNKNOWN_FILL
    assert "an unknown position is not a flat one" in refusal.reason


def test_an_unknown_position_is_refused_before_any_number_is_compared():
    with pytest.raises(PolicyRefusalError) as caught:
        _mandate().evaluate(_request(volume=99.0),
                            position=PositionState.unknown("the route was never reconciled"))
    refusal = caught.value.refusal
    assert refusal.rule == RULE_POSITION_STATE_UNKNOWN
    assert refusal.recovery == RECOVERY_RECONCILE_UNKNOWN_FILL
    assert refusal.state_is_unknown is True
    assert "never read as flat" in refusal.reason
    assert refusal.rule != RULE_VOLUME_ABOVE_CAP, \
        "the volume is meaningless while the exposure is unknown"


@pytest.mark.parametrize("fact", [None, {}, {"units": None}, {"units": "0"}, {"units": True}])
def test_a_position_fact_that_does_not_carry_a_number_is_unknown_and_never_zero(fact):
    state = PositionState.from_fact(fact)
    assert state.is_known is False
    assert state.units is None
    assert state.reason


def test_a_position_fact_that_carries_a_number_is_known_including_zero():
    assert PositionState.from_fact({"units": 0}).is_known is True
    assert PositionState.from_fact({"units": 0}).units == 0.0
    assert PositionState.from_fact({"units": -2.5}).units == pytest.approx(-2.5)


def test_a_known_state_without_units_and_an_unknown_state_with_units_cannot_exist():
    with pytest.raises(ValueError, match="never zero"):
        PositionState(certainty="known")
    with pytest.raises(ValueError, match="carries no units"):
        PositionState(certainty="unknown", units=0.0, reason="r")
    with pytest.raises(ValueError, match="must name why"):
        PositionState(certainty="unknown")


def test_an_unknown_state_refusal_can_never_be_declared_non_blocking():
    with pytest.raises(ValueError, match="not a refusal"):
        PolicyRefusal(rule=RULE_POSITION_STATE_UNKNOWN, reason="x",
                      recovery=RECOVERY_RECONCILE_UNKNOWN_FILL, interface="risk",
                      state_is_unknown=True, blocks_new_orders=False)


def test_a_rule_and_its_unknown_flag_can_never_disagree():
    with pytest.raises(ValueError, match="disagree about whether an effect is unproven"):
        PolicyRefusal(rule=RULE_POSITION_STATE_UNKNOWN, reason="x",
                      recovery=RECOVERY_RECONCILE_UNKNOWN_FILL, interface="risk",
                      state_is_unknown=False)


# ==================================================== the risk interface

@pytest.mark.parametrize("over,kwargs,rule", [
    ({"volume": 0.0}, {}, RULE_VOLUME_NOT_POSITIVE),
    ({"volume": 0.9}, {}, RULE_VOLUME_ABOVE_CAP),
    ({}, {"unresolved_commands_on_route": 1}, RULE_UNRESOLVED_COMMAND_ON_ROUTE),
    ({}, {"open_positions_on_symbol": 1}, RULE_OPEN_POSITIONS_AT_CAP),
    ({}, {"entries_today": 3}, RULE_DAILY_ENTRY_BUDGET_EXHAUSTED),
    ({}, {"realized_loss_today": -30.0}, RULE_DAILY_LOSS_LIMIT_REACHED),
])
def test_each_risk_rule_refuses_by_name_with_the_number_it_measured(over, kwargs, rule):
    with pytest.raises(PolicyRefusalError) as caught:
        _mandate().evaluate(_request(**over), position=PositionState.known(0.0), **kwargs)
    refusal = caught.value.refusal
    assert refusal.rule == rule
    assert refusal.interface == "risk"
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED
    assert refusal.measured is not None


def test_a_volume_above_the_cap_is_refused_and_never_silently_reduced():
    with pytest.raises(PolicyRefusalError) as caught:
        _mandate().evaluate(_request(volume=0.9), position=PositionState.known(0.0))
    refusal = caught.value.refusal
    assert refusal.measured == pytest.approx(0.9) and refusal.limit == pytest.approx(0.1)
    assert "never silently reduced" in refusal.reason


def test_the_loss_limit_reads_a_magnitude_and_not_a_sign_the_caller_chose():
    for reported in (-30.0, 30.0):
        with pytest.raises(PolicyRefusalError) as caught:
            _mandate().evaluate(_request(), position=PositionState.known(0.0),
                                realized_loss_today=reported)
        assert caught.value.refusal.measured == pytest.approx(30.0)


def test_at_the_loss_limit_a_closing_order_is_still_granted():
    close = _request(action=ACTION_CLOSE, volume=0.0)
    grant = _mandate().evaluate(close, position=PositionState.known(0.05),
                                realized_loss_today=-999.0, entries_today=99)
    assert isinstance(grant, MandateGrant), "risk-reducing orders stay permitted"


def test_an_unresolved_command_on_the_route_blocks_even_a_closing_order():
    """The route carries one command at a time by declared concurrency, so a
    second would race the first whichever direction it goes in."""
    close = _request(action=ACTION_CLOSE, volume=0.0)
    with pytest.raises(PolicyRefusalError) as caught:
        _mandate().evaluate(close, position=PositionState.known(0.05),
                            unresolved_commands_on_route=1)
    assert caught.value.refusal.rule == RULE_UNRESOLVED_COMMAND_ON_ROUTE


def test_a_mandate_that_permits_nothing_or_a_signed_loss_limit_refuses_to_exist():
    with pytest.raises(ValueError, match="positive"):
        _mandate(max_volume=0)
    with pytest.raises(ValueError, match="no mandate"):
        _mandate(max_open_positions_per_symbol=0)
    with pytest.raises(ValueError, match="no mandate"):
        _mandate(max_open_commands_per_day=0)
    with pytest.raises(ValueError, match="positive magnitude"):
        _mandate(max_daily_loss=-1.0)


# ============================================ the two interfaces together

def test_the_policy_runs_first_and_the_risk_mandate_is_not_consulted_after_a_refusal():
    """A refused order has exactly one reason, and it is the earliest true one:
    an order that is not allowed to exist is never measured against a cap."""
    consulted = []
    mandate = _mandate()
    original = RiskMandate.evaluate

    def _record(self, *args, **kwargs):
        consulted.append(True)
        return original(self, *args, **kwargs)

    with pytest.raises(PolicyRefusalError) as caught:
        evaluate_order(_request(symbol="GBPJPY", volume=9.9), policy=_policy(),
                       mandate=mandate, now=NOW, position=PositionState.known(0.0),
                       already_submitted=_never_submitted)
    assert caught.value.refusal.rule == RULE_SYMBOL_OUTSIDE_MANDATE
    assert consulted == [], "the risk mandate was never asked"


def test_both_interfaces_together_grant_a_well_formed_order():
    grant = evaluate_order(_request(), policy=_policy(), mandate=_mandate(), now=NOW,
                           position=PositionState.known(0.0),
                           already_submitted=_never_submitted)
    assert grant.entries_remaining == 3


def test_every_declared_rule_is_reachable_and_every_refusal_is_journalable():
    assert RULES == POLICY_RULES | RISK_RULES
    assert not POLICY_RULES & RISK_RULES
    refusal = PolicyRefusal(rule=RULE_VOLUME_ABOVE_CAP, reason="over the cap",
                            recovery=RECOVERY_REFUSE_TO_PROCEED, interface="risk",
                            measured=0.9, limit=0.1)
    fact = refusal.as_fact()
    assert fact["schema"] == "lts.mt5_policy_risk.refusal.v1"
    assert fact["reason"] == "over the cap"
    assert fact["blocks_new_orders"] is True
    assert fact["measured"] == 0.9 and fact["limit"] == 0.1


def test_a_refusal_of_ours_cannot_carry_a_venues_words_because_it_has_nowhere_to_put_them():
    """The structural distinction between the two taxonomies: this refusal type
    has no ``venue_reason`` field at all."""
    fields = set(PolicyRefusal.__dataclass_fields__)
    assert "venue_reason" not in fields and "venue" not in fields and "venue_code" not in fields
    assert "reason" in fields and "rule" in fields


def test_a_refusal_without_our_own_reason_refuses_to_exist():
    with pytest.raises(ValueError, match="silent no-op"):
        PolicyRefusal(rule=RULE_VOLUME_ABOVE_CAP, reason="",
                      recovery=RECOVERY_REFUSE_TO_PROCEED, interface="risk")


def test_no_policy_or_risk_refusal_is_ever_a_read():
    with pytest.raises(ValueError, match="no read-only refusal"):
        PolicyRefusal(rule=RULE_VOLUME_ABOVE_CAP, reason="x",
                      recovery=RECOVERY_REFUSE_TO_PROCEED, interface="risk",
                      operation=OPERATION_READ)


# ================================================ no message ever read

ADVERSARIAL = [
    "connection error: please retry immediately",
    "timeout — transient, safe to resend",
    "SUCCESS: order filled",
    "",
    "10009 done",
]


@pytest.mark.parametrize("prose", ADVERSARIAL)
def test_classification_is_invariant_under_the_terminals_prose(prose):
    """The same retcode under five messages, one of which claims success and one
    of which asks to be retried. The verdict must not move."""
    verdicts = {
        (r.kind, r.recovery, r.state_is_unknown, r.blocks_new_orders)
        for r in [classify_mt5_retcode(retcode=10017, venue_message=prose,
                                       operation=OPERATION_MUTATING)]
    }
    assert verdicts == {(KIND_INSUFFICIENT_ENTITLEMENT, RECOVERY_REFUSE_TO_PROCEED,
                         False, True)}


def test_no_classification_path_ever_returns_nothing():
    for code in sorted(set(MT5_RETCODE_KINDS) | {19999, -1}):
        if code in MT5_SUCCESS_RETCODES:
            continue
        for operation in (OPERATION_READ, OPERATION_MUTATING):
            refusal = classify_mt5_retcode(retcode=code, venue_message="x",
                                           operation=operation)
            assert isinstance(refusal, BrokerRefusal) and refusal.venue_reason


def _imports_of(name):
    source = Path(__file__).resolve().parents[2] / "app" / name
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    return imported


def test_neither_module_imports_anything_that_can_reach_a_network():
    assert _imports_of("mt5_refusal.py") == {"__future__", "typing", "app.broker_refusal"}
    assert _imports_of("mt5_policy_risk.py") == {"__future__", "dataclasses", "datetime",
                                                 "typing", "app.broker_refusal"}


@pytest.mark.parametrize("name", ["mt5_refusal.py", "mt5_policy_risk.py"])
def test_neither_module_ever_parses_a_message(name):
    source = Path(__file__).resolve().parents[2] / "app" / name
    tree = ast.parse(source.read_text(encoding="utf-8"))
    forbidden = {"lower", "upper", "startswith", "endswith", "find", "search",
                 "match", "split", "strip", "casefold"}
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & forbidden, sorted(called & forbidden)
