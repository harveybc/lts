"""Selected70 observation plugin.

The store already contains each column. This plugin applies only the recipe's
consumer operation, the fixed TRAIN mean and standard deviation, and the
13-by-70 stride-2 window. It does not fill a missing value with zero, and it
does not treat the pinned historical store as the current hour.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import numpy as np

from app.forward_pool.core import PoolRefusal

HOUR_NS = 3600 * 10**9


def require_recipe(recipe: Mapping[str, Any]) -> dict[str, Any]:
    """Accept the exported categorical recipe fields this plugin consumes."""

    if recipe.get("schema") != "expert_guided_consumer_recipe.v2":
        raise PoolRefusal("BUNDLE_SCHEMA")
    columns = recipe.get("tensor", {}).get("columns")
    listed = (recipe.get("list") or {}).get("columns")
    if not isinstance(columns, list) or not isinstance(listed, list):
        raise PoolRefusal("COLUMN_ORDER")
    names = [str(item["store_column"]) for item in columns]
    tensors = [str(item["tensor_name"]) for item in columns]
    if names != list(listed) or len(names) != len(set(names)):
        raise PoolRefusal("COLUMN_ORDER")
    for item, tensor in zip(columns, tensors):
        ops = item.get("consumer_ops")
        if ops not in ([], ["DIFF"]):
            raise PoolRefusal("CONSUMER_OP")
        expected = item["store_column"] + ("@diff" if ops else "")
        if tensor != expected:
            raise PoolRefusal("COLUMN_ORDER")
    scaler = recipe.get("scaler") or {}
    if scaler.get("policy") != "fixed_train":
        raise PoolRefusal("SCALER_POLICY")
    mean = np.asarray(scaler.get("mean"), dtype=np.float64)
    std = np.asarray(scaler.get("std"), dtype=np.float64)
    if scaler.get("features") != tensors or mean.shape != std.shape or mean.shape != (len(columns),):
        raise PoolRefusal("SCALER")
    if not np.isfinite(mean).all() or not np.isfinite(std).all() or np.any(std <= 0):
        raise PoolRefusal("SCALER")
    window = (recipe.get("tensor") or {}).get("window") or {}
    if window.get("observations") != 13 or window.get("stride_rows") != 2:
        raise PoolRefusal("WINDOW_CONTRACT")
    if window.get("includes_current_completed_bar") is not True:
        raise PoolRefusal("WINDOW_CONTRACT")
    agent = (recipe.get("business") or {}).get("agent_state")
    if not isinstance(agent, str) or not agent.startswith("none:"):
        raise PoolRefusal("AGENT_STATE_INPUT")
    return {
        "columns": tuple(names),
        "ops": tuple(tuple(item.get("consumer_ops") or []) for item in columns),
        "mean": mean,
        "std": std,
        "observations": 13,
        "stride": 2,
    }


def _cutoff_ns(recipe: Mapping[str, Any]) -> int | None:
    """Match ``FrozenRecipeInputs.load``: store rows at or after this instant are excluded."""

    text = (recipe.get("split") or {}).get("test_closed_from")
    if text is None:
        return None
    if not isinstance(text, str) or len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise PoolRefusal("STALE_INPUT")
    try:
        year, month, day = (int(part) for part in text.split("-"))
        instant = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError as exc:
        raise PoolRefusal("STALE_INPUT") from exc
    return int(instant.timestamp()) * 1_000_000_000


def _public(value: Any) -> Any:
    """Reject a filesystem path before it can enter a committed inventory."""

    if isinstance(value, str):
        if "/" in value or "\\" in value or "~" in value:
            raise PoolRefusal("COLUMN_ORDER")
        return value
    if isinstance(value, dict):
        return {str(key): _public(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    if isinstance(value, (bool, int)) or value is None:
        return value
    if isinstance(value, float):
        if not np.isfinite(value):
            raise PoolRefusal("COLUMN_ORDER")
        return value
    raise PoolRefusal("COLUMN_ORDER")


def build_windows(
    recipe: Mapping[str, Any],
    timestamps_ns: Sequence[int] | np.ndarray,
    values: np.ndarray,
) -> dict[str, Any]:
    """Historical replay, including its split cutoff. Timestamps are bar starts."""

    return _build_window_table(recipe, timestamps_ns, values, historical_split=True)


def _build_window_table(
    recipe: Mapping[str, Any],
    timestamps_ns: Sequence[int] | np.ndarray,
    values: np.ndarray,
    *,
    historical_split: bool,
) -> dict[str, Any]:

    contract = require_recipe(recipe)
    stamps = np.asarray(timestamps_ns, dtype=np.int64)
    matrix = np.asarray(values, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] != len(contract["columns"]) or matrix.shape[0] != len(stamps):
        raise PoolRefusal("COLUMN_ORDER")
    cutoff = _cutoff_ns(recipe) if historical_split else None
    if cutoff is not None:
        keep = stamps < cutoff
        if not bool(keep.any()):
            raise PoolRefusal("FEATURE_ABSENT")
        matrix = matrix[keep]
        stamps = stamps[keep]
    if len(stamps) == 0 or np.any(np.diff(stamps) <= 0):
        raise PoolRefusal("STALE_INPUT")
    finite = np.isfinite(matrix).all(axis=1)
    if not finite.any():
        raise PoolRefusal("FEATURE_ABSENT")
    first = int(np.flatnonzero(finite)[0])
    matrix = matrix[first:].copy()
    stamps = stamps[first:]
    for index, ops in enumerate(contract["ops"]):
        if ops == ("DIFF",):
            matrix[:, index] = np.r_[np.nan, np.diff(matrix[:, index])]
    scaled = (matrix - contract["mean"]) / contract["std"]
    stride = contract["stride"]
    observations = contract["observations"]
    slots = np.arange(len(matrix))[:, None] - stride * np.arange(observations - 1, -1, -1)
    complete = slots[:, 0] >= 0
    windows = scaled[slots.clip(0, len(matrix) - 1)].astype(np.float32)
    valid = complete & np.isfinite(windows).all(axis=(1, 2))
    close_ns = stamps + HOUR_NS
    return {
        "columns": contract["columns"],
        "bar_start_utc_ns": stamps,
        "bar_close_utc_ns": close_ns,
        "windows": windows,
        "valid": valid,
    }


def build_forward_observation(
    recipe: Mapping[str, Any],
    timestamps_ns: Sequence[int] | np.ndarray,
    values: np.ndarray,
    *,
    published_ns: np.ndarray,
    decision_time_ns: int,
    expected_close_ns: int,
) -> dict[str, Any]:
    """Build the exact requested completed bar without the historical split cap.

    The caller supplies the calendar's expected close and per-cell availability
    timestamps from governed producers. This function neither acquires data nor
    attests producer causality. A missing requested bar never falls back to an
    older valid observation. The frozen recipe and scaler remain unchanged.
    """

    contract = require_recipe(recipe)
    stamps = np.asarray(timestamps_ns, dtype=np.int64)
    matrix = np.asarray(values, dtype=np.float64)
    published = np.asarray(published_ns)
    if stamps.ndim != 1 or matrix.shape != (len(stamps), len(contract["columns"])):
        raise PoolRefusal("COLUMN_ORDER")
    if published.shape != matrix.shape or published.dtype.kind not in "iu":
        raise PoolRefusal("PUBLICATION_TIME")
    if expected_close_ns > decision_time_ns or expected_close_ns % HOUR_NS:
        raise PoolRefusal("DECISION_TIME")
    if len(stamps) == 0 or np.any(np.diff(stamps) <= 0):
        raise PoolRefusal("STALE_INPUT")
    # Cut at the requested completed bar before applying DIFF or scaling.
    included = stamps + HOUR_NS <= expected_close_ns
    if not included.any() or int(stamps[included][-1]) + HOUR_NS != expected_close_ns:
        raise PoolRefusal("EXPECTED_BAR_ABSENT")
    visible = matrix[included].copy()
    available = published[included] <= decision_time_ns
    visible[~available] = np.nan
    table = _build_window_table(recipe, stamps[included], visible, historical_split=False)
    index = len(table["valid"]) - 1
    window = observation_at(table, index)
    return {
        "columns": contract["columns"],
        "bar_close_utc_ns": int(table["bar_close_utc_ns"][index]),
        "decision_time_utc_ns": int(decision_time_ns),
        "features": window,
        "scaler_policy": "fixed_train",
        "purpose": "forward_inference",
    }


def observation_at(table: Mapping[str, Any], index: int) -> np.ndarray:
    valid = np.asarray(table["valid"], dtype=bool)
    if type(index) is not int or not 0 <= index < len(valid) or not bool(valid[index]):
        raise PoolRefusal("FEATURE_ABSENT")
    return np.array(table["windows"][index], dtype=np.float32, copy=True)


def latest_valid(table: Mapping[str, Any]) -> int:
    valid = np.flatnonzero(np.asarray(table["valid"], dtype=bool))
    if len(valid) == 0:
        raise PoolRefusal("FEATURE_ABSENT")
    return int(valid[-1])


def live_status(last_close_utc_ns: int, now_utc_ns: int) -> dict[str, Any]:
    """A historical close is replay evidence. It is not the current hour."""

    age = int(now_utc_ns) - int(last_close_utc_ns)
    fresh = 0 <= age < 2 * HOUR_NS
    return {
        "evidence_class": "LIVE_FINALIZED_BAR" if fresh else "HISTORICAL_REPLAY",
        "shadow_observing": False,
        "age_ns": age,
        "reason": None if fresh else "STORE_ENDS_BEFORE_CURRENT_HOUR",
    }


def column_availability(recipe: Mapping[str, Any]) -> list[dict[str, Any]]:
    contract = require_recipe(recipe)
    rows = []
    for item in recipe["tensor"]["columns"]:
        upstream = item.get("upstream") or {}
        learned = upstream.get("learned_producer")
        if learned is not None and not isinstance(learned, str):
            raise PoolRefusal("COLUMN_ORDER")
        rows.append(
            {
                "tensor_name": item["tensor_name"],
                "store_column": item["store_column"],
                "consumer_ops": list(item.get("consumer_ops") or []),
                "upstream_decision": upstream.get("decision"),
                "applied_in_store": bool(upstream.get("applied_in_store")),
                "learned_producer": learned,
                "fit_scope": "consumer" if upstream.get("fit_scope") else None,
                "scaling": "fixed_train",
                "impute": "refuse",
                "price_only_mt5_rebuild": False,
            }
        )
    if tuple(row["store_column"] for row in rows) != contract["columns"]:
        raise PoolRefusal("COLUMN_ORDER")
    return rows


def inventory_document(recipe: Mapping[str, Any], measured: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Public availability record. A path is refused; a missing hour stays null."""

    contract = require_recipe(recipe)
    window = (recipe.get("tensor") or {}).get("window") or {}
    offsets = window.get("slot_offsets_rows")
    if offsets is None:
        offsets = [-contract["stride"] * step for step in range(contract["observations"] - 1, -1, -1)]
    scaler = recipe.get("scaler") or {}
    body: dict[str, Any] = {
        "schema": "forward_pool.selected70_availability.v1",
        "recipe_identity": recipe.get("identity"),
        "scaler_policy": "fixed_train",
        "scaler_fit_period": scaler.get("fit_period"),
        "parent_scaler_sha256": (scaler.get("derivation") or {}).get("parent_scaler_sha256"),
        "observations": contract["observations"],
        "stride_rows": contract["stride"],
        "includes_current_completed_bar": True,
        "slot_offsets_rows": list(offsets),
        "warmup_rows": 1 + contract["stride"] * (contract["observations"] - 1),
        "impute": "refuse",
        "price_only_mt5_rebuild": False,
        "refresh_cadence": None,
        "columns": column_availability(recipe),
    }
    for key, value in dict(measured or {}).items():
        if key in {"columns", "impute", "price_only_mt5_rebuild", "scaler_policy"}:
            continue
        body[key] = value
    body["impute"] = "refuse"
    body["price_only_mt5_rebuild"] = False
    body["scaler_policy"] = "fixed_train"
    return _public(body)
