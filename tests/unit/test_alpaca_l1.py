from decimal import Decimal

import pytest

from app.alpaca_l1 import (
    AlpacaL1Executor,
    AlpacaL1Profile,
    AlpacaPaperError,
    verify_native_bracket,
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

    def account(self):
        return {"id": "paper", "status": "ACTIVE", "trading_blocked": False}

    def account_fingerprint(self, _account):
        return self.fingerprint

    def submit_bracket(self, plan):
        self.submit_calls += 1
        self._client_order_id = plan["client_order_id"]
        return {"id": "parent-id", "status": "accepted"}

    def order(self, _order_id):
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
