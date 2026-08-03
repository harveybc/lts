"""Milestone A: exact fake-broker effects contract (findings 063/064 core).

Every test asserts EFFECTS — the fake client's invocation log and the
durable journal — never returned strings alone. Sockets are booby-trapped
module-wide; ib_async is used for object construction only.
"""
import socket
from datetime import datetime, timezone

import pytest

from trading_contracts import OrderIntentV2, ProtectiveBracket, RiskEnvelope

from app.ibkr_l1_adapter import (
    L1AuthorizationError,
    L1ExecutionError,
    build_bracket,
)
from app.ibkr_l1_broker import (
    FakeBrokerRefusal,
    FakeIbkrClient,
    place_order_sequence,
    translate_bracket,
)
from app.ibkr_l1_executor import (
    BracketExecutor,
    CapabilityRecord,
    L1EffectUnknown,
    effect_id_for,
)
from app.ibkr_l1_journal import L1ExecutionOlap

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
ACCOUNT = "DU1234567"
CAP_HASH = "sha256:" + "c" * 64


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted in a fake-broker test")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


@pytest.fixture()
def olap(tmp_path):
    ledger = L1ExecutionOlap(tmp_path / "l1.db")
    yield ledger
    ledger.close()


def _intent(units=20000.0, sl=1.0850, tp=1.0910, idem="idem-a-1"):
    return OrderIntentV2(
        object_id=f"oi2-{idem}", as_of=NOW,
        producer={"name": "lts.demo_execution_service", "version": "0.2.0"},
        trace_id="t-a", account_ref="86aa086401855219",
        asset_id="fx:EUR/USD", venue="ibkr_paper", instrument="EUR.USD",
        intent_class="risk_increasing", order_type="market",
        delta_units=units,
        protection=ProtectiveBracket(stop_loss_price=sl, take_profit_price=tp),
        risk=RiskEnvelope(risk_fraction_at_stop=0.005,
                          gross_notional_fraction=0.05, margin_fraction=0.02,
                          daily_loss_budget_fraction=0.02,
                          reservation_id=f"rsv-{idem}"),
        capability_snapshot_hash=CAP_HASH, idempotency_key=idem,
    )


def _plan(intent=None, parent_id=1000):
    return build_bracket(
        intent or _intent(), parent_order_id=parent_id, account=ACCOUNT,
        price_decimals=5, quantity_decimals=0,
    )


def _capability(suffix="1", ceiling=20000.0):
    return CapabilityRecord(
        capability_sha256="a" * 63 + suffix,
        nonce_sha256="b" * 63 + suffix,
        metadata={"quantity_ceiling": ceiling, "max_entries": 1},
    )


# ── exact translation into real ib_async objects ──

def test_translation_produces_exact_contract():
    t = translate_bracket(_plan(), instrument="EUR.USD")
    assert t.contract.secType == "CASH"
    assert t.contract.symbol == "EUR"
    assert t.contract.currency == "USD"
    assert t.contract.exchange == "IDEALPRO"


def test_translation_parent_fields_are_exact():
    t = translate_bracket(_plan(), instrument="EUR.USD")
    p = t.parent
    assert (p.orderId, p.parentId, p.action, p.orderType) == (1000, 0, "BUY", "MKT")
    assert p.totalQuantity == 20000.0
    assert p.account == ACCOUNT and p.tif == "DAY"
    assert p.transmit is False and p.outsideRth is False
    assert p.lmtPrice > 1e300 and p.auxPrice > 1e300      # both unset


def test_translation_children_fields_are_exact():
    t = translate_bracket(_plan(), instrument="EUR.USD")
    tp, sl = t.take_profit, t.stop_loss
    assert (tp.orderId, tp.parentId, tp.action, tp.orderType) == (1001, 1000, "SELL", "LMT")
    assert tp.lmtPrice == 1.0910 and tp.auxPrice > 1e300
    assert tp.tif == "GTC" and tp.transmit is False
    assert (sl.orderId, sl.parentId, sl.action, sl.orderType) == (1002, 1000, "SELL", "STP")
    assert sl.auxPrice == 1.0850 and sl.lmtPrice > 1e300
    assert sl.tif == "GTC" and sl.transmit is True
    assert tp.totalQuantity == sl.totalQuantity == 20000.0
    assert tp.account == sl.account == ACCOUNT


def test_translation_short_side_is_mirrored():
    t = translate_bracket(
        _plan(_intent(units=-20000.0, sl=1.0910, tp=1.0850)),
        instrument="EUR.USD",
    )
    assert t.parent.action == "SELL"
    assert t.take_profit.action == "BUY" and t.take_profit.lmtPrice == 1.0850
    assert t.stop_loss.action == "BUY" and t.stop_loss.auxPrice == 1.0910


@pytest.mark.parametrize("corruption", [
    {"leg": "parent", "field": "transmit", "value": True},
    {"leg": "stop_loss", "field": "transmit", "value": False},
    {"leg": "take_profit", "field": "parentId", "value": 999},
    {"leg": "stop_loss", "field": "totalQuantity", "value": 10000.0},
    {"leg": "parent", "field": "orderType", "value": "LMT"},
    {"leg": "take_profit", "field": "account", "value": "DU999"},
])
def test_corrupted_plan_cannot_reach_a_broker_object(corruption):
    plan = _plan()
    getattr(plan, corruption["leg"])[corruption["field"]] = corruption["value"]
    with pytest.raises(L1ExecutionError):
        translate_bracket(plan, instrument="EUR.USD")


@pytest.mark.parametrize("bad", ["EURUSD", "EUR/USD", "EU.RUSD", "fx:EUR.USD", "E1.USD"])
def test_non_fx_instrument_refuses_translation(bad):
    with pytest.raises(L1ExecutionError, match="FX pair"):
        translate_bracket(_plan(), instrument=bad)


# ── submission: sequence, journal-first, no false success ──

def test_submission_invokes_parent_tp_sl_in_exact_sequence(olap):
    client = FakeIbkrClient(account=ACCOUNT)
    executor = BracketExecutor(olap, client)
    result = executor.submit_bracket(_intent(), _plan(), _capability())
    assert place_order_sequence(client.calls) == [1000, 1001, 1002]
    assert result["state"] == "submitted_pending_ack"
    assert result["acknowledged"] is False
    assert "submitted" not in result       # the lying key no longer exists


def test_submission_journals_effect_and_burns_capability_atomically(olap):
    client = FakeIbkrClient(account=ACCOUNT)
    BracketExecutor(olap, client).submit_bracket(_intent(), _plan(), _capability())
    effect = olap.effect_by_key("idem-a-1")
    assert effect["state"] == "submitted_pending_ack"
    assert effect["order_ids"] == [1000, 1001, 1002]
    cap = olap.capability_row("a" * 63 + "1")
    assert cap["state"] == "consumed"
    assert cap["consumed_effect_id"] == effect["effect_id"]
    kinds = [f["fact_kind"] for f in olap.broker_facts(effect["effect_id"])]
    assert kinds == ["call_attempt", "call_result"] * 3


def test_state_never_claims_submission_before_any_broker_call(olap):
    client = FakeIbkrClient(account=ACCOUNT, fail_on_place_call=1)
    executor = BracketExecutor(olap, client)
    with pytest.raises(L1EffectUnknown):
        executor.submit_bracket(_intent(), _plan(), _capability())
    effect = olap.effect_by_key("idem-a-1")
    assert effect["state"] == "effect_unknown"           # never success
    assert olap.broker_facts(effect["effect_id"], "call_result") == []
    assert len(olap.broker_facts(effect["effect_id"], "call_failure")) == 1


@pytest.mark.parametrize("fail_at,results_before", [(2, 1), (3, 2)])
def test_partial_call_sequence_is_unknown_never_success(olap, fail_at, results_before):
    client = FakeIbkrClient(account=ACCOUNT, fail_on_place_call=fail_at)
    executor = BracketExecutor(olap, client)
    with pytest.raises(L1EffectUnknown):
        executor.submit_bracket(_intent(), _plan(), _capability())
    effect = olap.effect_by_key("idem-a-1")
    assert effect["state"] == "effect_unknown"
    assert len(olap.broker_facts(effect["effect_id"], "call_attempt")) == fail_at
    assert len(olap.broker_facts(effect["effect_id"], "call_result")) == results_before


def test_duplicate_intent_replays_and_makes_no_new_calls(olap):
    client = FakeIbkrClient(account=ACCOUNT)
    executor = BracketExecutor(olap, client)
    executor.submit_bracket(_intent(), _plan(), _capability())
    calls_after_first = len(client.calls)
    replay = executor.submit_bracket(_intent(), _plan(), _capability("2"))
    assert replay["replayed"] is True
    assert replay["state"] == "submitted_pending_ack"
    assert len(client.calls) == calls_after_first
    assert olap.capability_row("a" * 63 + "2") is None   # second cap unburned


def test_restart_never_repeats_an_acknowledged_effect(olap):
    first_client = FakeIbkrClient(account=ACCOUNT)
    BracketExecutor(olap, first_client).submit_bracket(
        _intent(), _plan(), _capability())
    olap.advance_effect(effect_id_for("idem-a-1"), "acknowledged")
    fresh_client = FakeIbkrClient(account=ACCOUNT)
    replay = BracketExecutor(olap, fresh_client).submit_bracket(
        _intent(), _plan(), _capability("2"))
    assert replay["replayed"] is True and replay["state"] == "acknowledged"
    assert fresh_client.calls == []                       # zero broker effects


def test_concurrent_identical_intents_yield_one_effect(olap, monkeypatch):
    client = FakeIbkrClient(account=ACCOUNT)
    executor = BracketExecutor(olap, client)
    executor.submit_bracket(_intent(), _plan(), _capability())
    calls_after_first = len(client.calls)
    # the racing twin misses the pre-check (returns None once), then hits the
    # UNIQUE idempotency constraint inside the atomic unit and must replay
    real = olap.effect_by_key
    seen = {"n": 0}

    def racy(key):
        seen["n"] += 1
        return None if seen["n"] == 1 else real(key)

    monkeypatch.setattr(olap, "effect_by_key", racy)
    twin = executor.submit_bracket(_intent(), _plan(), _capability("2"))
    assert twin["replayed"] is True
    assert len(client.calls) == calls_after_first
    assert olap.capability_row("a" * 63 + "2") is None


def test_consumed_capability_cannot_authorize_a_second_bracket(olap):
    client = FakeIbkrClient(account=ACCOUNT)
    executor = BracketExecutor(olap, client)
    executor.submit_bracket(_intent(), _plan(), _capability())
    calls_after_first = len(client.calls)
    with pytest.raises(L1AuthorizationError, match="already consumed"):
        executor.submit_bracket(
            _intent(idem="idem-a-2"), _plan(parent_id=2000), _capability())
    assert len(client.calls) == calls_after_first
    assert olap.effect_by_key("idem-a-2") is None        # rollback was atomic


def test_quantity_above_capability_ceiling_refuses_without_resizing(olap):
    client = FakeIbkrClient(account=ACCOUNT)
    executor = BracketExecutor(olap, client)
    with pytest.raises(L1AuthorizationError, match="never resizes"):
        executor.submit_bracket(
            _intent(units=25000.0), _plan(_intent(units=25000.0)),
            _capability(ceiling=20000.0))
    assert client.calls == []
    assert olap.capability_row("a" * 63 + "1") is None


def test_global_hold_blocks_new_risk_before_capability_burn(olap):
    olap.set_state("halt", "hold")
    client = FakeIbkrClient(account=ACCOUNT)
    with pytest.raises(L1ExecutionError, match="hold"):
        BracketExecutor(olap, client).submit_bracket(
            _intent(), _plan(), _capability())
    assert client.calls == []
    assert olap.capability_row("a" * 63 + "1") is None


def test_wrong_connected_account_refuses_before_any_effect(olap):
    client = FakeIbkrClient(account="DU9999999")
    with pytest.raises(L1ExecutionError, match="account"):
        BracketExecutor(olap, client).submit_bracket(
            _intent(), _plan(), _capability())
    assert client.calls == []
    assert olap.effect_by_key("idem-a-1") is None


def test_missing_ceiling_metadata_is_never_read_as_unlimited(olap):
    bare = CapabilityRecord("a" * 64, "b" * 64, metadata={})
    client = FakeIbkrClient(account=ACCOUNT)
    with pytest.raises(L1AuthorizationError, match="ceiling"):
        BracketExecutor(olap, client).submit_bracket(_intent(), _plan(), bare)
    assert client.calls == []


# ── restart classification from durable facts ──

def test_resume_distinguishes_pre_effect_from_unknown(olap):
    olap.create_effect("l1e-pre", "idem-pre", "bracket_entry", [1, 2, 3], "a" * 64)
    olap.create_effect("l1e-amb", "idem-amb", "bracket_entry", [4, 5, 6], "d" * 64)
    olap.record_broker_fact("l1e-amb", "call_attempt", {"leg": "parent", "orderId": 4})
    report = {r["effect_id"]: r for r in
              BracketExecutor(olap, FakeIbkrClient(account=ACCOUNT)).resume_report()}
    # finding 073: proven zero-call crashes resolve TERMINALLY and visibly
    assert report["l1e-pre"]["classification"] == "aborted_no_call"
    assert olap.effect_row("l1e-pre")["state"] == "terminal_aborted_no_call"
    kinds = [f["fact_kind"] for f in olap.broker_facts("l1e-pre")]
    assert "no_call_abort" in kinds
    assert report["l1e-amb"]["classification"] == "effect_unknown"
    assert olap.effect_row("l1e-amb")["state"] == "effect_unknown"  # durable


def test_resume_reports_awaiting_acknowledgement_after_full_submission(olap):
    client = FakeIbkrClient(account=ACCOUNT)
    executor = BracketExecutor(olap, client)
    executor.submit_bracket(_intent(), _plan(), _capability())
    report = executor.resume_report()
    assert [r["classification"] for r in report] == ["awaiting_acknowledgement"]
    assert report[0]["call_attempts"] == 3 and report[0]["call_results"] == 3


def test_journal_refuses_illegal_promotion():
    import sqlite3 as _s
    from app.demo_execution_service import DemoExecutionError
    ledger = L1ExecutionOlap(":memory:")
    ledger.create_effect("l1e-x", "idem-x", "bracket_entry", [1, 2, 3], None)
    with pytest.raises(DemoExecutionError, match="illegal effect transition"):
        ledger.advance_effect("l1e-x", "acknowledged")
    ledger.close()


# ── the fake broker's own realism ──

def test_fake_broker_rejects_child_before_parent():
    client = FakeIbkrClient(account=ACCOUNT)
    t = translate_bracket(_plan(), instrument="EUR.USD")
    with pytest.raises(FakeBrokerRefusal, match="parent"):
        client.place_order(t.contract, t.take_profit)


def test_fake_broker_snapshots_are_immutable_facts():
    client = FakeIbkrClient(account=ACCOUNT)
    t = translate_bracket(_plan(), instrument="EUR.USD")
    client.place_order(t.contract, t.parent)
    t.parent.totalQuantity = 999999.0          # later mutation must not leak
    assert client.calls[0][1]["totalQuantity"] == 20000.0
    assert client.open_order_facts()[0]["totalQuantity"] == 20000.0
