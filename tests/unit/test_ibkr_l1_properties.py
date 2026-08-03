"""Hypothesis property tests for the F0 invariants (auditor order §3).

Property targets, after the deterministic counterexamples pass:

1. a risk-reducing order can never increase ``abs(position)`` or cross zero;
2. accepted exposure never exists without directly verified SL/TP coverage
   (any single-fact deviation is never protected);
3. cumulative fills are monotone, bounded by the requested quantity and
   conserve the original reservation; and
4. replay/restart never duplicates broker calls or exposure deltas.

SQLite-backed properties keep ``max_examples`` small: each example builds a
fresh ledger. Sockets stay booby-trapped module-wide.
"""
import socket
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from app.ibkr_l1_broker import FakeIbkrClient, translate_bracket
from app.ibkr_l1_outbox import L1OutboxConsumer
from app.ibkr_l1_recovery import verify_bracket_exact

from test_ibkr_l1_outbox import ACCOUNT, Env, NOW, QUOTE, _asset_intent
from test_ibkr_l1_recovery import _intent as _order_intent, _plan

_SQLITE_SETTINGS = settings(
    max_examples=10, deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted in a property test")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


# ── property 1: exact reduction, pure predicate ──

@given(
    position=st.floats(min_value=-1e6, max_value=1e6,
                       allow_nan=False, allow_infinity=False),
    delta=st.floats(min_value=-1e6, max_value=1e6,
                    allow_nan=False, allow_infinity=False),
)
@settings(max_examples=300, deadline=None)
def test_reduction_never_increases_abs_position_or_crosses_zero(position, delta):
    units, refusal = L1OutboxConsumer.exact_reduction_units(position, delta)
    if refusal is not None:
        assert units is None                        # refusal submits nothing
        return
    # execution allowed: units equal the proven position exactly, so the
    # post-trade position is exactly zero — no increase, no crossing
    assert units == position
    assert abs(position + delta) <= 1e-9            # exact intent agreement
    post = position - units
    assert post == 0.0
    assert abs(post) <= abs(position)


# ── property 2: any single-fact deviation is never protected ──

_MUTATIONS = st.sampled_from([
    ("parent", "status", "Cancelled"),
    ("parent", "status", "Rejected"),
    ("parent", "status", "Inactive"),
    ("take_profit", "status", "Cancelled"),
    ("take_profit", "status", "Rejected"),
    ("take_profit", "status", "Filled"),
    ("stop_loss", "status", "Cancelled"),
    ("stop_loss", "status", "Inactive"),
    ("parent", "action", "SELL"),
    ("take_profit", "action", "BUY"),
    ("stop_loss", "action", "BUY"),
    ("parent", "orderType", "LMT"),
    ("take_profit", "orderType", "MKT"),
    ("stop_loss", "orderType", "LMT"),
    ("take_profit", "lmtPrice", 1.0999),
    ("stop_loss", "auxPrice", 1.0001),
    ("parent", "totalQuantity", 19999.0),
    ("take_profit", "totalQuantity", 1.0),
    ("stop_loss", "parentId", 4242),
    ("take_profit", "parentId", 4242),
    ("parent", "account", "DU-EVIL-1"),
    ("stop_loss", "account", "DU-EVIL-1"),
    ("parent", "tif", "GTC"),
    ("take_profit", "tif", "DAY"),
    ("parent", "DROP", None),
    ("take_profit", "DROP", None),
    ("stop_loss", "DROP", None),
])


@given(mutation=_MUTATIONS)
@settings(max_examples=100, deadline=None)
def test_any_single_fact_deviation_is_never_protected(mutation):
    client = FakeIbkrClient(account=ACCOUNT)
    plan = _plan(_order_intent())
    translated = translate_bracket(plan, instrument="EUR.USD")
    for _, order in translated.legs():
        client.place_order(translated.contract, order)
    baseline = verify_bracket_exact(
        plan=plan, open_orders=client.open_order_facts(),
        instrument="EUR.USD")
    assert baseline["protected"] is True            # sanity: intact passes
    leg_ids = {"parent": 1000, "take_profit": 1001, "stop_loss": 1002}
    leg, field, value = mutation
    if field == "DROP":
        client.drop_order(leg_ids[leg])
    else:
        client.alter_order(leg_ids[leg], **{field: value})
    verdict = verify_bracket_exact(
        plan=plan, open_orders=client.open_order_facts(),
        instrument="EUR.USD")
    assert verdict["protected"] is False
    assert verdict["required_action"] == "cancel_flatten_and_global_hold"


# ── property 3: cumulative fills monotone, bounded, conserved ──

@given(
    increments=st.lists(
        st.floats(min_value=1.0, max_value=20000.0,
                  allow_nan=False, allow_infinity=False),
        min_size=1, max_size=5,
    )
)
@_SQLITE_SETTINGS
def test_cumulative_fills_are_monotone_bounded_and_conserved(increments):
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        try:
            env.mint()
            env.decide(_asset_intent())
            result = env.consumer.consume_entries(
                quote=QUOTE, now=NOW + timedelta(seconds=2))[0]
            effect_id, parent = result["effect_id"], result["order_ids"][0]
            contract = env.olap.effect_contract(effect_id)
            original = env.olap.reservation_row(
                contract["reservation_id"])["original_risk_fraction"]
            previous_cumulative = 0.0
            for step, increment in enumerate(increments):
                env.client.fill_parent(parent, increment)  # fake caps at 20k
                sync = env.consumer.sync_parent_fill(
                    effect_id, now=NOW + timedelta(seconds=3 + step))
                cumulative = sync["cumulative"]
                assert cumulative >= previous_cumulative - 1e-9   # monotone
                assert cumulative <= 20000.0 + 1e-9               # bounded
                previous_cumulative = cumulative
                if cumulative > 0:
                    exposure = env.olap.open_exposures()
                    assert exposure[0]["units_open"] == cumulative
                totals = env.olap.active_totals("2026-08-03")
                assert abs(totals["day_risk"] - original) < 1e-12  # conserved
        finally:
            env.olap.close()


# ── property 4: replay/restart never duplicates calls or exposure ──

@given(
    operations=st.lists(
        st.sampled_from(["replay_entry", "restart_resume", "sync_again"]),
        min_size=1, max_size=6,
    )
)
@_SQLITE_SETTINGS
def test_replay_and_restart_never_duplicate_broker_calls_or_exposure(operations):
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        try:
            env.mint()
            env.decide(_asset_intent())
            result = env.consumer.consume_entries(
                quote=QUOTE, now=NOW + timedelta(seconds=2))[0]
            effect_id, parent = result["effect_id"], result["order_ids"][0]
            env.client.fill_parent(parent, 20000.0)
            env.consumer.sync_parent_fill(effect_id, now=NOW + timedelta(seconds=3))
            placed_after_entry = sum(
                1 for name, _ in env.client.calls if name == "place_order")
            assert placed_after_entry == 3
            exposure_after_fill = env.olap.open_exposures()[0]["units_open"]
            consumer = env.consumer
            for step, operation in enumerate(operations):
                moment = NOW + timedelta(seconds=4 + step)
                if operation == "replay_entry":
                    env.mint(now=moment)               # fresh unused capability
                    consumer.consume_entries(quote=QUOTE, now=moment)
                elif operation == "restart_resume":
                    consumer = L1OutboxConsumer(       # process restart
                        env.service, env.olap, env.client, env.profile,
                        env.gate)
                    consumer.resume(now=moment)
                else:
                    consumer.sync_parent_fill(effect_id, now=moment)
                placed = sum(
                    1 for name, _ in env.client.calls if name == "place_order")
                assert placed == placed_after_entry    # never duplicated
                assert env.olap.open_exposures()[0]["units_open"] == (
                    exposure_after_fill
                )
        finally:
            env.olap.close()
