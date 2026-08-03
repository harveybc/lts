"""F0 corrections for auditor findings 069-074 (order 2026-08-03).

This file grows one section per F0 item, in Musashi's sequence. Sockets are
booby-trapped module-wide; everything runs on the fake client against the
shared L0/L1 SQLite ledger.
"""
import socket
from datetime import timedelta

import pytest

from app.demo_execution_service import DemoExecutionOlap
from app.ibkr_l1_executor import EFFECT_CONTRACT_SCHEMA
from app.ibkr_l1_journal import L1ExecutionOlap

from test_ibkr_l1_outbox import (
    ACCOUNT,
    Env,
    FINGERPRINT,
    NOW,
    QUOTE,
    _asset_intent,
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted in an F0 test")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


@pytest.fixture()
def env(tmp_path):
    environment = Env(tmp_path)
    yield environment
    environment.olap.close()


def _crash_before_ack(env, *, contract_con_id=None):
    """Drive one decision through submission, crashing before ack."""
    env.mint(contract_con_id=contract_con_id)
    env.decide(_asset_intent())

    def _boom(*args, **kwargs):
        raise ConnectionError("session lost before acknowledgement")

    original = env.consumer.controller.acknowledge
    env.consumer.controller.acknowledge = _boom
    with pytest.raises(ConnectionError):
        env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=2))
    env.consumer.controller.acknowledge = original
    return env.olap.nonterminal_effects()[0]["effect_id"]


# ── F0.1 (finding 072): durable immutable effect contract ──

def test_effect_contract_is_stored_and_redacts_account(env):
    env.mint()
    env.decide(_asset_intent())
    result = env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=2))[0]
    contract = env.olap.effect_contract(result["effect_id"])
    assert contract["schema"] == EFFECT_CONTRACT_SCHEMA
    assert contract["account_fingerprint"] == FINGERPRINT
    assert contract["price_decimals"] == 5
    assert contract["quantity_decimals"] == 0
    assert contract["plan"]["stop_loss"]["auxPrice"] == 1.0860
    for leg in contract["plan"].values():
        assert leg["account"] is None                  # raw id never persisted
    import json as _json
    assert ACCOUNT not in _json.dumps(contract)


def test_restart_preserves_and_enforces_expected_con_id(env):
    """Musashi 072 counterexample: a capability authorizing conId 12087792
    was re-acknowledged against conId 999. Now the stored contract governs:
    the fake reports conId 0, so resume must FAIL closed into recovery."""
    effect_id = _crash_before_ack(env, contract_con_id=12087792)
    assert env.olap.effect_contract(effect_id)["expected_con_id"] == 12087792
    outcomes = [o for o in env.consumer.resume(now=NOW + timedelta(seconds=3))
                if o["effect_id"] == effect_id]
    assert outcomes[0]["reacknowledged"] is False
    assert env.olap.effect_row(effect_id)["state"] == "terminal_cancelled"
    assert env.olap.get_state("halt") == "hold"
    verdicts = env.olap.broker_facts(effect_id, "ack_verdict")
    assert any("conId" in f for f in verdicts[-1]["fact"]["failures"])


def test_resume_after_account_change_refuses_and_holds(env):
    effect_id = _crash_before_ack(env)
    env.client.account = "DU-OTHER-9"                  # restart on wrong session
    calls_before = len(env.client.calls)
    outcomes = [o for o in env.consumer.resume(now=NOW + timedelta(seconds=3))
                if o["effect_id"] == effect_id]
    assert "does not match" in outcomes[0]["resume_refused"]
    assert env.olap.effect_row(effect_id)["state"] == "effect_unknown"
    assert env.olap.get_state("halt") == "hold"
    assert len(env.client.calls) == calls_before       # zero broker effects
    kinds = [f["fact_kind"] for f in env.olap.broker_facts(effect_id)]
    assert "resume_refusal" in kinds


def test_resume_uses_stored_rounding_not_current_config(env):
    effect_id = _crash_before_ack(env)
    env.consumer.price_decimals = 2                    # config drift after crash
    env.consumer.quantity_decimals = 3
    outcomes = [o for o in env.consumer.resume(now=NOW + timedelta(seconds=3))
                if o["effect_id"] == effect_id]
    assert outcomes[0]["reacknowledged"] is True       # contract, not config
    assert env.olap.effect_row(effect_id)["state"] == "acknowledged"


def test_legacy_effect_without_contract_refuses_resume(env):
    env.olap.create_effect("l1e-old", "idem-old", "bracket_entry",
                           [1, 2, 3], "e" * 64)
    for leg, order_id in (("parent", 1), ("take_profit", 2), ("stop_loss", 3)):
        env.olap.record_broker_fact("l1e-old", "call_attempt",
                                    {"leg": leg, "orderId": order_id})
        env.olap.record_broker_fact("l1e-old", "call_result",
                                    {"leg": leg, "orderId": order_id})
    env.olap.advance_effect("l1e-old", "submitted_pending_ack")
    outcomes = [o for o in env.consumer.resume(now=NOW + timedelta(seconds=3))
                if o["effect_id"] == "l1e-old"]
    assert outcomes[0]["resume_refused"] == "missing_effect_contract"
    assert env.olap.effect_row("l1e-old")["state"] == "effect_unknown"
    assert env.olap.get_state("halt") == "hold"


# ── F0.2 (finding 073): proven zero-call crashes resolve terminally ──

def test_zero_call_crash_resolves_terminally_and_unblocks_the_gate(env):
    # crash after the atomic capability/effect/contract commit, before any
    # broker call: effect exists, capability burned, zero attempt facts
    with env.olap.atomic_unit():
        env.olap.create_effect("l1e-crash", "idem-crash", "bracket_entry",
                               [7, 8, 9], "c" * 64)
        env.olap.consume_capability("c" * 64, "d" * 64, {"max_entries": 1},
                                    "l1e-crash")
    outcomes = [o for o in env.consumer.resume(now=NOW + timedelta(seconds=1))
                if o["effect_id"] == "l1e-crash"]
    assert outcomes[0]["classification"] == "aborted_no_call"
    assert env.olap.effect_row("l1e-crash")["state"] == "terminal_aborted_no_call"
    assert env.olap.capability_row("c" * 64)["state"] == "consumed"  # stays burned
    assert env.client.calls == []                      # provably zero calls
    # the canary gate is no longer blocked: a fresh decision executes
    env.mint()
    env.decide(_asset_intent())
    result = env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=2))[0]
    assert result["state"] == "acknowledged"


# ── F0.3 (findings 069/071): protection health + cumulative fills ──

def _entered(env, con_id=None):
    env.mint(contract_con_id=con_id)
    env.decide(_asset_intent())
    result = env.consumer.consume_entries(
        quote=QUOTE, now=NOW + timedelta(seconds=2))[0]
    assert result["state"] == "acknowledged"
    return result["effect_id"], result["order_ids"]


def test_stop_vanishing_after_fill_executes_recovery_and_reconciles_l0(env):
    """Musashi 069 counterexample: SL removed post-ack, parent filled —
    previously produced an unprotected position with halt=none."""
    env.client.auto_fill_market_orders = True
    effect_id, order_ids = _entered(env)
    env.client.drop_order(order_ids[2])                # stop loss vanishes
    env.client.fill_parent(order_ids[0], 20000.0)
    sync = env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=3))
    assert sync["protection_lost"] is True
    assert env.olap.get_state("halt") == "hold"
    assert env.client.position_facts() == []           # flattened, reconciled
    assert env.olap.effect_row(effect_id)["state"] == "terminal_flat"
    assert env.olap.open_exposures() == []
    contract = env.olap.effect_contract(effect_id)
    assert env.olap.reservation_row(
        contract["reservation_id"])["state"] == "released"


def test_stop_alteration_after_partial_fill_recovers(env):
    env.client.auto_fill_market_orders = True
    effect_id, order_ids = _entered(env)
    env.client.fill_parent(order_ids[0], 5000.0)
    first = env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=3))
    assert first["exposure"] == "opened_partial"
    env.client.alter_order(order_ids[2], auxPrice=1.0000)   # protection altered
    sync = env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=4))
    assert sync["protection_lost"] is True
    assert env.olap.get_state("halt") == "hold"
    assert env.client.position_facts() == []
    assert env.olap.effect_row(effect_id)["state"] == "terminal_flat"
    contract = env.olap.effect_contract(effect_id)
    assert env.olap.reservation_row(
        contract["reservation_id"])["state"] == "released"
    assert env.olap.open_exposures() == []


def test_cumulative_partial_fills_with_duplicates_and_restart(env):
    from app.ibkr_l1_outbox import L1OutboxConsumer
    effect_id, order_ids = _entered(env)
    parent = order_ids[0]

    env.client.fill_parent(parent, 5000.0)
    first = env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=3))
    assert first["cumulative"] == 5000.0
    assert first["exposure"] == "opened_partial"

    duplicate = env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=4))
    assert duplicate["cumulative"] == 5000.0
    assert "exposure" not in duplicate                 # no double application

    env.client.fill_parent(parent, 7000.0)
    second = env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=5))
    assert second["cumulative"] == 12000.0

    restarted = L1OutboxConsumer(                      # process restart
        env.service, env.olap, env.client, env.profile, env.gate)
    env.client.fill_parent(parent, 8000.0)
    third = restarted.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=6))
    assert third["cumulative"] == 20000.0
    assert third["reservation"] == "consumed"

    assert env.olap.open_exposures()[0]["units_open"] == 20000.0
    applied = [f["fact"]["cumulative"]
               for f in env.olap.broker_facts(effect_id, "fill_applied")]
    assert applied == [5000.0, 12000.0, 20000.0]       # monotone, no dups
    # conservation (finding 049 discipline): day risk equals the immutable
    # original reservation risk exactly once
    contract = env.olap.effect_contract(effect_id)
    reservation = env.olap.reservation_row(contract["reservation_id"])
    totals = env.olap.active_totals("2026-08-03")
    assert abs(totals["day_risk"] - reservation["original_risk_fraction"]) < 1e-12


def test_partial_fill_then_broker_cancel_recovers_and_releases_remainder(env):
    env.client.auto_fill_market_orders = True
    effect_id, order_ids = _entered(env)
    env.client.fill_parent(order_ids[0], 5000.0)
    env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=3))
    env.client.alter_order(order_ids[0], status="Cancelled")  # entry cancelled
    sync = env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=4))
    assert sync["protection_lost"] is True
    assert env.client.position_facts() == []           # 5k residual flattened
    assert env.olap.effect_row(effect_id)["state"] == "terminal_flat"
    contract = env.olap.effect_contract(effect_id)
    assert env.olap.reservation_row(
        contract["reservation_id"])["state"] == "released"
    assert env.olap.open_exposures() == []


def test_missing_filled_fact_is_never_read_as_zero(env):
    effect_id, order_ids = _entered(env)
    parent = order_ids[0]
    real = env.client.open_order_facts

    def stripped():
        facts = real()
        for fact in facts:
            if fact["orderId"] == parent:
                fact.pop("filled", None)
        return facts

    env.client.open_order_facts = stripped
    sync = env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=3))
    assert "never_zero" in sync["refused"]
    assert env.olap.effect_row(effect_id)["state"] == "effect_unknown"
    assert env.olap.get_state("halt") == "hold"


def test_position_disagreement_refuses_and_holds(env):
    effect_id, order_ids = _entered(env)
    env.client.fill_parent(order_ids[0], 5000.0)
    env.client.set_position(symbol="EUR", currency="USD", units=9000.0)  # drift
    sync = env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=3))
    assert "disagrees" in sync["refused"]
    assert env.olap.get_state("halt") == "hold"


# ── F0.4 (finding 070): exact risk-reducing preflight ──

def _filled_long_with_pending_flatten(env):
    """Entry acknowledged, fully filled, exposure open, flatten_all issued."""
    from trading_contracts import OwnerCommand
    env.client.auto_fill_market_orders = True
    effect_id, order_ids = _entered(env)
    env.client.fill_parent(order_ids[0], 20000.0)
    sync = env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=3))
    assert sync["reservation"] == "consumed"
    env.service.apply_owner_command(OwnerCommand(
        object_id="oc-f0-1", as_of=NOW + timedelta(seconds=4),
        producer={"name": "owner", "version": "0"}, trace_id="t-own",
        command="flatten_all", issuer_id="owner-1",
        exact_phrase="FLATTEN ALL DEMO POSITIONS NOW", nonce="n-f0-1",
        expires_at=NOW + timedelta(minutes=5), idempotency_key="cmd-f0-1",
    ), now=NOW + timedelta(seconds=4))
    return effect_id, order_ids


def _corrupt_flatten_delta(env, new_delta):
    import json as _json
    row = env.olap._con.execute(
        "SELECT idempotency_key, intent_json FROM decisions "
        "WHERE outcome='would_be_flatten'"
    ).fetchone()
    intent = _json.loads(row[1])
    intent["delta_units"] = new_delta
    env.olap._con.execute(
        "UPDATE decisions SET intent_json=? WHERE idempotency_key=?",
        (_json.dumps(intent), row[0]),
    )


def _place_count(client):
    return sum(1 for name, _ in client.calls if name == "place_order")


@pytest.mark.parametrize("corrupted_delta", [-40000.0, -10000.0, 20000.0])
def test_corrupted_flatten_delta_refuses_and_never_touches_the_broker(
    env, corrupted_delta
):
    """Musashi 070 counterexample: a -40000 flatten of a long 20000 SELL'd
    40000 and reversed the account. Now: exact disagreement refuses with
    zero broker calls and the position untouched."""
    _filled_long_with_pending_flatten(env)
    _corrupt_flatten_delta(env, corrupted_delta)
    places_before = _place_count(env.client)
    results = env.consumer.consume_flattens(now=NOW + timedelta(seconds=5))
    assert "never_resized" in results[0]["refused"]
    assert _place_count(env.client) == places_before   # zero submissions
    assert env.client._own_cash_units("EUR") == 20000.0
    assert env.olap.get_state("halt") == "hold"


def test_stale_position_refuses_flatten(env):
    _filled_long_with_pending_flatten(env)
    env.client.set_position(symbol="EUR", currency="USD", units=15000.0)
    places_before = _place_count(env.client)
    results = env.consumer.consume_flattens(now=NOW + timedelta(seconds=5))
    assert "disagrees" in results[0]["refused"]
    assert _place_count(env.client) == places_before
    assert env.olap.get_state("halt") == "hold"


def test_zero_position_with_flatten_intent_refuses(env):
    _filled_long_with_pending_flatten(env)
    env.client.set_position(symbol="EUR", currency="USD", units=0.0)
    results = env.consumer.consume_flattens(now=NOW + timedelta(seconds=5))
    assert "no_matching_position" in results[0]["refused"]
    assert env.olap.get_state("halt") == "hold"


def test_wrong_connected_account_refuses_flatten_before_any_read(env):
    _filled_long_with_pending_flatten(env)
    env.client.account = "DU-OTHER-9"
    places_before = _place_count(env.client)
    results = env.consumer.consume_flattens(now=NOW + timedelta(seconds=5))
    assert "not_authorized" in results[0]["refused"]
    assert _place_count(env.client) == places_before


def test_foreign_positions_never_count_toward_the_flatten(env):
    _filled_long_with_pending_flatten(env)
    env.client.set_position(symbol="EUR", currency="USD", units=5000.0,
                            account="DU-SOMEONE-ELSE")
    env.client.set_position(symbol="EUR", currency="USD", units=7000.0,
                            sec_type="FUT")
    results = env.consumer.consume_flattens(now=NOW + timedelta(seconds=5))
    assert results[0]["state"] == "terminal_flat"      # own 20k CASH matched
    assert env.client._own_cash_units("EUR") == 0.0
    survivors = {(p["account"], p["secType"], p["units"])
                 for p in env.client.position_facts()}
    assert ("DU-SOMEONE-ELSE", "CASH", 5000.0) in survivors
    assert (ACCOUNT, "FUT", 7000.0) in survivors       # untouched


# ── F0.5 (finding 074): one intent-class-aware L0 lifecycle path ──

def _flatten_decision_count(env):
    return env.olap._con.execute(
        "SELECT COUNT(*) FROM decisions WHERE outcome='would_be_flatten'"
    ).fetchone()[0]


def test_flatten_fill_routes_through_accepted_api_without_recursion(env):
    effect_id, order_ids = _filled_long_with_pending_flatten(env)
    assert _flatten_decision_count(env) == 1
    results = env.consumer.consume_flattens(now=NOW + timedelta(seconds=5))
    assert results[0]["state"] == "terminal_flat"
    assert env.olap.get_state("halt", "none") == "none"
    assert _flatten_decision_count(env) == 1           # no recursive emission
    # the reducing fill lives in the ONE chained lifecycle ledger via the
    # accepted service API
    flatten_intent_id = env.olap._con.execute(
        "SELECT json_extract(intent_json, '$.object_id') FROM decisions "
        "WHERE outcome='would_be_flatten'"
    ).fetchone()[0]
    assert env.olap.last_state(flatten_intent_id) == "filled"
    assert env.olap.open_exposures() == []


def test_reducing_overfill_holds_without_flatten_storm(env):
    from trading_contracts import ExecutionReportV2, OrderIntentV2
    effect_id, order_ids = _entered(env)
    env.client.fill_parent(order_ids[0], 20000.0)
    env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=3))
    target = env.olap.effect_contract(effect_id)["intent_object_id"]
    rogue = OrderIntentV2(
        object_id="oi2-rogue-flatten", as_of=NOW + timedelta(seconds=4),
        producer={"name": "t", "version": "0"}, trace_id="t-rogue",
        account_ref=FINGERPRINT, asset_id="fx:EUR/USD", venue="ibkr_paper",
        instrument="EUR.USD", intent_class="risk_reducing",
        reduce_action="flatten", reduce_target_order_intent_id=target,
        order_type="market", delta_units=-25000.0,
        idempotency_key="flatten:rogue",
    )
    env.olap.record_decision(
        "flatten:rogue", "would_be_flatten", None,
        rogue.model_dump_json(), None,
    )
    result = env.service.apply_execution_event(ExecutionReportV2(
        object_id="er-rogue", as_of=NOW + timedelta(seconds=5),
        producer={"name": "t", "version": "0"}, trace_id="t-rogue",
        order_intent_id="oi2-rogue-flatten", attempt_id="attempt-rogue",
        bracket_role="parent", state="filled",
        requested_units=-25000.0, filled_units=25000.0,
    ))
    assert result["emergency"] == "risk_reduction_violation_hold"
    assert env.olap.get_state("halt") == "hold"
    assert _flatten_decision_count(env) == 1           # NO flatten storm
    assert "emitted" not in result


def test_unknown_provenance_fill_remains_conservatively_unprotected(env):
    from trading_contracts import ExecutionReportV2
    result = env.service.apply_execution_event(ExecutionReportV2(
        object_id="er-ghost", as_of=NOW + timedelta(seconds=5),
        producer={"name": "t", "version": "0"}, trace_id="t-ghost",
        order_intent_id="oi2-ghost", attempt_id="attempt-ghost",
        bracket_role="parent", state="filled",
        requested_units=1000.0, filled_units=1000.0,
    ))
    assert result["emergency"] == "unprotected_exposure_hold_and_flatten"
    assert env.olap.get_state("halt") == "hold"


def test_l1_no_longer_appends_lifecycle_directly():
    from pathlib import Path
    import app.ibkr_l1_outbox as outbox_module
    source = Path(outbox_module.__file__).read_text()
    assert "append_lifecycle" not in source            # finding 074 removed


def test_kill_allows_only_exact_reduction_and_never_clears_halt(env):
    effect_id, order_ids = _filled_long_with_pending_flatten(env)
    env.olap.set_state("halt", "kill")                 # owner kill mid-flight
    results = env.consumer.consume_flattens(now=NOW + timedelta(seconds=5))
    assert results[0]["state"] == "terminal_flat"      # exact reduction runs
    assert env.olap.get_state("halt") == "kill"        # never cleared
    # no new risk under kill: L0 itself refuses the decision at the source
    env.mint(now=NOW + timedelta(seconds=6))
    decision = env.decide(_asset_intent(object_id="ai-f0-kill"),
                          now=NOW + timedelta(seconds=7))
    assert decision["outcome"] == "rejected"
    assert "halted:kill" in decision["reason"]
    assert env.consumer.consume_entries(
        quote=QUOTE, now=NOW + timedelta(seconds=8)) == []


# ── schema migration: additive, never destructive ──

def test_l0_ledger_migrates_additively_to_l1_schema(tmp_path):
    path = tmp_path / "legacy.sqlite"
    legacy = DemoExecutionOlap(path)
    legacy.record_decision("idem-legacy", "rejected", "test", None, None)
    legacy.set_state("halt", "none")
    legacy.close()
    upgraded = L1ExecutionOlap(path)
    assert upgraded.recorded_decision("idem-legacy")["outcome"] == "rejected"
    upgraded.create_effect("l1e-m", "idem-m", "bracket_entry", [1, 2, 3], None)
    upgraded.store_effect_contract("l1e-m", {"schema": "x"})
    assert upgraded.effect_contract("l1e-m") == {"schema": "x"}
    upgraded.close()
