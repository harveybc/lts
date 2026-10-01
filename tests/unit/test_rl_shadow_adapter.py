"""RL shadow adapter for lane G's rl_temporal.policy_bundle.v1 (agent-multi 02db0701).

Intake is checked field by field and a bundle lacking any intake field is refused
BY NAME. Decisions are read-only: shadow tier, execution_authorized false, zero
orders. The SB3 section builds a real tiny DQN/SAC bundle when stable_baselines3 is
installed and skips with its reason otherwise.
"""
import copy
import hashlib
import json

import pytest

from app.rl_shadow_adapter import (
    BUNDLE_SCHEMA, RLBundleRefused, check_versions, map_rl_action, validate_bundle,
)


def _doc(zip_sha, **over):
    doc = {
        "schema": BUNDLE_SCHEMA, "arm": "dqn_modular_temporal", "algorithm": "DQN",
        "policy_zip_sha256": zip_sha,
        "action_mapping": {"action_space_mode": "discrete", "space": "Discrete(3)",
                           "mapping": {"0": "hold", "1": "long", "2": "short"}},
        "normalization": {"feature_scaling": "zscore", "feature_scaling_window": 256, "feature_clip": 5.0,
                          "include_price_window": True, "include_agent_state": True,
                          "agent_state_contract": "v1", "window_size": 24, "window": 24,
                          "feature_columns": ["close", "volume"], "feature_binary_columns": [],
                          "preprocessor_plugin": "default", "bar_period": "4h", "other_frequency_streams": []},
        "representation": {"kind": "modular_temporal", "layout_digest": "l" * 64, "layout": {"feature_order": []},
                           "modular_config_sha256": "m" * 64, "regimes": {}, "donor": None, "identity": {},
                           "parameters": {}, "engine_pin": "3ecdb256"},
        "versions": {"stable_baselines3": "2.9.0", "torch": "2.13.0+cu130", "gymnasium": "1.3.0"},
        "tolerance": {"device": "cpu", "abs": 1e-6}, "num_timesteps": 100,
        "execution_authorized": False,
        "evidence": {"research_validated": False, "live_inference_eligible": False,
                     "live_execution_eligible": False},
    }
    for path, value in over.items():
        node = doc
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[key]
        if value is _DELETE:
            del node[keys[-1]]
        else:
            node[keys[-1]] = value
    return doc


_DELETE = object()


def _bundle(tmp_path, **over):
    d = tmp_path / "bundle"
    d.mkdir(exist_ok=True)
    (d / "policy.zip").write_bytes(b"zip bytes")
    sha = hashlib.sha256(b"zip bytes").hexdigest()
    (d / "bundle.json").write_text(json.dumps(_doc(sha, **over)))
    (d / "env_config.json").write_text(json.dumps({"window_size": 24}))
    return d


def test_complete_bundle_is_accepted(tmp_path):
    bundle = validate_bundle(_bundle(tmp_path))
    assert bundle.algorithm == "DQN" and bundle.arm == "dqn_modular_temporal"
    assert bundle.tier == "shadow_inference_only" and bundle.execution_authorized is False


@pytest.mark.parametrize("field", [
    "schema", "arm", "algorithm", "policy_zip_sha256", "action_mapping", "normalization", "representation",
    "versions", "execution_authorized", "evidence",
    "evidence.research_validated", "evidence.live_inference_eligible", "evidence.live_execution_eligible",
    "normalization.window", "normalization.feature_columns", "normalization.bar_period",
    "normalization.other_frequency_streams", "representation.kind", "representation.layout_digest",
    "representation.engine_pin", "versions.stable_baselines3", "versions.torch", "versions.gymnasium",
    "action_mapping.action_space_mode",
])
def test_bundle_lacking_any_intake_field_is_refused_by_name(tmp_path, field):
    with pytest.raises(RLBundleRefused) as error:
        validate_bundle(_bundle(tmp_path, **{field: _DELETE}))
    assert field in str(error.value)


@pytest.mark.parametrize("over, needle", [
    ({"execution_authorized": True}, "execution_authorized"),
    ({"representation.identity": {"execution_authorized": "yes"}}, "execution_authorized"),
    ({"normalization.bar_period": "UNDECLARED"}, "bar_period"),
    ({"normalization.other_frequency_streams": [{"name": "funding", "frequency": "8h"}]}, "alignment"),
    ({"action_mapping.mapping": {"0": "hold", "1": "short", "2": "long"}}, "mapping"),
    ({"action_mapping.action_space_mode": "multi_discrete"}, "action_space_mode"),
    ({"algorithm": "PPO"}, "algorithm"),
    ({"schema": "rl_temporal.policy_bundle.v0"}, "schema"),
    ({"evidence.live_execution_eligible": "no"}, "evidence.live_execution_eligible"),
    ({"representation.kind": "modular_temporal", "representation.layout_digest": None}, "layout_digest"),
])
def test_bad_intake_values_are_refused_by_name(tmp_path, over, needle):
    with pytest.raises(RLBundleRefused, match=needle):
        validate_bundle(_bundle(tmp_path, **over))


def test_native_flat_arm_needs_no_layout_or_engine_pin(tmp_path):
    bundle = validate_bundle(_bundle(tmp_path, **{"representation.kind": "native_flat",
                                                  "representation.layout_digest": None,
                                                  "representation.engine_pin": _DELETE}))
    assert bundle.representation_kind == "native_flat"


def test_policy_zip_tampering_is_refused(tmp_path):
    d = _bundle(tmp_path)
    (d / "policy.zip").write_bytes(b"other bytes")
    with pytest.raises(RLBundleRefused, match="policy_zip_sha256"):
        validate_bundle(d)


def test_continuous_mapping_uses_the_declared_threshold(tmp_path):
    mapping = {"action_space_mode": "continuous", "space": "Box(-1, 1, (1,))",
               "continuous_action_threshold": 0.33, "continuous_action_contract": "legacy_directional_v1",
               "effective_actions": {">= +thr": "long", "<= -thr": "short", "else": "hold"}}
    bundle = validate_bundle(_bundle(tmp_path, action_mapping=mapping, algorithm="SAC"))
    assert [map_rl_action(v, bundle.action_mapping) for v in (0.5, 0.33, 0.1, -0.33, -0.9)] == \
        ["long", "long", "hold", "short", "short"]
    with pytest.raises(RLBundleRefused, match="continuous_action_threshold"):
        validate_bundle(_bundle(tmp_path, action_mapping=dict(mapping, continuous_action_threshold=1.5)))


def test_discrete_mapping_and_unknown_actions():
    mapping = {"action_space_mode": "discrete", "mapping": {"0": "hold", "1": "long", "2": "short"}}
    assert [map_rl_action(a, mapping) for a in (0, 1, 2, 1.0)] == ["hold", "long", "short", "long"]
    with pytest.raises(RLBundleRefused):
        map_rl_action(3, mapping)


def test_version_pin_is_major_minor(tmp_path):
    bundle = validate_bundle(_bundle(tmp_path))
    check_versions(bundle, {"stable_baselines3": "2.9.7", "torch": "2.13.1", "gymnasium": "1.3.2"})
    with pytest.raises(RLBundleRefused, match="torch"):
        check_versions(bundle, {"stable_baselines3": "2.9.0", "torch": "2.11.0", "gymnasium": "1.3.0"})
    with pytest.raises(RLBundleRefused, match="gymnasium"):
        check_versions(bundle, {"stable_baselines3": "2.9.0", "torch": "2.13.0"})


# ------------------------------------------------------------ real SB3 shadow decisions


sb3 = pytest.importorskip("stable_baselines3", reason="stable_baselines3 is not installed in this environment")


def _real_bundle(tmp_path, algorithm):
    import gymnasium as gym
    import numpy as np
    import stable_baselines3
    import torch

    class Toy(gym.Env):
        def __init__(self, continuous):
            self.observation_space = gym.spaces.Box(-1, 1, (4,), dtype=np.float32)
            self.action_space = (gym.spaces.Box(-1, 1, (1,), dtype=np.float32) if continuous
                                 else gym.spaces.Discrete(3))

        def reset(self, *, seed=None, options=None):
            return np.zeros(4, dtype=np.float32), {}

        def step(self, action):
            return np.zeros(4, dtype=np.float32), 0.0, True, False, {}

    continuous = algorithm == "SAC"
    algo = getattr(stable_baselines3, algorithm)
    model = algo("MlpPolicy", Toy(continuous), seed=7, device="cpu",
                 **({"learning_starts": 0} if algorithm == "SAC" else {}))
    d = tmp_path / algorithm
    d.mkdir()
    model.save(d / "policy.zip")
    import gymnasium
    versions = {"stable_baselines3": stable_baselines3.__version__, "torch": torch.__version__,
                "gymnasium": gymnasium.__version__}
    mapping = ({"action_space_mode": "continuous", "space": "Box(-1, 1, (1,))", "continuous_action_threshold": 0.33,
                "continuous_action_contract": "legacy_directional_v1",
                "effective_actions": {">= +thr": "long", "<= -thr": "short", "else": "hold"}}
               if continuous else {"action_space_mode": "discrete", "space": "Discrete(3)",
                                   "mapping": {"0": "hold", "1": "long", "2": "short"}})
    doc = _doc(hashlib.sha256((d / "policy.zip").read_bytes()).hexdigest(), algorithm=algorithm,
               action_mapping=mapping, versions=versions,
               **{"representation.kind": "native_flat", "representation.layout_digest": None})
    (d / "bundle.json").write_text(json.dumps(doc))
    (d / "env_config.json").write_text(json.dumps({}))
    return d, model, np


@pytest.mark.parametrize("algorithm", ["DQN", "SAC"])
def test_real_sb3_bundle_gives_read_only_shadow_decisions(tmp_path, algorithm):
    from app.rl_shadow_adapter import RLShadowPolicy

    d, model, np = _real_bundle(tmp_path, algorithm)
    policy = RLShadowPolicy.load(d)
    observations = np.random.default_rng(0).uniform(-1, 1, (5, 4)).astype("float32")
    first = [policy.decide(obs, bar=f"b{i}") for i, obs in enumerate(observations)]
    again = [policy.decide(obs, bar=f"b{i}") for i, obs in enumerate(observations)]
    assert [d["output_sha256"] for d in first] == [d["output_sha256"] for d in again]
    for decision, obs in zip(first, observations):
        raw, _ = model.predict(obs, deterministic=True)
        assert decision["raw_action"] == pytest.approx(np.asarray(raw, dtype=float).reshape(-1).tolist())
        assert decision["execution_authorized"] is False and decision["orders_submitted"] == 0
        assert decision["tier"] == "shadow_inference_only"
        assert decision["action"] in {"long", "short", "hold"}
    assert not hasattr(policy, "submit") and not hasattr(policy, "execute")


def test_real_bundle_refuses_a_version_mismatch_before_loading(tmp_path, monkeypatch):
    from app import rl_shadow_adapter
    from app.rl_shadow_adapter import RLShadowPolicy

    d, _, _ = _real_bundle(tmp_path, "DQN")
    monkeypatch.setattr(rl_shadow_adapter, "running_versions",
                        lambda: {"stable_baselines3": "1.0.0", "torch": "1.0", "gymnasium": "0.1"})
    with pytest.raises(RLBundleRefused, match="stable_baselines3"):
        RLShadowPolicy.load(d)
