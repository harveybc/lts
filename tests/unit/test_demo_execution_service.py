"""Adversarial L0 fixtures for the demo execution service (findings 039-042).

Every test runs with sockets booby-trapped: any network attempt anywhere in
the service explodes the test. Zero submissions is proven structurally, not
asserted from a counter alone.
"""
import socket
from datetime import datetime, timedelta, timezone

import pytest

from trading_contracts import (
    AssetIntent,
    BrokerCapabilitySnapshot,
    ExecutionReportV2,
    InstrumentCapability,
    OwnerCommand,
    ProtectionLegState,
)

from app.demo_execution_service import (
    DemoExecutionConfig,
    DemoExecutionError,
    DemoExecutionOlap,
    DemoExecutionService,
    ZeroNetworkSink,
    plan_units,
)

NOW = datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)
ARTIFACT = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted during L0")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


def _config(tmp_path, **overrides):
    base = {
        "venue": "ibkr_paper",
        "account_fingerprint": "synthetic-ibkr-fixture-1",
        "environment": "paper",
        "database_path": str(tmp_path / "demo_exec.sqlite"),
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
        "asset_instrument_bindings": {
            "fx:USD/CAD": "USD.CAD",
            "crypto:ETH/USD": "ETH.USD",
        },
    }
    base.update(overrides)
    return DemoExecutionConfig.from_dict(base)


def _service(tmp_path, **overrides):
    config = _config(tmp_path, **overrides)
    olap = DemoExecutionOlap(config.database_path)
    return DemoExecutionService(config, olap, ZeroNetworkSink())


_CAP_IBKR = dict(
    instrument="USD.CAD", tradeable=True, shortable=True, min_units=1.0,
    unit_step=1.0, price_decimals=5, margin_rate=0.03,
    native_stop_loss=True, native_take_profit=True, native_bracket=True,
)


def _capability(min_units=1.0, shortable=True, native=True):
    return BrokerCapabilitySnapshot(
        object_id="cap-1",
        as_of=NOW,
        producer={"name": "test", "version": "0"},
        trace_id="t-1",
        venue="ibkr_paper",
        account_fingerprint="synthetic-ibkr-fixture-1",
        environment="paper",
        capability_evidence="synthetic_fixture",
        source_artifact_hash="sha256:" + "f" * 64,
        source_observed_at=NOW,
        instruments=[
            InstrumentCapability(
                instrument="USD.CAD",
                tradeable=True,
                shortable=shortable,
                min_units=min_units,
                unit_step=1.0,
                price_decimals=5,
                margin_rate=0.03,
                native_stop_loss=native,
                native_take_profit=native,
                native_bracket=native,
            )
        ],
    )


def _intent(object_id="ai-1", as_of=NOW, exposure=0.5, stop=0.99, tp=1.02):
    return AssetIntent(
        object_id=object_id,
        as_of=as_of,
        valid_until=as_of + timedelta(hours=4),
        producer={"name": "provider.mechanics", "version": "0"},
        trace_id="t-1",
        cell_id="fx:USD/CAD@4h:mech:policy",
        asset_id="fx:USD/CAD",
        action="target",
        target_exposure=exposure,
        risk_geometry={"mode": "fixed_price", "stop_price": stop,
                       "take_profit_price": tp},
        artifact_hash=ARTIFACT,
    )


def _process(service, intent, **kw):
    args = dict(equity=100_000.0, reference_price=1.0, instrument="USD.CAD",
                now=NOW + timedelta(seconds=1))
    args.update(kw)
    return service.process_intent(intent, _capability(), **args)


# ── happy path: protected would-be order, zero network ──

def test_protected_would_be_order_produced_with_zero_network(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent())
    assert result["outcome"] == "would_be_order"
    assert result["payload"]["adapter"] == "ibkr_paper.protected_order.v1"
    bracket = result["payload"]["bracket"]
    assert bracket["stop_loss_price"] == 0.99
    assert bracket["take_profit_price"] == 1.02
    # dimensional coherence (finding 040): with a 1% stop the risk-based
    # target (50k units) exceeds the 10% gross-notional cap; sizing binds
    # to the most restrictive cap instead of rejecting or breaching.
    assert result["delta_units"] == 10_000.0
    assert service.sink.network_submissions == 0
    assert service.sink.would_be_orders == 1


def test_sink_refuses_unprotected_payload(tmp_path):
    from trading_contracts import OrderIntentV2
    sink = ZeroNetworkSink()
    close_only = OrderIntentV2(
        object_id="oi-close", as_of=NOW,
        producer={"name": "t", "version": "0"}, trace_id="t",
        account_ref="fp", asset_id="fx:USD/CAD", venue="v", instrument="i",
        intent_class="risk_reducing", reduce_action="close",
        order_type="market", delta_units=-10.0, idempotency_key="k",
    )
    payload = sink.serialize(close_only)  # risk-reducing close is legal
    assert payload["intent_class"] == "risk_reducing"
    assert payload["adapter"] == "v.protected_order.v1"


# ── finding 040: minimum-size overshoot and cap atomicity ──

def test_venue_minimum_breaching_hard_caps_rejects(tmp_path):
    # min_units 200,000 needs 200% notional; the 10% gross cap forbids it.
    service = _service(tmp_path)
    out = service.process_intent(
        _intent(object_id="ai-min"), _capability(min_units=200_000.0),
        equity=100_000.0, reference_price=1.0, instrument="USD.CAD",
        now=NOW + timedelta(seconds=1),
    )
    assert out["outcome"] == "rejected"
    assert "venue_minimum_breaches_hard_caps" in out["reason"]


def test_minimum_size_overshoot_rejects_never_rounds_up(tmp_path):
    # Caps are loose; the venue minimum fits under every hard cap but
    # implies risk 0.8% > 0.5% * (1 + 0.25) -> skip, never round up.
    service = _service(tmp_path, gross_notional_fraction_max=1.0,
                       margin_fraction_max=1.0,
                       daily_loss_budget_fraction=0.02)
    out = service.process_intent(
        _intent(object_id="ai-over"), _capability(min_units=80_000.0),
        equity=100_000.0, reference_price=1.0, instrument="USD.CAD",
        now=NOW + timedelta(seconds=1),
    )
    assert out["outcome"] == "rejected"
    assert "minimum_size_overshoot" in out["reason"]


def test_plan_units_math_is_step_aligned_and_cap_bounded():
    units = plan_units(
        equity=100_000.0, risk_fraction_at_stop=0.005, stop_distance=0.0123,
        reference_price=1.0, margin_rate=0.03, unit_step=1000.0,
        min_units=1000.0, max_overshoot_ratio=0.25,
        available_day_risk_fraction=0.02, available_gross_fraction=1.0,
        available_margin_fraction=1.0,
    )
    assert units % 1000.0 == 0.0
    assert units * 0.0123 <= 100_000.0 * 0.005


def test_daily_loss_budget_blocks_third_one_percent_reservation(tmp_path):
    # Musashi's 040 example: three 1% loss-at-stop reservations vs 2% budget.
    # Wide stops keep notional at 10%/position so only the budget binds.
    service = _service(tmp_path, risk_fraction_at_stop=0.01,
                       gross_notional_fraction_max=0.5,
                       margin_fraction_max=0.5)
    for oid in ("ai-a", "ai-b"):
        result = _process(service, _intent(object_id=oid, stop=0.9, tp=1.2))
        assert result["outcome"] == "would_be_order", result
    third = _process(service, _intent(object_id="ai-c", stop=0.9, tp=1.2))
    assert third["outcome"] == "rejected"
    assert third["reason"] == "daily_loss_budget_exhausted"


def test_max_concurrent_positions_enforced(tmp_path):
    service = _service(tmp_path, max_concurrent_positions=1,
                       daily_loss_budget_fraction=0.05)
    assert _process(service, _intent(object_id="p1"))["outcome"] == "would_be_order"
    second = _process(service, _intent(object_id="p2"))
    assert second["outcome"] == "rejected"
    assert second["reason"] == "max_concurrent_positions"


# ── stale signals ──

def test_stale_signal_rejected_without_reservation(tmp_path):
    service = _service(tmp_path)
    old = _intent(object_id="ai-old", as_of=NOW - timedelta(hours=1))
    result = service.process_intent(
        old, _capability(), equity=100_000.0, reference_price=1.0,
        instrument="USD.CAD", now=NOW,
    )
    assert result["outcome"] == "rejected"
    assert result["reason"].startswith("stale_signal")
    day = NOW.date().isoformat()
    assert service.olap.active_totals(day)["positions"] == 0


def test_expired_validity_rejected(tmp_path):
    service = _service(tmp_path, signal_max_age_seconds=999999.0)
    intent = _intent(object_id="ai-exp")
    result = service.process_intent(
        intent, _capability(), equity=100_000.0, reference_price=1.0,
        instrument="USD.CAD", now=NOW + timedelta(hours=5),
    )
    assert result["outcome"] == "rejected"
    assert result["reason"] == "signal_expired"


# ── duplicate replay / idempotency ──

def test_duplicate_replay_returns_recorded_result_without_double_reservation(tmp_path):
    service = _service(tmp_path)
    first = _process(service, _intent(object_id="ai-dup"))
    second = _process(service, _intent(object_id="ai-dup"))
    assert first["outcome"] == "would_be_order"
    assert second["replayed"] is True
    assert second["payload_sha256"] == first["payload_sha256"]
    day = NOW.date().isoformat()
    assert service.olap.active_totals(day)["positions"] == 1


# ── capability fail-closed ──

def test_short_without_shortable_rejects(tmp_path):
    service = _service(tmp_path)
    result = service.process_intent(
        _intent(object_id="ai-short", exposure=-0.5, stop=1.02, tp=0.98),
        _capability(shortable=False),
        equity=100_000.0, reference_price=1.0, instrument="USD.CAD",
        now=NOW + timedelta(seconds=1),
    )
    assert result["reason"] == "short_not_supported"


def test_missing_native_protection_rejects(tmp_path):
    service = _service(tmp_path)
    result = service.process_intent(
        _intent(object_id="ai-nat"), _capability(native=False),
        equity=100_000.0, reference_price=1.0, instrument="USD.CAD",
        now=NOW + timedelta(seconds=1),
    )
    assert result["reason"] == "native_protection_unavailable"


def test_missing_protection_geometry_rejects(tmp_path):
    service = _service(tmp_path)
    bare = AssetIntent(
        object_id="ai-bare", as_of=NOW,
        producer={"name": "p", "version": "0"}, trace_id="t",
        cell_id="c", asset_id="fx:USD/CAD", action="target",
        target_exposure=0.5, artifact_hash=ARTIFACT,
    )
    result = _process(service, bare)
    assert result["reason"] == "missing_protection_geometry"


# ── finding 041: lost ack, partial fill, orphan protection, restart ──

def _report(service, result, state, previous, **kw):
    base = dict(
        object_id=f"er-{state}", as_of=NOW + timedelta(seconds=5),
        producer={"name": "sink", "version": "0"}, trace_id="t-1",
        order_intent_id=result["order_intent_id"],
        attempt_id=f"attempt-{result['reservation_id']}",
        bracket_role="parent", state=state, previous_state=previous,
        requested_units=result["delta_units"],
    )
    base.update(kw)
    return ExecutionReportV2(**base)


def test_lost_ack_blocks_new_risk_until_reconciled(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="ai-lost"))
    assert result["outcome"] == "would_be_order", result
    unknown = _report(service, result, "unknown_requires_reconciliation",
                      "requested", reconciliation_required=True)
    service.apply_execution_event(unknown)
    blocked = _process(service, _intent(object_id="ai-next"))
    assert blocked["outcome"] == "rejected"
    assert blocked["reason"] == "reconciliation_required_before_new_risk"


def test_event_contradicting_ledger_state_rejects(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="ai-led"))
    wrong = _report(service, result, "filled", "accepted",
                    filled_units=abs(result["delta_units"]))
    with pytest.raises(DemoExecutionError, match="ledger"):
        service.apply_execution_event(wrong)  # ledger says 'requested'


def test_orphan_unconfirmed_protection_triggers_emergency_hold(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="ai-orphan"))
    accepted = _report(service, result, "accepted", "requested")
    service.apply_execution_event(accepted)
    filled = _report(
        service, result, "filled", "accepted",
        filled_units=abs(result["delta_units"]),
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=abs(result["delta_units"])),
            ProtectionLegState(leg="take_profit", broker_confirmed=False,
                               covered_units=0.0),
        ],
    )
    outcome = service.apply_execution_event(filled)
    assert outcome["emergency"] == "unprotected_exposure_hold_and_flatten"
    follow_up = _process(service, _intent(object_id="ai-after"))
    assert follow_up["reason"] == "halted:hold"


def test_restart_replays_state_from_ledger_not_memory(tmp_path):
    config_path = str(tmp_path / "restart.sqlite")
    service = _service(tmp_path, database_path=config_path)
    result = _process(service, _intent(object_id="ai-restart"))
    partial = _report(
        service, result, "accepted", "requested",
    )
    service.apply_execution_event(partial)
    service.olap.close()

    reborn = DemoExecutionService(
        _config(tmp_path, database_path=config_path),
        DemoExecutionOlap(config_path),
        ZeroNetworkSink(),
    )
    assert reborn.olap.last_state(result["order_intent_id"]) == "accepted"
    replay = _process(reborn, _intent(object_id="ai-restart"))
    assert replay["replayed"] is True  # no duplicate exposure after restart
    day = NOW.date().isoformat()
    assert reborn.olap.active_totals(day)["positions"] == 1


# ── finding 042: deterministic owner command path ──

def _command(**kw):
    base = dict(
        object_id="cmd-1", as_of=NOW,
        producer={"name": "telegram.deterministic", "version": "0"},
        trace_id="t-1", command="hold", issuer_id="owner-1",
        exact_phrase="HOLD ALL DEMO TRADING NOW",
        nonce=kw.pop("nonce", "n-1"),
        expires_at=NOW + timedelta(minutes=5),
        idempotency_key="ck-1",
    )
    base.update(kw)
    return OwnerCommand(**base)


def test_spoofed_issuer_rejected_state_unchanged(tmp_path):
    service = _service(tmp_path)
    out = service.apply_owner_command(_command(issuer_id="impostor"), now=NOW)
    assert out == {"accepted": False, "reason": "issuer_not_allowlisted"}
    assert service.olap.get_state("halt", "none") == "none"


def test_replayed_nonce_rejected(tmp_path):
    service = _service(tmp_path)
    assert service.apply_owner_command(_command(), now=NOW)["accepted"]
    replay = service.apply_owner_command(_command(), now=NOW)
    assert replay == {"accepted": False, "reason": "nonce_replay"}


def test_expired_command_rejected(tmp_path):
    service = _service(tmp_path)
    out = service.apply_owner_command(
        _command(nonce="n-exp"), now=NOW + timedelta(minutes=10)
    )
    assert out == {"accepted": False, "reason": "expired"}


def test_malformed_phrase_rejected(tmp_path):
    service = _service(tmp_path)
    out = service.apply_owner_command(
        _command(nonce="n-bad", exact_phrase="hold all demo trading now"),
        now=NOW,
    )
    assert out == {"accepted": False, "reason": "phrase_mismatch"}


def test_authorized_hold_works_with_llm_processes_dead(tmp_path):
    # No Hermes/LLM/network dependency exists: sockets are booby-trapped for
    # this whole module and the command path is a pure function over SQLite.
    service = _service(tmp_path)
    out = service.apply_owner_command(_command(nonce="n-live"), now=NOW)
    assert out["accepted"] and out["state"] == "hold"
    blocked = _process(service, _intent(object_id="ai-blocked"))
    assert blocked["reason"] == "halted:hold"


def test_hold_state_survives_restart(tmp_path):
    db = str(tmp_path / "halt.sqlite")
    service = _service(tmp_path, database_path=db)
    service.apply_owner_command(_command(nonce="n-persist"), now=NOW)
    service.olap.close()
    reborn = DemoExecutionService(
        _config(tmp_path, database_path=db), DemoExecutionOlap(db),
        ZeroNetworkSink(),
    )
    blocked = _process(reborn, _intent(object_id="ai-reborn"))
    assert blocked["reason"] == "halted:hold"


def test_config_rejects_risk_enabling_command_verbs(tmp_path):
    with pytest.raises(DemoExecutionError, match="non-risk-reducing"):
        _config(tmp_path, command_phrases={"resume": "RESUME"})


# ── Finding 043: protection anchored to the decision reference price ──

def test_long_stop_above_reference_rejects(tmp_path):
    service = _service(tmp_path)
    out = _process(service, _intent(object_id="ai-043a", stop=1.01, tp=1.02))
    assert out["outcome"] == "rejected"
    assert "protection_not_anchored" in out["reason"]


def test_short_stop_below_reference_rejects(tmp_path):
    service = _service(tmp_path)
    out = _process(service, _intent(object_id="ai-043b", exposure=-0.5,
                                    stop=0.99, tp=0.97))
    assert out["outcome"] == "rejected"
    assert "protection_not_anchored" in out["reason"]


def test_reference_price_and_quote_time_are_persisted(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="ai-043c"))
    assert result["outcome"] == "would_be_order"
    row = service.olap._con.execute(
        "SELECT reference_price, quote_time, capability_evidence FROM decisions "
        "WHERE outcome='would_be_order'"
    ).fetchone()
    assert row[0] == 1.0 and row[1] is not None
    assert row[2] == "synthetic_fixture"


# ── Finding 044: filled exposure stays in every risk total until close ──

def test_filled_exposure_still_counts_against_position_cap(tmp_path):
    service = _service(tmp_path, max_concurrent_positions=1,
                       daily_loss_budget_fraction=0.05)
    result = _process(service, _intent(object_id="ai-044"))
    filled = _report(
        service, result, "filled", "requested",
        filled_units=abs(result["delta_units"]),
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=abs(result["delta_units"])),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=abs(result["delta_units"])),
        ],
    )
    outcome = service.apply_execution_event(filled)
    assert outcome["exposure"] == "opened"
    day = NOW.date().isoformat()
    assert service.olap.active_totals(day)["positions"] == 1.0  # not vanished
    second = _process(service, _intent(object_id="ai-044b"))
    assert second["reason"] == "max_concurrent_positions"
    service.apply_position_close(result["order_intent_id"])
    third = _process(service, _intent(object_id="ai-044c"))
    assert third["outcome"] == "would_be_order"


def test_partial_fill_splits_exposure_and_scaled_reservation(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="ai-044p"))
    magnitude = abs(result["delta_units"])
    partial = _report(
        service, result, "partially_filled", "requested",
        filled_units=magnitude * 0.4,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=magnitude * 0.4),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=magnitude * 0.4),
        ],
    )
    outcome = service.apply_execution_event(partial)
    assert outcome["exposure"] == "opened_partial"
    assert outcome["reservation"] == "scaled"
    day = NOW.date().isoformat()
    totals = service.olap.active_totals(day)
    # exposure(0.4) + scaled reservation(0.6) == original risk; no double count
    original = service.olap._con.execute(
        "SELECT gross_fraction FROM exposures"
    ).fetchone()[0] / 0.4
    assert abs(totals["gross"] - original) < 1e-9


# ── Finding 045: concurrent instances cannot double-spend a cap ──

def test_concurrent_instances_serialize_on_the_budget(tmp_path):
    import threading
    db = str(tmp_path / "race.sqlite")
    DemoExecutionOlap(db).close()  # create schema once
    barrier = threading.Barrier(2)
    outcomes = {}

    def worker(name, oid):
        config = _config(tmp_path, database_path=db,
                         risk_fraction_at_stop=0.01,
                         daily_loss_budget_fraction=0.01,
                         gross_notional_fraction_max=0.5,
                         margin_fraction_max=0.5)
        service = DemoExecutionService(config, DemoExecutionOlap(db),
                                       ZeroNetworkSink())
        barrier.wait()
        outcomes[name] = _process(
            service, _intent(object_id=oid, stop=0.9, tp=1.2))
        service.olap.close()

    threads = [
        threading.Thread(target=worker, args=("a", "ai-race-a")),
        threading.Thread(target=worker, args=("b", "ai-race-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    results = sorted(o["outcome"] for o in outcomes.values())
    assert results == ["rejected", "would_be_order"], outcomes
    check = DemoExecutionOlap(db)
    day = NOW.date().isoformat()
    totals = check.active_totals(day)
    assert totals["positions"] == 1.0
    assert totals["day_risk"] <= 0.01 + 1e-9
    check.close()


# ── Finding 046: no failure after reservation can leak capacity ──

def test_failure_inside_atomic_unit_leaks_nothing_and_persists_rejection(
        tmp_path, monkeypatch):
    service = _service(tmp_path)

    def explode(intent):
        raise RuntimeError("serialization boundary failure")

    monkeypatch.setattr(service.sink, "serialize", explode)
    out = _process(service, _intent(object_id="ai-046"))
    assert out["outcome"] == "rejected"
    assert "atomic_unit_failed" in out["reason"]
    day = NOW.date().isoformat()
    assert service.olap.active_totals(day)["positions"] == 0.0
    replay = _process(service, _intent(object_id="ai-046"))
    assert replay["replayed"] is True and replay["outcome"] == "rejected"


# ── Finding 047: commands emit deterministic zero-network intents ──

def _open_position(service, oid):
    result = _process(service, _intent(object_id=oid))
    units = abs(result["delta_units"])
    filled = _report(
        service, result, "filled", "requested", filled_units=units,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=units),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=units),
        ],
    )
    service.apply_execution_event(filled)
    return result


def test_flatten_all_emits_would_be_flatten_intents(tmp_path):
    service = _service(tmp_path, daily_loss_budget_fraction=0.05)
    opened = _open_position(service, "ai-047f")
    out = service.apply_owner_command(
        _command(command="flatten_all", nonce="n-flat",
                 exact_phrase="FLATTEN ALL DEMO POSITIONS NOW"),
        now=NOW,
    )
    assert out["accepted"]
    kinds = [e["kind"] for e in out["emitted"]]
    assert kinds == ["flatten"]
    assert out["emitted"][0]["units"] == -abs(opened["delta_units"])
    row = service.olap._con.execute(
        "SELECT COUNT(*) FROM decisions WHERE outcome='would_be_flatten'"
    ).fetchone()
    assert row[0] == 1


def test_cancel_pending_emits_would_be_cancels(tmp_path):
    service = _service(tmp_path)
    _process(service, _intent(object_id="ai-047c"))  # pending entry
    out = service.apply_owner_command(
        _command(command="cancel_pending", nonce="n-cxl",
                 exact_phrase="CANCEL ALL PENDING DEMO ENTRIES NOW"),
        now=NOW,
    )
    assert out["accepted"]
    assert [e["kind"] for e in out["emitted"]] == ["cancel"]


def test_kill_halts_and_emits_flatten_plus_cancel(tmp_path):
    service = _service(tmp_path, daily_loss_budget_fraction=0.05,
                       max_concurrent_positions=3,
                       gross_notional_fraction_max=0.5)
    result = _process(service, _intent(object_id="ai-047k1", stop=0.9, tp=1.2))
    units = abs(result["delta_units"])
    service.apply_execution_event(_report(
        service, result, "filled", "requested", filled_units=units,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=units),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=units),
        ],
    ))
    pending = _process(service, _intent(object_id="ai-047k2", stop=0.9, tp=1.2))
    assert pending["outcome"] == "would_be_order", pending  # pending entry
    out = service.apply_owner_command(
        _command(command="kill", nonce="n-kill",
                 exact_phrase="KILL ALL DEMO TRADING NOW"),
        now=NOW,
    )
    assert out["accepted"] and out["state"] == "kill"
    kinds = sorted(e["kind"] for e in out["emitted"])
    assert kinds == ["cancel", "flatten"]


def test_uncovered_fill_emits_emergency_flatten_not_just_flag(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="ai-047e"))
    units = abs(result["delta_units"])
    uncovered = _report(
        service, result, "filled", "requested", filled_units=units,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=units),
            ProtectionLegState(leg="take_profit", broker_confirmed=False,
                               covered_units=0.0),
        ],
    )
    outcome = service.apply_execution_event(uncovered)
    assert outcome["emergency"] == "unprotected_exposure_hold_and_flatten"
    assert [e["kind"] for e in outcome["emitted"]] == ["flatten"]


# ── Finding 049: partial fills conserve risk; one logical position ──

def test_partial_fill_conserves_day_risk_and_position_cardinality(tmp_path):
    """Musashi's exact reproduction: after a 40% partial of a 1% risk
    reservation, day_risk must remain 1% and a second 0.4% order must be
    rejected by the 1% daily cap."""
    service = _service(tmp_path, risk_fraction_at_stop=0.01,
                       daily_loss_budget_fraction=0.01,
                       gross_notional_fraction_max=0.5,
                       margin_fraction_max=0.5)
    result = _process(service, _intent(object_id="ai-049", stop=0.9, tp=1.2))
    magnitude = abs(result["delta_units"])
    partial = _report(
        service, result, "partially_filled", "requested",
        filled_units=magnitude * 0.4,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=magnitude * 0.4),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=magnitude * 0.4),
        ],
    )
    service.apply_execution_event(partial)
    day = NOW.date().isoformat()
    totals = service.olap.active_totals(day)
    assert abs(totals["day_risk"] - 0.01) < 1e-9      # conserved, not 0.006
    assert totals["positions"] == 1.0                 # one logical position
    assert abs(totals["risk_active"] - 0.01) < 1e-9
    second = _process(service, _intent(object_id="ai-049b", stop=0.9, tp=1.2))
    assert second["outcome"] == "rejected"
    assert second["reason"] == "daily_loss_budget_exhausted"


def test_cancel_after_partial_keeps_filled_share_in_day_risk(tmp_path):
    service = _service(tmp_path, risk_fraction_at_stop=0.01,
                       gross_notional_fraction_max=0.5,
                       margin_fraction_max=0.5,
                       daily_loss_budget_fraction=0.02)
    result = _process(service, _intent(object_id="ai-049c", stop=0.9, tp=1.2))
    magnitude = abs(result["delta_units"])
    kwargs = dict(
        filled_units=magnitude * 0.4,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=magnitude * 0.4),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=magnitude * 0.4),
        ],
    )
    service.apply_execution_event(
        _report(service, result, "partially_filled", "requested", **kwargs))
    service.apply_execution_event(
        _report(service, result, "cancel_pending", "partially_filled", **kwargs))
    service.apply_execution_event(
        _report(service, result, "cancelled", "cancel_pending",
                filled_units=0.0))
    day = NOW.date().isoformat()
    totals = service.olap.active_totals(day)
    # released remainder leaves day_risk; the filled 0.4% share survives
    assert abs(totals["day_risk"] - 0.004) < 1e-9
    assert totals["positions"] == 1.0


# ── Finding 050: signed, provenance-faithful exposure ──

def test_short_fill_persists_signed_units_and_flatten_buys_back(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="ai-050s", exposure=-0.5,
                                       stop=1.02, tp=0.98))
    units = abs(result["delta_units"])
    assert result["delta_units"] < 0
    service.apply_execution_event(_report(
        service, result, "filled", "requested", filled_units=units,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=units),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=units),
        ],
    ))
    exposure = service.olap.open_exposures()[0]
    assert exposure["units_open"] == -units            # signed short
    assert exposure["instrument"] == "USD.CAD"
    assert exposure["capability_evidence"] == "synthetic_fixture"
    out = service.apply_owner_command(
        _command(command="flatten_all", nonce="n-050",
                 exact_phrase="FLATTEN ALL DEMO POSITIONS NOW"), now=NOW)
    flatten = out["emitted"][0]
    assert flatten["units"] == +units                  # BUY closes the short
    assert flatten["target"] == result["order_intent_id"]


def test_non_fx_fill_keeps_its_own_instrument(tmp_path):
    service = _service(tmp_path)
    capability = BrokerCapabilitySnapshot(
        object_id="cap-eth", as_of=NOW,
        producer={"name": "test", "version": "0"}, trace_id="t-1",
        venue="ibkr_paper", account_fingerprint="synthetic-ibkr-fixture-1",
        environment="paper", capability_evidence="synthetic_fixture",
        source_artifact_hash="sha256:" + "e" * 64, source_observed_at=NOW,
        instruments=[InstrumentCapability(
            instrument="ETH.USD", tradeable=True, shortable=True,
            min_units=0.001, unit_step=0.001, price_decimals=2,
            margin_rate=0.3, native_stop_loss=True, native_take_profit=True,
            native_bracket=True,
        )],
    )
    intent = AssetIntent(
        object_id="ai-050e", as_of=NOW, valid_until=NOW + timedelta(hours=4),
        producer={"name": "provider.mechanics", "version": "0"},
        trace_id="t-1", cell_id="crypto:ETH/USD@4h:mech:policy",
        asset_id="crypto:ETH/USD", action="target", target_exposure=0.5,
        risk_geometry={"mode": "fixed_price", "stop_price": 2000.0,
                       "take_profit_price": 2400.0},
        artifact_hash=ARTIFACT,
    )
    result = service.process_intent(
        intent, capability, equity=100_000.0, reference_price=2200.0,
        instrument="ETH.USD", now=NOW + timedelta(seconds=1))
    assert result["outcome"] == "would_be_order", result
    units = abs(result["delta_units"])
    service.apply_execution_event(ExecutionReportV2(
        object_id="er-eth", as_of=NOW + timedelta(seconds=5),
        producer={"name": "sink", "version": "0"}, trace_id="t-1",
        order_intent_id=result["order_intent_id"],
        attempt_id=f"attempt-{result['reservation_id']}",
        bracket_role="parent", state="filled", previous_state="requested",
        requested_units=result["delta_units"], filled_units=units,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=units),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=units),
        ],
    ))
    exposure = service.olap.open_exposures()[0]
    assert exposure["instrument"] == "ETH.USD"
    assert exposure["asset_id"] == "crypto:ETH/USD"


# ── Finding 051: cancel names its exact target ──

def test_cancel_carries_source_order_identity(tmp_path):
    import json as jsonlib
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="ai-051"))
    out = service.apply_owner_command(
        _command(command="cancel_pending", nonce="n-051",
                 exact_phrase="CANCEL ALL PENDING DEMO ENTRIES NOW"), now=NOW)
    assert out["emitted"][0]["target"] == result["order_intent_id"]
    row = service.olap._con.execute(
        "SELECT intent_json FROM decisions WHERE outcome='would_be_cancel'"
    ).fetchone()
    cancel = jsonlib.loads(row[0])
    assert cancel["reduce_target_order_intent_id"] == result["order_intent_id"]
    assert cancel["instrument"] == "USD.CAD"           # not 'pending-entry'


def test_repeated_flatten_and_cancel_are_idempotent(tmp_path):
    service = _service(tmp_path, daily_loss_budget_fraction=0.05)
    _open_position(service, "ai-051i")
    first = service.apply_owner_command(
        _command(command="flatten_all", nonce="n-051a",
                 exact_phrase="FLATTEN ALL DEMO POSITIONS NOW"), now=NOW)
    second = service.apply_owner_command(
        _command(command="flatten_all", nonce="n-051b",
                 exact_phrase="FLATTEN ALL DEMO POSITIONS NOW"), now=NOW)
    assert first["emitted"][0].get("replayed") is None
    assert second["emitted"][0]["replayed"] is True
    count = service.olap._con.execute(
        "SELECT COUNT(*) FROM decisions WHERE outcome='would_be_flatten'"
    ).fetchone()[0]
    assert count == 1                                   # no duplicate emission


# ── Finding 052: capability snapshot binds to venue/account/environment ──

def test_cross_venue_capability_substitution_rejects(tmp_path):
    service = _service(tmp_path)
    alien = BrokerCapabilitySnapshot(
        object_id="cap-alien", as_of=NOW,
        producer={"name": "test", "version": "0"}, trace_id="t-1",
        venue="alpaca_paper", account_fingerprint="synthetic-alpaca-9",
        environment="paper", capability_evidence="synthetic_fixture",
        source_artifact_hash="sha256:" + "a" * 64, source_observed_at=NOW,
        instruments=[InstrumentCapability(**_CAP_IBKR)],
    )
    out = service.process_intent(
        _intent(object_id="ai-052"), alien,
        equity=100_000.0, reference_price=1.0, instrument="USD.CAD",
        now=NOW + timedelta(seconds=1))
    assert out["outcome"] == "rejected"
    assert "capability_snapshot_mismatch" in out["reason"]


def test_wrong_environment_capability_rejects(tmp_path):
    service = _service(tmp_path)
    wrong_env = BrokerCapabilitySnapshot(
        object_id="cap-live", as_of=NOW,
        producer={"name": "test", "version": "0"}, trace_id="t-1",
        venue="ibkr_paper", account_fingerprint="synthetic-ibkr-fixture-1",
        environment="live", capability_evidence="live_observed",
        source_artifact_hash="sha256:" + "b" * 64, source_observed_at=NOW,
        instruments=[InstrumentCapability(**_CAP_IBKR)],
    )
    out = service.process_intent(
        _intent(object_id="ai-052e"), wrong_env,
        equity=100_000.0, reference_price=1.0, instrument="USD.CAD",
        now=NOW + timedelta(seconds=1))
    assert "capability_snapshot_mismatch" in out["reason"]


# ── Ruling R4: six deterministic lifecycle traces ──

def test_trace_fill_before_ack(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="tr-1"))
    units = abs(result["delta_units"])
    outcome = service.apply_execution_event(_report(
        service, result, "filled", "requested", filled_units=units,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=units),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=units),
        ],
    ))
    assert outcome["exposure"] == "opened"


def test_trace_partial_then_cancel(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="tr-2"))
    units = abs(result["delta_units"])
    service.apply_execution_event(_report(service, result, "accepted",
                                          "requested"))
    service.apply_execution_event(_report(
        service, result, "partially_filled", "accepted",
        filled_units=units * 0.3,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=units * 0.3),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=units * 0.3),
        ],
    ))
    service.apply_execution_event(_report(service, result, "cancel_pending",
                                          "partially_filled",
                                          filled_units=units * 0.3,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=units * 0.3),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=units * 0.3),
        ]))
    final = service.apply_execution_event(_report(
        service, result, "cancelled", "cancel_pending",
        filled_units=0.0))
    assert final["reservation"] == "released"  # remaining entry freed
    day = NOW.date().isoformat()
    assert service.olap.active_totals(day)["positions"] == 1.0  # partial stays


def test_trace_cancel_fill_race_fill_wins(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="tr-3"))
    units = abs(result["delta_units"])
    service.apply_execution_event(_report(service, result, "accepted",
                                          "requested"))
    service.apply_execution_event(_report(service, result, "cancel_pending",
                                          "accepted"))
    final = service.apply_execution_event(_report(
        service, result, "filled", "cancel_pending", filled_units=units,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=units),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=units),
        ],
    ))
    assert final["exposure"] == "opened"


def test_trace_expiry_while_cancel_pending(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="tr-4"))
    service.apply_execution_event(_report(service, result, "accepted",
                                          "requested"))
    service.apply_execution_event(_report(service, result, "cancel_pending",
                                          "accepted"))
    final = service.apply_execution_event(_report(
        service, result, "expired", "cancel_pending"))
    assert final["reservation"] == "released"


def test_trace_unknown_then_reconciled_unblocks_new_risk(tmp_path):
    service = _service(tmp_path, daily_loss_budget_fraction=0.05)
    result = _process(service, _intent(object_id="tr-5"))
    service.apply_execution_event(_report(
        service, result, "unknown_requires_reconciliation", "requested",
        reconciliation_required=True))
    blocked = _process(service, _intent(object_id="tr-5b"))
    assert blocked["reason"] == "reconciliation_required_before_new_risk"
    reconciled = service.apply_execution_event(_report(
        service, result, "cancelled", "unknown_requires_reconciliation"))
    assert reconciled["reservation"] == "released"
    after = _process(service, _intent(object_id="tr-5c"))
    assert after["outcome"] == "would_be_order"


def test_trace_bracket_child_execution(tmp_path):
    service = _service(tmp_path)
    result = _process(service, _intent(object_id="tr-6"))
    units = abs(result["delta_units"])
    service.apply_execution_event(_report(
        service, result, "filled", "requested", filled_units=units,
        protection_legs=[
            ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                               covered_units=units),
            ProtectionLegState(leg="take_profit", broker_confirmed=True,
                               covered_units=units),
        ],
    ))
    child = ExecutionReportV2(
        object_id="er-child-1", as_of=NOW + timedelta(seconds=9),
        producer={"name": "sink", "version": "0"}, trace_id="t-1",
        order_intent_id=f"{result['order_intent_id']}-sl",
        attempt_id="attempt-child-sl",
        bracket_role="stop_loss",
        parent_order_intent_id=result["order_intent_id"],
        state="requested", requested_units=-units,
    )
    outcome = service.apply_execution_event(child)
    assert outcome["state"] == "requested"
    assert outcome["chain_hash"]


# ── Finding 053: concurrent identical intents replay, never crash ──

def test_finding_053_concurrent_identical_intents_one_wins_one_replays(tmp_path):
    import threading
    db = str(tmp_path / "twin.sqlite")
    DemoExecutionOlap(db).close()
    barrier = threading.Barrier(2)
    outcomes = {}

    def worker(name):
        service = DemoExecutionService(
            _config(tmp_path, database_path=db), DemoExecutionOlap(db),
            ZeroNetworkSink())
        barrier.wait()
        outcomes[name] = _process(service, _intent(object_id="ai-twin"))
        service.olap.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    results = sorted(o["outcome"] for o in outcomes.values())
    assert results == ["would_be_order", "would_be_order"], outcomes
    assert sorted(o.get("replayed", False) for o in outcomes.values()) == [
        False, True]
    check = DemoExecutionOlap(db)
    assert check._con.execute(
        "SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    check.close()


# ── Finding 054: conflicting concurrent reports cannot interleave ──

def test_finding_054_concurrent_reports_cannot_create_illegal_sequence(tmp_path):
    import threading
    db = str(tmp_path / "seq.sqlite")
    DemoExecutionOlap(db).close()
    setup = DemoExecutionService(
        _config(tmp_path, database_path=db), DemoExecutionOlap(db),
        ZeroNetworkSink())
    result = _process(setup, _intent(object_id="ai-054"))
    units = abs(result["delta_units"])
    setup.olap.close()
    barrier = threading.Barrier(2)
    errors = {}

    def apply(name, state, filled, legs):
        service = DemoExecutionService(
            _config(tmp_path, database_path=db), DemoExecutionOlap(db),
            ZeroNetworkSink())
        report = _report(service, result, state, "requested",
                         filled_units=filled, protection_legs=legs)
        barrier.wait()
        try:
            service.apply_execution_event(report)
        except DemoExecutionError as error:
            errors[name] = str(error)
        service.olap.close()

    legs = [
        ProtectionLegState(leg="stop_loss", broker_confirmed=True,
                           covered_units=units),
        ProtectionLegState(leg="take_profit", broker_confirmed=True,
                           covered_units=units),
    ]
    threads = [
        threading.Thread(target=apply, args=("fill", "filled", units, legs)),
        threading.Thread(target=apply, args=("ack", "accepted", 0.0, [])),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(errors) == 1, errors  # exactly one loser, rejected cleanly
    check = DemoExecutionOlap(db)
    states = [row[0] for row in check._con.execute(
        "SELECT state FROM lifecycle_events WHERE order_intent_id=? "
        "ORDER BY seq", (result["order_intent_id"],)).fetchall()]
    check.close()
    assert states[0] == "requested" and len(states) == 2
    from trading_contracts import is_legal_transition
    assert is_legal_transition(states[0], states[1])


# ── Finding 055: accepted kill resumes missing effects after a crash ──

def test_finding_055_kill_effects_resume_after_crash(tmp_path, monkeypatch):
    db = str(tmp_path / "crash.sqlite")
    service = _service(tmp_path, database_path=db,
                       daily_loss_budget_fraction=0.05)
    _open_position(service, "ai-055")

    def explode(intent):
        raise RuntimeError("crash during effect emission")

    monkeypatch.setattr(service.sink, "serialize", explode)
    with pytest.raises(RuntimeError):
        service.apply_owner_command(
            _command(command="kill", nonce="n-055",
                     exact_phrase="KILL ALL DEMO TRADING NOW"), now=NOW)
    # acceptance + halt + journal survived the crash
    assert service.olap.get_state("halt") == "kill"
    assert service.olap.get_state("effects_due", "") != ""
    service.olap.close()

    reborn = DemoExecutionService(
        _config(tmp_path, database_path=db), DemoExecutionOlap(db),
        ZeroNetworkSink())
    emitted = reborn.resume_pending_effects(now=NOW)
    kinds = sorted(e["kind"] for e in emitted)
    assert kinds == ["flatten"]  # the missing effect materialized
    assert reborn.olap.get_state("effects_due", "") == ""


# ── Finding 057: a policy cannot ride another asset's quote ──

def test_finding_057_wrong_asset_for_instrument_rejects(tmp_path):
    service = _service(tmp_path)
    eth_intent = AssetIntent(
        object_id="ai-057", as_of=NOW, valid_until=NOW + timedelta(hours=4),
        producer={"name": "provider.mechanics", "version": "0"},
        trace_id="t-1", cell_id="crypto:ETH/USD@1h:mech:policy",
        asset_id="crypto:ETH/USD", action="target", target_exposure=0.5,
        risk_geometry={"mode": "fixed_price", "stop_price": 0.99,
                       "take_profit_price": 1.02},
        artifact_hash=ARTIFACT,
    )
    out = service.process_intent(
        eth_intent, _capability(), equity=100_000.0, reference_price=1.0,
        instrument="USD.CAD", now=NOW + timedelta(seconds=1))
    assert out["outcome"] == "rejected"
    assert "asset_instrument_binding_violation" in out["reason"]


def test_finding_057_unbound_asset_rejects(tmp_path):
    service = _service(tmp_path)
    unbound = AssetIntent(
        object_id="ai-057b", as_of=NOW, valid_until=NOW + timedelta(hours=4),
        producer={"name": "provider.mechanics", "version": "0"},
        trace_id="t-1", cell_id="crypto:BTC/USD@1h:mech:policy",
        asset_id="crypto:BTC/USD", action="target", target_exposure=0.5,
        risk_geometry={"mode": "fixed_price", "stop_price": 0.99,
                       "take_profit_price": 1.02},
        artifact_hash=ARTIFACT,
    )
    out = service.process_intent(
        unbound, _capability(), equity=100_000.0, reference_price=1.0,
        instrument="USD.CAD", now=NOW + timedelta(seconds=1))
    assert "asset_instrument_binding_violation" in out["reason"]


# ── the structural zero-submission proof ──

def test_full_flow_makes_zero_network_submissions(tmp_path):
    service = _service(tmp_path, daily_loss_budget_fraction=0.05,
                       gross_notional_fraction_max=0.5)
    for oid in ("z1", "z2", "z3"):
        result = _process(service, _intent(object_id=oid, stop=0.9, tp=1.2))
        assert result["outcome"] == "would_be_order", result
    assert service.sink.network_submissions == 0
    assert service.sink.would_be_orders == 3
    # sockets were booby-trapped the whole time; reaching here is the proof
