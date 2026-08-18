"""Hash-verified, manifest-gated SB3 SAC policy for Paper/Demo routes.

SAC analog of :mod:`app.live_model_selection` (ETH order WP-F). The
manifest is an atomic pointer to a trained stable-baselines3 SAC
artifact; every load re-verifies the artifact and config hashes, the
route identity and the execution-tier eligibility flags. The policy maps
the deterministic continuous action to long/short/hold with the SAME
threshold semantics as the training simulator (gym-fx `_coerce_action`),
and hashes every observation so each decision is traceable to its exact
input.

The observation is the TRAINING observation contract (gym-fx feature
window), not raw bars: callers must provide a vector whose provenance
matches the manifest's observation_contract. Golden parity against the
training pipeline (tests/data/sac_golden_parity_eth_v2.json) is the
gate — an adapter that cannot reproduce the training actions bit-exactly
must never hold venue authority.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "prediction_provider.live_sac_manifest.v1"


class LiveSacSelectionError(RuntimeError):
    pass


def _path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(_path(path).read_bytes()).hexdigest()


class LiveSacPolicy:
    """One verified SAC artifact with deterministic inference."""

    def __init__(
        self,
        *,
        model: Any,
        model_id: str,
        asset_id: str,
        timeframe: str,
        artifact_sha256: str,
        continuous_action_threshold: float,
        continuous_action_contract: str = "legacy_directional_v1",
        continuous_exit_threshold: float = 0.0,
    ) -> None:
        self._model = model
        self.model_id = model_id
        self.asset_id = asset_id
        self.timeframe = timeframe
        self.artifact_sha256 = artifact_sha256
        self.continuous_action_threshold = float(continuous_action_threshold)
        self.continuous_action_contract = str(continuous_action_contract)
        self.continuous_exit_threshold = float(continuous_exit_threshold)
        if not 0.0 < self.continuous_action_threshold < 1.0:
            raise LiveSacSelectionError(
                "continuous_action_threshold must be in (0, 1)")
        if self.continuous_action_contract not in (
            "legacy_directional_v1", "target_exposure_hysteresis_v2"
        ):
            raise LiveSacSelectionError("unknown continuous action contract")
        if (
            self.continuous_exit_threshold < 0.0
            or (
                self.continuous_action_contract
                == "target_exposure_hysteresis_v2"
                and self.continuous_exit_threshold
                >= self.continuous_action_threshold
            )
        ):
            raise LiveSacSelectionError(
                "continuous_exit_threshold must be non-negative and below "
                "continuous_action_threshold for the v2 contract"
            )

    @classmethod
    def load(
        cls,
        artifact_path: str | Path,
        expected_sha256: str,
        *,
        model_id: str,
        asset_id: str,
        timeframe: str,
        continuous_action_threshold: float,
        continuous_action_contract: str = "legacy_directional_v1",
        continuous_exit_threshold: float = 0.0,
    ) -> "LiveSacPolicy":
        actual = _sha256(artifact_path)
        if actual != expected_sha256:
            raise LiveSacSelectionError("SAC artifact hash mismatch")
        try:
            from stable_baselines3 import SAC
        except ImportError as exc:  # pragma: no cover - env misconfiguration
            raise LiveSacSelectionError(
                "stable-baselines3 is required for SAC live inference"
            ) from exc
        model = SAC.load(str(_path(artifact_path)), device="cpu")
        return cls(
            model=model, model_id=model_id, asset_id=asset_id,
            timeframe=timeframe, artifact_sha256=actual,
            continuous_action_threshold=continuous_action_threshold,
            continuous_action_contract=continuous_action_contract,
            continuous_exit_threshold=continuous_exit_threshold,
        )

    @property
    def observation_space(self) -> Any:
        return self._model.observation_space

    def predict(
        self,
        observation: Any,
        *,
        current_exposure: float = 0.0,
        pending_entry: bool = False,
    ) -> dict[str, Any]:
        """Deterministic action for one observation, with input hashing."""
        import numpy as np

        array = np.asarray(observation, dtype=np.float32)
        if array.shape != tuple(self._model.observation_space.shape):
            raise LiveSacSelectionError(
                f"observation shape {array.shape} does not match the"
                f" model contract"
                f" {tuple(self._model.observation_space.shape)}")
        if not np.isfinite(array).all():
            raise LiveSacSelectionError(
                "observation contains non-finite values")
        raw, _state = self._model.predict(array, deterministic=True)
        value = float(np.asarray(raw).reshape(-1)[0])
        threshold = self.continuous_action_threshold
        if value >= threshold:
            action = "long"
        elif value <= -threshold:
            action = "short"
        elif (
            self.continuous_action_contract
            == "target_exposure_hysteresis_v2"
            and abs(value) <= self.continuous_exit_threshold
            and (abs(float(current_exposure)) > 1e-12 or pending_entry)
        ):
            action = "close"
        else:
            action = "hold"
        return {
            "action": action,
            "raw_action": value,
            "continuous_action_threshold": threshold,
            "continuous_exit_threshold": self.continuous_exit_threshold,
            "continuous_action_contract": self.continuous_action_contract,
            "input_sha256": hashlib.sha256(array.tobytes()).hexdigest(),
        }


class SelectedSacPolicy:
    """Treat a manifest as an atomic pointer to the selected SAC champion."""

    def __init__(
        self,
        *,
        manifest_file: str | Path,
        expected_asset_id: str,
        expected_timeframe: str,
        execution_tier: str,
    ) -> None:
        self.manifest_file = _path(manifest_file)
        self.expected_asset_id = expected_asset_id
        self.expected_timeframe = expected_timeframe
        self.execution_tier = execution_tier
        self.manifest_sha256 = ""
        self.manifest: dict[str, Any] = {}
        self.policy: LiveSacPolicy
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> bool:
        try:
            manifest_bytes = self.manifest_file.read_bytes()
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            if not force and manifest_sha256 == self.manifest_sha256:
                return False
            manifest = json.loads(manifest_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveSacSelectionError(
                "selected SAC manifest is unreadable") from exc
        if manifest.get("schema") != MANIFEST_SCHEMA:
            raise LiveSacSelectionError(
                "selected SAC manifest schema is unsupported")
        if manifest.get("asset_id") != self.expected_asset_id:
            raise LiveSacSelectionError(
                "selected SAC model asset does not match the route")
        if manifest.get("timeframe") != self.expected_timeframe:
            raise LiveSacSelectionError(
                "selected SAC model timeframe does not match the route")
        if self.execution_tier == "demo_research_canary":
            if manifest.get("research_validated") is not True:
                raise LiveSacSelectionError(
                    "Demo research SAC model lacks validation evidence")
        elif self.execution_tier == "promoted_paper":
            if (
                manifest.get("live_inference_eligible") is not True
                or manifest.get("live_execution_eligible") is not True
            ):
                raise LiveSacSelectionError(
                    "SAC model is not promoted for Paper execution")
        else:
            raise LiveSacSelectionError("unknown model execution tier")
        contract = manifest.get("observation_contract")
        if not isinstance(contract, dict) or not contract.get("source"):
            raise LiveSacSelectionError(
                "SAC manifest lacks an observation_contract — live"
                " observation provenance must be declared")
        if manifest.get("observation_parity_verified") is not True:
            raise LiveSacSelectionError(
                "SAC manifest lacks golden observation/action parity"
                " evidence")
        artifact_path = _path(manifest.get("artifact_file", ""))
        config_path = _path(manifest.get("config_file", ""))
        if _sha256(config_path) != manifest.get("config_sha256"):
            raise LiveSacSelectionError(
                "selected SAC config hash mismatch")
        policy = LiveSacPolicy.load(
            artifact_path, str(manifest.get("artifact_sha256") or ""),
            model_id=str(manifest.get("model_id") or ""),
            asset_id=self.expected_asset_id,
            timeframe=self.expected_timeframe,
            continuous_action_threshold=float(
                contract.get("continuous_action_threshold", 0.0) or 0.0),
            continuous_action_contract=str(
                contract.get(
                    "continuous_action_contract", "legacy_directional_v1"
                )
            ),
            continuous_exit_threshold=float(
                contract.get("continuous_exit_threshold", 0.0) or 0.0
            ),
        )
        if policy.model_id != manifest.get("model_id"):
            raise LiveSacSelectionError(
                "manifest and SAC artifact identities disagree")
        self.manifest = manifest
        self.policy = policy
        self.manifest_sha256 = manifest_sha256
        return True
