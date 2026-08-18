from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.project3_sac_observation import (
    SacObservationError,
    build_sac_observation,
    compute_project3_features,
    load_live_observation_spec,
    normalize_closed_bars,
)


def _bars(count: int = 800):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        trend = 1500.0 + index * 0.7
        cycle = 12.0 * np.sin(index / 17.0)
        close = trend + cycle
        rows.append({
            "time": (start + timedelta(hours=4 * index)).isoformat(),
            "open": close - 0.5,
            "high": close + 2.0,
            "low": close - 2.0,
            "close": close,
            "volume": 1000.0 + 50.0 * np.cos(index / 11.0),
            "complete": True,
        })
    return rows


def test_closed_bar_contract_refuses_incomplete_or_reordered_data():
    bars = _bars(3)
    bars[1]["complete"] = False
    with pytest.raises(SacObservationError, match="incomplete"):
        normalize_closed_bars(bars)

    bars = _bars(3)
    bars[1], bars[2] = bars[2], bars[1]
    with pytest.raises(SacObservationError, match="strictly time ordered"):
        normalize_closed_bars(bars)


def test_feature_formulas_are_causal_under_future_bar_append():
    frame = normalize_closed_bars(_bars())
    before = compute_project3_features(frame.iloc[:-1]).iloc[-1]
    after = compute_project3_features(frame).iloc[-2]
    pd.testing.assert_series_equal(
        before.drop(labels="timestamp"),
        after.drop(labels="timestamp"),
        check_names=False,
    )


def test_live_stationary_observation_has_exact_shape_state_and_hash():
    frame = normalize_closed_bars(_bars())
    feature_columns = [
        column for column in compute_project3_features(frame).columns
        if column != "timestamp"
    ]
    result = build_sac_observation(
        _bars(), feature_columns=feature_columns,
        binary_columns=["vol_regime_high", "vol_regime_low"],
        window_size=32, scaling_window=256, clip=10.0,
        initial_equity=10_000.0, current_equity=10_100.0,
        position_units=0.25, entry_price=1900.0, holding_bars=21,
        holding_duration_scale_bars=42,
    )
    observation = result["observation"]
    assert observation.shape == (32 * len(feature_columns) + 4,)
    assert np.isfinite(observation).all()
    assert result["observation_dimension"] == observation.size
    assert len(result["input_sha256"]) == 64

    # Gymnasium Dict flatten order is alphabetical: equity, features,
    # holding duration, position, unrealized PnL.
    assert observation[0] == pytest.approx(0.01)
    assert observation[-3] == pytest.approx(0.5)
    assert observation[-2] == pytest.approx(1.0)
    expected_pnl = 0.25 * (_bars()[-1]["close"] - 1900.0) / 10_000.0
    assert observation[-1] == pytest.approx(expected_pnl)


def test_unknown_binary_feature_is_refused():
    frame = normalize_closed_bars(_bars())
    feature_columns = [
        column for column in compute_project3_features(frame).columns
        if column != "timestamp"
    ]
    with pytest.raises(SacObservationError, match="unknown binary"):
        build_sac_observation(
            _bars(), feature_columns=feature_columns,
            binary_columns=["not_a_feature"], window_size=32,
            scaling_window=256, clip=10.0, initial_equity=10_000.0,
            current_equity=10_000.0, position_units=0.0,
            entry_price=0.0, holding_bars=0,
            holding_duration_scale_bars=42,
        )


def test_incomplete_feature_warmup_is_refused():
    frame = normalize_closed_bars(_bars())
    feature_columns = [
        column for column in compute_project3_features(frame).columns
        if column != "timestamp"
    ]
    with pytest.raises(SacObservationError, match="warm-up"):
        build_sac_observation(
            _bars(300), feature_columns=feature_columns,
            binary_columns=["vol_regime_high", "vol_regime_low"],
            window_size=32, scaling_window=256, clip=10.0,
            initial_equity=10_000.0, current_equity=10_000.0,
            position_units=0.0, entry_price=0.0, holding_bars=0,
            holding_duration_scale_bars=42,
        )


def test_live_spec_binds_config_features_to_artifact_shape(tmp_path):
    frame = normalize_closed_bars(_bars())
    feature_columns = [
        column for column in compute_project3_features(frame).columns
        if column != "timestamp"
    ]
    config = {
        "feature_columns": feature_columns,
        "feature_binary_columns": ["vol_regime_high", "vol_regime_low"],
    }
    config_path = tmp_path / "config.json"
    config_bytes = json.dumps(config).encode()
    config_path.write_bytes(config_bytes)
    feature_bytes = json.dumps(
        feature_columns, separators=(",", ":")
    ).encode()
    contract = {
        "preprocessor_plugin": "feature_window_preprocessor",
        "feature_scaling": "rolling_zscore",
        "feature_scaling_window": 256,
        "feature_clip": 10.0,
        "include_price_window": False,
        "include_agent_state": True,
        "agent_state_contract": "live_stationary_v2",
        "holding_duration_scale_bars": 42,
        "window_size": 32,
        "continuous_action_contract": "target_exposure_hysteresis_v2",
        "continuous_exit_threshold": 0.02,
        "feature_columns_sha256": hashlib.sha256(feature_bytes).hexdigest(),
    }
    selector = SimpleNamespace(
        manifest={
            "config_file": str(config_path),
            "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "observation_contract": contract,
        },
        policy=SimpleNamespace(
            observation_space=SimpleNamespace(
                shape=(32 * len(feature_columns) + 4,)
            ),
            continuous_exit_threshold=0.02,
        ),
    )
    spec = load_live_observation_spec(selector)
    assert spec["observation_dimension"] == 2660
    assert spec["feature_columns"] == feature_columns

    selector.policy.observation_space.shape = (2724,)
    with pytest.raises(SacObservationError, match="observation shape"):
        load_live_observation_spec(selector)
