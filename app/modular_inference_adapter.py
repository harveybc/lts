"""Modular inference/feature/action adapter for LTS paper routes (shadow only).

The existing Paper/Demo runners select a ``prediction_provider`` live-linear
policy through ``SelectedLinearPolicy``; they cannot consume a modular Keras
model.  This module is the missing adapter.  It consumes a versioned contract,
``lts.modular_inference_contract.v1``, exported next to a measured candidate
by predictor's ``tools/export_modular_paper_candidate.py``, and splits the work
into three separately checked sides:

* **feature side** - completed bars are validated (timezone, order, OHLC
  geometry, completeness) and cut at an explicit ``as_of``; every bar is placed
  on the session grid by its close (right edge); gaps beyond the contract bound
  refuse.  Any stream with a different frequency must pass through
  :func:`causal_align` - an as-of join that only admits observations available
  at or before each grid right edge and within a declared staleness - before the
  window is assembled.  Equal shapes are never taken as proof of aligned times.
* **inference side** - the predictor engine module is loaded from the exact path
  and SHA-256 the contract names (no fallback), the model archive and its weight
  identity are verified, and both outputs are checked against the contract:
  the forecast head and the rank-three encoder bottleneck (MS15).
* **action side** - a declared dead-band maps the forecast to long/short/hold.

Every output carries ``execution_authorized: False`` and the tier
``shadow_inference_only``.  Nothing here constructs an order intent, touches a
broker client, or is wired into a runner.  Promotion is a separate decision that
waits for model evidence; forecasting error is not trading evidence.
"""
from __future__ import annotations

import bisect
import hashlib
import importlib.util
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

CONTRACT_SCHEMA = "lts.modular_inference_contract.v1"
FEATURE_SCHEMA = "lts.modular_features.closed_bars.v1"
ACTION_SCHEMA = "lts.modular_action.v1"
ALIGNMENT_SCHEMA = "lts.causal_alignment.v1"
OBSERVATION_SCHEMA = "lts.modular_observation.v1"
INFERENCE_SCHEMA = "lts.modular_inference.v1"
TIER = "shadow_inference_only"
FEATURE_KINDS = {"log_return", "range_fraction", "log_volume_z", "asof_field"}
BAR_FIELDS = ("open", "high", "low", "close", "volume")


class ModularAdapterError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise ModularAdapterError(f"unreadable file: {Path(path).name}") from exc


def parse_time(value: Any) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ModularAdapterError("timestamps must be timezone-aware")
    return stamp.astimezone(timezone.utc)


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ModularAdapterError(f"non-numeric {label}") from exc
    if not math.isfinite(number):
        raise ModularAdapterError(f"non-finite {label}")
    return number


def _positive(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ModularAdapterError(f"{label} must be a positive number")
    return float(value)


def _walk_execution_claims(value: Any, trail: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "execution_authorized" and item is not False:
                raise ModularAdapterError(f"contract claims execution authority at {trail}{key}")
            _walk_execution_claims(item, f"{trail}{key}.")
    elif isinstance(value, list):
        for item in value:
            _walk_execution_claims(item, trail)


# ---------------------------------------------------------------- contract


@dataclass(frozen=True)
class ModularContract:
    path: Path
    sha256: str
    data: dict

    @property
    def model_id(self) -> str:
        return self.data["model_id"]

    @property
    def asset_id(self) -> str:
        return self.data["asset_id"]

    @property
    def timeframe(self) -> str:
        return self.data["timeframe"]

    @property
    def window(self) -> int:
        return int(self.data["time"]["window"])

    @property
    def feature_names(self) -> list[str]:
        return list(self.data["features"]["names"])

    def resolve(self, name: str) -> Path:
        path = Path(os.path.expandvars(name)).expanduser()
        return path if path.is_absolute() else self.path.parent / path


def validate_contract(data: Any) -> None:
    """Structural and time-contract checks; raises ModularAdapterError."""
    if not isinstance(data, dict) or data.get("schema") != CONTRACT_SCHEMA:
        raise ModularAdapterError("modular inference contract schema is unsupported")
    if data.get("contract_version") != 1:
        raise ModularAdapterError("modular inference contract version is unsupported")
    required = {"model_id", "asset_id", "timeframe", "engine", "artifact", "modular_config",
                "time", "features", "outputs", "action", "evidence"}
    missing = sorted(required - set(data))
    if missing:
        raise ModularAdapterError(f"contract is missing {missing}")
    for key in ("model_id", "asset_id", "timeframe"):
        if not isinstance(data[key], str) or not data[key]:
            raise ModularAdapterError(f"contract {key} is missing")
    _walk_execution_claims(data)

    time_spec, features, config = data["time"], data["features"], data["modular_config"]
    window = time_spec.get("window")
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise ModularAdapterError("time.window must be a positive integer")
    sample_hours = _positive(time_spec.get("sample_hours"), "time.sample_hours")
    _positive(time_spec.get("max_gap_hours"), "time.max_gap_hours")
    offset = time_spec.get("bar_close_offset_hours")
    if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not 0 <= offset <= 24 * 7:
        raise ModularAdapterError("time.bar_close_offset_hours must be declared")
    streams = time_spec.get("streams")
    if not isinstance(streams, list) or not streams:
        raise ModularAdapterError("time.streams must be declared")
    stream_map = {}
    for stream in streams:
        name = stream.get("name") if isinstance(stream, dict) else None
        if not isinstance(name, str) or name in stream_map:
            raise ModularAdapterError("stream names must be unique strings")
        if stream.get("alignment") not in {"native_grid", "asof"}:
            raise ModularAdapterError(f"stream {name} lacks a causal alignment method")
        if stream["alignment"] == "asof":
            _positive(stream.get("max_staleness_hours"), f"stream {name} max_staleness_hours")
        stream_map[name] = stream
    native = [s for s in streams if s["alignment"] == "native_grid"]
    if len(native) != 1 or native[0]["name"] != "bars":
        raise ModularAdapterError("exactly one native-grid stream named 'bars' is required")

    if features.get("schema") != FEATURE_SCHEMA:
        raise ModularAdapterError("feature contract schema is unsupported")
    definitions, names = features.get("definitions"), features.get("names")
    if (not isinstance(definitions, list) or not definitions or not isinstance(names, list)
            or [d.get("name") for d in definitions] != names or len(set(names)) != len(names)):
        raise ModularAdapterError("feature definitions and names disagree")
    for definition in definitions:
        kind, params = definition.get("kind"), definition.get("params", {})
        if kind not in FEATURE_KINDS or not isinstance(params, dict):
            raise ModularAdapterError(f"unknown feature kind {kind!r}")
        if kind == "log_volume_z":
            size = params.get("window")
            if isinstance(size, bool) or not isinstance(size, int) or size < 2:
                raise ModularAdapterError("log_volume_z needs an integer window >= 2")
        if kind == "asof_field":
            stream = stream_map.get(params.get("stream"))
            if stream is None or stream["alignment"] != "asof" or not isinstance(params.get("field"), str):
                raise ModularAdapterError("asof_field must name an as-of stream and field")
    scaler = features.get("scaler", {})
    means, scales = scaler.get("mean"), scaler.get("scale")
    if (not isinstance(means, list) or not isinstance(scales, list)
            or not len(means) == len(scales) == len(names)):
        raise ModularAdapterError("scaler dimensions disagree with features")
    for value in means:
        _finite(value, "scaler mean")
    for value in scales:
        _positive(value, "scaler scale")
    if scaler.get("fitted_on") != "train_partition_only":
        raise ModularAdapterError("scaler must be fitted on the training partition only")
    history = features.get("history_bars_required")
    if isinstance(history, bool) or not isinstance(history, int) or history < window + 1:
        raise ModularAdapterError("features.history_bars_required is inconsistent with the window")

    # The time contract binds the model's own configuration: same window, the same
    # ordered features and the same nominal sampling period.
    if (config.get("window") != window or config.get("feature_names") != names
            or config.get("sample_hours") != time_spec.get("sample_hours")):
        raise ModularAdapterError("model configuration disagrees with the time/feature contract")
    outputs = data["outputs"]
    bottleneck = outputs.get("bottleneck", {})
    if (bottleneck.get("rank") != 3
            or bottleneck.get("shape") != [config.get("output_steps"), config.get("output_channels")]):
        raise ModularAdapterError("bottleneck contract must be the rank-three encoder output")
    forecast = outputs.get("forecast", {})
    if (not isinstance(forecast.get("horizons"), list) or not forecast["horizons"]
            or forecast["horizons"] != config.get("horizons")):
        raise ModularAdapterError("forecast horizons disagree with the model configuration")
    _positive(forecast.get("scaled_by"), "outputs.forecast.scaled_by")

    action = data["action"]
    if action.get("schema") != ACTION_SCHEMA or action.get("rule") != "forecast_threshold":
        raise ModularAdapterError("action contract is unsupported")
    long_above = _finite(action.get("long_above"), "action.long_above")
    short_below = _finite(action.get("short_below"), "action.short_below")
    if short_below > long_above or action.get("otherwise") != "hold":
        raise ModularAdapterError("action dead-band is inconsistent")
    for key in ("horizon_index", "target_index"):
        index = action.get(key)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ModularAdapterError(f"action.{key} must be a non-negative integer")
    if action["horizon_index"] >= len(forecast["horizons"]) or action["target_index"] >= config.get("target_count", 1):
        raise ModularAdapterError("action indexes a forecast that does not exist")

    engine, artifact, evidence = data["engine"], data["artifact"], data["evidence"]
    for spec, keys in ((engine, ("path", "sha256")), (artifact, ("file", "sha256", "weights_sha256"))):
        for key in keys:
            if not isinstance(spec.get(key), str) or not spec[key]:
                raise ModularAdapterError(f"contract identity field {key} is missing")
    for flag in ("research_validated", "live_inference_eligible", "live_execution_eligible"):
        if not isinstance(evidence.get(flag), bool):
            raise ModularAdapterError(f"evidence.{flag} must be declared")
    del sample_hours


def load_contract(path: str | Path, *, verify_files: bool = True) -> ModularContract:
    """Parse, validate and (by default) re-derive every referenced file digest."""
    path = Path(os.path.expandvars(str(path))).expanduser()
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ModularAdapterError("modular inference contract is unreadable") from exc
    validate_contract(data)
    contract = ModularContract(path=path.resolve(), sha256=_sha256_bytes(raw), data=data)
    if verify_files:
        checks = [(contract.resolve(data["engine"]["path"]), data["engine"]["sha256"], "engine"),
                  (contract.resolve(data["artifact"]["file"]), data["artifact"]["sha256"], "artifact")]
        evidence = data["evidence"]
        for key in ("metrics", "golden"):
            if evidence.get(f"{key}_file"):
                checks.append((contract.resolve(evidence[f"{key}_file"]),
                               evidence.get(f"{key}_sha256", ""), key))
        for file_path, expected, label in checks:
            if _sha256_file(file_path) != expected:
                raise ModularAdapterError(f"{label} hash mismatch")
    return contract


# ---------------------------------------------------------------- feature side


def causal_align(
    observations: Sequence[Mapping[str, Any]],
    grid: Sequence[datetime],
    *,
    stream: str,
    max_staleness: timedelta,
) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    """As-of join of one stream onto grid right edges, admitting no future sample.

    ``observations`` carry ``available_at`` (when the value could first be known,
    not merely when it was measured) and must be strictly ordered by it.  Each
    grid point takes the last observation with ``available_at <= grid point``;
    a missing or stale observation refuses rather than being filled.
    """
    times = [parse_time(item["available_at"]) for item in observations]
    for earlier, later in zip(times, times[1:]):
        if later <= earlier:
            raise ModularAdapterError(f"stream {stream} is not strictly time ordered")
    grid = [parse_time(point) for point in grid]
    for earlier, later in zip(grid, grid[1:]):
        if later <= earlier:
            raise ModularAdapterError("alignment grid is not strictly increasing")
    chosen, sources = [], []
    for point in grid:
        position = bisect.bisect_right(times, point) - 1
        if position < 0:
            raise ModularAdapterError(f"stream {stream} has no observation at or before {point.isoformat()}")
        if point - times[position] > max_staleness:
            raise ModularAdapterError(f"stream {stream} is stale at {point.isoformat()}")
        chosen.append(observations[position])
        sources.append(times[position].isoformat())
    record = {"schema": ALIGNMENT_SCHEMA, "stream": stream, "method": "asof_last_available",
              "max_staleness_seconds": max_staleness.total_seconds(),
              "grid": [point.isoformat() for point in grid], "source_available_at": sources}
    record["sha256"] = _sha256_bytes(_canonical(record))
    return chosen, record


def normalize_bars(bars: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate completed bars; incomplete or disordered input refuses."""
    result, previous = [], None
    for bar in bars:
        if bar.get("complete") is not True:
            raise ModularAdapterError("incomplete bar cannot enter modular inference")
        stamp = parse_time(bar["time"])
        if previous is not None and stamp <= previous:
            raise ModularAdapterError("bars must be strictly time ordered")
        previous = stamp
        values = {name: _finite(bar[name], name) for name in BAR_FIELDS}
        tolerance = max(abs(values["close"]), 1.0) * 1e-9
        if (values["low"] <= 0 or values["high"] < values["low"] - tolerance
                or not values["low"] - tolerance <= values["close"] <= values["high"] + tolerance
                or values["volume"] < 0):
            raise ModularAdapterError("invalid OHLCV geometry")
        result.append({"time": stamp, **values})
    return result


def _feature_rows(bars: Sequence[Mapping[str, Any]], definitions, aligned) -> list[list[float]]:
    """Unscaled features for the last len(aligned-grid) bars, each from bars[..i] only."""
    count = len(bars)
    rows = []
    first = count - len(next(iter(aligned.values()))) if aligned else None
    for i in range(count):
        row = []
        for definition in definitions:
            kind, params = definition["kind"], definition.get("params", {})
            if kind == "log_return":
                value = math.log(bars[i]["close"] / bars[i - 1]["close"]) if i >= 1 else math.nan
            elif kind == "range_fraction":
                value = (bars[i]["high"] - bars[i]["low"]) / bars[i]["close"]
            elif kind == "log_volume_z":
                size = params["window"]
                if i + 1 < size:
                    value = math.nan
                else:
                    block = [math.log1p(bars[j]["volume"]) for j in range(i - size + 1, i + 1)]
                    mean = sum(block) / size
                    std = math.sqrt(sum((v - mean) ** 2 for v in block) / size)
                    value = (block[-1] - mean) / max(std, 1e-12)
            else:  # asof_field: only defined on the aligned window grid
                values = aligned.get(params["stream"])
                value = (_finite(values[i - first][params["field"]], definition["name"])
                         if values is not None and first is not None and i >= first else math.nan)
            row.append(value)
        rows.append(row)
    return rows


def build_observation(
    contract: ModularContract,
    bars: Sequence[Mapping[str, Any]],
    *,
    as_of: datetime | str | None = None,
    streams: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Assemble one scaled window on the session grid, cut causally at ``as_of``."""
    time_spec, features = contract.data["time"], contract.data["features"]
    offset = timedelta(hours=float(time_spec["bar_close_offset_hours"]))
    normalized = normalize_bars(bars)
    if as_of is not None:
        cutoff = parse_time(as_of)
        normalized = [bar for bar in normalized if bar["time"] + offset <= cutoff]
    history = int(features["history_bars_required"])
    if len(normalized) < history:
        raise ModularAdapterError(f"at least {history} completed bars are required")
    tail = normalized[-history:]
    edges = [bar["time"] + offset for bar in tail]
    max_gap = timedelta(hours=float(time_spec["max_gap_hours"]))
    for earlier, later in zip(edges, edges[1:]):
        if later - earlier > max_gap:
            raise ModularAdapterError(f"gap before {later.isoformat()} exceeds the contract bound")
    window = contract.window
    grid = edges[-window:]
    declared = {s["name"]: s for s in time_spec["streams"]}
    aligned, records = {}, []
    for name, spec in declared.items():
        if spec["alignment"] != "asof":
            continue
        supplied = (streams or {}).get(name)
        if supplied is None:
            raise ModularAdapterError(f"declared stream {name} was not supplied")
        values, record = causal_align(
            supplied, grid, stream=name,
            max_staleness=timedelta(hours=float(spec["max_staleness_hours"])))
        aligned[name], records = values, records + [record]
    for name in (streams or {}):
        if name not in declared or declared[name]["alignment"] != "asof":
            raise ModularAdapterError(f"undeclared stream {name} cannot enter the window")
    rows = _feature_rows(tail, features["definitions"], aligned)[-window:]
    means, scales = features["scaler"]["mean"], features["scaler"]["scale"]
    scaled = []
    for row in rows:
        values = []
        for value, name, mean, scale in zip(row, features["names"], means, scales):
            values.append(_finite((_finite(value, name) - mean) / scale, name))
        scaled.append(values)
    fact = {
        "schema": OBSERVATION_SCHEMA, "feature_contract": FEATURE_SCHEMA,
        "contract_sha256": contract.sha256, "model_id": contract.model_id,
        "last_closed_bar": tail[-1]["time"].isoformat(),
        "grid_right_edges": [edge.isoformat() for edge in grid],
        "feature_names": features["names"], "window": scaled,
        "bars_sha256": _sha256_bytes(_canonical([
            {**bar, "time": bar["time"].isoformat()} for bar in tail])),
        "alignment": records,
    }
    return {**fact, "input_sha256": _sha256_bytes(_canonical(fact))}


# ---------------------------------------------------------------- action side


def map_action(forecast_value: float, action: Mapping[str, Any]) -> str:
    value = _finite(forecast_value, "forecast")
    if value > float(action["long_above"]):
        return "long"
    if value < float(action["short_below"]):
        return "short"
    return "hold"


# ---------------------------------------------------------------- inference side


def _load_engine(contract: ModularContract):
    engine = contract.data["engine"]
    path = contract.resolve(engine["path"])
    if _sha256_file(path) != engine["sha256"]:
        raise ModularAdapterError("engine hash mismatch")
    name = "lts_pinned_modular_temporal_" + engine["sha256"][:16]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ModularAdapterError("engine module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def running_keras_version() -> str:
    import keras

    return str(keras.__version__)


def _major_minor(version: str) -> tuple[str, ...]:
    return tuple(str(version).split(".")[:2])


def check_keras_pin(contract: ModularContract, running: str, *,
                    allow_unpinned_keras: bool = False) -> None:
    """Fail closed before deserialization, as predictor's load_bundle does."""
    recorded = contract.data["engine"].get("keras_version")
    if recorded is None:
        if allow_unpinned_keras:
            return
        raise ModularAdapterError(
            f"contract records no engine.keras_version; running Keras {running}. "
            "Refusing to deserialize (allow_unpinned_keras is off)")
    if not isinstance(recorded, str) or len(_major_minor(recorded)) != 2:
        raise ModularAdapterError(f"contract engine.keras_version {recorded!r} is malformed")
    if _major_minor(recorded) != _major_minor(running):
        raise ModularAdapterError(
            f"archive was exported under Keras {recorded}; running Keras {running}. "
            "A major.minor mismatch is refused before deserialization")


class ModularPolicy:
    """Hash-verified modular model behind the contract; shadow inference only."""

    def __init__(self, contract: ModularContract, model: Any, engine: Any) -> None:
        self.contract = contract
        self.model = model
        self.engine = engine
        self.model_id = contract.model_id
        self.asset_id = contract.asset_id
        self.timeframe = contract.timeframe
        self.artifact_sha256 = contract.data["artifact"]["sha256"]
        self.contract_sha256 = contract.sha256

    @classmethod
    def load(cls, contract_path: str | Path, *, require_cpu: bool = True,
             allow_unpinned_keras: bool = False) -> "ModularPolicy":
        """Load the contract's model; refuse a Keras major.minor different from the export's.

        ``allow_unpinned_keras`` admits a contract that predates the
        ``engine.keras_version`` field (v1-era replays only); it never admits a
        recorded version that disagrees with the running one.
        """
        if require_cpu and os.environ.get("CUDA_VISIBLE_DEVICES") != "":
            raise ModularAdapterError("modular shadow inference must run with CUDA_VISIBLE_DEVICES=''")
        contract = load_contract(contract_path)
        check_keras_pin(contract, running_keras_version(),
                        allow_unpinned_keras=allow_unpinned_keras)
        engine = _load_engine(contract)
        artifact = contract.resolve(contract.data["artifact"]["file"])
        model = engine.keras.models.load_model(artifact, compile=False, safe_mode=True)
        if engine.weights_hash(model) != contract.data["artifact"]["weights_sha256"]:
            raise ModularAdapterError("artifact weight identity mismatch")
        config = contract.data["modular_config"]
        expected_input = (contract.window, len(contract.feature_names))
        expected_outputs = [
            (len(config["horizons"]), int(config.get("target_count", 1))),
            tuple(contract.data["outputs"]["bottleneck"]["shape"]),
        ]
        shapes = [tuple(shape[1:]) for shape in (model.output_shape if isinstance(model.output_shape, list) else [model.output_shape])]
        if tuple(model.input_shape[1:]) != expected_input or shapes != expected_outputs:
            raise ModularAdapterError("model shapes disagree with the contract")
        return cls(contract, model, engine)

    def predict(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        if observation.get("feature_contract") != FEATURE_SCHEMA or observation.get("schema") != OBSERVATION_SCHEMA:
            raise ModularAdapterError("observation feature contract mismatch")
        if observation.get("contract_sha256") != self.contract_sha256:
            raise ModularAdapterError("observation was built for a different contract")
        fact = {key: value for key, value in observation.items() if key != "input_sha256"}
        if _sha256_bytes(_canonical(fact)) != observation.get("input_sha256"):
            raise ModularAdapterError("observation digest mismatch")
        window = observation["window"]
        if (len(window) != self.contract.window
                or any(len(row) != len(self.contract.feature_names) for row in window)):
            raise ModularAdapterError("observation window shape mismatch")
        import numpy as np

        batch = np.asarray([window], dtype=np.float32)
        forecast, bottleneck = self.model([batch], training=False)
        forecast = np.asarray(forecast, dtype=np.float64)[0]
        bottleneck = np.asarray(bottleneck, dtype=np.float32)[0]
        outputs, action = self.contract.data["outputs"], self.contract.data["action"]
        scaled_by = float(outputs["forecast"]["scaled_by"])
        values = (forecast * scaled_by).tolist()
        chosen = values[action["horizon_index"]][action["target_index"]]
        result = {
            "schema": INFERENCE_SCHEMA, "tier": TIER, "execution_authorized": False,
            "model_id": self.model_id, "artifact_sha256": self.artifact_sha256,
            "contract_sha256": self.contract_sha256, "asset_id": self.asset_id,
            "timeframe": self.timeframe, "last_closed_bar": observation["last_closed_bar"],
            "input_sha256": observation["input_sha256"],
            "forecast": {"horizons": outputs["forecast"]["horizons"],
                         "target": outputs["forecast"]["target"], "values": values,
                         "scaled": forecast.tolist()},
            "bottleneck": {"shape": list(bottleneck.shape),
                           "sha256": _sha256_bytes(np.ascontiguousarray(bottleneck).tobytes()),
                           "values": bottleneck.astype(np.float64).tolist()},
            "action": map_action(chosen, action),
            "action_input": chosen,
        }
        result["output_sha256"] = _sha256_bytes(_canonical(result))
        return result
