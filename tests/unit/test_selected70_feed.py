"""The selected70 window uses the recipe constants and refuses a fabricated bar."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from app.forward_pool.core import PoolRefusal
from app.forward_pool.selected70_feed import (
    HOUR_NS,
    build_windows,
    column_availability,
    inventory_document,
    latest_valid,
    live_status,
    observation_at,
)


def recipe(ops: list[list[str]] | None = None) -> dict:
    ops = ops or [[], []]
    names = ["RAW_CLOSE_LOG_RETURN", "ATR"]
    columns = []
    for name, op in zip(names, ops):
        columns.append(
            {
                "store_column": name,
                "tensor_name": name + ("@diff" if op else ""),
                "consumer_ops": op,
                "upstream": {"decision": "NONE", "applied_in_store": False},
            }
        )
    tensors = [item["tensor_name"] for item in columns]
    return {
        "schema": "expert_guided_consumer_recipe.v2",
        "list": {"columns": [item["store_column"] for item in columns]},
        "tensor": {
            "columns": columns,
            "window": {"observations": 13, "stride_rows": 2, "includes_current_completed_bar": True},
        },
        "scaler": {"policy": "fixed_train", "features": tensors, "mean": [0.0, 10.0], "std": [1.0, 2.0]},
        "business": {"agent_state": "none: no agent feedback in the observation"},
    }


def test_window_scales_and_does_not_use_the_future():
    body = recipe()
    stamps = np.arange(30, dtype=np.int64) * HOUR_NS
    values = np.column_stack([np.arange(30, dtype=np.float64), np.full(30, 12.0)])
    table = build_windows(body, stamps, values)
    last = latest_valid(table)
    full = observation_at(table, last)
    short = build_windows(body, stamps[: last + 1], values[: last + 1])
    assert np.array_equal(observation_at(short, latest_valid(short)), full)
    changed = values.copy()
    changed[-1, 0] = 999.0
    moved = build_windows(body, stamps, changed)
    earlier = last - 1
    assert np.array_equal(observation_at(table, earlier), observation_at(moved, earlier))
    assert full.shape == (13, 2)
    assert full[-1, 0] == np.float32(29.0)
    assert full[-1, 1] == np.float32(1.0)


def test_missing_value_is_not_zero_and_old_store_is_replay():
    body = recipe()
    stamps = np.arange(30, dtype=np.int64) * HOUR_NS
    values = np.ones((30, 2))
    values[10, 1] = np.nan
    table = build_windows(body, stamps, values)
    try:
        observation_at(table, 10)
    except PoolRefusal as exc:
        assert exc.code == "FEATURE_ABSENT"
    else:
        raise AssertionError("nan row was accepted")
    status = live_status(int(table["bar_close_utc_ns"][-1]), int(table["bar_close_utc_ns"][-1]) + 10 * HOUR_NS)
    assert status["evidence_class"] == "HISTORICAL_REPLAY"
    assert status["shadow_observing"] is False
    rows = column_availability(body)
    assert rows[0]["impute"] == "refuse" and rows[0]["price_only_mt5_rebuild"] is False


def test_diff_is_applied_only_when_the_recipe_asks():
    body = recipe([["DIFF"], []])
    stamps = np.arange(30, dtype=np.int64) * HOUR_NS
    values = np.column_stack([np.arange(30, dtype=np.float64), np.arange(30, dtype=np.float64)])
    table = build_windows(body, stamps, values)
    window = observation_at(table, latest_valid(table))
    assert window[-1, 0] == np.float32(1.0)
    assert not np.isfinite(table["windows"][0, -1, 0])
    rows = column_availability(body)
    assert rows[0]["tensor_name"] == "RAW_CLOSE_LOG_RETURN@diff"
    assert rows[0]["store_column"] == "RAW_CLOSE_LOG_RETURN"


def test_closed_split_drops_the_tail_and_a_path_cannot_enter_the_inventory():
    body = recipe()
    body["split"] = {"test_closed_from": "2024-01-03"}
    base = np.int64(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
    stamps = base + np.arange(50, dtype=np.int64) * HOUR_NS
    values = np.column_stack([np.arange(50, dtype=np.float64), np.full(50, 12.0)])
    table = build_windows(body, stamps, values)
    last = latest_valid(table)
    assert int(table["bar_start_utc_ns"][last]) == int(base + 47 * HOUR_NS)
    changed = values.copy()
    changed[-1, 0] = 999.0
    moved = build_windows(body, stamps, changed)
    assert np.array_equal(observation_at(table, last), observation_at(moved, latest_valid(moved)))
    document = inventory_document(body, {"row_count": 48, "refresh_reason": "STORE_ENDS_BEFORE_CURRENT_HOUR"})
    assert document["warmup_rows"] == 25
    assert document["impute"] == "refuse"
    assert document["price_only_mt5_rebuild"] is False
    try:
        inventory_document(body, {"producers": [{"file": "/tmp/secret.parquet"}]})
    except PoolRefusal as exc:
        assert exc.code == "COLUMN_ORDER"
    else:
        raise AssertionError("a private path entered the inventory")
