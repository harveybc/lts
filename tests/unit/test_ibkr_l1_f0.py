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
