"""RP157 lane 3: the demo/paper adapters' refusal and recovery contract.

No socket is opened, no credential is read, no venue is contacted and no
order is placed. Every case is a REAL object — an exception raised by the
adapter's own code path driven through an injected session double, or a
real venue error object handed to the classifier — exactly as the P2/E2
battery in ``tests/test_alpaca_retry_taxonomy_offline.py`` does. Sockets
are booby-trapped module-wide.

What is being pinned:

* every refusal is TYPED and NAMED, and carries the venue's own reason
  verbatim — never a reworded one;
* every recovery is DECLARED: retry with its bound, resume from the
  persisted client order id, reconcile an unknown fill, or refuse;
* a refusal is never downgraded into a silent no-op;
* an unknown state is never assumed flat, and it blocks further orders
  until it is reconciled;
* an entitlement or authentication failure is never retried as if it were
  transient.
"""
from __future__ import annotations

import ast
import errno
import json
import socket
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests
from trading_contracts import OrderIntentV2, ProtectiveBracket, RiskEnvelope

from app.alpaca_l1 import (
    AlpacaL1Executor,
    AlpacaL1Profile,
    AlpacaPaperTradingClient,
)
from app.alpaca_paper_lab import AlpacaPaperClient, AlpacaPaperError
from app.broker_refusal import (
    IBKR_ERROR_KINDS,
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
    RetryBound,
    classify_exception,
    classify_http_refusal,
    classify_ibkr_error,
    duplicate_client_order_id_refusal,
    market_closed_refusal,
    stale_quote_refusal,
    venue_facts,
)
from app.capital_demo_lab import CapitalDemoClient, CapitalDemoError
from app.ibkr_l1_adapter import build_bracket
from app.ibkr_l1_broker import FakeBrokerRefusal, FakeIbkrClient
from app.ibkr_l1_executor import BracketExecutor, CapabilityRecord
from app.ibkr_l1_journal import L1ExecutionOlap
from app.oanda_practice_lab import OandaPracticeClient, OandaPracticeError
from app.runner_retry_taxonomy import classify_runner_exception

NOW = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)
ACCOUNT = "DU1234567"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted in an offline test")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


# ----------------------------------------------------------- HTTP doubles
class Response:
    """A venue answer. It holds bytes the venue would have sent; it has no
    transport behind it."""

    def __init__(self, status, payload, *, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = b"{}"

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class Session:
    """An injected session that answers from a script and never connects."""

    def __init__(self, answer):
        self.headers = {}
        self.calls = []
        self._answer = answer

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


def _alpaca_read_failure(answer):
    client = AlpacaPaperClient("key", "secret", session=Session(answer))
    with pytest.raises(AlpacaPaperError) as caught:
        client.account()
    return caught.value


def _alpaca_write_failure(answer):
    client = AlpacaPaperTradingClient("key", "secret", session=Session(answer))
    with pytest.raises(AlpacaPaperError) as caught:
        client.submit_bracket({
            "symbol": "SPY", "qty": "1", "side": "buy",
            "stop_price": "490", "take_profit_price": "510",
            "time_in_force": "gtc", "client_order_id": "lts-abc",
        })
    return caught.value


def _capital_failure(answer):
    client = CapitalDemoClient("key", "identifier", "password",
                               session=Session(answer))
    with pytest.raises(CapitalDemoError) as caught:
        client.authenticate()
    return caught.value


def _oanda_failure(answer):
    client = OandaPracticeClient("account", "token", session=Session(answer))
    with pytest.raises(OandaPracticeError) as caught:
        client.account_details()
    return caught.value


# ============================================================ the interface
def test_no_kind_can_be_built_with_an_undeclared_recovery():
    for kind in KINDS:
        with pytest.raises(ValueError):
            BrokerRefusal(kind=kind, venue="v", venue_reason="r",
                          recovery="ignore_it")


def test_a_refusal_without_the_venues_reason_refuses_to_exist():
    """An empty reason would be a refusal that says nothing — a silent
    no-op wearing a type."""
    with pytest.raises(ValueError, match="silent no-op"):
        BrokerRefusal(kind=KIND_VENUE_REJECTION, venue="alpaca_paper",
                      venue_reason="", recovery=RECOVERY_REFUSE_TO_PROCEED)


@pytest.mark.parametrize("kind", [
    KIND_INSUFFICIENT_ENTITLEMENT, KIND_AUTHENTICATION_FAILURE,
    KIND_INSTRUMENT_NOT_PERMITTED, KIND_MARKET_CLOSED,
    KIND_DUPLICATE_CLIENT_ORDER_ID,
])
def test_these_kinds_can_never_declare_a_retry(kind):
    with pytest.raises(ValueError):
        BrokerRefusal(
            kind=kind, venue="alpaca_paper", venue_reason="r",
            recovery=RECOVERY_RETRY_WITH_BOUND,
            retry_bound=RetryBound(base_seconds=1.0, cap_seconds=2.0),
        )


def test_a_mutating_call_is_never_retried():
    with pytest.raises(ValueError, match="never retried"):
        BrokerRefusal(
            kind=KIND_TRANSPORT_FAILURE, venue="alpaca_paper",
            venue_reason="reset by peer", recovery=RECOVERY_RETRY_WITH_BOUND,
            operation=OPERATION_MUTATING,
            retry_bound=RetryBound(base_seconds=1.0, cap_seconds=2.0),
        )


def test_an_unknown_state_can_never_be_declared_non_blocking():
    with pytest.raises(ValueError, match="never assumed flat"):
        BrokerRefusal(
            kind=KIND_VENUE_REJECTION, venue="alpaca_paper",
            venue_reason="503", recovery=RECOVERY_RECONCILE_UNKNOWN_FILL,
            operation=OPERATION_MUTATING, state_is_unknown=True,
            blocks_new_orders=False,
        )


def test_a_retry_carries_its_bound_and_nothing_else_does():
    with pytest.raises(ValueError, match="declares its bound"):
        BrokerRefusal(kind=KIND_TRANSPORT_FAILURE, venue="v",
                      venue_reason="r", recovery=RECOVERY_RETRY_WITH_BOUND)
    with pytest.raises(ValueError, match="declares its bound"):
        BrokerRefusal(kind=KIND_VENUE_REJECTION, venue="v", venue_reason="r",
                      recovery=RECOVERY_REFUSE_TO_PROCEED,
                      retry_bound=RetryBound(base_seconds=1.0, cap_seconds=2.0))


def test_an_unbounded_or_impossible_retry_bound_refuses():
    for bad in ((0.0, 10.0, None), (1.0, 0.0, None), (10.0, 1.0, None),
                (1.0, 10.0, 0)):
        with pytest.raises(ValueError):
            RetryBound(base_seconds=bad[0], cap_seconds=bad[1],
                       max_attempts=bad[2])


def test_the_fact_is_journalable_and_keeps_the_reason_byte_for_byte():
    reason = 'insufficient buying power: "SPY" 1 @ $510.00'
    refusal = classify_http_refusal(
        venue="alpaca_paper", status=403, venue_reason=reason,
        operation=OPERATION_MUTATING,
    )
    fact = refusal.as_fact()
    assert json.loads(json.dumps(fact))["venue_reason"] == reason
    assert fact["kind"] == KIND_INSUFFICIENT_ENTITLEMENT
    assert fact["recovery"] == RECOVERY_REFUSE_TO_PROCEED


# ======================================== alpaca paper — the read adapter
@pytest.mark.parametrize("status,kind,recovery", [
    (401, KIND_AUTHENTICATION_FAILURE, RECOVERY_REFUSE_TO_PROCEED),
    (403, KIND_INSUFFICIENT_ENTITLEMENT, RECOVERY_REFUSE_TO_PROCEED),
    (404, KIND_VENUE_REJECTION, RECOVERY_REFUSE_TO_PROCEED),
    (422, KIND_VENUE_REJECTION, RECOVERY_REFUSE_TO_PROCEED),
    (429, KIND_TRANSPORT_FAILURE, RECOVERY_RETRY_WITH_BOUND),
    (503, KIND_TRANSPORT_FAILURE, RECOVERY_RETRY_WITH_BOUND),
])
def test_alpaca_read_refusals_are_typed_from_the_status(status, kind, recovery):
    error = _alpaca_read_failure(
        Response(status, {"code": 40110000, "message": "venue said so"})
    )
    refusal = classify_exception(error, venue="alpaca_paper",
                                 operation=OPERATION_READ)
    assert refusal.kind == kind
    assert refusal.recovery == recovery
    assert refusal.venue_status == status
    assert refusal.venue_reason == "venue said so"


def test_alpaca_carries_the_venue_code_as_a_structured_fact():
    error = _alpaca_read_failure(
        Response(422, {"code": 42210000, "message": "qty must be > 0"})
    )
    assert venue_facts(error) == {
        "venue_status": 422, "venue_code": 42210000,
        "venue_reason": "qty must be > 0",
    }


@pytest.mark.parametrize("cause", [
    requests.ConnectionError("connection aborted"),
    requests.Timeout("read timed out"),
    requests.ConnectionError(
        ConnectionResetError(errno.ECONNRESET, "reset by peer")),
])
def test_an_alpaca_read_transport_failure_retries_with_a_bound(cause):
    error = _alpaca_read_failure(cause)
    refusal = classify_exception(error, venue="alpaca_paper",
                                 operation=OPERATION_READ)
    assert refusal.kind == KIND_TRANSPORT_FAILURE
    assert refusal.recovery == RECOVERY_RETRY_WITH_BOUND
    assert refusal.retry_bound.cap_seconds == 300.0
    assert refusal.transient_cause_type == type(cause).__name__
    # the two taxonomies agree: the runner would also retry this
    assert classify_runner_exception(error) == "transient"


# ======================================= alpaca paper — the write adapter
def test_the_same_transport_failure_on_a_write_is_reconciled_not_retried():
    """This is the whole point of the operation axis. A GET that failed
    changed nothing; a POST that failed may have placed an order."""
    error = _alpaca_write_failure(requests.ConnectionError("connection aborted"))
    read = classify_exception(error, venue="alpaca_paper",
                              operation=OPERATION_READ)
    write = classify_exception(error, venue="alpaca_paper",
                               operation=OPERATION_MUTATING)
    assert read.recovery == RECOVERY_RETRY_WITH_BOUND
    assert write.recovery == RECOVERY_RECONCILE_UNKNOWN_FILL
    assert write.state_is_unknown is True
    assert write.blocks_new_orders is True


def test_an_alpaca_write_rejection_proves_nothing_was_placed():
    error = _alpaca_write_failure(
        Response(422, {"code": 42210000, "message": "insufficient qty"})
    )
    refusal = classify_exception(error, venue="alpaca_paper",
                                 operation=OPERATION_MUTATING)
    assert refusal.kind == KIND_VENUE_REJECTION
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED
    assert refusal.state_is_unknown is False
    assert refusal.blocks_new_orders is True


def test_an_alpaca_write_500_leaves_an_unproven_outcome():
    error = _alpaca_write_failure(Response(502, {"message": "bad gateway"}))
    refusal = classify_exception(error, venue="alpaca_paper",
                                 operation=OPERATION_MUTATING)
    assert refusal.kind == KIND_TRANSPORT_FAILURE
    assert refusal.recovery == RECOVERY_RECONCILE_UNKNOWN_FILL
    assert refusal.state_is_unknown is True


def test_an_alpaca_venue_duplicate_stays_fail_closed_without_a_code_table():
    """Alpaca's numeric code space was not observable offline, so this
    repository declares none: an unrecognised code is a fail-closed venue
    rejection, never a retry and never a silent pass. Naming it
    ``duplicate_client_order_id`` at this venue is an entitlement-mandate
    item, not a code change."""
    error = _alpaca_write_failure(
        Response(422, {"code": 42210000,
                       "message": "client_order_id must be unique"})
    )
    refusal = classify_exception(error, venue="alpaca_paper",
                                 operation=OPERATION_MUTATING)
    assert refusal.kind == KIND_VENUE_REJECTION
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED
    assert refusal.venue_reason == "client_order_id must be unique"
    assert refusal.is_transient is False


# ============================================= capital.com demo — observer
@pytest.mark.parametrize("status,kind", [
    (401, KIND_AUTHENTICATION_FAILURE),
    (403, KIND_INSUFFICIENT_ENTITLEMENT),
    (400, KIND_VENUE_REJECTION),
])
def test_capital_demo_refusals_are_typed(status, kind):
    error = _capital_failure(
        Response(status, {"errorCode": "error.invalid.details"})
    )
    refusal = classify_exception(error, venue="capital_demo",
                                 operation=OPERATION_READ)
    assert refusal.kind == kind
    assert refusal.venue_reason == "error.invalid.details"
    assert refusal.venue_status == status


def test_a_capital_demo_transport_failure_is_transient_both_ways():
    error = _capital_failure(requests.ConnectionError("name resolution failed"))
    refusal = classify_exception(error, venue="capital_demo")
    assert refusal.kind == KIND_TRANSPORT_FAILURE
    assert refusal.recovery == RECOVERY_RETRY_WITH_BOUND
    assert classify_runner_exception(error) == "transient"


def test_a_capital_demo_body_we_cannot_read_is_never_invented():
    error = _capital_failure(Response(403, ValueError("not JSON")))
    refusal = classify_exception(error, venue="capital_demo")
    assert refusal.kind == KIND_INSUFFICIENT_ENTITLEMENT
    assert refusal.venue_reason == "HTTP 403"


# ================================================ oanda practice — v20
@pytest.mark.parametrize("code,kind", [
    ("INSUFFICIENT_AUTHORIZATION", KIND_INSUFFICIENT_ENTITLEMENT),
    ("MARKET_HALTED", KIND_MARKET_CLOSED),
    ("INSTRUMENT_NOT_TRADEABLE", KIND_INSTRUMENT_NOT_PERMITTED),
    ("CLIENT_ORDER_ID_ALREADY_EXISTS", KIND_DUPLICATE_CLIENT_ORDER_ID),
])
def test_oanda_reject_reasons_are_typed_from_the_machine_code(code, kind):
    """The venue's reject reason is an enum value, not prose, so keying on
    it is not message sniffing. Note the status alone would have said
    'venue_rejection' for three of these four."""
    error = _oanda_failure(
        Response(400, {"errorCode": code, "errorMessage": "rejected"})
    )
    refusal = classify_exception(error, venue="oanda_practice",
                                 operation=OPERATION_MUTATING)
    assert refusal.kind == kind
    assert refusal.venue_code == code
    assert refusal.is_transient is False


def test_an_unknown_oanda_code_falls_through_to_a_fail_closed_rejection():
    error = _oanda_failure(
        Response(400, {"errorCode": "SOME_REASON_ADDED_NEXT_YEAR",
                       "errorMessage": "rejected"})
    )
    refusal = classify_exception(error, venue="oanda_practice",
                                 operation=OPERATION_MUTATING)
    assert refusal.kind == KIND_VENUE_REJECTION
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED


# ============================================================ ibkr paper
@pytest.mark.parametrize("code,kind", [
    (103, KIND_DUPLICATE_CLIENT_ORDER_ID),
    (200, KIND_INSTRUMENT_NOT_PERMITTED),
    (201, KIND_VENUE_REJECTION),
    (354, KIND_INSUFFICIENT_ENTITLEMENT),
    (502, KIND_TRANSPORT_FAILURE),
    (1100, KIND_TRANSPORT_FAILURE),
])
def test_ibkr_error_codes_are_typed_from_the_documented_code(code, kind):
    refusal = classify_ibkr_error(
        error_code=code, error_string="the venue's own words",
        operation=OPERATION_READ,
    )
    assert refusal.kind == kind
    assert refusal.venue_reason == "the venue's own words"


def test_ibkr_connectivity_restoration_codes_are_not_refusals():
    """1101/1102 say the connection came BACK. Typing them as refusals
    would page on recovery."""
    assert 1101 not in IBKR_ERROR_KINDS and 1102 not in IBKR_ERROR_KINDS


def test_an_unlisted_ibkr_code_is_a_fail_closed_rejection():
    refusal = classify_ibkr_error(error_code=9999, error_string="new code")
    assert refusal.kind == KIND_VENUE_REJECTION
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED


def test_the_fake_brokers_own_refusal_object_classifies_fail_closed():
    """A real venue error object from this repository's IBKR double."""
    error = FakeBrokerRefusal("fake TWS: parent order 9000 unknown")
    refusal = classify_exception(error, venue="ibkr_paper",
                                 operation=OPERATION_MUTATING)
    assert refusal.kind == KIND_VENUE_REJECTION
    assert refusal.state_is_unknown is True
    assert refusal.blocks_new_orders is True
    assert refusal.venue_reason == "fake TWS: parent order 9000 unknown"
    assert classify_runner_exception(error) == "fatal"


def test_a_lost_tws_connection_mid_transmission_is_unknown_not_transient():
    error = ConnectionError("fake TWS: connection lost during order "
                            "transmission")
    assert classify_runner_exception(error) == "transient"
    refusal = classify_exception(error, venue="ibkr_paper",
                                 operation=OPERATION_MUTATING)
    assert refusal.kind == KIND_TRANSPORT_FAILURE
    assert refusal.recovery == RECOVERY_RECONCILE_UNKNOWN_FILL
    assert refusal.state_is_unknown is True


# ======================================== market closed and stale quotes
def test_market_closed_is_read_from_the_clock_fact_not_from_prose():
    refusal = market_closed_refusal(
        venue="alpaca_paper",
        clock_fact={"is_open": False, "next_open": "2026-09-28T13:30:00Z"},
    )
    assert refusal.kind == KIND_MARKET_CLOSED
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED
    assert "2026-09-28T13:30:00Z" in refusal.venue_reason


def test_a_clock_without_is_open_is_never_read_as_open():
    refusal = market_closed_refusal(venue="alpaca_paper",
                                    clock_fact={"timestamp": "…"})
    assert refusal.kind == KIND_VENUE_REJECTION
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED


@pytest.mark.parametrize("reason_code", [
    "quote_missing", "quote_missing_field:bid", "quote_invalid",
    "quote_stale:312.4s",
])
def test_a_stale_quote_defers_a_read_and_refuses_a_write(reason_code):
    """These are the outbox's own namespaced tokens, not venue prose."""
    deferral = stale_quote_refusal(venue="ibkr_paper", reason_code=reason_code)
    assert deferral.kind == KIND_STALE_QUOTE
    assert deferral.recovery == RECOVERY_RETRY_WITH_BOUND
    assert deferral.retry_bound.max_attempts == 3
    order = stale_quote_refusal(venue="ibkr_paper", reason_code=reason_code,
                                operation=OPERATION_MUTATING)
    assert order.recovery == RECOVERY_REFUSE_TO_PROCEED


# ================================================= recovery: duplicates
def _alpaca_profile():
    return AlpacaL1Profile(
        venue="alpaca_paper", environment="paper",
        account_fingerprint="0123456789abcdef", symbol="SPY",
        asset_id="equity:SPY", quantity_ceiling=Decimal("1"),
        max_orders_per_day=4, max_risk_fraction_at_stop=Decimal("0.001"),
    )


class RecordingAlpacaClient:
    """Counts submissions. It is the only way to prove a second order was
    NOT placed."""

    def __init__(self, *, on_submit=None):
        self.submit_calls = 0
        self.client_order_ids = []
        self._on_submit = on_submit

    def account(self):
        return {"id": "paper", "status": "ACTIVE", "trading_blocked": False}

    def account_fingerprint(self, _account):
        return "0123456789abcdef"

    def submit_bracket(self, plan):
        self.submit_calls += 1
        self.client_order_ids.append(plan["client_order_id"])
        if self._on_submit is not None:
            raise self._on_submit
        return {"id": "parent-id", "status": "accepted"}

    def order(self, _order_id):
        return {
            "id": "parent-id", "client_order_id": self.client_order_ids[-1],
            "symbol": "SPY", "qty": "1", "side": "buy", "type": "market",
            "time_in_force": "gtc", "order_class": "bracket",
            "status": "accepted",
            "legs": [
                {"id": "tp", "side": "sell", "type": "limit", "qty": "1",
                 "limit_price": "510", "time_in_force": "gtc"},
                {"id": "sl", "side": "sell", "type": "stop", "qty": "1",
                 "stop_price": "490", "time_in_force": "gtc"},
            ],
        }

    def positions(self):
        return []

    def cancel_order(self, _order_id):
        raise AssertionError("no cancel expected in this test")

    def close_position(self, _symbol):
        raise AssertionError("no flatten expected in this test")


def _alpaca_submit_args(key="spy:2026-09-25"):
    return dict(
        idempotency_key=key, symbol="SPY", asset_id="equity:SPY",
        qty=Decimal("1"), side="buy", stop_price=Decimal("490"),
        take_profit_price=Decimal("510"),
        risk_fraction_at_stop=Decimal("0.0005"),
        model_evidence={
            "model_id": "spy-baseline-v1", "artifact_sha256": "a" * 64,
            "config_sha256": "b" * 64, "input_sha256": "c" * 64,
        },
    )


def test_alpaca_a_repeated_client_order_id_never_places_a_second_order(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    client = RecordingAlpacaClient()
    executor = AlpacaL1Executor(store, client, _alpaca_profile())
    first = executor.submit(**_alpaca_submit_args())
    replay = executor.submit(**_alpaca_submit_args())
    assert client.submit_calls == 1
    assert replay["replayed"] is True
    assert replay["effect_id"] == first["effect_id"]
    store.close()


def test_alpaca_a_restart_resumes_from_the_persisted_id_and_never_resends(
    tmp_path,
):
    """A NEW executor and a NEW client over the same journal: the resume
    reads the persisted effect, not the current configuration, and the
    second client is never called."""
    path = tmp_path / "ledger.sqlite"
    store = L1ExecutionOlap(path)
    first_client = RecordingAlpacaClient()
    AlpacaL1Executor(store, first_client, _alpaca_profile()).submit(
        **_alpaca_submit_args()
    )
    store.close()

    resumed_store = L1ExecutionOlap(path)
    second_client = RecordingAlpacaClient()
    resumed = AlpacaL1Executor(
        resumed_store, second_client, _alpaca_profile()
    ).submit(**_alpaca_submit_args())
    assert second_client.submit_calls == 0
    assert resumed["replayed"] is True
    contract = resumed_store.effect_contract(resumed["effect_id"])
    assert contract["client_order_id"] == first_client.client_order_ids[0]
    resumed_store.close()


def test_a_venue_reported_duplicate_resumes_from_the_persisted_id():
    refusal = duplicate_client_order_id_refusal(
        venue="ibkr_paper", client_order_id="lts-abc123",
        venue_reason="Duplicate order id", venue_code=103,
    )
    assert refusal.kind == KIND_DUPLICATE_CLIENT_ORDER_ID
    assert refusal.recovery == RECOVERY_RESUME_FROM_PERSISTED_CLIENT_ORDER_ID
    assert refusal.client_order_id == "lts-abc123"
    # the id exists at the venue; what it DID is not yet known
    assert refusal.state_is_unknown is True
    assert refusal.blocks_new_orders is True
    assert refusal.is_transient is False


# =========================== recovery: unknown state blocks further orders
def test_alpaca_an_unknown_submission_is_typed_journalled_and_blocks(tmp_path):
    """The venue was called and never answered. The effect stays unknown,
    the typed refusal is journalled with the venue's own words, the hold is
    set, and a DIFFERENT idempotency key may not place new risk."""
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    wrapped = AlpacaPaperError("/v2/orders request failed: ConnectionError")
    wrapped.__cause__ = requests.ConnectionError("connection aborted")
    client = RecordingAlpacaClient(on_submit=wrapped)
    executor = AlpacaL1Executor(store, client, _alpaca_profile())

    with pytest.raises(AlpacaPaperError):
        executor.submit(**_alpaca_submit_args())

    effect = store.effect_by_key("spy:2026-09-25")
    assert effect["state"] == "effect_unknown"
    assert store.get_state("halt") == "hold"
    facts = store.broker_facts(effect["effect_id"], "submit_refusal")
    assert len(facts) == 1
    fact = facts[0]["fact"]
    assert fact["kind"] == KIND_TRANSPORT_FAILURE
    assert fact["recovery"] == RECOVERY_RECONCILE_UNKNOWN_FILL
    assert fact["state_is_unknown"] is True
    assert fact["venue_reason"] == "connection aborted"

    with pytest.raises(AlpacaPaperError, match="blocked"):
        executor.submit(**_alpaca_submit_args(key="spy:2026-09-26"))
    assert client.submit_calls == 1
    store.close()


def test_alpaca_new_risk_is_blocked_while_an_effect_is_unknown(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    executor = AlpacaL1Executor(store, RecordingAlpacaClient(),
                                _alpaca_profile())
    assert executor.new_risk_blocker() is None
    store.create_effect("alpaca-x", "some-key", "alpaca_bracket_entry", [])
    store.advance_effect("alpaca-x", "effect_unknown")
    assert executor.new_risk_blocker() == "effect_unknown:alpaca-x"
    store.close()


def test_alpaca_new_risk_is_blocked_while_a_hold_is_set(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    client = RecordingAlpacaClient()
    executor = AlpacaL1Executor(store, client, _alpaca_profile())
    store.set_state("halt", "hold")
    with pytest.raises(AlpacaPaperError, match="halted:hold"):
        executor.submit(**_alpaca_submit_args())
    assert client.submit_calls == 0
    store.close()


# ---------------------------------------------------------------- ibkr
def _intent(idem="idem-rp157-1"):
    return OrderIntentV2(
        object_id=f"oi2-{idem}", as_of=NOW,
        producer={"name": "lts.demo_execution_service", "version": "0.2.0"},
        trace_id="t-rp157", account_ref="86aa086401855219",
        asset_id="fx:EUR/USD", venue="ibkr_paper", instrument="EUR.USD",
        intent_class="risk_increasing", order_type="market",
        delta_units=20000.0,
        protection=ProtectiveBracket(stop_loss_price=1.0850,
                                     take_profit_price=1.0910),
        risk=RiskEnvelope(risk_fraction_at_stop=0.005,
                          gross_notional_fraction=0.05, margin_fraction=0.02,
                          daily_loss_budget_fraction=0.02,
                          reservation_id=f"rsv-{idem}"),
        capability_snapshot_hash="sha256:" + "c" * 64, idempotency_key=idem,
    )


def _capability(suffix="1"):
    return CapabilityRecord(
        capability_sha256="a" * 63 + suffix,
        nonce_sha256="b" * 63 + suffix,
        metadata={"quantity_ceiling": 20000.0, "max_entries": 1},
    )


def test_ibkr_a_repeated_intent_never_places_a_second_bracket(tmp_path):
    store = L1ExecutionOlap(tmp_path / "l1.db")
    client = FakeIbkrClient(account=ACCOUNT)
    executor = BracketExecutor(store, client)
    intent = _intent()
    plan = build_bracket(intent, parent_order_id=1000, account=ACCOUNT,
                         price_decimals=5, quantity_decimals=0)
    executor.submit_bracket(intent, plan, _capability())
    places = [c for c in client.calls if c[0] == "place_order"]
    replay = executor.submit_bracket(intent, plan, _capability("2"))
    assert replay["replayed"] is True
    assert [c for c in client.calls if c[0] == "place_order"] == places
    store.close()


def test_ibkr_a_failure_mid_flight_is_unknown_and_resume_never_promotes(
    tmp_path,
):
    path = tmp_path / "l1.db"
    store = L1ExecutionOlap(path)
    client = FakeIbkrClient(account=ACCOUNT, fail_on_place_call=1)
    executor = BracketExecutor(store, client)
    intent = _intent()
    plan = build_bracket(intent, parent_order_id=1000, account=ACCOUNT,
                         price_decimals=5, quantity_decimals=0)
    with pytest.raises(Exception) as caught:
        executor.submit_bracket(intent, plan, _capability())
    refusal = classify_exception(caught.value, venue="ibkr_paper",
                                 operation=OPERATION_MUTATING)
    assert refusal.state_is_unknown is True
    assert refusal.recovery == RECOVERY_RECONCILE_UNKNOWN_FILL
    store.close()

    # restart: a new executor over the same journal
    resumed_store = L1ExecutionOlap(path)
    resumed_client = FakeIbkrClient(account=ACCOUNT)
    report = BracketExecutor(resumed_store, resumed_client).resume_report()
    assert [row["classification"] for row in report] == ["effect_unknown"]
    assert report[0]["call_attempts"] == 1 and report[0]["call_results"] == 0
    assert resumed_client.calls == [], "resume contacted the broker"
    resumed_store.close()


def test_ibkr_a_hold_refuses_new_risk_before_any_broker_call(tmp_path):
    store = L1ExecutionOlap(tmp_path / "l1.db")
    store.set_state("halt", "hold")
    client = FakeIbkrClient(account=ACCOUNT)
    intent = _intent()
    plan = build_bracket(intent, parent_order_id=1000, account=ACCOUNT,
                         price_decimals=5, quantity_decimals=0)
    with pytest.raises(Exception, match="global hold"):
        BracketExecutor(store, client).submit_bracket(intent, plan,
                                                      _capability())
    assert client.calls == []
    store.close()


# ============================== entitlement is never retried as transient
@pytest.mark.parametrize("venue,error_factory", [
    ("alpaca_paper",
     lambda: _alpaca_read_failure(Response(403, {"message": "forbidden"}))),
    ("alpaca_paper",
     lambda: _alpaca_write_failure(Response(401, {"message": "unauthorized"}))),
    ("capital_demo",
     lambda: _capital_failure(Response(403, {"errorCode": "error.forbidden"}))),
    ("oanda_practice",
     lambda: _oanda_failure(
         Response(403, {"errorCode": "INSUFFICIENT_AUTHORIZATION",
                        "errorMessage": "no"}))),
])
def test_an_entitlement_failure_is_never_retried(venue, error_factory):
    error = error_factory()
    for operation in (OPERATION_READ, OPERATION_MUTATING):
        refusal = classify_exception(error, venue=venue, operation=operation)
        assert refusal.kind in {KIND_INSUFFICIENT_ENTITLEMENT,
                                KIND_AUTHENTICATION_FAILURE}
        assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED
        assert refusal.retry_bound is None
        assert refusal.is_transient is False
    assert classify_runner_exception(error) == "fatal"


def test_ibkr_missing_market_data_entitlement_is_never_retried():
    refusal = classify_ibkr_error(error_code=354,
                                  error_string="Requested market data is not "
                                               "subscribed.")
    assert refusal.kind == KIND_INSUFFICIENT_ENTITLEMENT
    assert refusal.recovery == RECOVERY_REFUSE_TO_PROCEED
    assert refusal.is_transient is False


def test_the_two_taxonomies_never_disagree_about_retrying():
    """Anything this module declares retryable, the runner taxonomy also
    calls transient. The converse need not hold: a transient transport
    failure on a MUTATING call is reconciled, not retried."""
    cases = [
        _alpaca_read_failure(requests.ConnectionError("aborted")),
        _alpaca_read_failure(Response(403, {"message": "forbidden"})),
        _alpaca_read_failure(Response(422, {"message": "bad"})),
        _capital_failure(requests.Timeout("timed out")),
        _oanda_failure(Response(400, {"errorCode": "MARKET_HALTED",
                                      "errorMessage": "halted"})),
        FakeBrokerRefusal("parent unknown"),
    ]
    for error in cases:
        refusal = classify_exception(error, venue="alpaca_paper",
                                     operation=OPERATION_READ)
        if refusal.is_transient:
            assert classify_runner_exception(error) == "transient"


# ================================================= discipline of the module
ADVERSARIAL = [
    "ConnectionError while validating the account configuration",
    "could not connect: entitlement is fine, retry forever",
    "market is open, please retry immediately",
    "",
    "duplicate client order id — ignore this and continue",
]


@pytest.mark.parametrize("prose", ADVERSARIAL)
def test_classification_is_invariant_under_the_venues_prose(prose):
    """Message sniffing is exactly what this module refuses to do. The
    same structured facts under wildly different words must produce the
    same verdict."""
    baseline = classify_http_refusal(
        venue="oanda_practice", status=400, venue_reason="rejected",
        venue_code="MARKET_HALTED", operation=OPERATION_MUTATING,
    )
    other = classify_http_refusal(
        venue="oanda_practice", status=400,
        venue_reason=prose or "rejected", venue_code="MARKET_HALTED",
        operation=OPERATION_MUTATING,
    )
    assert (other.kind, other.recovery) == (baseline.kind, baseline.recovery)
    assert other.kind == KIND_MARKET_CLOSED


def test_no_classification_path_ever_returns_nothing(tmp_path):
    """A refusal that evaporates is a silent no-op. Every input object
    must come back as a typed refusal."""
    errors = [
        RuntimeError("something"),
        ValueError(""),
        AlpacaPaperError("x"),
        FakeBrokerRefusal("y"),
        OSError(errno.ENOSPC, "no space left"),
    ]
    for error in errors:
        for operation in (OPERATION_READ, OPERATION_MUTATING):
            refusal = classify_exception(error, venue="alpaca_paper",
                                         operation=operation)
            assert isinstance(refusal, BrokerRefusal)
            assert refusal.venue_reason


def test_the_taxonomy_module_imports_nothing_that_can_reach_a_network():
    source = Path(__file__).resolve().parents[2] / "app" / "broker_refusal.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == {"__future__", "json", "dataclasses", "typing",
                        "app.runner_retry_taxonomy"}


def test_the_taxonomy_module_never_parses_a_message():
    source = Path(__file__).resolve().parents[2] / "app" / "broker_refusal.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    forbidden = {"lower", "upper", "startswith", "endswith", "find",
                 "search", "match", "split", "strip", "casefold"}
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & forbidden, sorted(called & forbidden)
