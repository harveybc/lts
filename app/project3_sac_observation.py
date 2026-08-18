"""Causal ETH SAC observation shared by Paper/Demo inference adapters.

The feature formulas mirror financial-data Stage 2.2. The final vector mirrors
gym-fx ``feature_window_preprocessor`` under ``live_stationary_v2``:
rolling z-score, binary passthrough, row-major feature window and four
live-observable agent-state scalars in Gymnasium Dict key order.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


class SacObservationError(RuntimeError):
    pass


def _sha256(path: str | Path) -> str:
    resolved = Path(os.path.expandvars(str(path))).expanduser()
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


def _feature_columns_sha256(columns: Sequence[str]) -> str:
    payload = json.dumps(
        [str(name) for name in columns], separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def load_live_observation_spec(selector: Any) -> dict[str, Any]:
    """Bind a selected SAC artifact to its exact executable input contract."""
    manifest = selector.manifest
    contract = manifest.get("observation_contract")
    if not isinstance(contract, dict):
        raise SacObservationError("SAC observation contract is missing")
    required = {
        "preprocessor_plugin": "feature_window_preprocessor",
        "feature_scaling": "rolling_zscore",
        "include_price_window": False,
        "include_agent_state": True,
        "agent_state_contract": "live_stationary_v2",
        "continuous_action_contract": "target_exposure_hysteresis_v2",
    }
    for key, expected in required.items():
        if contract.get(key) != expected:
            raise SacObservationError(
                f"SAC observation contract requires {key}={expected!r}"
            )
    config_path = Path(
        os.path.expandvars(str(manifest.get("config_file", "")))
    ).expanduser()
    if not config_path.is_file() or _sha256(config_path) != str(
        manifest.get("config_sha256") or ""
    ):
        raise SacObservationError("SAC runtime config hash mismatch")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SacObservationError("SAC runtime config is unreadable") from exc
    feature_columns = config.get("feature_columns")
    binary_columns = config.get("feature_binary_columns") or []
    if not isinstance(feature_columns, list) or not feature_columns:
        raise SacObservationError("SAC runtime config lacks feature_columns")
    if not isinstance(binary_columns, list):
        raise SacObservationError(
            "SAC runtime config feature_binary_columns is invalid"
        )
    feature_hash = _feature_columns_sha256(feature_columns)
    if feature_hash != str(contract.get("feature_columns_sha256") or ""):
        raise SacObservationError("SAC feature-column identity mismatch")
    try:
        window_size = int(contract["window_size"])
        scaling_window = int(contract["feature_scaling_window"])
        clip = float(contract["feature_clip"])
        holding_scale = int(contract["holding_duration_scale_bars"])
        exit_threshold = float(contract["continuous_exit_threshold"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SacObservationError(
            "SAC executable observation fields are incomplete"
        ) from exc
    expected_dimension = window_size * len(feature_columns) + 4
    actual_shape = tuple(selector.policy.observation_space.shape)
    if actual_shape != (expected_dimension,):
        raise SacObservationError(
            f"SAC artifact observation shape {actual_shape} does not match "
            f"the declared {(expected_dimension,)}"
        )
    if exit_threshold != selector.policy.continuous_exit_threshold:
        raise SacObservationError("SAC exit-threshold identity mismatch")
    return {
        "feature_columns": feature_columns,
        "binary_columns": binary_columns,
        "window_size": window_size,
        "scaling_window": scaling_window,
        "clip": clip,
        "holding_duration_scale_bars": holding_scale,
        "observation_dimension": expected_dimension,
        "feature_columns_sha256": feature_hash,
    }


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    rs = _ema_wilder(up, period) / _ema_wilder(down, period)
    return 100 - (100 / (1 + rs))


def _ema_wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean()


def _autocorr(series: pd.Series, window: int, lag: int) -> pd.Series:
    return series.rolling(window).corr(series.shift(lag))


def _sanitize(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.replace([np.inf, -np.inf], np.nan)
    for column in result.columns:
        if column != "timestamp":
            result[column] = pd.to_numeric(
                result[column], errors="coerce"
            ).astype("float32")
    return result


def normalize_closed_bars(
    bars: Sequence[Mapping[str, Any]],
) -> pd.DataFrame:
    """Validate closed OHLCV bars and return a strictly ordered frame."""
    rows: list[dict[str, Any]] = []
    previous: datetime | None = None
    for item in bars:
        if item.get("complete") is not True:
            raise SacObservationError("incomplete bar cannot enter SAC inference")
        try:
            timestamp = datetime.fromisoformat(
                str(item["time"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SacObservationError("bar timestamp is invalid") from exc
        if timestamp.tzinfo is None:
            raise SacObservationError("bar timestamp must be timezone-aware")
        timestamp = timestamp.astimezone(timezone.utc)
        if previous is not None and timestamp <= previous:
            raise SacObservationError("bars must be strictly time ordered")
        previous = timestamp
        try:
            values = {
                key: float(item[key])
                for key in ("open", "high", "low", "close", "volume")
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise SacObservationError("bar OHLCV values are invalid") from exc
        if not all(np.isfinite(list(values.values()))):
            raise SacObservationError("bar OHLCV values must be finite")
        tolerance = max(abs(values["close"]), 1.0) * 1e-9
        if (
            values["low"] <= 0
            or values["high"] < values["low"]
            or values["close"] < values["low"] - tolerance
            or values["close"] > values["high"] + tolerance
        ):
            raise SacObservationError("bar OHLC geometry is invalid")
        rows.append({"timestamp": timestamp, **values})
    if not rows:
        raise SacObservationError("no closed bars supplied")
    return pd.DataFrame(rows)


def compute_project3_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Reproduce the 83-column Stage-2.2 technical/statistical contract."""
    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    volume = frame["volume"].astype(float)
    technical = pd.DataFrame({"timestamp": frame["timestamp"]})

    for horizon in (1, 5, 10, 20, 60):
        technical[f"return_{horizon}"] = close.pct_change(horizon)
        technical[f"log_return_{horizon}"] = np.log(
            close / close.shift(horizon)
        )
    for period in (10, 20, 50, 100, 200):
        technical[f"sma_{period}"] = close.rolling(period).mean()
        technical[f"ema_{period}"] = _ema(close, period)
        technical[f"close_sma_ratio_{period}"] = (
            close / technical[f"sma_{period}"] - 1
        )
    ema12, ema26 = _ema(close, 12), _ema(close, 26)
    technical["macd"] = ema12 - ema26
    technical["macd_signal"] = _ema(technical["macd"], 9)
    technical["macd_hist"] = technical["macd"] - technical["macd_signal"]
    for period in (7, 14, 21):
        technical[f"rsi_{period}"] = _rsi(close, period)
    low14, high14 = low.rolling(14).min(), high.rolling(14).max()
    technical["stoch_k"] = 100 * (close - low14) / (high14 - low14)
    technical["stoch_d"] = technical["stoch_k"].rolling(3).mean()
    technical["williams_r_14"] = -100 * (high14 - close) / (high14 - low14)
    typical = (high + low + close) / 3
    mean_typical = typical.rolling(14).mean()
    mean_abs_deviation = (typical - mean_typical).abs().rolling(14).mean()
    technical["cci_14"] = (
        (typical - mean_typical) / (0.015 * mean_abs_deviation)
    )
    for period in (10, 20, 60):
        technical[f"roc_{period}"] = close.pct_change(period)
    technical["mom_10"] = close.diff(10)
    technical["mom_20"] = close.diff(20)
    middle = close.rolling(20).mean()
    deviation = close.rolling(20).std()
    technical["bb_upper"] = middle + 2 * deviation
    technical["bb_middle"] = middle
    technical["bb_lower"] = middle - 2 * deviation
    technical["bb_pct_b"] = (
        (close - technical["bb_lower"])
        / (technical["bb_upper"] - technical["bb_lower"])
    )
    technical["bb_width"] = (
        (technical["bb_upper"] - technical["bb_lower"]) / middle
    )
    true_range = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    technical["atr_14"] = true_range.rolling(14).mean()
    technical["natr_14"] = technical["atr_14"] / close
    for period in (10, 20, 60):
        technical[f"hist_vol_{period}"] = (
            technical["log_return_1"].rolling(period).std()
            * np.sqrt(period)
        )
    technical["ema_cross_10_50"] = (
        technical["ema_10"] - technical["ema_50"]
    ) / close
    technical["ema_cross_20_100"] = (
        technical["ema_20"] - technical["ema_100"]
    ) / close
    technical["trend_slope_50"] = (
        np.log(close.replace(0, np.nan)).diff(50) / 50
    )
    technical["trend_strength_50"] = technical["trend_slope_50"].abs()
    signed = np.sign(close.diff()).fillna(0)
    technical["obv"] = (signed * volume.fillna(0)).cumsum()
    technical["obv_delta_20"] = technical["obv"].diff(20)
    for period in (10, 20):
        technical[f"volume_sma_{period}"] = volume.rolling(period).mean()
    technical["volume_ratio_20"] = volume / technical["volume_sma_20"]
    technical["vwap_60"] = (
        (typical * volume).rolling(60).sum() / volume.rolling(60).sum()
    )
    money_flow = typical * volume
    positive = money_flow.where(typical.diff() > 0, 0).rolling(14).sum()
    negative = money_flow.where(typical.diff() < 0, 0).rolling(14).sum().abs()
    technical["mfi_14"] = 100 - (100 / (1 + positive / negative))

    statistical = pd.DataFrame({"timestamp": frame["timestamp"]})
    returns = np.log(close / close.shift()).replace([np.inf, -np.inf], np.nan)
    statistical["statistical__log_return_1"] = returns
    for window in (20, 60, 252):
        statistical[f"roll_mean_ret_{window}"] = returns.rolling(window).mean()
        statistical[f"roll_std_ret_{window}"] = returns.rolling(window).std()
        statistical[f"roll_skew_ret_{window}"] = returns.rolling(window).skew()
        statistical[f"roll_kurt_ret_{window}"] = returns.rolling(window).kurt()
    statistical["realized_var_12"] = returns.pow(2).rolling(12).sum()
    statistical["realized_var_48"] = returns.pow(2).rolling(48).sum()
    statistical["autocorr_lag1_100"] = _autocorr(returns, 100, 1)
    statistical["autocorr_lag5_100"] = _autocorr(returns, 100, 5)
    statistical["sqret_autocorr_lag1_100"] = _autocorr(
        returns.pow(2), 100, 1
    )
    vol20, vol252 = returns.rolling(20).std(), returns.rolling(252).std()
    statistical["vol_regime_high"] = (
        vol20 > vol252.rolling(252).quantile(0.75)
    ).astype(float)
    statistical["vol_regime_low"] = (
        vol20 < vol252.rolling(252).quantile(0.25)
    ).astype(float)
    statistical["hurst_proxy_200"] = (
        0.5 + 0.5 * _autocorr(returns, 200, 1)
    ).clip(0, 1)
    statistical["zscore_close_100"] = (
        (close - close.rolling(100).mean()) / close.rolling(100).std()
    )
    return _sanitize(technical).merge(
        _sanitize(statistical), on="timestamp", how="inner"
    )


def build_sac_observation(
    bars: Sequence[Mapping[str, Any]],
    *,
    feature_columns: Sequence[str],
    binary_columns: Sequence[str],
    window_size: int,
    scaling_window: int,
    clip: float,
    initial_equity: float,
    current_equity: float,
    position_units: float,
    entry_price: float,
    holding_bars: int,
    holding_duration_scale_bars: int,
) -> dict[str, Any]:
    """Build the exact flattened live-stationary policy input."""
    if window_size < 1 or scaling_window < window_size:
        raise SacObservationError("invalid feature/scaling window")
    if holding_duration_scale_bars < 1 or initial_equity <= 0:
        raise SacObservationError("invalid agent-state normalization")
    frame = normalize_closed_bars(bars)
    features = compute_project3_features(frame)
    missing = [name for name in feature_columns if name not in features]
    if missing:
        raise SacObservationError(f"missing feature columns: {missing[:5]}")
    if len(features) < scaling_window:
        raise SacObservationError(
            f"at least {scaling_window} derived rows are required; "
            f"got {len(features)}"
        )
    matrix = features[list(feature_columns)].to_numpy(dtype=np.float64)
    history = matrix[-scaling_window:]
    window = matrix[-window_size:]
    if not np.isfinite(history).all():
        raise SacObservationError(
            "derived feature warm-up is incomplete; more closed bars are "
            "required"
        )
    mean, std = history.mean(axis=0), history.std(axis=0)
    std = np.where(std < 1e-8, 1.0, std)
    scaled = ((window - mean) / std).astype(np.float32)
    binary_names = set(binary_columns)
    unknown_binary = binary_names.difference(feature_columns)
    if unknown_binary:
        raise SacObservationError(
            f"unknown binary feature columns: {sorted(unknown_binary)[:5]}"
        )
    binary = np.array(
        [name in binary_names for name in feature_columns], dtype=bool
    )
    if binary.any():
        scaled[:, binary] = window[:, binary].astype(np.float32)
    np.clip(scaled, -float(clip), float(clip), out=scaled)
    scaled = np.nan_to_num(
        scaled, nan=0.0, posinf=float(clip), neginf=-float(clip)
    )

    price = float(frame["close"].iloc[-1])
    pnl = (
        float(position_units) * (price - float(entry_price))
        if position_units and entry_price > 0
        else 0.0
    )
    state = {
        "equity_norm": np.array(
            [(float(current_equity) - initial_equity) / initial_equity],
            dtype=np.float32,
        ),
        "features": scaled,
        "holding_duration_norm": np.array(
            [min(1.0, max(0, int(holding_bars)) / holding_duration_scale_bars)],
            dtype=np.float32,
        ),
        "position": np.array(
            [1.0 if position_units > 0 else (-1.0 if position_units < 0 else 0.0)],
            dtype=np.float32,
        ),
        "unrealized_pnl_norm": np.array([pnl / initial_equity], dtype=np.float32),
    }
    vector = np.concatenate(
        [np.asarray(state[name], dtype=np.float32).reshape(-1)
         for name in sorted(state)]
    )
    return {
        "schema": "lts.project3_sac_observation.v1",
        "last_closed_bar": frame["timestamp"].iloc[-1].isoformat(),
        "observation": vector,
        "input_sha256": hashlib.sha256(vector.tobytes()).hexdigest(),
        "feature_rows": len(features),
        "observation_dimension": int(vector.size),
    }
