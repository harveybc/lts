import json

import pytest

from app.alpaca_paper_lab import (
    ADAPTER_VERSION,
    AlpacaPaperClient,
    AlpacaPaperError,
    AlpacaPaperLab,
    AlpacaPaperLabConfig,
    AlpacaPaperOlap,
)
from plugins_broker.alpaca_paper_broker import AlpacaPaperBroker


def _config(tmp_path, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "lts.alpaca.paper_lab_config.v1",
        "environment": "paper",
        "trading_base_url": "https://paper-api.alpaca.markets",
        "data_base_url": "https://data.alpaca.markets",
        "data_location": "us",
        "database_path": str(tmp_path / "alpaca.sqlite"),
        "timeout_seconds": 3,
        "secrets": {
            "api_key_env": "TEST_ALPACA_KEY",
            "api_secret_env": "TEST_ALPACA_SECRET",
        },
        "orders": {"enabled": False},
        "instruments": [
            {
                "cell_id": "btc_1h",
                "canonical_asset": "btcusdt",
                "alpaca_symbol": "BTC/USD",
                "asset_class": "crypto",
                "role": "short_horizon",
                "timeframe": "1h",
                "priority": 1,
            },
            {
                "cell_id": "btc_4h",
                "canonical_asset": "btcusdt",
                "alpaca_symbol": "BTC/USD",
                "asset_class": "crypto",
                "role": "medium_long_horizon",
                "timeframe": "4h",
                "priority": 2,
            },
        ],
    }
    payload.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return AlpacaPaperLabConfig.load(path)


class FakeClient:
    def __init__(self):
        self.probes = [
            {
                "endpoint": "account",
                "observed_at": "2026-07-29T20:00:00+00:00",
                "latency_ms": 12.5,
                "http_status": 200,
                "success": True,
                "request_id_fingerprint": "request-hash",
                "error_kind": None,
            }
        ]

    def account(self):
        return {
            "id": "private-account-uuid",
            "account_number": "private-account-number",
            "status": "ACTIVE",
            "currency": "USD",
            "account_blocked": False,
            "trading_blocked": False,
            "transfers_blocked": False,
            "shorting_enabled": True,
            "pattern_day_trader": False,
            "equity": "100000",
            "cash": "100000",
            "buying_power": "200000",
        }

    @staticmethod
    def account_fingerprint(account):
        return "account-hash"

    def clock(self):
        return {"is_open": True, "timestamp": "2026-07-29T20:00:00Z"}

    def assets(self, asset_class):
        assert asset_class == "crypto"
        return [
            {
                "id": "public-asset-uuid",
                "symbol": "BTC/USD",
                "class": "crypto",
                "status": "active",
                "tradable": True,
                "marginable": False,
                "shortable": False,
                "fractionable": True,
                "min_order_size": "0.00001",
                "min_trade_increment": "0.000000001",
                "price_increment": "1",
            }
        ]

    def positions(self):
        return []

    def open_orders(self):
        return []

    def latest_crypto_quotes(self, symbols, *, location):
        assert symbols == ["BTC/USD"]
        assert location == "us"
        return {
            "BTC/USD": {
                "bp": 100000,
                "ap": 100010,
                "bs": 1.5,
                "as": 1.25,
                "t": "2026-07-29T20:00:00Z",
            }
        }


def test_config_is_paper_only_and_credentials_are_external(tmp_path):
    config = _config(tmp_path)
    assert config.credentials(
        {"TEST_ALPACA_KEY": "key", "TEST_ALPACA_SECRET": "secret"}
    ) == ("key", "secret")

    with pytest.raises(AlpacaPaperError, match="paper-only"):
        _config(tmp_path / "live", environment="live")
    with pytest.raises(AlpacaPaperError, match="read-only"):
        _config(tmp_path / "orders", orders={"enabled": True})
    with pytest.raises(AlpacaPaperError, match="Paper trading endpoint"):
        _config(
            tmp_path / "endpoint",
            trading_base_url="https://api.alpaca.markets",
        )


def test_client_rejects_live_and_unapproved_data_endpoints():
    with pytest.raises(AlpacaPaperError, match="cannot connect to Alpaca Live"):
        AlpacaPaperClient(
            "key",
            "secret",
            trading_base_url="https://api.alpaca.markets",
        )
    with pytest.raises(AlpacaPaperError, match="Unapproved"):
        AlpacaPaperClient(
            "key",
            "secret",
            data_base_url="https://example.invalid",
        )


def test_preflight_records_cells_quotes_and_redacted_account(tmp_path):
    config = _config(tmp_path)
    store = AlpacaPaperOlap(config.database_path)
    try:
        result = AlpacaPaperLab(config, FakeClient(), store).preflight()
        assert result["adapter_version"] == ADAPTER_VERSION
        assert result["available_cells"] == ["btc_1h", "btc_4h"]
        assert result["missing_cells"] == []
        assert result["quotes_received"] == ["BTC/USD"]
        assert result["orders_submitted"] == 0
        assert result["protected_execution_eligible"] is False

        rows = store.connection.execute(
            """
            SELECT cell_id,shortable,marginable,protected_execution_eligible
            FROM instrument_capabilities ORDER BY cell_id
            """
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("btc_1h", 0, 0, 0),
            ("btc_4h", 0, 0, 0),
        ]
        account_json = store.connection.execute(
            "SELECT capability_json FROM account_capabilities"
        ).fetchone()[0]
        assert "private-account-uuid" not in account_json
        assert "private-account-number" not in account_json
        assert account_json.count("<redacted>") == 2

        quote = store.connection.execute(
            "SELECT spread,spread_bps FROM quote_observations"
        ).fetchone()
        assert quote["spread"] == pytest.approx(10.0)
        assert quote["spread_bps"] == pytest.approx(0.9999500025)
        assert store.report()["latest_session"]["status"] == "complete"
    finally:
        store.close()


def test_broker_adapter_is_fail_closed_for_every_mutation():
    broker = AlpacaPaperBroker({})
    for result in (
        broker.open_order(),
        broker.modify_order(),
        broker.close_order(),
        broker.execute_order(),
    ):
        assert result["success"] is False
        assert "disabled" in result["error"]
