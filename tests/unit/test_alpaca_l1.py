from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from trading_contracts import (
    AssetIntent,
    BrokerCapabilitySnapshot,
    ExecutionReportV2,
    InstrumentCapability,
    OrderIntentV2,
)

from app.alpaca_l1 import (
    AlpacaL1Executor,
    AlpacaL1Profile,
    AlpacaPaperError,
    verify_native_bracket,
)
from app.demo_execution_service import (
    DemoExecutionConfig,
    DemoExecutionService,
    ZeroNetworkSink,
)
from app.ibkr_l1_journal import L1ExecutionOlap


def _profile():
    return AlpacaL1Profile(
        venue="alpaca_paper",
        environment="paper",
        account_fingerprint="0123456789abcdef",
        symbol="SPY",
        asset_id="equity:SPY",
        quantity_ceiling=Decimal("1"),
        max_orders_per_day=4,
        max_risk_fraction_at_stop=Decimal("0.001"),
    )


def _order(**overrides):
    order = {
        "id": "parent-id",
        "client_order_id": "lts-client",
        "symbol": "SPY",
        "qty": "1",
        "side": "buy",
        "type": "market",
        "time_in_force": "gtc",
        "order_class": "bracket",
        "status": "accepted",
        "legs": [
            {"id": "tp", "side": "sell", "type": "limit", "qty": "1",
             "limit_price": "510", "time_in_force": "gtc"},
            {"id": "sl", "side": "sell", "type": "stop", "qty": "1",
             "stop_price": "490", "time_in_force": "gtc"},
        ],
    }
    order.update(overrides)
    return order


def _contract():
    return {
        "symbol": "SPY", "qty": "1", "side": "buy",
        "stop_price": "490", "take_profit_price": "510",
        "client_order_id": "lts-client", "time_in_force": "gtc",
    }


class FakeClient:
    def __init__(self, *, protected=True, account="0123456789abcdef"):
        self.protected = protected
        self.fingerprint = account
        self.submit_calls = 0
        self.cancel_calls = 0
        self.close_calls = 0
        self._client_order_id = None
        self.order_payload = None

    def account(self):
        return {"id": "paper", "status": "ACTIVE", "trading_blocked": False}

    def account_fingerprint(self, _account):
        return self.fingerprint

    def submit_bracket(self, plan):
        self.submit_calls += 1
        self._client_order_id = plan["client_order_id"]
        return {"id": "parent-id", "status": "accepted"}

    def order(self, _order_id):
        if self.order_payload is not None:
            return self.order_payload
        legs = _order()["legs"] if self.protected else []
        return _order(client_order_id=self._client_order_id, legs=legs)

    def cancel_order(self, _order_id):
        self.cancel_calls += 1

    def positions(self):
        return []

    def close_position(self, _symbol):
        self.close_calls += 1
        return {"id": "flatten"}


def _evidence():
    return {
        "model_id": "spy-baseline-v1",
        "artifact_sha256": "a" * 64,
        "config_sha256": "b" * 64,
        "input_sha256": "c" * 64,
    }


def test_exact_native_bracket_is_required():
    assert verify_native_bracket(_order(), _contract())["protected"] is True
    assert "legs" in verify_native_bracket(_order(legs=[]), _contract())["failures"]
    altered = _order()
    altered["legs"][1]["stop_price"] = "480"
    assert "stop_price" in verify_native_bracket(altered, _contract())["failures"]


def test_submit_is_durable_idempotent_and_binds_model_evidence(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    client = FakeClient()
    executor = AlpacaL1Executor(store, client, _profile())
    args = dict(
        idempotency_key="spy:2026-08-03", symbol="SPY", asset_id="equity:SPY",
        qty=Decimal("1"), side="buy", stop_price=Decimal("490"),
        take_profit_price=Decimal("510"), risk_fraction_at_stop=Decimal("0.0005"),
        model_evidence=_evidence(),
    )
    result = executor.submit(**args)
    replay = executor.submit(**args)
    assert result["protected"] is True
    assert replay["replayed"] is True
    assert client.submit_calls == 1
    effect = store.effect_by_key(args["idempotency_key"])
    assert effect["state"] == "acknowledged"
    assert store.effect_contract(effect["effect_id"])["model_evidence"] == _evidence()


def test_connected_account_mismatch_never_submits(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    client = FakeClient(account="ffffffffffffffff")
    executor = AlpacaL1Executor(store, client, _profile())
    with pytest.raises(AlpacaPaperError, match="not authorized"):
        executor.submit(
            idempotency_key="x", symbol="SPY", asset_id="equity:SPY",
            qty=Decimal("1"), side="buy", stop_price=Decimal("490"),
            take_profit_price=Decimal("510"), risk_fraction_at_stop=Decimal("0.0005"),
            model_evidence=_evidence(),
        )
    assert client.submit_calls == 0


def test_missing_protection_cancels_and_holds(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    client = FakeClient(protected=False)
    executor = AlpacaL1Executor(store, client, _profile())
    result = executor.submit(
        idempotency_key="x", symbol="SPY", asset_id="equity:SPY",
        qty=Decimal("1"), side="buy", stop_price=Decimal("490"),
        take_profit_price=Decimal("510"), risk_fraction_at_stop=Decimal("0.0005"),
        model_evidence=_evidence(),
    )
    assert result["protected"] is False
    assert client.cancel_calls == 1
    assert store.get_state("halt") == "hold"


def test_day_protection_is_refused():
    order = _order(time_in_force="day")
    for leg in order["legs"]:
        leg["time_in_force"] = "day"
    verdict = verify_native_bracket(order, _contract())
    assert verdict["protected"] is False
    assert "time_in_force" in verdict["failures"]


def test_risk_and_daily_entry_limits_are_enforced(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    client = FakeClient()
    executor = AlpacaL1Executor(store, client, _profile())
    base = dict(
        symbol="SPY", asset_id="equity:SPY", qty=Decimal("1"), side="buy",
        stop_price=Decimal("490"), take_profit_price=Decimal("510"),
        model_evidence=_evidence(),
    )
    with pytest.raises(AlpacaPaperError, match="stop risk"):
        executor.submit(
            idempotency_key="risk-too-large",
            risk_fraction_at_stop=Decimal("0.002"), **base,
        )
    assert client.submit_calls == 0

    for number in range(_profile().max_orders_per_day):
        executor.submit(
            idempotency_key=f"entry-{number}",
            risk_fraction_at_stop=Decimal("0.0005"), **base,
        )
    with pytest.raises(AlpacaPaperError, match="daily Paper order budget"):
        executor.submit(
            idempotency_key="entry-over-budget",
            risk_fraction_at_stop=Decimal("0.0005"), **base,
        )
    assert client.submit_calls == _profile().max_orders_per_day


def test_crypto_route_is_impossible_in_profile(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(
        '{"schema":"lts.alpaca.paper_l1_profile.v1","venue":"alpaca_paper",'
        '"environment":"paper","asset_class":"crypto",'
        '"account_fingerprint":"0123456789abcdef","orders":{"enabled":true},'
        '"symbol":"BTC/USD","asset_id":"crypto:BTC/USD",'
        '"quantity_ceiling":1,"max_orders_per_day":1,'
        '"max_risk_fraction_at_stop":0.001}', encoding="utf-8"
    )
    with pytest.raises(AlpacaPaperError, match="Only US equities"):
        AlpacaL1Profile.load(profile)


def test_terminal_broker_effect_reconciles_l0_and_unblocks_next_signal(tmp_path):
    database = tmp_path / "ledger.sqlite"
    config = DemoExecutionConfig.from_dict({
        "venue": "alpaca_paper",
        "account_fingerprint": "0123456789abcdef",
        "environment": "paper",
        "database_path": str(database),
        "risk_fraction_at_stop": 0.001,
        "max_overshoot_ratio": 0.25,
        "gross_notional_fraction_max": 0.10,
        "margin_fraction_max": 0.10,
        "daily_loss_budget_fraction": 0.01,
        "max_concurrent_positions": 1,
        "signal_max_age_seconds": 3600.0,
        "owner_issuer_allowlist": ["owner"],
        "command_phrases": {},
        "asset_instrument_bindings": {"equity:SPY": "SPY"},
    })
    store = L1ExecutionOlap(database)
    service = DemoExecutionService(config, store, ZeroNetworkSink())
    client = FakeClient()
    executor = AlpacaL1Executor(store, client, _profile(), service)
    now = datetime.now(timezone.utc)
    reservation_id = "rsv-legacy"
    idempotency_key = "legacy-signal"
    intent = OrderIntentV2(
        object_id="oi2-legacy", as_of=now,
        producer={"name": "test", "version": "1"}, trace_id="legacy-trace",
        account_ref="0123456789abcdef", asset_id="equity:SPY",
        venue="alpaca_paper", instrument="SPY",
        intent_class="risk_increasing", order_type="market",
        delta_units=-1.0, idempotency_key=idempotency_key,
        capability_snapshot_hash="sha256:" + "f" * 64,
        protection={"stop_loss_price": 510.0, "take_profit_price": 490.0},
        risk={
            "risk_fraction_at_stop": 0.0005,
            "gross_notional_fraction": 0.005,
            "margin_fraction": 0.005,
            "daily_loss_budget_fraction": 0.01,
            "reservation_id": reservation_id,
        },
        preflight={
            "source_model_id": "model",
            "source_artifact_sha256": "sha256:" + "a" * 64,
            "source_config_sha256": "sha256:" + "b" * 64,
            "source_input_sha256": "c" * 64,
        },
    )
    effect_id = "alpaca-legacy"
    active_fill = _order(
        status="filled", side="sell", qty="1", filled_qty="1",
        filled_avg_price="500", time_in_force="day",
        client_order_id="lts-client",
        legs=[
            {"id": "tp", "side": "buy", "type": "limit", "qty": "1",
             "limit_price": "490", "time_in_force": "day", "status": "new"},
            {"id": "sl", "side": "buy", "type": "stop", "qty": "1",
             "stop_price": "510", "time_in_force": "day", "status": "held"},
        ],
    )
    terminal = dict(active_fill)
    terminal["legs"] = [dict(leg, status="canceled") for leg in active_fill["legs"]]
    client.order_payload = terminal
    with store.atomic_unit():
        store.reserve(reservation_id, idempotency_key, now.date().isoformat(),
                      0.0005, 0.005, 0.005)
        store.record_decision(
            idempotency_key, "would_be_order", None,
            intent.model_dump_json(), {"adapter": "alpaca_paper"},
            capability_evidence="live_observed",
        )
        store.append_lifecycle(ExecutionReportV2(
            object_id="er-legacy-request", as_of=now,
            producer={"name": "test", "version": "1"},
            trace_id="legacy-trace", order_intent_id=intent.object_id,
            attempt_id=f"attempt-{reservation_id}", bracket_role="parent",
            state="requested", requested_units=-1.0,
        ))
        store.create_effect(effect_id, idempotency_key,
                            "alpaca_bracket_entry", ["parent-id"])
        store.store_effect_contract(effect_id, {
            "symbol": "SPY", "qty": "1", "side": "sell",
            "stop_price": "510", "take_profit_price": "490",
            "client_order_id": "lts-client",
        })
        store.record_broker_fact(effect_id, "monitor_snapshot", active_fill)
        store.advance_effect(effect_id, "effect_unknown")
        store.advance_effect(effect_id, "submitted_pending_ack")
        store.advance_effect(effect_id, "acknowledged")
        store.advance_effect(effect_id, "terminal_flat")

    repaired = executor.reconcile_terminal_effects()
    assert repaired == [{
        "reconciled": True,
        "reservation_id": reservation_id,
        "lifecycle_state": "filled",
        "changed": True,
    }]
    assert store.reservation_row(reservation_id)["state"] == "consumed"
    assert store.exposure_state("exp-oi2-legacy") == "closed"
    assert store.active_totals(now.date().isoformat())["positions"] == 0
    assert executor.reconcile_terminal_effects() == []

    next_intent = AssetIntent(
        object_id="next-signal", as_of=now,
        valid_until=now + timedelta(minutes=30),
        producer={"name": "test", "version": "1"}, trace_id="next-trace",
        cell_id="equity:SPY@1d:model", asset_id="equity:SPY",
        action="target", target_exposure=-1.0,
        risk_geometry={
            "mode": "fixed_price", "stop_price": 101.0,
            "take_profit_price": 98.0,
        },
        artifact_hash="sha256:" + "d" * 64,
    )
    capability = BrokerCapabilitySnapshot(
        object_id="cap-next", as_of=now,
        producer={"name": "test", "version": "1"}, trace_id="cap-trace",
        venue="alpaca_paper", account_fingerprint="0123456789abcdef",
        environment="paper", capability_evidence="live_observed",
        source_artifact_hash="sha256:" + "e" * 64,
        source_observed_at=now,
        instruments=[InstrumentCapability(
            instrument="SPY", tradeable=True, shortable=True,
            min_units=1.0, unit_step=1.0, price_decimals=2,
            margin_rate=1.0, native_stop_loss=True,
            native_take_profit=True, native_bracket=True,
        )],
    )
    decision = service.process_intent(
        next_intent, capability, equity=100_000.0, reference_price=100.0,
        quote_time=now, instrument="SPY", now=now,
    )
    assert decision["outcome"] == "would_be_order", decision
