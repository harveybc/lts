"""Greedy categorical bundle gate.

Continuous SAC, rolling z-score, agent-state inputs and stochastic sampling
are refused. They are not converted into NOP/BUY/SELL.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np

from app.forward_pool.core import PoolRefusal

CODEC = "NOP_BUY_SELL_CLOSE_OPPOSITE_V1"
SCHEMA = "structural_sac.inference.v1"
BUNDLE_FILES = frozenset({"RECIPE.json", "MODEL_CONFIG.json", "model.pt", "PROBE.npz"})
LEGACY_LOADERS = frozenset(
    {
        "live_sac_selection",
        "project3_sac_observation",
        "continuous_sac",
        "rolling_zscore",
        "linear",
    }
)
AGENT_STATE_NONE = "none: no agent feedback in the observation"


def greedy_signal(logits: Sequence[float] | np.ndarray) -> int:
    """First-index argmax, which is the native tie policy of ``ndarray.argmax``.

    Equal logits select NOP. A tie between BUY and SELL that beats NOP selects
    BUY, because index 1 precedes index 2. Any other tie rule is a different policy.
    """

    array = np.asarray(logits, dtype=np.float64)
    if array.shape != (3,) or not np.isfinite(array).all():
        raise PoolRefusal("ACTION_MAPPING")
    return int(np.argmax(array))


def _columns(recipe: Mapping[str, Any]) -> list[str]:
    try:
        columns = list(recipe["list"]["columns"])
    except (KeyError, TypeError) as exc:
        raise PoolRefusal("COLUMN_ORDER") from exc
    if not columns or any(type(column) is not str or not column for column in columns):
        raise PoolRefusal("COLUMN_ORDER")
    return columns


def authenticate_bundle(
    manifest: Mapping[str, Any],
    recipe: Mapping[str, Any],
    *,
    mode: str,
    loader: str,
    expected_columns: Sequence[str] | None = None,
    expected_scaler_sha256: str | None = None,
) -> dict[str, Any]:
    """Accept one immutable categorical bundle description, or refuse it."""

    if loader in LEGACY_LOADERS or loader != "categorical_greedy_bundle":
        raise PoolRefusal("LEGACY_ADAPTER")
    if mode != "greedy":
        raise PoolRefusal("STOCHASTIC_MODE")
    if manifest.get("schema") != SCHEMA:
        raise PoolRefusal("BUNDLE_SCHEMA")
    if manifest.get("codec") != CODEC:
        raise PoolRefusal("CODEC")
    files = manifest.get("files")
    if not isinstance(files, Mapping) or set(files) != BUNDLE_FILES:
        raise PoolRefusal("BUNDLE_FILES")
    columns = _columns(recipe)
    if list(manifest.get("columns") or []) != columns:
        raise PoolRefusal("COLUMN_ORDER")
    if expected_columns is not None and list(expected_columns) != columns:
        raise PoolRefusal("COLUMN_ORDER")
    scaler = recipe.get("scaler")
    if not isinstance(scaler, Mapping) or scaler.get("policy") != "fixed_train":
        raise PoolRefusal("SCALER_POLICY")
    if "rolling" in str(scaler.get("policy")).lower() or "rolling" in str(scaler.get("derivation", "")).lower():
        raise PoolRefusal("ROLLING_NORMALIZATION")
    business = recipe.get("business")
    if not isinstance(business, Mapping):
        raise PoolRefusal("BUSINESS_CONTRACT")
    if business.get("agent_state") != AGENT_STATE_NONE:
        raise PoolRefusal("AGENT_STATE_INPUT")
    window = (recipe.get("tensor") or {}).get("window") or {}
    if window.get("observations") != 13 or window.get("stride_rows") != 2:
        raise PoolRefusal("WINDOW_CONTRACT")
    groups = ((recipe.get("representation") or {}).get("groups") or {}).get("groups")
    if not isinstance(groups, Mapping) or len(groups) < 1:
        raise PoolRefusal("GROUP_LAYOUT")
    if expected_scaler_sha256 is not None and recipe.get("scaler_sha256") != expected_scaler_sha256:
        raise PoolRefusal("SCALER")
    action_space = str(manifest.get("action_space", "categorical"))
    if action_space != "categorical":
        raise PoolRefusal("CONTINUOUS_POLICY")
    return {
        "columns": columns,
        "group_count": len(groups),
        "observations": 13,
        "stride": 2,
        "codec": CODEC,
        "inference": "greedy",
    }
