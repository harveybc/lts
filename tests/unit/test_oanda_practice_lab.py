import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.oanda_practice_lab import (
    ORDER_CONFIRMATION,
    OandaPracticeClient,
    OandaPracticeError,
    OandaPracticeLab,
    PracticeLabConfig,
    PracticeOlap,
)


def _config(tmp_path: Path, *, orders_enabled: bool = False) -> PracticeLabConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema": "lts.oanda.practice_lab_config.v1",
                "environment": "practice",
                "database_path": str(tmp_path / "practice.sqlite"),
                "secrets": {
                    "account_id_env": "TEST_ACCOUNT",
                    "access_token_env": "TEST_TOKEN",
                },
                "polling": {
                    "price_seconds": 0.01,
                    "account_seconds": 0.01,
                    "transaction_seconds": 0.01,
                },
                "instruments": [
                    {
                        "canonical_asset": "eurusd",
                        "oanda_instrument": "EUR_USD",
                        "role": "execution_control",
                        "timeframe": "1h",
                        "priority": 1,
                    }
                ],
                "orders": {"enabled": orders_enabled},
            }
        ),
        encoding="utf-8",
    )
    return PracticeLabConfig.load(path)


class FakeClient:
    account_fingerprint = "abc123"

    def __init__(self):
        self.orders = []

    def account_details(self):
        return {
            "lastTransactionID": "10",
            "account": {
                "id": "secret-account",
                "currency": "USD",
                "balance": "10000",
                "NAV": "10001",
                "unrealizedPL": "1",
                "pl": "4",
                "financing": "-0.5",
                "commission": "0",
                "marginUsed": "20",
                "marginAvailable": "9981",
                "marginCloseoutPercent": "0.01",
                "openTradeCount": 1,
                "openPositionCount": 1,
                "pendingOrderCount": 0,
                "hedgingEnabled": False,
            },
        }

    def instruments(self):
        return {
            "instruments": [
                {
                    "name": "EUR_USD",
                    "displayPrecision": 5,
                    "pipLocation": -4,
                    "minimumTradeSize": "1",
                    "marginRate": "0.02",
                }
            ]
        }

    def prices(self, instruments):
        return {
            "prices": [
                {
                    "instrument": instruments[0],
                    "time": "2026-07-29T12:00:00Z",
                    "status": "tradeable",
                    "bids": [{"price": "1.10000", "liquidity": 1000}],
                    "asks": [{"price": "1.10020", "liquidity": 900}],
                }
            ]
        }

    def transactions_since(self, transaction_id):
        return {
            "lastTransactionID": "11",
            "transactions": [
                {
                    "id": "11",
                    "accountID": "secret-account",
                    "type": "DAILY_FINANCING",
                    "time": "2026-07-29T21:00:00Z",
                    "financing": "-0.50",
                }
            ],
        }

    def create_order(self, order):
        self.orders.append(order)
        return {
            "orderFillTransaction": {
                "orderID": "20",
                "price": "1.10021",
                "tradeOpened": {"tradeID": "21"},
            }
        }


def test_config_is_practice_only_and_reads_secrets_from_environment(tmp_path):
    config = _config(tmp_path)
    assert config.credentials({"TEST_ACCOUNT": "acct", "TEST_TOKEN": "tok"}) == (
        "acct",
        "tok",
    )
    payload = json.loads((tmp_path / "config.json").read_text())
    payload["environment"] = "live"
    (tmp_path / "live.json").write_text(json.dumps(payload))
    with pytest.raises(OandaPracticeError, match="practice-only"):
        PracticeLabConfig.load(tmp_path / "live.json")


def test_rest_client_rejects_live_base_url_and_requires_protection():
    with pytest.raises(OandaPracticeError, match="cannot connect"):
        OandaPracticeClient("acct", "tok", base_url="https://api-fxtrade.oanda.com")
    client = OandaPracticeClient("acct", "tok", session=Mock())
    with pytest.raises(OandaPracticeError, match="requires SL and TP"):
        client.create_order({"instrument": "EUR_USD", "type": "MARKET"})


def test_preflight_records_capabilities_without_account_identity(tmp_path):
    config = _config(tmp_path)
    store = PracticeOlap(config.database_path)
    try:
        result = OandaPracticeLab(config, FakeClient(), store).preflight()
        assert result["available_instruments"] == ["EUR_USD"]
        row = store.connection.execute(
            "SELECT available,display_precision,pip_location FROM instrument_capabilities"
        ).fetchone()
        assert tuple(row) == (1, 5, -4)
        snapshot = store.connection.execute(
            "SELECT snapshot_json FROM account_snapshots"
        ).fetchone()[0]
        assert "secret-account" not in snapshot
        assert "<redacted>" in snapshot
    finally:
        store.close()


def test_observation_is_restart_safe_and_deduplicates_transactions(tmp_path):
    config = _config(tmp_path)
    store = PracticeOlap(config.database_path)
    try:
        lab = OandaPracticeLab(config, FakeClient(), store)
        lab.preflight()
        result = lab.observe(0.04)
        assert result["price_rows"] >= 1
        assert store.report()["transactions"] == 1
        lab.observe(0.02)
        assert store.report()["transactions"] == 1
        report = store.report()
        assert report["operational_health"]["transaction_cursor_present"] is True
        assert report["account_performance"]["mean_weekly_return_fraction"] is None
    finally:
        store.close()


def test_report_only_annualizes_complete_observed_weeks(tmp_path):
    config = _config(tmp_path)
    store = PracticeOlap(config.database_path)
    try:
        session_id = store.start_session("observe", "fingerprint", {})
        for nav in ("100", "90", "110"):
            store.record_account(
                session_id,
                {
                    "lastTransactionID": "10",
                    "account": {"balance": nav, "NAV": nav},
                },
            )
        rows = store.connection.execute(
            "SELECT id FROM account_snapshots ORDER BY id"
        ).fetchall()
        for row, observed_at in zip(
            rows,
            (
                "2026-07-01T00:00:00+00:00",
                "2026-07-04T00:00:00+00:00",
                "2026-07-08T00:00:00+00:00",
            ),
        ):
            store.connection.execute(
                "UPDATE account_snapshots SET observed_at=? WHERE id=?",
                (observed_at, row["id"]),
            )
        store.connection.commit()

        performance = store.report()["account_performance"]
        assert performance["complete_weeks"] == 1
        assert performance["mean_weekly_return_fraction"] == pytest.approx(0.1)
        assert performance["observed_max_drawdown_fraction"] == pytest.approx(0.1)
        assert performance["mean_weekly_rap_fraction"] == pytest.approx(0.0)
        assert performance["annual_return_fraction_additive_52w"] == pytest.approx(5.2)
    finally:
        store.close()


def test_canary_needs_double_opt_in_and_always_attaches_protection(tmp_path):
    disabled = _config(tmp_path)
    store = PracticeOlap(disabled.database_path)
    try:
        with pytest.raises(OandaPracticeError, match="require config enablement"):
            OandaPracticeLab(disabled, FakeClient(), store).protected_market_canary(
                instrument="EUR_USD",
                side="buy",
                units=1,
                stop_distance_pips=10,
                reward_risk_ratio=2,
                confirmation=ORDER_CONFIRMATION,
            )
    finally:
        store.close()

    enabled = _config(tmp_path / "enabled", orders_enabled=True)
    store = PracticeOlap(enabled.database_path)
    client = FakeClient()
    try:
        result = OandaPracticeLab(enabled, client, store).protected_market_canary(
            instrument="EUR_USD",
            side="sell",
            units=1,
            stop_distance_pips=10,
            reward_risk_ratio=2,
            confirmation=ORDER_CONFIRMATION,
        )
        assert result["accepted"] is True
        order = client.orders[0]
        assert order["stopLossOnFill"]["price"] == "1.10100"
        assert order["takeProfitOnFill"]["price"] == "1.09800"
        assert int(order["units"]) < 0
        report = store.report()
        assert report["execution_reports"] == 1
        assert report["execution_health"]["acceptance_rate"] == 1.0
        assert report["execution_health"]["sl_tp_attachment_rate"] == 1.0
    finally:
        store.close()


def test_http_error_does_not_leak_authorization():
    session = Mock()
    response = Mock(status_code=401)
    response.json.return_value = {"errorMessage": "unauthorized"}
    session.request.return_value = response
    client = OandaPracticeClient("acct", "tok", session=session)
    with pytest.raises(OandaPracticeError, match="unauthorized"):
        client.account_details()
