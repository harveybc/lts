"""Acceptance tests P01–P12 for the offline hourly forward pool.

No test opens a broker socket, reads a live ledger, or starts a service unit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.forward_pool.accounts import Position, RouteBook, VirtualAccount
from app.forward_pool.adapter import AGENT_STATE_NONE, CODEC, SCHEMA, authenticate_bundle, greedy_signal
from app.forward_pool.cohort import (
    SELECTED70_COLUMNS,
    SELECTED70_HISTORICAL,
    SELECTED70_MODEL_HEX,
    CandidateSpec,
    PoolService,
    WindowMetrics,
    explain_divergence,
    performance_report,
)
from app.forward_pool.core import BusinessContract, PoolRefusal, command_checked, command_for, order_units
from app.forward_pool.snapshot import HourlyBar, materialize

NS = 1_700_000_000_000_000_000
HASH = "sha256:" + "ab" * 32
ROOT = Path(__file__).resolve().parents[2] / "app" / "forward_pool"


def recipe(columns=("RAW_CLOSE_LOG_RETURN",), policy="fixed_train", agent=AGENT_STATE_NONE):
    return {
        "list": {"columns": list(columns)},
        "scaler": {"policy": policy, "derivation": "fixed train fit"},
        "scaler_sha256": HASH,
        "business": {"agent_state": agent},
        "tensor": {"window": {"observations": 13, "stride_rows": 2}},
        "representation": {"groups": {"groups": {str(i): {} for i in range(28)}}},
    }


def manifest(columns=("RAW_CLOSE_LOG_RETURN",), **overrides):
    body = {
        "schema": SCHEMA,
        "codec": CODEC,
        "files": {name: "x" for name in ("RECIPE.json", "MODEL_CONFIG.json", "model.pt", "PROBE.npz")},
        "columns": list(columns),
        "action_space": "categorical",
    }
    body.update(overrides)
    return body


def bar(**overrides) -> HourlyBar:
    body = dict(
        snapshot_id="hour-1",
        bar_close_utc_ns=NS,
        decision_time_utc_ns=NS,
        availability_watermark_utc_ns=NS,
        finalized=True,
        instrument="EURUSD",
        columns=("RAW_CLOSE_LOG_RETURN",),
        values=(0.01,),
        published_utc_ns=(NS - 1,),
        bid=1.1000,
        ask=1.1002,
        high=1.1010,
        low=1.0990,
        session_id="week",
        operational=True,
        before_force_flat=True,
        same_session_next=True,
        civil_offset_minutes=0,
    )
    body.update(overrides)
    return HourlyBar(**body)


def spec(**overrides) -> CandidateSpec:
    body = dict(
        model_sha256=HASH,
        recipe_sha256=HASH,
        scaler_sha256=HASH,
        strategy_sha256=HASH,
        code_sha256=HASH,
        train_cutoff="2024-01-01",
        columns=("RAW_CLOSE_LOG_RETURN",),
        train_return=11.51453,
        validation_return=13.80849,
        train_orders=748,
        validation_orders=206,
        train_orders_per_week=3.57895,
        validation_orders_per_week=3.81481,
        fixed_signal=0,
    )
    body.update(overrides)
    return CandidateSpec(**body)


def contract() -> BusinessContract:
    value = BusinessContract()
    value.validate()
    return value


def test_p01_greedy_tie_policy_and_bundle_rejections():
    assert greedy_signal([0.0, 0.0, 0.0]) == 0
    assert greedy_signal([0.2, 0.2, 0.1]) == 0
    assert greedy_signal([0.0, 0.5, 0.5]) == 1
    assert greedy_signal([0.1, 0.2, 0.9]) == 2
    with pytest.raises(PoolRefusal) as caught:
        greedy_signal([0.1, 0.2])
    assert caught.value.code == "ACTION_MAPPING"
    with pytest.raises(PoolRefusal) as caught:
        greedy_signal([0.1, float("nan"), 0.2])
    assert caught.value.code == "ACTION_MAPPING"
    accepted = authenticate_bundle(manifest(), recipe(), mode="greedy", loader="categorical_greedy_bundle")
    assert accepted["group_count"] == 28 and accepted["inference"] == "greedy"
    for mode, loader, mutated, code in (
        ("sample", "categorical_greedy_bundle", {}, "STOCHASTIC_MODE"),
        ("greedy", "live_sac_selection", {}, "LEGACY_ADAPTER"),
        ("greedy", "project3_sac_observation", {}, "LEGACY_ADAPTER"),
        ("greedy", "continuous_sac", {}, "LEGACY_ADAPTER"),
        ("greedy", "categorical_greedy_bundle", {"action_space": "box"}, "CONTINUOUS_POLICY"),
    ):
        with pytest.raises(PoolRefusal) as caught:
            authenticate_bundle(manifest(**mutated), recipe(), mode=mode, loader=loader)
        assert caught.value.code == code
    with pytest.raises(PoolRefusal) as caught:
        authenticate_bundle(manifest(("OTHER",)), recipe(("OTHER",), policy="rolling_zscore"), mode="greedy", loader="categorical_greedy_bundle")
    assert caught.value.code == "SCALER_POLICY"
    with pytest.raises(PoolRefusal) as caught:
        authenticate_bundle(
            manifest(),
            recipe(agent="position_and_cash"),
            mode="greedy",
            loader="categorical_greedy_bundle",
        )
    assert caught.value.code == "AGENT_STATE_INPUT"
    with pytest.raises(PoolRefusal) as caught:
        authenticate_bundle(manifest(("SWAPPED",)), recipe(), mode="greedy", loader="categorical_greedy_bundle")
    assert caught.value.code == "COLUMN_ORDER"
    with pytest.raises(PoolRefusal) as caught:
        authenticate_bundle(
            manifest(),
            recipe(),
            mode="greedy",
            loader="categorical_greedy_bundle",
            expected_scaler_sha256="sha256:" + "cd" * 32,
        )
    assert caught.value.code == "SCALER"
    text = "\n".join(path.read_text(encoding="utf-8") for path in ROOT.glob("*.py"))
    assert "import app.live_sac_selection" not in text
    assert "import app.project3_sac_observation" not in text
    assert SELECTED70_MODEL_HEX == "079bef0ea6f3b9327772f4204e5d5dbbd4be69a5ab58c32b424aa3cb7c686404"
    assert SELECTED70_HISTORICAL["evidence_class"] == "offline_reference_not_forward"
    for signal in (0, 1, 2):
        for position in (-1, 0, 1):
            assert command_checked(signal, position) == command_for(signal, position)


def test_p02_causal_snapshot_stale_revision_and_dst(tmp_path: Path):
    left = materialize(bar(civil_offset_minutes=60), ("RAW_CLOSE_LOG_RETURN",))
    right = materialize(bar(civil_offset_minutes=-300), ("RAW_CLOSE_LOG_RETURN",))
    assert left.feature_hash == right.feature_hash
    future = bar(
        columns=("RAW_CLOSE_LOG_RETURN", "LEAK"),
        values=(0.01, 9.0),
        published_utc_ns=(NS - 1, NS + 10),
    )
    changed = bar(
        columns=("RAW_CLOSE_LOG_RETURN", "LEAK"),
        values=(0.01, -9.0),
        published_utc_ns=(NS - 1, NS + 10),
    )
    assert materialize(future, ("RAW_CLOSE_LOG_RETURN",)).feature_hash == materialize(
        changed, ("RAW_CLOSE_LOG_RETURN",)
    ).feature_hash
    assert materialize(future, ("LEAK",)).refusal == "FUTURE_FEATURE"
    assert materialize(bar(finalized=False), ("RAW_CLOSE_LOG_RETURN",)).refusal == "INCOMPLETE_BAR"
    assert materialize(bar(availability_watermark_utc_ns=NS - 5), ("RAW_CLOSE_LOG_RETURN",)).refusal == "STALE_INPUT"

    service = PoolService(tmp_path / "pool")
    try:
        record = service.register(spec(fixed_signal=1), now_ns=NS)
        service.mark_parity(record.candidate_id)
        service.mark_shadow(record.candidate_id, now_ns=NS)
        opened = service.step(bar(snapshot_id="open"), sl=1.05, tp=1.20)
        assert opened["decisions"][0]["position"] == 1
        balance = record.account.balance
        revised = service.step(bar(snapshot_id="revised", values=(-0.5,)))
        assert revised["replayed"] is True and revised["submitted"] == 0
        assert record.account.balance == balance
        stale = bar(
            snapshot_id="stale",
            bar_close_utc_ns=NS + 3_600_000_000_000,
            decision_time_utc_ns=NS + 3_600_000_000_000,
            availability_watermark_utc_ns=NS,
            published_utc_ns=(NS + 3_600_000_000_000 - 1,),
            bid=1.04,
            ask=1.0402,
            low=1.03,
            high=1.05,
        )
        closed = service.step(stale, sl=1.05, tp=1.20)
        assert closed["decisions"][0]["refusal"] == "STALE_INPUT"
        assert closed["decisions"][0]["position"] == 0
        assert record.account.closes[-1].reason == "SL"
        blocked = service.step(
            bar(
                snapshot_id="later",
                bar_close_utc_ns=NS + 7_200_000_000_000,
                decision_time_utc_ns=NS + 7_200_000_000_000,
                availability_watermark_utc_ns=NS + 7_200_000_000_000,
                published_utc_ns=(NS + 7_200_000_000_000 - 1,),
                finalized=False,
            )
        )
        assert blocked["decisions"][0]["refusal"] == "INCOMPLETE_BAR"
        assert record.account.entries == 1
    finally:
        service.close()


def test_p03_independent_ledgers_and_one_materialization(tmp_path: Path):
    service = PoolService(tmp_path / "pool")
    try:
        left = service.register(spec(code_sha256="sha256:" + "11" * 32, fixed_signal=1), now_ns=NS)
        right = service.register(spec(code_sha256="sha256:" + "22" * 32, fixed_signal=1), now_ns=NS)
        assert left.candidate_id != right.candidate_id
        for record in (left, right):
            service.mark_parity(record.candidate_id)
            service.mark_shadow(record.candidate_id, now_ns=NS)
        result = service.step(bar(), sl=1.05, tp=1.20)
        assert result["materialize_calls"] == 1
        assert left.account.balance == right.account.balance
        assert left.account.position is not None and right.account.position is not None
        assert left.account.position.units == right.account.position.units
        left.account.balance += 123.0
        assert right.account.balance != left.account.balance
        other = VirtualAccount("other", "other", left.account.balance)
        assert other.position is None
    finally:
        service.close()


def test_p04_journal_before_send_unknown_and_replay():
    route = RouteBook("eurusd-demo", tier="demo", authorized=True)
    route.writer = "owner"
    seen = {"calls": 0, "state": None}

    def transport(effect):
        seen["calls"] += 1
        seen["state"] = effect.state
        return "ack"

    effect_id = "effect-1"
    first = route.submit(
        effect_id=effect_id,
        owner="owner",
        command=1,
        snapshot_id="hour-1",
        now_ns=NS,
        transport=transport,
    )
    assert seen["state"] == "journaled_pending" and seen["calls"] == 1
    second = route.submit(
        effect_id=effect_id,
        owner="owner",
        command=1,
        snapshot_id="hour-1",
        now_ns=NS,
        transport=transport,
    )
    assert second is first and seen["calls"] == 1

    unknown = RouteBook("eurusd-demo", tier="demo", authorized=True)
    unknown.writer = "owner"

    def boom(_effect):
        raise PoolRefusal("UNKNOWN_OUTCOME")

    with pytest.raises(PoolRefusal) as caught:
        unknown.submit(
            effect_id="effect-u",
            owner="owner",
            command=1,
            snapshot_id="hour-1",
            now_ns=NS,
            transport=boom,
        )
    assert caught.value.code == "UNKNOWN_OUTCOME"
    with pytest.raises(PoolRefusal) as caught:
        unknown.submit(
            effect_id="effect-u",
            owner="owner",
            command=1,
            snapshot_id="hour-1",
            now_ns=NS,
            transport=transport,
        )
    assert caught.value.code == "UNKNOWN_UNRECONCILED"
    unknown.reconcile("effect-u", "absent")
    followed = unknown.submit(
        effect_id="effect-2",
        owner="owner",
        command=0,
        snapshot_id="hour-2",
        now_ns=NS + 1,
        transport=transport,
    )
    assert followed.state == "acknowledged"


def test_p05_single_writer_tier_and_real_capital_disabled(tmp_path: Path):
    with pytest.raises(PoolRefusal) as caught:
        RouteBook("real", tier="real")
    assert caught.value.code == "REAL_CAPITAL_DISABLED"
    with pytest.raises(PoolRefusal) as caught:
        RouteBook("crypto", asset="BTCUSD", tier="demo", authorized=True)
    assert caught.value.code == "ASSET_MISMATCH"
    paper = RouteBook("paper", tier="paper", authorized=False)
    paper.writer = "owner"
    with pytest.raises(PoolRefusal) as caught:
        paper.submit(effect_id="e", owner="owner", command=1, snapshot_id="s", now_ns=NS, transport=lambda _e: "ack")
    assert caught.value.code == "UNAUTHORIZED_ROUTE"
    demo = RouteBook("demo", tier="demo", authorized=True)
    demo.writer = "owner"
    with pytest.raises(PoolRefusal) as caught:
        demo.submit(effect_id="e", owner="other", command=1, snapshot_id="s", now_ns=NS, transport=lambda _e: "ack")
    assert caught.value.code == "CONCURRENT_WRITER"
    service = PoolService(tmp_path / "pool")
    try:
        record = service.register(spec(), now_ns=NS)
        service.mark_parity(record.candidate_id)
        service.mark_shadow(record.candidate_id, now_ns=NS)
        service.mark_execution_eligible(record.candidate_id)
        assert service.paper_demo_executing is False
        assert service.shadow_observing is False
    finally:
        service.close()


def test_p06_flat_handover_keeps_owner_and_cash():
    route = RouteBook("demo", tier="demo", authorized=True)
    route.writer = "opener"
    route.position = Position(1, 1000, 1.1, 1.0, 1.2, "opener")
    route.cash = 10_050.0
    assert route.request_switch("successor", now_ns=NS, equity=10_040.0) == "QUEUED_UNTIL_FLAT"
    assert route.writer == "opener" and route.position.owner == "opener"
    with pytest.raises(PoolRefusal) as caught:
        route.submit(effect_id="switch", owner="successor", command=2, snapshot_id="s", now_ns=NS)
    assert caught.value.code == "CONCURRENT_WRITER"
    assert route.position.owner == "opener" and route.cash == 10_050.0
    route.position = None
    assert route.note_flat(NS + 1, route.cash) == "successor"
    assert route.writer == "successor"
    assert route.cash == 10_050.0
    assert route.tenure[-1]["previous"] == "opener"
    assert route.tenure[-1]["cash"] == 10_050.0


def test_p07_size_protection_and_weekly_boundaries_long_and_short():
    assert order_units(10_000, 1.1, 1.0) == int(__import__("math").floor(10_000 * 0.1 * 50 / 1.1))
    assert order_units(10, 100.0, 1.0) is None
    tiny = order_units(10_000, 1.1, 0.00001)
    assert tiny is None or tiny >= 1
    with pytest.raises(PoolRefusal) as caught:
        BusinessContract(max_entries_per_day=1).validate()
    assert caught.value.code == "DAILY_CAP_REINTRODUCED"
    with pytest.raises(PoolRefusal) as caught:
        BusinessContract(level_policy="clip").validate()
    assert caught.value.code == "LEVEL_CLIPPING"
    with pytest.raises(PoolRefusal) as caught:
        BusinessContract(implicit_reversal=True).validate()
    assert caught.value.code == "IMPLICIT_REVERSAL"

    rules = contract()
    long_book = VirtualAccount("long", "long")
    opened = long_book.apply(1, bar(), rules, entry_sl=1.05, entry_tp=1.20)
    assert opened["position"] == 1 and opened["refusal"] is None
    assert long_book.position is not None and long_book.position.entry == pytest.approx(1.1002)
    both = bar(
        snapshot_id="both",
        bar_close_utc_ns=NS + 1,
        decision_time_utc_ns=NS + 1,
        availability_watermark_utc_ns=NS + 1,
        published_utc_ns=(NS,),
        low=1.0,
        high=1.3,
        bid=1.1,
        ask=1.1002,
    )
    stopped = long_book.apply(0, both, rules)
    assert stopped["close_reason"] == "SL" and stopped["position"] == 0

    short_book = VirtualAccount("short", "short")
    short_open = short_book.apply(2, bar(), rules, entry_sl=1.15, entry_tp=1.05)
    assert short_open["position"] == -1
    assert short_book.position is not None and short_book.position.entry == pytest.approx(1.1000)
    taken = bar(
        snapshot_id="tp",
        bar_close_utc_ns=NS + 2,
        decision_time_utc_ns=NS + 2,
        availability_watermark_utc_ns=NS + 2,
        published_utc_ns=(NS + 1,),
        low=1.04,
        high=1.12,
        bid=1.1,
        ask=1.1002,
    )
    assert short_book.apply(0, taken, rules)["close_reason"] == "TP"

    reverse = VirtualAccount("reverse", "reverse")
    reverse.apply(1, bar(), rules, entry_sl=1.0, entry_tp=1.3)
    flat = reverse.apply(
        2,
        bar(
            snapshot_id="close",
            bar_close_utc_ns=NS + 3,
            decision_time_utc_ns=NS + 3,
            availability_watermark_utc_ns=NS + 3,
            published_utc_ns=(NS + 2,),
            bid=1.11,
            ask=1.1102,
            low=1.10,
            high=1.12,
        ),
        rules,
    )
    assert flat["command"] == 3 and flat["position"] == 0 and flat["close_reason"] == "AGENT_CLOSE"
    embargo = VirtualAccount("embargo", "embargo")
    refused = embargo.apply(1, bar(operational=False), rules, entry_sl=1.0, entry_tp=1.2)
    assert refused["refusal"] == "BEFORE_OPERATIONAL_START" and refused["position"] == 0
    held = VirtualAccount("flat-week", "flat-week")
    held.apply(1, bar(), rules, entry_sl=1.0, entry_tp=1.3)
    forced = held.apply(0, bar(snapshot_id="week-end", before_force_flat=False, bar_close_utc_ns=NS + 4, decision_time_utc_ns=NS + 4, availability_watermark_utc_ns=NS + 4, published_utc_ns=(NS + 3,)), rules)
    assert forced["close_reason"] == "FORCED_FLAT" and forced["position"] == 0


def test_p08_eligibility_nulls_and_common_window(tmp_path: Path):
    service = PoolService(tmp_path / "pool")
    try:
        quiet = service.register(spec(train_orders=0, validation_orders=0, train_orders_per_week=0, validation_orders_per_week=0), now_ns=NS)
        assert quiet.state == "INELIGIBLE" and "ZERO_ORDERS" in quiet.reasons
        missing = service.register(
            spec(code_sha256="sha256:" + "33" * 32, train_orders=None, train_orders_per_week=None),
            now_ns=NS,
        )
        assert "ZERO_ORDERS" not in missing.reasons
        assert "METRIC_MISSING_TRAIN" in missing.reasons
        reference = service.seed_selected70(
            now_ns=NS,
            recipe_sha256=HASH,
            scaler_sha256=HASH,
            strategy_sha256=HASH,
            code_sha256="sha256:" + "44" * 32,
        )
        challenger = service.register(spec(code_sha256="sha256:" + "55" * 32), now_ns=NS)
        risky = service.register(spec(code_sha256="sha256:" + "66" * 32), now_ns=NS)
        service.incumbent_id = reference.candidate_id
        week = dict(window_id="2026-W45", complete_common_week=True, coverage_valid=True)
        service.post_window(reference.candidate_id, WindowMetrics(net_return=0.01, max_drawdown=0.04, orders=4, orders_per_week=4, train_profit=9.0, **week))
        service.post_window(challenger.candidate_id, WindowMetrics(net_return=0.03, max_drawdown=0.02, orders=5, orders_per_week=5, train_profit=-1.0, critic_q=10.0, **week))
        service.post_window(risky.candidate_id, WindowMetrics(net_return=0.50, max_drawdown=0.01, orders=6, orders_per_week=6, risk_contract_ok=False, **week))
        selected = service.rank()
        assert selected["winner"] == challenger.candidate_id
        assert reference.candidate_id in service.records
        assert any(note["reason"] == "RISK_CONTRACT" for note in selected["notes"])
        tied = service.register(spec(code_sha256="sha256:" + "77" * 32), now_ns=NS)
        service.post_window(tied.candidate_id, WindowMetrics(net_return=0.03, max_drawdown=0.05, orders=5, orders_per_week=5, **week))
        assert service.rank()["winner"] == challenger.candidate_id
        service.post_window(challenger.candidate_id, WindowMetrics(net_return=0.03, max_drawdown=0.02, orders=5, orders_per_week=5, complete_common_week=False, coverage_valid=True, window_id="2026-W45"))
        retained = service.rank()
        assert retained["winner"] == reference.candidate_id and retained["reason"] == "INSUFFICIENT_SUPPORT"
    finally:
        service.close()


def test_p09_disconnect_unknown_position_and_stale_authority():
    rules = contract()
    book = VirtualAccount("book", "book")
    book.apply(1, bar(), rules, entry_sl=1.05, entry_tp=1.2)
    disconnected = bar(
        snapshot_id="down",
        bar_close_utc_ns=NS + 1,
        decision_time_utc_ns=NS + 1,
        availability_watermark_utc_ns=NS + 1,
        published_utc_ns=(NS,),
        bid=1.04,
        ask=1.0402,
        low=1.03,
        high=1.045,
        connected=False,
    )
    closed = book.apply(1, disconnected, rules)
    assert closed["position"] == 0 and closed["close_reason"] == "SL"
    fresh = VirtualAccount("fresh", "fresh")
    blocked = fresh.apply(1, disconnected, rules, entry_sl=1.0, entry_tp=1.2)
    assert blocked["refusal"] == "BROKER_DISCONNECTED" and blocked["position"] == 0
    foreign = VirtualAccount("foreign", "foreign")
    foreign.position = Position(-1, 10, 1.1, 1.2, 1.0, "someone-else")
    foreign.unrelated_locked = True
    before = foreign.balance
    refused = foreign.apply(1, bar(), rules, entry_sl=1.0, entry_tp=1.2)
    assert refused["refusal"] == "UNRELATED_POSITION"
    assert foreign.position is not None and foreign.position.owner == "someone-else"
    assert foreign.balance == before
    route = RouteBook("demo", tier="demo", authorized=True)
    route.writer = "owner"
    route.position = Position(1, 10, 1.1, 1.0, 1.2, "owner")
    route.authority_expires_utc_ns = NS
    with pytest.raises(PoolRefusal) as caught:
        route.submit(effect_id="late", owner="owner", command=1, snapshot_id="s", now_ns=NS + 1)
    assert caught.value.code == "STALE_AUTHORITY"
    assert route.position.owner == "owner"


def test_p10_restart_resumes_bar_without_catch_up_orders(tmp_path: Path):
    root = tmp_path / "pool"
    service = PoolService(root)
    record = service.register(spec(fixed_signal=1), now_ns=NS)
    service.mark_parity(record.candidate_id)
    service.mark_shadow(record.candidate_id, now_ns=NS)
    service.step(
        bar(snapshot_id="warm", catch_up=True, bar_close_utc_ns=NS - 1, decision_time_utc_ns=NS - 1, availability_watermark_utc_ns=NS - 1, published_utc_ns=(NS - 2,)),
        sl=1.05,
        tp=1.2,
    )
    service.step(bar(snapshot_id="live"), sl=1.05, tp=1.2)
    entries = record.account.entries
    assert entries == 1
    service.close()
    resumed = PoolService(root)
    try:
        assert resumed.last_bar_utc_ns == NS
        assert resumed.warmup == ["warm"]
        replay = resumed.step(bar(snapshot_id="live-again"))
        assert replay["replayed"] is True and replay["submitted"] == 0
        assert resumed.records[record.candidate_id].account.entries == 1
        catch = resumed.step(
            bar(
                snapshot_id="more-warm",
                catch_up=True,
                bar_close_utc_ns=NS + 10,
                decision_time_utc_ns=NS + 10,
                availability_watermark_utc_ns=NS + 10,
                published_utc_ns=(NS + 9,),
            )
        )
        assert catch["submitted"] == 0 and catch["catch_up"] is True
        assert resumed.records[record.candidate_id].account.entries == 1
        assert resumed.paper_demo_executing is False
    finally:
        resumed.close()


def test_p11_divergence_explains_cash_and_does_not_rewrite_history():
    first = explain_divergence(
        decision_id="d1",
        direction=1,
        units=1000,
        modeled_price=1.10,
        observed_price=1.1002,
        modeled_fee=0.0,
        observed_fee=1.0,
        modeled_financing=0.0,
        observed_financing=-0.25,
        spread=0.0002,
        latency_ms=40.0,
        rejection=None,
        partial_fill=False,
        modeled_cash=10_000.0,
        observed_cash=9_999.45,
    )
    assert first["slippage"] == pytest.approx(0.0002)
    assert first["unexplained_cash"] == pytest.approx(0.0)
    frozen = json.dumps(first, sort_keys=True)
    second = explain_divergence(
        decision_id="d2",
        direction=1,
        units=1000,
        modeled_price=1.2,
        observed_price=None,
        modeled_fee=0.0,
        observed_fee=None,
        modeled_financing=0.0,
        observed_financing=None,
        spread=None,
        latency_ms=None,
        rejection="REJECTED",
        partial_fill=True,
        modeled_cash=10_000.0,
        observed_cash=None,
    )
    assert second["unexplained_cash"] is None
    assert second["slippage"] is None
    assert json.dumps(first, sort_keys=True) == frozen
    report = performance_report(VirtualAccount("r", "r"), [10_000.0])
    assert report["sharpe_daily_sqrt365"] is None and report["sharpe_reason"] == "INSUFFICIENT_DAYS"


def test_p12_new_identity_and_immutable_artifact(tmp_path: Path):
    service = PoolService(tmp_path / "pool")
    try:
        original = service.register(spec(historical={"VALIDATION": {"net_return_pct": 13.80849}}), now_ns=NS)
        successor = service.register(spec(train_cutoff="2025-01-01", scaler_sha256="sha256:" + "ef" * 32), now_ns=NS + 1)
        assert original.candidate_id != successor.candidate_id
        again = service.register(spec(historical={"VALIDATION": {"net_return_pct": 13.80849}}), now_ns=NS + 2)
        assert again is original
        assert original.spec.historical["VALIDATION"]["net_return_pct"] == 13.80849
        assert successor.spec.historical is None
        service.seal(original.candidate_id, original.spec.model_sha256)
        service.seal(original.candidate_id, original.spec.model_sha256)
        with pytest.raises(PoolRefusal) as caught:
            service.seal(original.candidate_id, "sha256:" + "00" * 32)
        assert caught.value.code == "IMMUTABLE_ARTIFACT"
        assert original.account.balance == 10_000.0
        successor.account.balance = 1.0
        assert original.account.balance == 10_000.0
    finally:
        service.close()


def test_f01_selected70_registers_seventy_columns_and_refuses_silent_nop(tmp_path: Path):
    service = PoolService(tmp_path / "pool")
    try:
        record = service.seed_selected70(
            now_ns=NS,
            recipe_sha256=HASH,
            scaler_sha256=HASH,
            strategy_sha256=HASH,
            code_sha256=HASH,
        )
        assert len(record.spec.columns) == 70
        assert record.spec.columns == SELECTED70_COLUMNS
        assert record.spec.fixed_signal is None
        service.mark_parity(record.candidate_id)
        service.mark_shadow(record.candidate_id, now_ns=NS)
        values = tuple(float(index + 1) / 1000 for index in range(70))
        quiet = service.step(
            bar(
                columns=SELECTED70_COLUMNS,
                values=values,
                published_utc_ns=tuple(NS - 1 for _ in range(70)),
            ),
            sl=1.05,
            tp=1.2,
        )
        assert quiet["decisions"][0]["refusal"] == "INFERENCE_ABSENT"
        assert record.account.entries == 0
        seen = {}

        def infer(observed: tuple[float, ...]) -> int:
            seen["width"] = len(observed)
            return 2

        service.bind_inference(record.candidate_id, infer)
        unplanned = service.step(
            bar(
                snapshot_id="unplanned",
                bar_close_utc_ns=NS + 3_600_000_000_000,
                decision_time_utc_ns=NS + 3_600_000_000_000,
                availability_watermark_utc_ns=NS + 3_600_000_000_000,
                columns=SELECTED70_COLUMNS,
                values=values,
                published_utc_ns=tuple(NS + 3_600_000_000_000 - 1 for _ in range(70)),
            ),
            sl=1.15,
            tp=1.05,
        )
        assert seen["width"] == 70
        assert unplanned["decisions"][0]["refusal"] == "PLAN_ABSENT"
        assert record.account.entries == 0

        def planner(signal: int, observed: tuple[float, ...], balance: float) -> tuple[float, float]:
            assert signal == 2 and len(observed) == 70 and balance == 10_000.0
            return (1.15, 1.05)

        service.bind_planner(record.candidate_id, planner)
        called = service.step(
            bar(
                snapshot_id="planned",
                bar_close_utc_ns=NS + 7_200_000_000_000,
                decision_time_utc_ns=NS + 7_200_000_000_000,
                availability_watermark_utc_ns=NS + 7_200_000_000_000,
                columns=SELECTED70_COLUMNS,
                values=values,
                published_utc_ns=tuple(NS + 7_200_000_000_000 - 1 for _ in range(70)),
            ),
            sl=1.0,
            tp=1.3,
        )
        assert called["decisions"][0]["signal"] == 2
        assert called["decisions"][0]["position"] == -1
        assert record.account.position is not None
        assert record.account.position.sl == pytest.approx(1.15)
        assert record.account.position.tp == pytest.approx(1.05)
    finally:
        service.close()


def test_f02_unknown_effect_and_closes_survive_reload(tmp_path: Path):
    root = tmp_path / "pool"
    service = PoolService(root)
    record = service.seed_selected70(
        now_ns=NS,
        recipe_sha256=HASH,
        scaler_sha256=HASH,
        strategy_sha256=HASH,
        code_sha256=HASH,
    )
    service.route.writer = record.candidate_id
    try:
        service.route.submit(
            effect_id="unknown-fixture",
            owner=record.candidate_id,
            command=1,
            snapshot_id="completed-hour",
            now_ns=NS,
            transport=lambda _effect: "unknown",
        )
    except PoolRefusal as exc:
        assert exc.code == "UNKNOWN_OUTCOME"
    before = {"halted": service.route.halted_unknown, "effects": len(service.route.effects)}
    book = VirtualAccount("book", "book")
    book.apply(1, bar(), contract(), entry_sl=1.05, entry_tp=1.2)
    later = bar(
        snapshot_id="after",
        bar_close_utc_ns=NS + 1,
        decision_time_utc_ns=NS + 1,
        availability_watermark_utc_ns=NS + 1,
        published_utc_ns=(NS,),
        low=1.0,
        high=1.2,
        bid=1.04,
        ask=1.0402,
    )
    assert book.apply(0, later, contract())["close_reason"] == "SL"
    record.account.closes = list(book.closes)
    record.account.entries = book.entries
    service._save()
    service.close()
    resumed = PoolService(root)
    try:
        after = {"halted": resumed.route.halted_unknown, "effects": len(resumed.route.effects)}
        assert before == after
        assert before == {"halted": True, "effects": 1}
        effect = resumed.route.effects["unknown-fixture"]
        assert effect.state == "effect_unknown" and effect.snapshot_id == "completed-hour"
        with pytest.raises(PoolRefusal) as caught:
            resumed.route.submit(
                effect_id="retry",
                owner=record.candidate_id,
                command=1,
                snapshot_id="next",
                now_ns=NS + 1,
                transport=lambda _effect: "ack",
            )
        assert caught.value.code == "UNKNOWN_UNRECONCILED"
        restored = resumed.records[record.candidate_id].account
        assert restored.closes[0].reason == "SL"
        assert restored.closes[0].owner == "book"
    finally:
        resumed.close()


def test_f02_process_death_during_transport_stays_unknown(tmp_path: Path):
    root = tmp_path / "pool"
    script = tmp_path / "die_at_transport.py"
    script.write_text(
        textwrap.dedent(
            """
            import os
            import signal
            import sys

            from app.forward_pool.cohort import PoolService

            pool = PoolService(sys.argv[1])
            digest = "sha256:" + "ab" * 32
            record = pool.seed_selected70(
                now_ns=1,
                recipe_sha256=digest,
                scaler_sha256=digest,
                strategy_sha256=digest,
                code_sha256=digest,
            )
            pool.route.writer = record.candidate_id

            def transport(_effect):
                os.kill(os.getpid(), signal.SIGKILL)

            pool.route.submit(
                effect_id="dying",
                owner=record.candidate_id,
                command=1,
                snapshot_id="hour",
                now_ns=1,
                transport=transport,
            )
            """
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(script), str(root)],
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    assert completed.returncode != 0
    service = PoolService(root)
    try:
        assert service.route.halted_unknown is True
        effect = service.route.effects["dying"]
        assert effect.state == "effect_unknown" and effect.transport_calls >= 1
    finally:
        service.close()


def test_f03_shadow_wealth_does_not_replace_executor_cash(tmp_path: Path):
    service = PoolService(tmp_path / "pool")
    try:
        record = service.register(spec(fixed_signal=1), now_ns=NS)
        service.mark_parity(record.candidate_id)
        service.mark_shadow(record.candidate_id, now_ns=NS)
        record.account.balance = 12_000.0
        record.account.position = None
        service.route.writer = record.candidate_id
        service.route.cash = 8_500.0
        service.route_account.balance = 8_500.0
        service.route.position = Position(1, 100, 1.1, 1.0, 1.2, record.candidate_id)
        service.route_account.position = service.route.position
        service._mirror_route(record.candidate_id, bar())
        assert service.route.cash == 8_500.0
        assert service.route.position is not None and service.route.position.owner == record.candidate_id
        assert record.account.balance == 12_000.0
        service.route.authorized = True
        service.route.tier = "demo"
        assert service.route.request_switch("successor", now_ns=NS, equity=8_500.0) == "QUEUED_UNTIL_FLAT"
        service.route.position = None
        assert service.route.note_flat(NS + 1, service.route.cash) == "successor"
        assert service.route.cash == 8_500.0
        assert record.account.balance == 12_000.0
        assert service.route.tenure[-1]["cash"] == 8_500.0
    finally:
        service.close()


def test_f04_pre_entry_extremes_do_not_close_a_new_order():
    rules = contract()
    completed = bar(high=1.1010, low=1.0990, bid=1.1000, ask=1.1002)
    long_book = VirtualAccount("long", "long")
    opened = long_book.apply(1, completed, rules, entry_sl=1.0995, entry_tp=1.1020)
    assert long_book.entries == 1
    assert opened["close_reason"] is None and opened["position"] == 1
    assert long_book.balance == 10_000.0
    short_book = VirtualAccount("short", "short")
    short_open = short_book.apply(2, completed, rules, entry_sl=1.1005, entry_tp=1.0980)
    assert short_open["close_reason"] is None and short_open["position"] == -1
    gap = bar(
        snapshot_id="gap",
        bar_close_utc_ns=NS + 3_600_000_000_000,
        decision_time_utc_ns=NS + 3_600_000_000_000,
        availability_watermark_utc_ns=NS + 3_600_000_000_000,
        published_utc_ns=(NS + 3_600_000_000_000 - 1,),
        bid=1.0900,
        ask=1.0902,
        low=1.0890,
        high=1.0910,
    )
    stopped = long_book.apply(0, gap, rules)
    assert stopped["close_reason"] == "SL"
    assert long_book.closes[-1].price == pytest.approx(1.0900)
    both = bar(
        snapshot_id="both-later",
        bar_close_utc_ns=NS + 7_200_000_000_000,
        decision_time_utc_ns=NS + 7_200_000_000_000,
        availability_watermark_utc_ns=NS + 7_200_000_000_000,
        published_utc_ns=(NS + 7_200_000_000_000 - 1,),
        bid=1.1000,
        ask=1.1002,
        low=1.0970,
        high=1.1040,
    )
    fresh = VirtualAccount("fresh", "fresh")
    fresh.apply(2, completed, rules, entry_sl=1.1005, entry_tp=1.0980)
    assert fresh.apply(0, both, rules)["close_reason"] == "SL"


def test_registered_candidate_cannot_open(tmp_path: Path):
    service = PoolService(tmp_path / "pool")
    try:
        record = service.register(spec(fixed_signal=1), now_ns=NS)
        result = service.step(bar(), sl=1.05, tp=1.2)
        assert result["decisions"][0]["refusal"] == "ADMISSION_STATE"
        assert record.account.entries == 0
        absent = materialize(bar(), SELECTED70_COLUMNS)
        assert absent.refusal == "FEATURE_ABSENT" and absent.values is None
    finally:
        service.close()


def test_isolated_root_refuses_live_ledger_paths(tmp_path: Path):
    with pytest.raises(PoolRefusal) as caught:
        PoolService(Path.home() / ".local" / "state" / "lts" / "forward-pool")
    assert caught.value.code == "LIVE_LEDGER_PATH"
    service = PoolService(tmp_path / "ok")
    service.close()
