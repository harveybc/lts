"""Milestone D: the accepted L0 path drives the L1 effects consumer (066).

The outbox is the L0 decisions table; quantity comes from L0 plan_units;
profile/capability are ceilings that refuse, never resize; the canary
sequence long -> reconciled flat -> short -> reconciled flat is enforced by
the journal. Sockets stay booby-trapped: the entire integration runs on the
fake client.
"""
import hashlib
import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from trading_contracts import (
    AssetIntent,
    BrokerCapabilitySnapshot,
    InstrumentCapability,
    OwnerCommand,
)

from app.demo_execution_service import (
    DemoExecutionConfig,
    DemoExecutionService,
    ZeroNetworkSink,
)
from app.ibkr_l1_adapter import L1Profile
from app.ibkr_l1_broker import FakeIbkrClient
from app.ibkr_l1_capability import CapabilityGate
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_l1_outbox import L1OutboxConsumer

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from mint_paper_capability import mint_payload, write_capability  # noqa: E402

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
ACCOUNT = "DU-TEST-1"
FINGERPRINT = hashlib.sha256(ACCOUNT.encode()).hexdigest()[:16]
ARTIFACT = "sha256:" + "a" * 64
QUOTE = {"bid": 1.08790, "ask": 1.08810, "time": NOW}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted in the L1 outbox path")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


def _profile(tmp_path, **overrides):
    payload = {
        "schema_version": "lts.ibkr.paper.l1.profile.v2",
        "venue": "ibkr_paper",
        "environment": "paper",
        "host": "127.0.0.1",
        "port": 7497,
        "client_id": 77,
        "account_fingerprint_algorithm": "account_id_sha256_16",
        "account_fingerprint": FINGERPRINT,
        "instrument": "EUR.USD",
        "asset_id": "fx:EUR/USD",
        "max_orders_this_activation": 2,
        "quantity_ceiling": 20000.0,
        "stop_distance_price_max": 0.0020,
        "take_profit_distance_price_max": 0.0040,
        "max_spread_price": 0.0003,
    }
    payload.update(overrides)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload))
    return L1Profile.load(path)


def _snapshot(evidence="live_observed", fingerprint=FINGERPRINT):
    return BrokerCapabilitySnapshot(
        object_id="cap-l1", as_of=NOW, producer={"name": "t", "version": "0"},
        trace_id="t-1", venue="ibkr_paper",
        account_fingerprint=fingerprint, environment="paper",
        capability_evidence=evidence,
        source_artifact_hash="sha256:" + "f" * 64, source_observed_at=NOW,
        instruments=[InstrumentCapability(
            instrument="EUR.USD", tradeable=True, shortable=True,
            min_units=20000.0, unit_step=20000.0, price_decimals=5,
            margin_rate=0.03, native_stop_loss=True, native_take_profit=True,
            native_bracket=True,
        )],
    )


def _asset_intent(object_id="ai-l1-1", exposure=0.5, sl=1.0860, tp=1.0910,
                  as_of=NOW):
    return AssetIntent(
        object_id=object_id, as_of=as_of,
        valid_until=as_of + timedelta(hours=4),
        producer={"name": "provider.mechanics", "version": "0"},
        trace_id="t-1", cell_id="fx:EUR/USD@4h:mech:policy",
        asset_id="fx:EUR/USD", action="target", target_exposure=exposure,
        risk_geometry={"mode": "fixed_price", "stop_price": sl,
                       "take_profit_price": tp},
        artifact_hash=ARTIFACT,
    )


class Env:
    def __init__(self, tmp_path, **profile_overrides):
        config = DemoExecutionConfig.from_dict({
            "venue": "ibkr_paper",
            "account_fingerprint": FINGERPRINT,
            "environment": "paper",
            "database_path": str(tmp_path / "l1_demo.sqlite"),
            "risk_fraction_at_stop": 0.005,
            "max_overshoot_ratio": 0.25,
            "gross_notional_fraction_max": 0.10,
            "margin_fraction_max": 0.10,
            "daily_loss_budget_fraction": 0.02,
            "max_concurrent_positions": 3,
            "signal_max_age_seconds": 300.0,
            "owner_issuer_allowlist": ["owner-1"],
            "command_phrases": {
                "hold": "HOLD ALL DEMO TRADING NOW",
                "kill": "KILL ALL DEMO TRADING NOW",
                "flatten_all": "FLATTEN ALL DEMO POSITIONS NOW",
                "cancel_pending": "CANCEL ALL PENDING DEMO ENTRIES NOW",
            },
            "asset_instrument_bindings": {"fx:EUR/USD": "EUR.USD"},
        })
        self.olap = L1ExecutionOlap(config.database_path)
        self.service = DemoExecutionService(config, self.olap, ZeroNetworkSink())
        self.client = FakeIbkrClient(
            account=ACCOUNT, auto_fill_market_orders=True)
        self.profile = _profile(tmp_path, **profile_overrides)
        self.store = tmp_path / "capstore"
        self.gate = CapabilityGate(self.store)
        self.consumer = L1OutboxConsumer(
            self.service, self.olap, self.client, self.profile, self.gate,
            price_decimals=5, quantity_decimals=0,
        )
        self._nonce_counter = 0

    def mint(self, ceiling=20000.0, now=None, contract_con_id=None):
        payload = mint_payload(
            self.profile, quantity_ceiling=ceiling,
            max_risk_fraction_at_stop=0.005, validity_seconds=900,
            contract_con_id=contract_con_id, now=now or NOW,
        )
        return write_capability(payload, self.store)

    def decide(self, intent, now=None):
        return self.service.process_intent(
            intent, _snapshot(), equity=250_000.0, reference_price=1.0880,
            instrument="EUR.USD", now=now or NOW + timedelta(seconds=1),
        )

    def flatten_all(self, nonce, now=None):
        now = now or NOW + timedelta(seconds=10)
        return self.service.apply_owner_command(OwnerCommand(
            object_id=f"oc-{nonce}", as_of=now,
            producer={"name": "owner", "version": "0"}, trace_id="t-own",
            command="flatten_all", issuer_id="owner-1",
            exact_phrase="FLATTEN ALL DEMO POSITIONS NOW", nonce=nonce,
            expires_at=now + timedelta(minutes=5),
            idempotency_key=f"cmd-{nonce}",
        ), now=now)


@pytest.fixture()
def env(tmp_path):
    environment = Env(tmp_path)
    yield environment
    environment.olap.close()


def _place_calls(client):
    return [fact for name, fact in client.calls if name == "place_order"]


# ── the full canary: long -> flat -> short -> flat ──

def test_full_canary_long_flat_short_flat(env):
    env.mint()
    # LONG: L0 sizes account-relatively to exactly the 20k ceiling
    decision = env.decide(_asset_intent())
    assert decision["outcome"] == "would_be_order"
    assert decision["delta_units"] == 20000.0          # L0 plan_units, not profile

    results = env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=2))
    assert len(results) == 1 and results[0]["protected"] is True
    assert results[0]["state"] == "acknowledged"
    long_effect = results[0]["effect_id"]
    long_parent = results[0]["order_ids"][0]
    long_order_intent = json.loads(
        env.olap.decision_intent_json(results[0]["idempotency_key"]))
    assert env.olap.last_state(long_order_intent["object_id"]) == "accepted"

    env.client.fill_parent(long_parent, 20000.0)
    fill = env.consumer.sync_parent_fill(long_effect, now=NOW + timedelta(seconds=3))
    assert fill["reservation"] == "consumed" and fill["exposure"] == "opened"
    assert env.olap.open_exposures()[0]["units_open"] == 20000.0

    # RECONCILED FLAT via the accepted owner-command path
    command = env.flatten_all("nonce-1")
    assert command["accepted"] is True
    flat = env.consumer.consume_flattens(now=NOW + timedelta(seconds=11))
    assert [f["state"] for f in flat] == ["terminal_flat"]
    assert env.client.position_facts() == []           # direct broker fact
    assert env.olap.open_exposures() == []
    assert env.olap.effect_row(long_effect)["state"] == "terminal_flat"

    # SHORT: only now possible; needs a fresh owner capability
    env.mint(now=NOW + timedelta(seconds=12))
    decision2 = env.decide(
        _asset_intent(object_id="ai-l1-2", exposure=-0.5, sl=1.0900, tp=1.0850),
        now=NOW + timedelta(seconds=13),
    )
    assert decision2["outcome"] == "would_be_order"
    assert decision2["delta_units"] == -20000.0
    results2 = env.consumer.consume_entries(
        quote={"bid": 1.08790, "ask": 1.08810, "time": NOW + timedelta(seconds=13)},
        now=NOW + timedelta(seconds=14),
    )
    assert results2[0]["state"] == "acknowledged"
    short_effect = results2[0]["effect_id"]
    short_parent = results2[0]["order_ids"][0]
    env.client.fill_parent(short_parent, 20000.0)
    fill2 = env.consumer.sync_parent_fill(short_effect, now=NOW + timedelta(seconds=15))
    assert fill2["exposure"] == "opened"
    assert env.olap.open_exposures()[0]["units_open"] == -20000.0

    env.flatten_all("nonce-2", now=NOW + timedelta(seconds=20))
    flat2 = env.consumer.consume_flattens(now=NOW + timedelta(seconds=21))
    assert [f["state"] for f in flat2] == ["terminal_flat"]

    # end state: everything terminal, flat, conserved and journaled
    assert env.client.position_facts() == []
    assert env.olap.open_exposures() == []
    assert env.olap.nonterminal_effects() == []
    assert env.olap.l1_entry_count() == 2
    placed = _place_calls(env.client)
    assert len(placed) == 8                            # 3 + flatten + 3 + flatten
    flatten_actions = [
        p["action"] for p in placed
        if p["transmit"] and p["orderType"] == "MKT" and not p["parentId"]
    ]
    assert flatten_actions == ["SELL", "BUY"]


def test_parent_fill_proof_survives_open_order_and_execution_cache_eviction(env):
    env.mint()
    env.decide(_asset_intent())
    result = env.consumer.consume_entries(
        quote=QUOTE, now=NOW + timedelta(seconds=2))[0]
    effect_id = result["effect_id"]
    parent_id = result["order_ids"][0]
    env.client.fill_parent(parent_id, 20000.0)
    env.client.drop_order(parent_id)

    first = env.consumer.sync_parent_fill(
        effect_id, now=NOW + timedelta(seconds=3))
    assert first["exposure"] == "opened"
    assert env.olap.get_state("halt", "none") == "none"

    # A process/cache transition can remove the live execution cache. The
    # append-only broker fact remains usable, but current position and both
    # child orders are still re-read and required.
    env.client.drop_execution_fact(parent_id)
    second = env.consumer.sync_parent_fill(
        effect_id, now=NOW + timedelta(seconds=4))
    assert second["position_reconciled"] is True
    assert env.olap.get_state("halt", "none") == "none"
    assert len(env.olap.broker_facts(
        effect_id, "parent_fill_execution")) == 1


def test_retained_parent_fill_never_masks_missing_child(env):
    env.mint()
    env.decide(_asset_intent())
    result = env.consumer.consume_entries(
        quote=QUOTE, now=NOW + timedelta(seconds=2))[0]
    effect_id = result["effect_id"]
    parent_id, _take_id, stop_id = result["order_ids"]
    env.client.fill_parent(parent_id, 20000.0)
    env.client.drop_order(parent_id)
    env.client.drop_order(stop_id)

    sync = env.consumer.sync_parent_fill(
        effect_id, now=NOW + timedelta(seconds=3))

    assert sync["protection_lost"] is True
    assert any("stop_loss" in failure for failure in sync["failures"])
    assert env.olap.get_state("halt") == "hold"


# ── ceilings refuse, never resize ──

def test_quantity_above_profile_ceiling_is_rejected_not_resized(tmp_path):
    env = Env(tmp_path, quantity_ceiling=10000.0)
    try:
        env.mint(ceiling=10000.0)
        decision = env.decide(_asset_intent())
        assert decision["delta_units"] == 20000.0      # L0 sized under ITS caps
        results = env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=2))
        assert results[0]["state"] == "terminal_rejected"
        assert "never_resized" in results[0]["reason"]
        assert _place_calls(env.client) == []          # zero broker effects
    finally:
        env.olap.close()


def test_synthetic_capability_evidence_is_refused(env):
    env.mint()
    env.service.process_intent(
        _asset_intent(), _snapshot(), equity=250_000.0, reference_price=1.0880,
        instrument="EUR.USD", now=NOW + timedelta(seconds=1),
    )
    # forge the decision evidence downgrade directly in the ledger
    env.olap._con.execute(
        "UPDATE decisions SET capability_evidence='synthetic_fixture'")
    results = env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=2))
    assert results[0]["state"] == "terminal_rejected"
    assert "live_observed" in results[0]["reason"]
    assert _place_calls(env.client) == []


@pytest.mark.parametrize("quote,reason", [
    ({"bid": 1.08790, "ask": 1.08850, "time": NOW}, "spread"),
    ({"bid": 1.08790, "ask": 1.08810,
      "time": NOW - timedelta(seconds=300)}, "stale"),
    ({"bid": 1.08790, "ask": 1.08810,
      "time": NOW + timedelta(seconds=300)}, "future"),
    ({"bid": 1.08810, "ask": 1.08790, "time": NOW}, "invalid"),
    (None, "quote_missing"),
])
def test_bad_quotes_defer_and_never_destroy_the_decision(env, quote, reason):
    env.mint()
    env.decide(_asset_intent())
    results = env.consumer.consume_entries(quote=quote, now=NOW + timedelta(seconds=2))
    assert results[0]["state"] == "deferred"
    assert reason in results[0]["reason"]
    assert _place_calls(env.client) == []
    assert env.olap.effect_by_key(results[0]["idempotency_key"]) is None
    # the same pending decision executes once a healthy quote returns
    retry = env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=3))
    assert retry[0]["state"] == "acknowledged"


def test_stale_decision_is_terminally_rejected(env):
    env.mint()
    env.decide(_asset_intent())
    late = NOW + timedelta(seconds=400)
    results = env.consumer.consume_entries(
        quote={"bid": 1.08790, "ask": 1.08810, "time": late}, now=late)
    assert results[0]["state"] == "terminal_rejected"
    assert "decision_stale" in results[0]["reason"]
    assert _place_calls(env.client) == []


def test_geometry_beyond_profile_distance_ceiling_is_refused(env):
    env.mint()
    env.decide(_asset_intent(sl=1.0850))               # 30 pips > 20 pip max
    results = env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=2))
    assert results[0]["state"] == "terminal_rejected"
    assert "stop_distance" in results[0]["reason"]


def test_entry_budget_exhaustion_is_refused(tmp_path):
    env = Env(tmp_path, max_orders_this_activation=1)
    try:
        env.mint()
        env.olap.create_effect("l1e-old", "idem-old", "bracket_entry",
                               [1, 2, 3], "f" * 64)
        env.olap.advance_effect("l1e-old", "terminal_cancelled")
        env.decide(_asset_intent())
        results = env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=2))
        assert results[0]["state"] == "terminal_rejected"
        assert "entry_budget" in results[0]["reason"]
    finally:
        env.olap.close()


# ── deferrals: recoverable, never silently dropped ──

def test_no_capability_defers_without_any_socket_or_effect(env):
    env.decide(_asset_intent())
    results = env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=2))
    assert results[0]["state"] == "deferred"
    assert "no_capability" in results[0]["reason"]
    assert env.client.calls == []                      # zero broker reads/writes
    assert env.olap.effect_by_key(results[0]["idempotency_key"]) is None
    # once the owner mints, the same pending decision proceeds
    env.mint()
    retry = env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=3))
    assert retry[0]["state"] == "acknowledged"


def test_new_entry_is_impossible_while_any_effect_is_nonterminal(env):
    env.mint()
    env.olap.create_effect("l1e-live", "idem-live", "bracket_entry",
                           [7, 8, 9], "e" * 64)
    env.decide(_asset_intent())
    results = env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=2))
    assert results[0]["state"] == "deferred"
    assert results[0]["reason"] == "previous_effect_not_terminal"
    assert _place_calls(env.client) == []


def test_halt_defers_entry_consumption(env):
    env.mint()
    env.decide(_asset_intent())
    env.olap.set_state("halt", "hold")
    results = env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=2))
    assert results[0]["state"] == "deferred"
    assert "halted" in results[0]["reason"]


# ── crash between submission and acknowledgement ──

def test_crash_before_ack_resumes_through_exact_acknowledgement(env):
    env.mint()
    env.decide(_asset_intent())

    def _boom(*args, **kwargs):
        raise ConnectionError("session lost before acknowledgement")

    original = env.consumer.controller.acknowledge
    env.consumer.controller.acknowledge = _boom
    with pytest.raises(ConnectionError):
        env.consumer.consume_entries(quote=QUOTE, now=NOW + timedelta(seconds=2))
    env.consumer.controller.acknowledge = original

    effect = env.olap.nonterminal_effects()[0]
    assert effect["state"] == "submitted_pending_ack"  # durable, not success
    outcomes = env.consumer.resume(now=NOW + timedelta(seconds=3))
    resumed = [o for o in outcomes if o["effect_id"] == effect["effect_id"]]
    assert resumed[0]["reacknowledged"] is True
    assert resumed[0]["state"] == "acknowledged"
