import json

import pytest

from app.ibkr_paper_lab import (
    ADAPTER_VERSION,
    IbkrPaperError,
    IbkrPaperLab,
    IbkrPaperLabConfig,
    IbkrPaperOlap,
    IbkrTwsPaperClient,
)
from plugins_broker.ibkr_paper_broker import IbkrPaperBroker


def _config(tmp_path, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "lts.ibkr.paper_lab_config.v1",
        "environment": "paper",
        "read_only": True,
        "host": "127.0.0.1",
        "port": 7497,
        "client_id": 12,
        "timeout_seconds": 2,
        "database_path": str(tmp_path / "ibkr.sqlite"),
        "contracts": [
            {
                "cell_id": "spy_1h",
                "canonical_asset": "spy",
                "symbol": "SPY",
                "security_type": "STK",
                "currency": "USD",
                "exchange": "SMART",
                "role": "equity_control",
                "timeframe": "1h",
                "priority": 1,
            },
            {
                "cell_id": "eurusd_4h",
                "canonical_asset": "eurusd",
                "symbol": "EURUSD",
                "security_type": "CASH",
                "currency": "USD",
                "exchange": "IDEALPRO",
                "role": "fx_control",
                "timeframe": "4h",
                "priority": 2,
            },
        ],
    }
    payload.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return IbkrPaperLabConfig.load(path)


class FakeClient:
    def __init__(self):
        self.probes = [
            {
                "endpoint": "connect",
                "observed_at": "2026-07-29T21:00:00+00:00",
                "latency_ms": 25.0,
                "success": True,
                "error_kind": None,
            }
        ]

    def snapshot(self, selections):
        assert [item.cell_id for item in selections] == ["spy_1h", "eurusd_4h"]
        return {
            "account_fingerprints": ["already-hashed-account"],
            "api_version": "2.1.0",
            "server_version": 200,
            "platform": "tws",
            "account_summary": [
                {
                    "account_fingerprint": "already-hashed-account",
                    "tag": "NetLiquidation",
                    "value": "1000000",
                    "currency": "USD",
                    "model_code": "",
                }
            ],
            "positions": [],
            "open_orders": [],
            "contracts": {
                "spy_1h": {
                    "con_id": 756733,
                    "symbol": "SPY",
                    "local_symbol": "SPY",
                    "security_type": "STK",
                    "currency": "USD",
                    "exchange": "SMART",
                    "primary_exchange": "ARCA",
                    "trading_class": "SPY",
                },
                "eurusd_4h": None,
            },
        }


def test_config_allows_only_local_paper_read_only(tmp_path):
    config = _config(tmp_path)
    assert config.port == 7497
    with pytest.raises(IbkrPaperError, match="paper-only"):
        _config(tmp_path / "live", environment="live")
    with pytest.raises(IbkrPaperError, match="read-only"):
        _config(tmp_path / "write", read_only=False)
    with pytest.raises(IbkrPaperError, match="localhost"):
        _config(tmp_path / "remote", host="192.168.1.10")
    with pytest.raises(IbkrPaperError, match="Paper port"):
        _config(tmp_path / "live-port", port=7496)
    with pytest.raises(IbkrPaperError, match="cannot be tracked"):
        _config(tmp_path / "identity", account="DU123456")


def test_native_client_rejects_live_ports_and_remote_hosts():
    with pytest.raises(IbkrPaperError, match="non-Paper"):
        IbkrTwsPaperClient("127.0.0.1", 7496, 1)
    with pytest.raises(IbkrPaperError, match="local"):
        IbkrTwsPaperClient("192.168.1.10", 7497, 1)


def test_preflight_persists_capability_without_account_identity(tmp_path):
    config = _config(tmp_path)
    store = IbkrPaperOlap(config.database_path)
    try:
        result = IbkrPaperLab(config, FakeClient(), store).preflight()
        assert result["adapter_version"] == ADAPTER_VERSION
        assert result["available_cells"] == ["spy_1h"]
        assert result["missing_cells"] == ["eurusd_4h"]
        assert result["orders_submitted"] == 0
        assert result["protected_execution_eligible"] is False

        rows = store.connection.execute(
            """
            SELECT cell_id,available,protected_execution_eligible
            FROM contract_capabilities ORDER BY priority
            """
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("spy_1h", 1, 0),
            ("eurusd_4h", 0, 0),
        ]
        stored = config.database_path.read_bytes()
        assert b"DU123456" not in stored
        assert store.report()["latest_session"]["status"] == "complete"
    finally:
        store.close()


def test_broker_adapter_rejects_every_mutation():
    broker = IbkrPaperBroker({})
    for result in (
        broker.open_order(),
        broker.modify_order(),
        broker.close_order(),
        broker.execute_order(),
    ):
        assert result["success"] is False
        assert "disabled" in result["error"]
