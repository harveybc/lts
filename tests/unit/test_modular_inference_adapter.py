"""Behaviour tests for the modular inference/feature/action adapter (lane M05).

The TensorFlow section needs the predictor engine file; point
``LTS_MODULAR_ENGINE`` at ``predictor_plugins/modular_temporal.py``.  Without
it that section is skipped with its reason; every other test runs offline.
"""
import copy
import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.modular_inference_adapter import (
    ModularAdapterError, build_observation, causal_align, load_contract, map_action,
    validate_contract,
)

FEATURES = [
    {"name": "log_return", "kind": "log_return", "params": {}},
    {"name": "range_fraction", "kind": "range_fraction", "params": {}},
    {"name": "log_volume_z20", "kind": "log_volume_z", "params": {"window": 20}},
]
WINDOW = 24


def _contract(**overrides):
    config = {
        "window": WINDOW, "sample_hours": 24, "feature_names": [f["name"] for f in FEATURES],
        "output_steps": 6, "output_channels": 8, "horizons": [1], "target_count": 1,
    }
    data = {
        "schema": "lts.modular_inference_contract.v1", "contract_version": 1,
        "model_id": "m05-fixture", "asset_id": "equity:SPY", "timeframe": "1d",
        "engine": {"path": "engine.py", "sha256": "0" * 64},
        "artifact": {"file": "model.keras", "sha256": "0" * 64, "weights_sha256": "0" * 64},
        "modular_config": config,
        "time": {"grid": "venue_session_close", "sample_hours": 24, "window": WINDOW,
                 "bar_label": "session_start", "bar_close_offset_hours": 16, "max_gap_hours": 120,
                 "streams": [{"name": "bars", "frequency": "1d", "alignment": "native_grid"}]},
        "features": {"schema": "lts.modular_features.closed_bars.v1", "definitions": FEATURES,
                     "names": [f["name"] for f in FEATURES], "history_bars_required": 19 + WINDOW,
                     "scaler": {"fitted_on": "train_partition_only", "mean": [0.0, 0.01, 0.0],
                                "scale": [0.01, 0.005, 1.0]}},
        "outputs": {"forecast": {"horizons": [1], "target": "log_return_next_session", "scaled_by": 0.01},
                    "bottleneck": {"shape": [6, 8], "rank": 3}},
        "action": {"schema": "lts.modular_action.v1", "rule": "forecast_threshold", "horizon_index": 0,
                   "target_index": 0, "long_above": 0.001, "short_below": -0.001, "otherwise": "hold"},
        "evidence": {"class": "local_smoke", "research_validated": False,
                     "live_inference_eligible": False, "live_execution_eligible": False},
    }
    for path, value in overrides.items():
        node = data
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return data


def _write_contract(tmp_path, data):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(data))
    return load_contract(path, verify_files=False)


def _bars(count=80, start=datetime(2026, 3, 2, 5, tzinfo=timezone.utc)):
    bars, price, day = [], 100.0, start
    for i in range(count):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        price *= math.exp(0.003 * math.sin(i / 3.0))
        bars.append({"time": day.isoformat(), "open": price * 0.999, "high": price * 1.004,
                     "low": price * 0.995, "close": price, "volume": 1000 + 37 * (i % 11),
                     "complete": True})
        day += timedelta(days=1)
    return bars


# ------------------------------------------------------------ contract side


def test_contract_validates_and_binds_time_to_model_config(tmp_path):
    validate_contract(_contract())
    for path, value in [("modular_config.window", 12), ("modular_config.sample_hours", 1),
                        ("modular_config.feature_names", ["range_fraction", "log_return", "log_volume_z20"])]:
        with pytest.raises(ModularAdapterError, match="disagrees"):
            validate_contract(_contract(**{path: value}))


def test_contract_refuses_missing_time_facts_and_unknown_versions():
    bad = _contract()
    del bad["time"]["bar_close_offset_hours"]
    with pytest.raises(ModularAdapterError, match="bar_close_offset_hours"):
        validate_contract(bad)
    with pytest.raises(ModularAdapterError, match="version"):
        validate_contract(_contract(contract_version=2))
    with pytest.raises(ModularAdapterError, match="schema"):
        validate_contract(_contract(schema="lts.modular_inference_contract.v0"))
    with pytest.raises(ModularAdapterError, match="training partition"):
        validate_contract(_contract(**{"features.scaler.fitted_on": "all_rows"}))
    with pytest.raises(ModularAdapterError, match="rank-three"):
        validate_contract(_contract(**{"outputs.bottleneck.shape": [48]}))


def test_contract_refuses_any_execution_authority_claim():
    with pytest.raises(ModularAdapterError, match="execution authority"):
        validate_contract(_contract(**{"evidence.execution_authorized": True}))
    with pytest.raises(ModularAdapterError, match="execution authority"):
        validate_contract(_contract(**{"action.execution_authorized": "yes"}))


def test_undeclared_alignment_refuses():
    bad = _contract()
    bad["time"]["streams"].append({"name": "quotes", "frequency": "5min", "alignment": "shape_match"})
    with pytest.raises(ModularAdapterError, match="causal alignment"):
        validate_contract(bad)


def test_file_digests_are_re_derived(tmp_path):
    engine, model = tmp_path / "engine.py", tmp_path / "model.keras"
    engine.write_text("# engine\n")
    model.write_bytes(b"model")
    data = _contract(**{"engine.sha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
                        "artifact.sha256": hashlib.sha256(b"model").hexdigest()})
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(data))
    load_contract(path)
    model.write_bytes(b"tampered")
    with pytest.raises(ModularAdapterError, match="artifact hash mismatch"):
        load_contract(path)


# ------------------------------------------------------------ feature side


def test_observation_is_causal_at_as_of(tmp_path):
    contract = _write_contract(tmp_path, _contract())
    bars = _bars()
    as_of = datetime.fromisoformat(bars[60]["time"]) + timedelta(hours=16)
    base = build_observation(contract, bars, as_of=as_of)
    assert base["last_closed_bar"] == bars[60]["time"]
    assert len(base["window"]) == WINDOW and len(base["grid_right_edges"]) == WINDOW
    future = copy.deepcopy(bars)
    for bar in future[61:]:
        bar["close"] *= 3.0
        bar["high"] = bar["close"] * 1.01
        bar["volume"] *= 50
    assert build_observation(contract, future, as_of=as_of)["input_sha256"] == base["input_sha256"]
    inside = copy.deepcopy(bars)
    inside[59]["volume"] *= 4
    assert build_observation(contract, inside, as_of=as_of)["input_sha256"] != base["input_sha256"]
    # One second before the close, bar 60 is not yet complete and cannot enter.
    earlier = build_observation(contract, bars, as_of=as_of - timedelta(seconds=1))
    assert earlier["last_closed_bar"] == bars[59]["time"]


def test_feature_values_follow_their_definitions(tmp_path):
    contract = _write_contract(tmp_path, _contract(**{
        "features.scaler.mean": [0.0, 0.0, 0.0], "features.scaler.scale": [1.0, 1.0, 1.0]}))
    bars = _bars()
    obs = build_observation(contract, bars)
    last, prev = bars[-1], bars[-2]
    assert obs["window"][-1][0] == pytest.approx(math.log(last["close"] / prev["close"]), abs=1e-15)
    assert obs["window"][-1][1] == pytest.approx((last["high"] - last["low"]) / last["close"], abs=1e-15)
    block = [math.log1p(bar["volume"]) for bar in bars[-20:]]
    mean = sum(block) / 20
    std = math.sqrt(sum((v - mean) ** 2 for v in block) / 20)
    assert obs["window"][-1][2] == pytest.approx((block[-1] - mean) / std, abs=1e-12)


def test_bars_refuse_incomplete_disordered_gapped_or_short(tmp_path):
    contract = _write_contract(tmp_path, _contract())
    bars = _bars()
    bad = copy.deepcopy(bars)
    bad[-1]["complete"] = False
    with pytest.raises(ModularAdapterError, match="incomplete"):
        build_observation(contract, bad)
    with pytest.raises(ModularAdapterError, match="ordered"):
        build_observation(contract, bars[:-2] + [bars[-1], bars[-2]])
    gapped = copy.deepcopy(bars)
    for bar in gapped[-5:]:
        bar["time"] = (datetime.fromisoformat(bar["time"]) + timedelta(days=30)).isoformat()
    with pytest.raises(ModularAdapterError, match="gap"):
        build_observation(contract, gapped)
    with pytest.raises(ModularAdapterError, match="at least"):
        build_observation(contract, bars[:30])
    naive = copy.deepcopy(bars)
    naive[-1]["time"] = "2026-06-01T05:00:00"
    with pytest.raises(ModularAdapterError, match="timezone"):
        build_observation(contract, naive)


def test_causal_align_admits_no_future_sample_and_refuses_stale():
    grid = [datetime(2026, 9, 1, h, tzinfo=timezone.utc) for h in (10, 11, 12)]
    quotes = [{"available_at": datetime(2026, 9, 1, 9, 55, tzinfo=timezone.utc), "mid": 1.0},
              {"available_at": datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc), "mid": 2.0},
              {"available_at": datetime(2026, 9, 1, 11, 5, tzinfo=timezone.utc), "mid": 3.0},
              {"available_at": datetime(2026, 9, 1, 12, 0, 1, tzinfo=timezone.utc), "mid": 99.0}]
    values, record = causal_align(quotes, grid, stream="quotes", max_staleness=timedelta(hours=1))
    assert [v["mid"] for v in values] == [1.0, 2.0, 3.0]  # 12:00:01 is future for 12:00
    assert record["schema"] == "lts.causal_alignment.v1" and len(record["sha256"]) == 64
    with pytest.raises(ModularAdapterError, match="stale"):
        causal_align(quotes[:1], grid, stream="quotes", max_staleness=timedelta(minutes=30))
    with pytest.raises(ModularAdapterError, match="no observation"):
        causal_align(quotes[1:], grid, stream="quotes", max_staleness=timedelta(hours=5))
    with pytest.raises(ModularAdapterError, match="ordered"):
        causal_align(list(reversed(quotes)), grid, stream="quotes", max_staleness=timedelta(hours=5))


def _mixed_contract():
    data = _contract()
    data["time"]["streams"].append({"name": "quotes", "frequency": "5min", "alignment": "asof",
                                    "max_staleness_hours": 2})
    data["features"]["definitions"] = FEATURES + [
        {"name": "spread_bps", "kind": "asof_field", "params": {"stream": "quotes", "field": "spread_bps"}}]
    data["features"]["names"] = [f["name"] for f in data["features"]["definitions"]]
    data["features"]["scaler"]["mean"].append(0.0)
    data["features"]["scaler"]["scale"].append(1.0)
    data["modular_config"]["feature_names"] = data["features"]["names"]
    return data


def test_mixed_frequency_stream_needs_its_own_alignment(tmp_path):
    contract = _write_contract(tmp_path, _mixed_contract())
    bars = _bars()
    offset = timedelta(hours=16)
    quotes = []
    for bar in bars:
        close = datetime.fromisoformat(bar["time"]) + offset
        for minutes, spread in ((-10, 2.0), (-5, 3.0), (5, 500.0)):  # the +5 is after the close
            quotes.append({"available_at": close + timedelta(minutes=minutes), "spread_bps": spread})
    obs = build_observation(contract, bars, streams={"quotes": quotes})
    assert [row[3] for row in obs["window"]] == [3.0] * WINDOW
    assert obs["alignment"][0]["stream"] == "quotes"
    with pytest.raises(ModularAdapterError, match="not supplied"):
        build_observation(contract, bars)
    # Equal lengths do not establish aligned times: a stream shifted into the
    # future, one sample per grid point, still refuses rather than lining up.
    shifted = [{"available_at": datetime.fromisoformat(b["time"]) + offset + timedelta(minutes=1),
                "spread_bps": 1.0} for b in bars]
    with pytest.raises(ModularAdapterError):
        build_observation(contract, bars[:-1], streams={"quotes": shifted[1:]})
    plain = _write_contract(tmp_path, _contract())
    with pytest.raises(ModularAdapterError, match="undeclared stream"):
        build_observation(plain, bars, streams={"quotes": quotes})


# ------------------------------------------------------------ action side


def test_action_dead_band():
    action = _contract()["action"]
    assert map_action(0.002, action) == "long"
    assert map_action(-0.002, action) == "short"
    assert map_action(0.001, action) == "hold"
    assert map_action(-0.001, action) == "hold"
    with pytest.raises(ModularAdapterError):
        map_action(float("nan"), action)


# ------------------------------------------------------------ route boundaries


def test_linear_route_refuses_modular_artifacts(tmp_path):
    """Backward compatibility: nothing modular can slip into the linear runner."""
    from prediction_provider_mechanics import LiveLinearPolicy, LiveLinearPolicyError
    from app.live_model_selection import LiveModelSelectionError, SelectedLinearPolicy

    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(_contract()))
    with pytest.raises(LiveModelSelectionError, match="schema"):
        SelectedLinearPolicy(manifest_file=contract_path, expected_asset_id="equity:SPY",
                             expected_timeframe="1d", execution_tier="demo_research_canary")
    contract = load_contract(contract_path, verify_files=False)
    observation = build_observation(contract, _bars())
    policy = LiveLinearPolicy(
        model_id="x", asset_id="equity:SPY", timeframe="1d", feature_names=("a",), means=(0.0,),
        scales=(1.0,), coefficients=(1.0,), intercept=0.0, probability_threshold=0.5,
        artifact_sha256="0" * 64)
    with pytest.raises(LiveLinearPolicyError, match="feature contract"):
        policy.predict(observation)


def test_modular_policy_refuses_linear_observation(tmp_path):
    from prediction_provider_mechanics import build_closed_bar_features
    from app.modular_inference_adapter import ModularPolicy

    contract = _write_contract(tmp_path, _contract())
    policy = ModularPolicy(contract, model=None, engine=None)
    linear = build_closed_bar_features(_bars(60))
    with pytest.raises(ModularAdapterError, match="feature contract"):
        policy.predict(linear)
    observation = build_observation(contract, _bars())
    forged = dict(observation, window=[[0.0] * 3] * WINDOW)
    with pytest.raises(ModularAdapterError, match="digest"):
        policy.predict(forged)


# ------------------------------------------------------------ inference side (TensorFlow)

ENGINE = os.environ.get("LTS_MODULAR_ENGINE")
needs_engine = pytest.mark.skipif(
    not ENGINE or not (Path(ENGINE).is_file() or (Path(ENGINE) / "__init__.py").is_file()),
    reason="LTS_MODULAR_ENGINE does not name predictor's modular_temporal (file or package)")


def _export(tmp_path, *, seed=7, keras_version="running"):
    import importlib.util

    import numpy as np

    from app.modular_inference_adapter import engine_digest, load_engine_module

    engine = load_engine_module(ENGINE, engine_digest(ENGINE))
    engine.keras.utils.set_random_seed(seed)
    config = engine.default_config([f["name"] for f in FEATURES])
    config["sample_hours"] = 24
    config = engine._normalize(config)
    bundle = engine.build_modular(config)
    model = engine.keras.Model(bundle.forecast_model.inputs,
                               [bundle.forecast_model.outputs[0], bundle.encoder_model.outputs[0]])
    path = tmp_path / "model.keras"
    model.save(path)
    data = _contract()
    data["modular_config"] = bundle.config
    from app.modular_inference_adapter import engine_digest

    data["engine"] = {"path": ENGINE, "sha256": engine_digest(ENGINE)}
    if keras_version == "running":
        import keras

        data["engine"]["keras_version"] = keras.__version__
    elif keras_version is not None:
        data["engine"]["keras_version"] = keras_version
    data["artifact"] = {"file": "model.keras", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "weights_sha256": engine.weights_hash(model)}
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(data))
    return contract_path, model, np


@needs_engine
def test_policy_matches_direct_model_and_exports_rank_three_bottleneck(tmp_path, monkeypatch):
    from app.modular_inference_adapter import ModularPolicy

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    contract_path, direct, np = _export(tmp_path)
    policy = ModularPolicy.load(contract_path)
    observation = build_observation(policy.contract, _bars())
    first, second = policy.predict(observation), policy.predict(observation)
    assert first == second
    assert first["execution_authorized"] is False and first["tier"] == "shadow_inference_only"
    assert first["bottleneck"]["shape"] == [6, 8]
    forecast, bottleneck = direct.predict(np.asarray([observation["window"]], dtype=np.float32), verbose=0)
    assert np.allclose(np.asarray(first["forecast"]["scaled"]), forecast[0], atol=1e-6)
    assert np.allclose(np.asarray(first["bottleneck"]["values"]), bottleneck[0], atol=1e-6)
    assert first["action"] == map_action(first["action_input"], policy.contract.data["action"])


@needs_engine
def test_policy_refuses_gpu_env_tampered_weights_and_wrong_engine(tmp_path, monkeypatch):
    from app.modular_inference_adapter import ModularPolicy

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    contract_path, _, _ = _export(tmp_path)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    with pytest.raises(ModularAdapterError, match="CUDA_VISIBLE_DEVICES"):
        ModularPolicy.load(contract_path)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    data = json.loads(contract_path.read_text())
    data["artifact"]["weights_sha256"] = "f" * 64
    contract_path.write_text(json.dumps(data))
    with pytest.raises(ModularAdapterError, match="weight identity"):
        ModularPolicy.load(contract_path)
    data["engine"]["sha256"] = "e" * 64
    contract_path.write_text(json.dumps(data))
    with pytest.raises(ModularAdapterError, match="engine hash"):
        ModularPolicy.load(contract_path)


# ------------------------------------------------------------ Keras pin (fail closed)


def test_keras_pin_match_mismatch_and_missing_without_tensorflow(tmp_path):
    from app.modular_inference_adapter import check_keras_pin

    contract = _write_contract(tmp_path, _contract(**{"engine.keras_version": "3.13.2"}))
    check_keras_pin(contract, "3.13.9")  # same major.minor: admitted
    with pytest.raises(ModularAdapterError, match=r"Keras 3\.13\.2.*running Keras 3\.15\.0"):
        check_keras_pin(contract, "3.15.0")
    bare = _write_contract(tmp_path, _contract())
    with pytest.raises(ModularAdapterError, match="no engine.keras_version.*3.15.0"):
        check_keras_pin(bare, "3.15.0")
    check_keras_pin(bare, "3.15.0", allow_unpinned_keras=True)
    with pytest.raises(ModularAdapterError, match="Keras 3.13.2"):  # the flag never admits a mismatch
        check_keras_pin(contract, "3.15.0", allow_unpinned_keras=True)


@needs_engine
def test_policy_loads_when_keras_major_minor_matches(tmp_path, monkeypatch):
    from app.modular_inference_adapter import ModularPolicy

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    contract_path, _, _ = _export(tmp_path)
    assert ModularPolicy.load(contract_path).model is not None


@needs_engine
def test_policy_refuses_keras_mismatch_before_deserialization(tmp_path, monkeypatch):
    import keras
    from app import modular_inference_adapter as adapter

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    contract_path, _, _ = _export(tmp_path, keras_version="2.99.0")
    called = []
    monkeypatch.setattr(adapter, "_load_engine", lambda c: called.append(c))
    with pytest.raises(ModularAdapterError) as error:
        adapter.ModularPolicy.load(contract_path)
    assert "2.99.0" in str(error.value) and keras.__version__ in str(error.value)
    assert called == []  # refused before the engine or archive was touched


@needs_engine
def test_policy_refuses_missing_keras_field_unless_explicitly_allowed(tmp_path, monkeypatch):
    from app.modular_inference_adapter import ModularPolicy

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    contract_path, _, _ = _export(tmp_path, keras_version=None)
    with pytest.raises(ModularAdapterError, match="allow_unpinned_keras is off"):
        ModularPolicy.load(contract_path)
    assert ModularPolicy.load(contract_path, allow_unpinned_keras=True).model is not None


def test_package_engine_digest_covers_every_module(tmp_path):
    from app.modular_inference_adapter import engine_digest

    package = tmp_path / "engine"
    (package / "__pycache__").mkdir(parents=True)
    (package / "__init__.py").write_text("from .core import x\n")
    (package / "core.py").write_text("x = 1\n")
    (package / "__pycache__" / "core.cpython-312.pyc").write_bytes(b"ignored")
    first = engine_digest(package)
    (package / "__pycache__" / "core.cpython-312.pyc").write_bytes(b"still ignored")
    assert engine_digest(package) == first
    (package / "core.py").write_text("x = 2\n")
    assert engine_digest(package) != first
    (package / "__init__.py").unlink()
    with pytest.raises(ModularAdapterError, match="__init__"):
        engine_digest(package)


# ------------------------------------------------------------ conditioning contract (M01 provenance v1)


@pytest.mark.parametrize("declared, operational_ok", [
    (None, False), ("UNKNOWN", False), ("SYNTHETIC_OFFLINE", False), ("OPERATIONAL", True)])
def test_operational_requirement_refuses_unknown_and_synthetic(tmp_path, declared, operational_ok):
    from app.modular_inference_adapter import check_conditioning

    data = _contract()
    if declared is not None:
        data["provenance"] = {"provenance_schema": "predictor.modular.provenance.v1",
                              "conditioning_contract": declared}
    contract = _write_contract(tmp_path, data)
    assert check_conditioning(contract, None) == (declared or "UNKNOWN")  # shadow records it
    if operational_ok:
        assert check_conditioning(contract, "OPERATIONAL") == "OPERATIONAL"
    else:
        with pytest.raises(ModularAdapterError, match="CONDITIONING_CONTRACT_NOT_OPERATIONAL"):
            check_conditioning(contract, "OPERATIONAL")


def test_unrecognised_conditioning_value_is_refused(tmp_path):
    from app.modular_inference_adapter import check_conditioning

    data = _contract()
    data["provenance"] = {"conditioning_contract": "OPERATIONALISH"}
    with pytest.raises(ModularAdapterError, match="not recognised"):
        check_conditioning(_write_contract(tmp_path, data), None)


@needs_engine
def test_policy_load_refuses_non_operational_before_the_engine_is_touched(tmp_path, monkeypatch):
    from app import modular_inference_adapter as adapter

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    contract_path, _, _ = _export(tmp_path)
    touched = []
    monkeypatch.setattr(adapter, "_load_engine", lambda c: touched.append(c))
    with pytest.raises(ModularAdapterError, match="CONDITIONING_CONTRACT_NOT_OPERATIONAL"):
        adapter.ModularPolicy.load(contract_path, require_conditioning="OPERATIONAL")
    assert touched == []
