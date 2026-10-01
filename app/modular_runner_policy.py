"""Runner-facing selection of a modular policy: shadow tier only.

``AlpacaModelRunner`` selects its policy through a selector exposing
``refresh()``, ``manifest`` and ``policy``.  ``SelectedModularPolicy`` gives a
modular contract that same surface, so the runner that actually serves the
route consumes it, but it refuses every execution tier except
``shadow_inference_only``.  The policy it yields carries ``shadow_only = True``,
which makes the runner take its shadow branch: closed bars -> adapter
observation -> modular inference -> action -> one due-bar decision fact, with no
session, position, quote or order logic reached.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.live_model_selection import LiveModelSelectionError
from app.modular_inference_adapter import (
    CONTRACT_SCHEMA, TIER, ModularAdapterError, ModularPolicy, build_observation,
)


class RunnerModularPolicy:
    """The modular policy as the runner sees it; never executable."""

    shadow_only = True

    def __init__(self, policy: ModularPolicy) -> None:
        self._policy = policy
        self.contract = policy.contract
        self.model_id = policy.model_id
        self.asset_id = policy.asset_id
        self.timeframe = policy.timeframe
        self.artifact_sha256 = policy.artifact_sha256
        self.contract_sha256 = policy.contract_sha256

    def build_observation(self, bars: Sequence[Mapping[str, Any]], *, as_of=None) -> dict[str, Any]:
        return build_observation(self.contract, bars, as_of=as_of)

    def predict(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        inference = self._policy.predict(observation)
        if inference.get("execution_authorized") is not False or inference.get("tier") != TIER:
            raise ModularAdapterError("modular inference left the shadow tier")
        return inference


class SelectedModularPolicy:
    """Hash-checked, hot-reloadable pointer to one modular contract."""

    def __init__(self, *, contract_file: str | Path, expected_asset_id: str,
                 expected_timeframe: str, execution_tier: str,
                 allow_unpinned_keras: bool = False) -> None:
        if execution_tier != TIER:
            raise LiveModelSelectionError(
                f"modular policies run only in the {TIER} tier, not {execution_tier!r}")
        self.contract_file = Path(os.path.expandvars(str(contract_file))).expanduser()
        self.expected_asset_id = expected_asset_id
        self.expected_timeframe = expected_timeframe
        self.execution_tier = execution_tier
        self.allow_unpinned_keras = allow_unpinned_keras
        self.contract_sha256 = ""
        self.manifest: dict[str, Any] = {}
        self.policy: RunnerModularPolicy
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> bool:
        try:
            digest = hashlib.sha256(self.contract_file.read_bytes()).hexdigest()
        except OSError as exc:
            raise LiveModelSelectionError("modular contract is unreadable") from exc
        if not force and digest == self.contract_sha256:
            return False
        try:
            policy = ModularPolicy.load(self.contract_file,
                                        allow_unpinned_keras=self.allow_unpinned_keras)
        except ModularAdapterError as exc:
            raise LiveModelSelectionError(f"modular contract refused: {exc}") from exc
        if policy.asset_id != self.expected_asset_id or policy.timeframe != self.expected_timeframe:
            raise LiveModelSelectionError("modular contract does not match the route")
        if policy.contract_sha256 != digest:
            raise LiveModelSelectionError("modular contract changed while loading")
        self.policy = RunnerModularPolicy(policy)
        self.manifest = {
            "schema": CONTRACT_SCHEMA, "model_id": policy.model_id,
            "config_sha256": digest, "manifest_sha256": digest,
            "execution_tier": self.execution_tier,
        }
        self.contract_sha256 = digest
        return True

    def identity(self) -> dict[str, Any]:
        engine = self.policy.contract.data["engine"]
        return {
            "model_family": "modular", "model_id": self.policy.model_id,
            "artifact_sha256": self.policy.artifact_sha256,
            "contract_sha256": self.contract_sha256,
            "engine_sha256": engine.get("sha256"),
            "engine_source_commit": engine.get("source_commit"),
            "keras_version": engine.get("keras_version"),
            "execution_tier": self.execution_tier, "execution_authorized": False,
        }
