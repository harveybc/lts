"""Shadow intake for lane G's RL policy bundles (rl_temporal.policy_bundle.v1).

Lane G (agent-multi ``rl_temporal/checkpoint.py``, 02db0701) saves a directory with
``policy.zip`` (SB3 SAC or DQN), ``bundle.json`` and ``env_config.json``. This module:

* validates the bundle field by field and refuses a missing or bad intake field BY
  NAME. It covers the schema, arm, algorithm, the zip digest, the action mapping,
  normalization (window, feature columns, a declared bar period, and declared
  alignment for any other-frequency stream), the representation (with the layout
  digest and engine pin for the modular arm), versions, ``execution_authorized``
  (present and false; no true claim anywhere) and the three evidence flags;
* refuses a stable_baselines3/torch/gymnasium major.minor mismatch before loading;
* maps raw actions with the bundle's own declared mapping;
* produces read-only shadow decisions. There is no order path, no broker client and
  no runner wiring: every decision is ``tier: shadow_inference_only``,
  ``execution_authorized: false`` and ``orders_submitted: 0``.

The forecast-versus-naive gate does not apply to RL policy admission (owner order
b327b771, section 6).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

BUNDLE_SCHEMA = "rl_temporal.policy_bundle.v1"
DECISION_SCHEMA = "lts.rl_shadow_decision.v1"
TIER = "shadow_inference_only"
DISCRETE_MAPPING = {"0": "hold", "1": "long", "2": "short"}
ALGORITHMS = ("SAC", "DQN")
VERSION_KEYS = ("stable_baselines3", "torch", "gymnasium")


class RLBundleRefused(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _require(doc: Mapping[str, Any], path: str) -> Any:
    node: Any = doc
    for key in path.split("."):
        if not isinstance(node, Mapping) or key not in node:
            raise RLBundleRefused(f"bundle lacks intake field {path}")
        node = node[key]
    return node


def _walk_execution(value: Any, trail: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "execution_authorized" and item is not False:
                raise RLBundleRefused(f"bundle claims execution_authorized at {trail}{key}")
            _walk_execution(item, f"{trail}{key}.")
    elif isinstance(value, list):
        for item in value:
            _walk_execution(item, trail)


def _major_minor(version: Any) -> tuple:
    return tuple(str(version).split("+")[0].split(".")[:2])


@dataclass(frozen=True)
class RLBundle:
    directory: Path
    doc: dict
    bundle_sha256: str

    @property
    def algorithm(self) -> str:
        return self.doc["algorithm"]

    @property
    def arm(self) -> str:
        return self.doc["arm"]

    @property
    def action_mapping(self) -> dict:
        return self.doc["action_mapping"]

    @property
    def representation_kind(self) -> str:
        return self.doc["representation"]["kind"]

    @property
    def tier(self) -> str:
        return TIER

    @property
    def execution_authorized(self) -> bool:
        return False


def _validate_mapping(mapping: Mapping[str, Any]) -> None:
    mode = _require({"action_mapping": mapping}, "action_mapping.action_space_mode")
    if mode == "discrete":
        if _require({"action_mapping": mapping}, "action_mapping.mapping") != DISCRETE_MAPPING:
            raise RLBundleRefused("action_mapping.mapping must be exactly {0: hold, 1: long, 2: short}")
    elif mode == "continuous":
        threshold = _require({"action_mapping": mapping}, "action_mapping.continuous_action_threshold")
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not math.isfinite(threshold) or not 0 < threshold < 1):
            raise RLBundleRefused("action_mapping.continuous_action_threshold must be in (0, 1)")
        _require({"action_mapping": mapping}, "action_mapping.continuous_action_contract")
    else:
        raise RLBundleRefused(f"action_mapping.action_space_mode {mode!r} is not discrete or continuous")


def validate_bundle(directory: str | Path) -> RLBundle:
    """Every intake field present and admissible, or RLBundleRefused naming it."""
    d = Path(directory)
    try:
        raw = (d / "bundle.json").read_bytes()
        doc = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise RLBundleRefused("bundle.json is unreadable") from exc
    if not isinstance(doc, dict):
        raise RLBundleRefused("bundle.json root must be an object")
    for path in ("schema", "arm", "algorithm", "policy_zip_sha256", "action_mapping", "normalization",
                 "representation", "versions", "execution_authorized", "evidence",
                 "evidence.research_validated", "evidence.live_inference_eligible",
                 "evidence.live_execution_eligible", "normalization.window", "normalization.feature_columns",
                 "normalization.bar_period", "normalization.other_frequency_streams", "representation.kind",
                 "versions.stable_baselines3", "versions.torch", "versions.gymnasium",
                 "action_mapping.action_space_mode"):
        _require(doc, path)
    if doc["schema"] != BUNDLE_SCHEMA:
        raise RLBundleRefused(f"schema {doc['schema']!r} is not {BUNDLE_SCHEMA}")
    if doc["algorithm"] not in ALGORITHMS:
        raise RLBundleRefused(f"algorithm {doc['algorithm']!r} is not one of {ALGORITHMS}")
    if doc["execution_authorized"] is not False:
        raise RLBundleRefused("execution_authorized must be explicitly false")
    _walk_execution(doc)
    for flag in ("research_validated", "live_inference_eligible", "live_execution_eligible"):
        if not isinstance(doc["evidence"][flag], bool):
            raise RLBundleRefused(f"evidence.{flag} must be a boolean")
    _validate_mapping(doc["action_mapping"])
    norm = doc["normalization"]
    if isinstance(norm["window"], bool) or not isinstance(norm["window"], int) or norm["window"] < 1:
        raise RLBundleRefused("normalization.window must be a positive integer")
    if not isinstance(norm["feature_columns"], list):
        raise RLBundleRefused("normalization.feature_columns must be a list")
    if not isinstance(norm["bar_period"], str) or not norm["bar_period"] or norm["bar_period"] == "UNDECLARED":
        raise RLBundleRefused("normalization.bar_period is UNDECLARED")
    streams = norm["other_frequency_streams"]
    if not isinstance(streams, list):
        raise RLBundleRefused("normalization.other_frequency_streams must be a list")
    for stream in streams:
        if not isinstance(stream, Mapping) or stream.get("alignment") not in ("asof",) \
                or not stream.get("max_staleness_hours"):
            raise RLBundleRefused("normalization.other_frequency_streams entries need a causal alignment "
                                  "(alignment: asof with max_staleness_hours)")
    rep = doc["representation"]
    if rep["kind"] not in ("native_flat", "modular_temporal"):
        raise RLBundleRefused(f"representation.kind {rep['kind']!r} is unknown")
    if rep["kind"] == "modular_temporal":
        if not rep.get("layout_digest"):
            raise RLBundleRefused("representation.layout_digest is required for modular_temporal")
        if not _require(doc, "representation.engine_pin"):
            raise RLBundleRefused("representation.engine_pin is required for modular_temporal")
    elif "layout_digest" not in rep:
        raise RLBundleRefused("bundle lacks intake field representation.layout_digest")
    expected = doc["policy_zip_sha256"]
    if not isinstance(expected, str) or len(expected) != 64:
        raise RLBundleRefused("policy_zip_sha256 is malformed")
    try:
        actual = hashlib.sha256((d / "policy.zip").read_bytes()).hexdigest()
    except OSError as exc:
        raise RLBundleRefused("policy.zip is unreadable") from exc
    if actual != expected:
        raise RLBundleRefused("policy_zip_sha256 does not match policy.zip")
    return RLBundle(directory=d, doc=doc, bundle_sha256=hashlib.sha256(raw).hexdigest())


def running_versions() -> dict[str, str]:
    out = {}
    for key, module in (("stable_baselines3", "stable_baselines3"), ("torch", "torch"), ("gymnasium", "gymnasium")):
        try:
            out[key] = __import__(module).__version__
        except ImportError:
            pass
    return out


def check_versions(bundle: RLBundle, running: Mapping[str, str]) -> None:
    for key in VERSION_KEYS:
        if key not in running:
            raise RLBundleRefused(f"versions.{key}: not installed here; bundle needs {bundle.doc['versions'][key]}")
        if _major_minor(bundle.doc["versions"][key]) != _major_minor(running[key]):
            raise RLBundleRefused(f"versions.{key}: bundle {bundle.doc['versions'][key]} vs running {running[key]} "
                                  "(major.minor mismatch refused before loading)")


def map_rl_action(raw: Any, mapping: Mapping[str, Any]) -> str:
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    if len(values) != 1:
        raise RLBundleRefused("expected one action value")
    value = float(values[0])
    if not math.isfinite(value):
        raise RLBundleRefused("non-finite action")
    if mapping.get("action_space_mode") == "discrete":
        key = str(int(value)) if value == int(value) else None
        if key not in DISCRETE_MAPPING:
            raise RLBundleRefused(f"discrete action {raw!r} has no declared mapping")
        return DISCRETE_MAPPING[key]
    threshold = float(mapping["continuous_action_threshold"])
    if value >= threshold:
        return "long"
    if value <= -threshold:
        return "short"
    return "hold"


class RLShadowPolicy:
    """Read-only decisions from a validated bundle; there is no execution path."""

    def __init__(self, bundle: RLBundle, model: Any) -> None:
        self.bundle = bundle
        self._model = model

    @classmethod
    def load(cls, directory: str | Path, *, device: str = "cpu") -> "RLShadowPolicy":
        bundle = validate_bundle(directory)
        check_versions(bundle, running_versions())
        import stable_baselines3

        algo = getattr(stable_baselines3, bundle.algorithm)
        model = algo.load(bundle.directory / "policy.zip", device=device)
        return cls(bundle, model)

    def decide(self, observation: Any, *, bar: str) -> dict[str, Any]:
        import numpy as np

        obs = np.asarray(observation, dtype=np.float32)
        raw, _ = self._model.predict(obs, deterministic=True)
        values = [float(v) for v in np.asarray(raw, dtype=float).reshape(-1)]
        decision = {
            "schema": DECISION_SCHEMA, "tier": TIER, "execution_authorized": False, "orders_submitted": 0,
            "bar": bar, "arm": self.bundle.arm, "algorithm": self.bundle.algorithm,
            "bundle_sha256": self.bundle.bundle_sha256,
            "policy_zip_sha256": self.bundle.doc["policy_zip_sha256"],
            "input_sha256": hashlib.sha256(np.ascontiguousarray(obs).tobytes()).hexdigest(),
            "raw_action": values, "action": map_rl_action(values, self.bundle.action_mapping),
        }
        decision["output_sha256"] = hashlib.sha256(_canonical(decision)).hexdigest()
        return decision
