import json
import sqlite3
from datetime import datetime, timezone

import pytest

from app.multi_venue_shadow import (
    MultiVenueShadow,
    MultiVenueShadowConfig,
    MultiVenueShadowError,
    MultiVenueShadowOlap,
    QuoteReader,
)


def _source_databases(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    alpaca = tmp_path / "alpaca.sqlite"
    connection = sqlite3.connect(alpaca)
    connection.execute(
        """
        CREATE TABLE quote_observations (
            session_id TEXT,symbol TEXT,broker_time TEXT,observed_at TEXT,
            bid REAL,ask REAL,mid REAL,spread REAL,spread_bps REAL,
            bid_size REAL,ask_size REAL,quote_json TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO quote_observations VALUES
        ('a','BTC/USD','2026-07-30T12:00:00+00:00',
         '2026-07-30T12:00:00+00:00',99,101,100,2,200,1,1,'{}')
        """
    )
    connection.commit()
    connection.close()

    ibkr = tmp_path / "ibkr.sqlite"
    connection = sqlite3.connect(ibkr)
    connection.execute(
        """
        CREATE TABLE quote_observations (
            session_id TEXT,cell_id TEXT,symbol TEXT,broker_time TEXT,
            observed_at TEXT,bid REAL,ask REAL,mid REAL,last REAL,close REAL,
            mark_price REAL,spread REAL,spread_bps REAL,bid_size REAL,
            ask_size REAL,market_data_type INTEGER,quote_json TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO quote_observations VALUES
        ('i','spy','SPY','2026-07-30T12:00:00+00:00',
         '2026-07-30T12:00:00+00:00',199,201,200,200,198,200,2,100,
         1,1,3,'{}')
        """
    )
    connection.commit()
    connection.close()
    return alpaca, ibkr


def _config(tmp_path, **overrides):
    alpaca, ibkr = _source_databases(tmp_path)
    payload = {
        "schema": "lts.multi_venue_shadow_config.v1",
        "mode": "shadow_no_orders",
        "database_path": str(tmp_path / "shadow.sqlite"),
        "initial_nav": 1000,
        "orders": {"enabled": False},
        "source_databases": {
            "alpaca_paper": str(alpaca),
            "ibkr_paper": str(ibkr),
        },
        "cells": [
            {
                "cell_id": "btc",
                "venue": "alpaca_paper",
                "symbol": "BTC/USD",
                "source_key": "BTC/USD",
                "role": "crypto",
                "horizon": "short",
                "weight": 0.5,
                "max_quote_age_seconds": 3600,
            },
            {
                "cell_id": "spy",
                "venue": "ibkr_paper",
                "symbol": "SPY",
                "source_key": "spy",
                "role": "equity",
                "horizon": "medium",
                "weight": 0.5,
                "max_quote_age_seconds": 3600,
            },
        ],
    }
    payload.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return MultiVenueShadowConfig.load(path)


def test_config_rejects_orders_and_bad_weights(tmp_path):
    with pytest.raises(MultiVenueShadowError, match="forbidden"):
        _config(tmp_path / "orders", orders={"enabled": True})
    with pytest.raises(MultiVenueShadowError, match="sum to 1"):
        _config(
            tmp_path / "weights",
            cells=[
                {
                    "cell_id": "btc",
                    "venue": "alpaca_paper",
                    "symbol": "BTC/USD",
                    "source_key": "BTC/USD",
                    "role": "crypto",
                    "horizon": "short",
                    "weight": 0.4,
                }
            ],
        )


def test_shadow_marks_sources_and_never_submits_orders(tmp_path):
    config = _config(tmp_path)
    store = MultiVenueShadowOlap(config.database_path)
    try:
        result = MultiVenueShadow(
            config,
            QuoteReader(config.source_databases),
            store,
        ).snapshot(now=datetime(2026, 7, 30, 12, 5, tzinfo=timezone.utc))
        assert result["status"] == "complete"
        assert result["available_weight"] == 1.0
        assert result["nav"] == 1000.0
        assert result["orders_submitted"] == 0
        assert store.report()["latest_snapshot"]["status"] == "complete"
        assert len(store.report()["latest_cells"]) == 2
    finally:
        store.close()


def test_missing_source_is_recorded_as_cash_and_degraded(tmp_path):
    config = _config(tmp_path)
    connection = sqlite3.connect(config.source_databases["ibkr_paper"])
    connection.execute("DELETE FROM quote_observations")
    connection.commit()
    connection.close()
    store = MultiVenueShadowOlap(config.database_path)
    try:
        result = MultiVenueShadow(
            config,
            QuoteReader(config.source_databases),
            store,
        ).snapshot(now=datetime(2026, 7, 30, 12, 5, tzinfo=timezone.utc))
        assert result["status"] == "degraded"
        assert result["missing_cells"] == 1
        assert result["available_weight"] == 0.5
        assert result["nav"] == 1000.0
    finally:
        store.close()
