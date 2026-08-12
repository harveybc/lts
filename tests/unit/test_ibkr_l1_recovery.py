"""Milestone C: exact acknowledgement and executable recovery (finding 065).

The reproducer scenario that defeated the predecessor — cancelled/altered
children counting as protected, recovery existing only as a string — must
fail here, and the recovery must be an executed, journaled, idempotent
effect sequence: hold, cancel, flatten, mandatory reconciliation.
"""
import socket
from datetime import datetime, timezone

import pytest

from trading_contracts import OrderIntentV2, ProtectiveBracket, RiskEnvelope

from app.ibkr_l1_adapter import L1ExecutionError, build_bracket
from app.ibkr_l1_broker import FakeIbkrClient
from app.ibkr_l1_executor import BracketExecutor, CapabilityRecord, L1EffectUnknown
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_l1_recovery import (
    BracketLifecycleController,
    verify_bracket_exact,
)

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


def _intent(units=20000.0, sl=1.0850, tp=1.0910, idem="idem-c-1"):
    return OrderIntentV2(
        object_id=f"oi2-{idem}", as_of=NOW,
        producer={"name": "lts.demo_execution_service", "version": "0.2.0"},
        trace_id="t-c", account_ref="c0ff137a3cc1a363",
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


def _plan(intent=None):
    return build_bracket(intent or _intent(), parent_order_id=1000,
                         account=ACCOUNT, price_decimals=5, quantity_decimals=0)


def _capability(suffix="1"):
    return CapabilityRecord(
        capability_sha256="a" * 63 + suffix,
        nonce_sha256="b" * 63 + suffix,
        metadata={"quantity_ceiling": 20000.0, "max_entries": 1},
    )


def _submitted(olap, client_factory=None, **client_kw):
    client = (client_factory or FakeIbkrClient)(account=ACCOUNT, **client_kw)
    executor = BracketExecutor(olap, client)
    plan = _plan()
    result = executor.submit_bracket(_intent(), plan, _capability())
    controller = BracketLifecycleController(olap, client)
    return client, controller, plan, result["effect_id"]


def _cancels(client):
    return [fact["orderId"] for name, fact in client.calls if name == "cancel_order"]


def _flattens(client):
    return [fact for name, fact in client.calls
            if name == "place_order" and fact["orderId"] >= 9000]


# ── the exact verifier ──

def test_intact_bracket_is_protected_and_acknowledged(olap):
    client, controller, plan, effect_id = _submitted(olap)
    verdict = controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    assert verdict["protected"] is True and verdict["failures"] == []
    assert olap.effect_row(effect_id)["state"] == "acknowledged"


def test_filled_parent_with_open_children_is_protected(olap):
    client, controller, plan, effect_id = _submitted(olap)
    client.fill_parent(1000, 20000.0)
    verdict = controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    assert verdict["protected"] is True


def test_filled_parent_absent_with_position_and_children_is_protected(olap):
    """Observed defect: TWS removes a filled parent from open orders while
    its position and both correctly linked protection children remain."""
    client, controller, plan, effect_id = _submitted(olap)
    client.fill_parent(1000, 20000.0)
    client.drop_order(1000)

    verdict = controller.acknowledge(
        effect_id, plan, instrument="EUR.USD")

    assert verdict["protected"] is True
    assert verdict["parent_evidence"] == "direct_execution"
    assert olap.effect_row(effect_id)["state"] == "acknowledged"
    assert olap.get_state("halt", "none") == "none"
    assert [fact["orderId"] for fact in client.open_order_facts()] == [
        1001, 1002]
    assert olap.broker_facts(effect_id, "parent_fill_execution")


def test_absent_parent_proof_never_adopts_foreign_account_position(olap):
    client, controller, plan, effect_id = _submitted(olap)
    client.fill_parent(1000, 20000.0)
    client.drop_order(1000)
    client.set_position(symbol="EUR", currency="USD", units=0.0)
    client.set_position(
        symbol="EUR", currency="USD", units=20000.0,
        account="DU7654321",
    )

    verdict = controller.acknowledge(
        effect_id, plan, instrument="EUR.USD")

    assert verdict["protected"] is False
    assert any("current position 0" in failure
               for failure in verdict["failures"])
    assert olap.get_state("halt") == "hold"


def test_audit_065_reproducer_scenario_now_fails_closed(olap):
    """TP altered to a cancelled market order, SL rejected at a wrong price:
    the predecessor called this protected; it must recover instead."""
    client, controller, plan, effect_id = _submitted(olap)
    client.alter_order(1001, orderType="MKT", lmtPrice=None, status="Cancelled")
    client.alter_order(1002, auxPrice=1.2000, status="Rejected")
    verdict = controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    assert verdict["protected"] is False
    assert any("take_profit" in f for f in verdict["failures"])
    assert any("stop_loss" in f for f in verdict["failures"])
    # recovery EXECUTED: hold persisted, open parent cancelled, terminal state
    assert olap.get_state("halt") == "hold"
    assert 1000 in _cancels(client)
    assert olap.effect_row(effect_id)["state"] == "terminal_cancelled"


@pytest.mark.parametrize("mutation", [
    {"order_id": 1001, "fields": {"status": "Cancelled"}},
    {"order_id": 1002, "fields": {"status": "Rejected"}},
    {"order_id": 1002, "fields": {"status": "Inactive"}},
    {"order_id": 1001, "fields": {"orderType": "MKT"}},
    {"order_id": 1001, "fields": {"lmtPrice": 1.0999}},
    {"order_id": 1002, "fields": {"auxPrice": 1.0000}},
    {"order_id": 1002, "fields": {"account": "DU7654321"}},
    {"order_id": 1001, "fields": {"action": "BUY"}},
    {"order_id": 1002, "fields": {"totalQuantity": 10000.0}},
    {"order_id": 1001, "fields": {"parentId": 999}},
    {"order_id": 1000, "fields": {"tif": "GTC"}},
    {"order_id": 1002, "fields": {"account": None}},
    {"order_id": 1001, "fields": {"contract": {"secType": "CASH", "symbol": "USD",
                                               "currency": "CAD",
                                               "exchange": "IDEALPRO", "conId": 0}}},
])
def test_any_identity_or_status_deviation_is_not_protected(olap, mutation):
    client, controller, plan, effect_id = _submitted(olap)
    client.alter_order(mutation["order_id"], **mutation["fields"])
    facts = client.open_order_facts()
    verdict = verify_bracket_exact(plan=plan, open_orders=facts,
                                   instrument="EUR.USD")
    assert verdict["protected"] is False
    assert verdict["required_action"] == "cancel_flatten_and_global_hold"


def test_missing_leg_is_never_protected(olap):
    client, controller, plan, effect_id = _submitted(olap)
    client.drop_order(1002)
    verdict = verify_bracket_exact(
        plan=plan, open_orders=client.open_order_facts(), instrument="EUR.USD")
    assert verdict["protected"] is False
    assert any("no direct broker evidence" in f for f in verdict["failures"])


def test_empty_broker_evidence_is_never_success():
    verdict = verify_bracket_exact(plan=_plan(), open_orders=[],
                                   instrument="EUR.USD")
    assert verdict["protected"] is False
    assert len(verdict["failures"]) == 3


def test_expected_con_id_mismatch_is_not_protected(olap):
    client, controller, plan, effect_id = _submitted(olap)
    verdict = verify_bracket_exact(
        plan=plan, open_orders=client.open_order_facts(),
        instrument="EUR.USD", expected_con_id=12087792)
    assert verdict["protected"] is False           # fake reports conId 0


# ── executed recovery ──

def test_missing_stop_loss_triggers_hold_cancel_and_terminal(olap):
    client, controller, plan, effect_id = _submitted(olap)
    client.drop_order(1002)
    verdict = controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    assert verdict["protected"] is False
    assert olap.get_state("halt") == "hold"
    assert sorted(_cancels(client)) == [1000, 1001]   # open legs cancelled
    assert olap.effect_row(effect_id)["state"] == "terminal_cancelled"
    kinds = [f["fact_kind"] for f in olap.broker_facts(effect_id)]
    for required in ("recovery_hold", "recovery_cancel_attempt",
                     "recovery_cancel_result", "recovery_terminal"):
        assert required in kinds


def test_partial_fill_before_protection_flattens_and_reconciles(olap):
    client, controller, plan, effect_id = _submitted(olap)
    client.auto_fill_market_orders = True
    client.fill_parent(1000, 5000.0)                  # partial fill, long 5000
    client.drop_order(1002)                           # SL never materialized
    verdict = controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    assert verdict["protected"] is False
    flattens = _flattens(client)
    assert len(flattens) == 1
    assert flattens[0]["action"] == "SELL"
    assert flattens[0]["totalQuantity"] == 5000.0
    assert client.position_facts() == []              # reconciled flat
    assert olap.effect_row(effect_id)["state"] == "terminal_flat"
    kinds = [f["fact_kind"] for f in olap.broker_facts(effect_id)]
    assert "recovery_reconciled_flat" in kinds


def test_async_flatten_polls_without_duplicate_then_reconciles(olap):
    client, controller, plan, effect_id = _submitted(olap)
    client.fill_parent(1000, 20000.0)                 # full fill, long 20000
    client.drop_order(1002)
    # auto_fill stays False: the flatten order is placed but never fills
    verdict = controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    assert verdict["recovery"]["complete"] is False
    assert olap.effect_row(effect_id)["state"] == "recovering"
    assert olap.get_state("halt") == "hold"
    assert len(_flattens(client)) == 1
    pending = controller.recover(effect_id, plan, instrument="EUR.USD")
    assert pending["state"] == "recovering"
    assert len(_flattens(client)) == 1
    attempt = olap.broker_facts(effect_id, "recovery_flatten_attempt")[-1]
    flatten_id = attempt["fact"]["orderId"]
    client.alter_order(
        flatten_id, status="Filled", filled=20000.0, remaining=0.0
    )
    client.set_position(symbol="EUR", currency="USD", units=0.0)
    retry = controller.recover(effect_id, plan, instrument="EUR.USD")
    assert retry["complete"] is True and retry["state"] == "terminal_flat"
    assert olap.get_state("halt") == "hold"           # never cleared by code


def test_cancel_failure_journals_unknown_then_retry_completes(olap):
    client, controller, plan, effect_id = _submitted(olap)
    client.drop_order(1002)
    client.fail_cancel = True
    verdict = controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    assert verdict["recovery"]["complete"] is False
    assert olap.effect_row(effect_id)["state"] == "effect_unknown"
    kinds = [f["fact_kind"] for f in olap.broker_facts(effect_id)]
    assert "recovery_failure" in kinds
    client.fail_cancel = False
    retry = controller.recover(effect_id, plan, instrument="EUR.USD")
    assert retry["complete"] is True
    assert olap.effect_row(effect_id)["state"] == "terminal_cancelled"


def test_recovery_is_idempotent_after_terminal(olap):
    client, controller, plan, effect_id = _submitted(olap)
    client.drop_order(1002)
    controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    calls_after = len(client.calls)
    replay = controller.recover(effect_id, plan, instrument="EUR.USD")
    assert replay["replayed"] is True and replay["complete"] is True
    assert len(client.calls) == calls_after           # zero new broker effects


def test_owner_kill_is_honored_and_never_downgraded(olap):
    client, controller, plan, effect_id = _submitted(olap)
    olap.set_state("halt", "kill")                    # owner kill mid-lifecycle
    client.drop_order(1002)
    verdict = controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    assert verdict["protected"] is False
    assert olap.get_state("halt") == "kill"           # kill outranks hold
    assert 1000 in _cancels(client)                   # risk-reducing still runs
    # and no new risk can be submitted while killed (executor gate)
    executor = BracketExecutor(olap, client)
    with pytest.raises(L1ExecutionError, match="hold"):
        executor.submit_bracket(_intent(idem="idem-c-2"), plan, _capability("2"))


def test_disconnect_mid_submission_reconciles_via_acknowledge(olap):
    client = FakeIbkrClient(account=ACCOUNT, fail_on_place_call=3)
    executor = BracketExecutor(olap, client)
    plan = _plan()
    with pytest.raises(L1EffectUnknown):
        executor.submit_bracket(_intent(), plan, _capability())
    effect_id = olap.effect_by_key("idem-c-1")["effect_id"]
    client.fail_on_place_call = None                  # link restored
    controller = BracketLifecycleController(olap, client)
    verdict = controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    assert verdict["protected"] is False              # SL leg never reached TWS
    assert sorted(_cancels(client)) == [1000, 1001]
    assert olap.effect_row(effect_id)["state"] == "terminal_cancelled"
    assert olap.get_state("halt") == "hold"


def test_acknowledge_refuses_pre_effect_states(olap):
    olap.create_effect("l1e-p", "idem-p", "bracket_entry", [1, 2, 3], "e" * 64)
    controller = BracketLifecycleController(olap, FakeIbkrClient(account=ACCOUNT))
    with pytest.raises(L1ExecutionError, match="cannot be acknowledged"):
        controller.acknowledge("l1e-p", _plan(), instrument="EUR.USD")


def test_acknowledged_effect_replays_without_broker_reads(olap):
    client, controller, plan, effect_id = _submitted(olap)
    controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    reads_after = len(client.calls)
    replay = controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    assert replay["replayed"] is True and replay["protected"] is True
    assert len(client.calls) == reads_after


# ── finding 100: post-1100 source hierarchy and bounded convergence ──


class PoisonedCacheClient(FakeIbkrClient):
    """Reproduces the 2026-08-04 incident: after a 1100/1102 blip the
    position cache reports a poisoned value while server-side execution
    reports carry the truth."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._connectivity = []
        self._execution_units = {}
        self.poisoned_positions = None

    def connectivity_events(self):
        return list(self._connectivity)

    def execution_units_for_order(self, order_id):
        return self._execution_units.get(int(order_id))

    def position_facts(self):
        if self.poisoned_positions is not None:
            return list(self.poisoned_positions)
        return super().position_facts()


def _poisoned_recovery(olap):
    client, controller, plan, effect_id = _submitted(
        olap, client_factory=PoisonedCacheClient)
    client.fill_parent(1000, 20000.0)
    client.drop_order(1002)
    verdict = controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    assert verdict["recovery"]["complete"] is False
    attempt = olap.broker_facts(effect_id, "recovery_flatten_attempt")[-1]
    flatten_id = attempt["fact"]["orderId"]
    client.alter_order(flatten_id, status="Filled", filled=20000.0,
                       remaining=0.0)
    # The blip: caches poisoned (+20000 ghost), executions truthful.
    client._connectivity = [(1100, "2026-08-04T04:17:55+00:00"),
                            (1102, "2026-08-04T04:17:55+00:00")]
    client._execution_units[int(flatten_id)] = -20000.0   # SELL flatten
    client.poisoned_positions = [{
        "symbol": "EUR", "currency": "USD", "secType": "CASH",
        "account": ACCOUNT, "conId": 554321987, "units": 20000.0,
    }]
    return client, controller, plan, effect_id, flatten_id


def test_suspect_cache_never_trusts_poison_and_never_flip_flops(olap):
    client, controller, plan, effect_id, _fid = _poisoned_recovery(olap)
    for _pass in range(4):
        result = controller.recover(effect_id, plan, instrument="EUR.USD")
        assert result["state"] == "recovering"        # never effect_unknown
        assert result["derived_units"] == pytest.approx(0.0)
    suspects = olap.broker_facts(effect_id, "suspect_cache")
    assert len(suspects) == 4
    assert olap.effect_row(effect_id)["state"] == "recovering"
    assert not olap.broker_facts(effect_id, "recovery_reconciled_flat")


def test_bounded_agreement_converges_and_records_lineage(olap):
    client, controller, plan, effect_id, _fid = _poisoned_recovery(olap)
    controller.recover(effect_id, plan, instrument="EUR.USD")  # disagree
    client.poisoned_positions = []                     # cache heals: flat
    outcomes = []
    for _pass in range(3):
        outcomes.append(
            controller.recover(effect_id, plan, instrument="EUR.USD"))
    assert [o["state"] for o in outcomes[:2]] == ["recovering",
                                                  "recovering"]
    assert outcomes[2]["complete"] is True
    assert outcomes[2]["state"] == "terminal_flat"
    convergence = olap.broker_facts(effect_id, "cache_convergence")
    assert len(convergence) == 1
    fact = convergence[0]["fact"]
    assert fact["samples"] == 3
    assert fact["source_hierarchy"][0] == "executions"
    assert fact["seconds_since_connectivity_loss"] is not None
    samples = olap.broker_facts(effect_id, "cache_agreement_sample")
    assert [s["fact"]["agree"] for s in samples] == [
        False, True, True, True]
    assert olap.get_state("halt") == "hold"            # never code-cleared


def test_underivable_position_stays_held_under_suspicion(olap):
    client, controller, plan, effect_id, fid = _poisoned_recovery(olap)
    del client._execution_units[int(fid)]              # no execution report
    result = controller.recover(effect_id, plan, instrument="EUR.USD")
    assert result["state"] == "recovering"
    assert result["derived_units"] is None
    assert olap.broker_facts(effect_id, "suspect_cache")


def test_no_connectivity_events_keeps_original_behavior(olap):
    client, controller, plan, effect_id = _submitted(olap)
    client.fill_parent(1000, 20000.0)
    client.drop_order(1002)
    controller.acknowledge(effect_id, plan, instrument="EUR.USD")
    attempt = olap.broker_facts(effect_id, "recovery_flatten_attempt")[-1]
    client.alter_order(int(attempt["fact"]["orderId"]), status="Filled",
                       filled=20000.0, remaining=0.0)
    client.set_position(symbol="EUR", currency="USD", units=0.0)
    result = controller.recover(effect_id, plan, instrument="EUR.USD")
    assert result["complete"] is True                  # no extra samples
    assert not olap.broker_facts(effect_id, "cache_agreement_sample")
