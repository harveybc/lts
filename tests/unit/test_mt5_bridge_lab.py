from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.mt5_bridge_lab import (
    Mt5BridgeConfig,
    Mt5BridgeError,
    Mt5BridgeStore,
    create_mt5_bridge_app,
    create_signed_headers,
)


SECRET = b"0123456789abcdef0123456789abcdef"
EA_PATH = (
    Path(__file__).resolve().parents[2]
    / "mt5"
    / "MQL5"
    / "Experts"
    / "LtsMt5ReadOnlyBridge.mq5"
)


def _config(tmp_path: Path) -> Mt5BridgeConfig:
    return Mt5BridgeConfig(
        database_path=tmp_path / "mt5.sqlite",
        secret_env="LTS_MT5_BRIDGE_SECRET",
        environment="demo",
        read_only=True,
        bind_host="127.0.0.1",
        port=8766,
        max_clock_skew_seconds=90,
        nonce_retention_seconds=900,
        stale_heartbeat_seconds=180,
        allowed_account_fingerprints=(),
    )


def test_read_only_ea_default_watchlist_covers_selected_crypto_and_fx() -> None:
    source = EA_PATH.read_text(encoding="utf-8")
    for symbol in (
        "SOLUSD",
        "ETHUSD",
        "BTCUSD",
        "ADAUSD",
        "DOGEUSD",
        "XRPUSD",
        "USDCAD",
        "EURJPY",
        "EURUSD",
        "AUDUSD",
        "GBPJPY",
        "USDJPY",
        "NZDUSD",
    ):
        assert symbol in source


def _signed_post(client: TestClient, path: str, payload: dict, nonce: str):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            **create_signed_headers(
                SECRET,
                "POST",
                path,
                body,
                timestamp=int(time.time()),
                nonce=nonce,
            ),
        },
    )


def test_config_is_demo_read_only_and_contains_no_secret(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema": "lts.mt5.bridge_config.v1",
                "environment": "demo",
                "read_only": True,
                "secret_env": "LTS_MT5_BRIDGE_SECRET",
            }
        )
    )
    config = Mt5BridgeConfig.load(path)
    assert config.read_only is True
    assert config.environment == "demo"

    value = json.loads(path.read_text())
    value["secret"] = "must-not-be-tracked"
    path.write_text(json.dumps(value))
    with pytest.raises(Mt5BridgeError, match="Credentials"):
        Mt5BridgeConfig.load(path)


def test_heartbeat_requires_valid_signature_and_rejects_replay(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = Mt5BridgeStore(config.database_path)
    client = TestClient(create_mt5_bridge_app(config, store, SECRET))
    payload = {
        "schema": "lts.mt5.heartbeat.v1",
        "adapter_version": "test-ea",
        "account_fingerprint": "0123456789abcdef",
        "server_fingerprint": "fedcba9876543210",
        "environment": "demo",
        "connected": True,
        "trade_allowed": False,
        "terminal_build": 5000,
        "terminal_ping_ms": 12.5,
        "observed_at": "2026-07-30T12:00:00+00:00",
    }
    try:
        assert client.post("/v1/heartbeat", json=payload).status_code == 401
        first = _signed_post(client, "/v1/heartbeat", payload, "nonce-0000000001")
        replay = _signed_post(client, "/v1/heartbeat", payload, "nonce-0000000001")
        assert first.status_code == 200
        assert first.json()["read_only"] is True
        assert replay.status_code == 401
        assert "already used" in replay.json()["detail"]

        status = client.get("/v1/status")
        assert status.status_code == 200
        value = status.json()
        assert value["schema"] == "lts.mt5.operational_status.v1"
        assert value["available"] is True
        assert value["heartbeat"]["connected"] is True
        serialized = json.dumps(value)
        assert "account_fingerprint" not in serialized
        assert "server_fingerprint" not in serialized
        assert "0123456789abcdef" not in serialized
        assert "fedcba9876543210" not in serialized
    finally:
        store.close()


def test_snapshot_persists_reconciliation_and_symbol_cost_facts(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = Mt5BridgeStore(config.database_path)
    client = TestClient(create_mt5_bridge_app(config, store, SECRET))
    payload = {
        "schema": "lts.mt5.snapshot.v1",
        "account_fingerprint": "0123456789abcdef",
        "observed_at": "2026-07-30T12:00:00+00:00",
        "currency": "USD",
        "balance": 10000.0,
        "equity": 10005.0,
        "margin": 0.0,
        "free_margin": 10005.0,
        "positions": [],
        "orders": [],
        "symbols": [
            {
                "symbol": "EURUSD",
                "bid": 1.1,
                "ask": 1.1002,
                "point": 0.00001,
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01,
                "trade_mode": 4,
                "observed_at": "2026-07-30T12:00:00+00:00",
            }
        ],
    }
    try:
        response = _signed_post(
            client,
            "/v1/snapshot",
            payload,
            "nonce-0000000002",
        )
        assert response.status_code == 200
        report = store.report()
        assert report["latest_snapshot"]["positions_total"] == 0
        assert report["latest_snapshot"]["symbols_total"] == 1
    finally:
        store.close()

    connection = sqlite3.connect(config.database_path)
    try:
        row = connection.execute(
            "SELECT spread,spread_points FROM symbol_snapshots"
        ).fetchone()
    finally:
        connection.close()
    assert row[0] == pytest.approx(0.0002)
    assert row[1] == pytest.approx(20.0)


def test_trade_events_are_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = Mt5BridgeStore(config.database_path)
    client = TestClient(create_mt5_bridge_app(config, store, SECRET))
    payload = {
        "schema": "lts.mt5.trade_event.v1",
        "event_id": "event-00000001",
        "account_fingerprint": "0123456789abcdef",
        "event_type": "manual_deal",
        "order_ticket": "10",
        "deal_ticket": "11",
        "symbol": "EURUSD",
        "volume": 0.01,
        "price": 1.1,
        "result_code": 10009,
        "observed_at": "2026-07-30T12:00:00+00:00",
    }
    try:
        first = _signed_post(client, "/v1/events", payload, "nonce-0000000003")
        second = _signed_post(client, "/v1/events", payload, "nonce-0000000004")
        assert first.json()["duplicate"] is False
        assert second.json()["duplicate"] is True
    finally:
        store.close()
