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


def forecast_eligibility(contract) -> dict[str, Any]:
    """Forecast-vs-naive decision for the horizon this contract's action consumes.

    The contract's ``evidence.forecast_naive`` names the frozen evidence record
    (predictor.forecast_naive_evidence.v1) and the consumer declaration. The action
    reads exactly one forecast horizon (``action.horizon_index``), so the declaration
    must map exactly that horizon, in the same step unit. A missing, unreadable or
    failing record is a SKIP, never a pass.
    """
    import json as _json

    from app.forecast_naive_gate import SKIPPED, evaluate

    spec = (contract.data.get("evidence") or {}).get("forecast_naive")
    skip = lambda reason, detail=None: {"status": SKIPPED, "failures": [
        {"reason": reason, "family": "forecast", "horizon": None, "detail": detail}], "horizons": []}
    if not isinstance(spec, Mapping):
        return skip("evidence_not_configured", "contract evidence.forecast_naive is absent")
    declared = spec.get("declared")
    try:
        record = _json.loads(contract.resolve(spec.get("record_file", "")).read_text())
    except (OSError, ValueError, TypeError):
        return skip("evidence_unreadable", spec.get("record_file"))
    fam = ((declared or {}).get("families") or {}).get("forecast") or {}
    action, outputs = contract.data["action"], contract.data["outputs"]["forecast"]
    consumed_horizon = outputs["horizons"][action["horizon_index"]]
    if fam.get("horizons") != [consumed_horizon]:
        return skip("consumed_horizon_mismatch",
                    {"declared": fam.get("horizons"), "action_reads": [consumed_horizon]})
    if fam.get("period_hours") != contract.data["time"]["sample_hours"]:
        return skip("period_mismatch", {"declared": fam.get("period_hours"),
                                        "contract": contract.data["time"]["sample_hours"]})
    consumption = {"asset": declared.get("asset"), "declared": declared,
                   "families": {"forecast": {"consumed": 1, "horizons": fam["horizons"], "spec": dict(fam)}}}
    return evaluate({"forecast": record}, consumption, families=("forecast",))


class SelectedModularPolicy:
    """Hash-checked, hot-reloadable pointer to one modular contract."""

    def __init__(self, *, contract_file: str | Path, expected_asset_id: str,
                 expected_timeframe: str, execution_tier: str,
                 allow_unpinned_keras: bool = False,
                 require_forecast_eligibility: bool = False) -> None:
        if execution_tier != TIER:
            raise LiveModelSelectionError(
                f"modular policies run only in the {TIER} tier, not {execution_tier!r}")
        self.contract_file = Path(os.path.expandvars(str(contract_file))).expanduser()
        self.expected_asset_id = expected_asset_id
        self.expected_timeframe = expected_timeframe
        self.execution_tier = execution_tier
        self.allow_unpinned_keras = allow_unpinned_keras
        self.require_forecast_eligibility = require_forecast_eligibility
        self.eligibility: dict[str, Any] | None = None
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
        if self.require_forecast_eligibility:
            # Dormant until a passing record exists: decided before any model is loaded.
            from app.modular_inference_adapter import load_contract
            try:
                contract = load_contract(self.contract_file)
            except ModularAdapterError as exc:
                raise LiveModelSelectionError(f"modular contract refused: {exc}") from exc
            self.eligibility = forecast_eligibility(contract)
            if self.eligibility["status"] != "ELIGIBLE":
                reasons = sorted({f["reason"] for f in self.eligibility["failures"]})
                raise LiveModelSelectionError(
                    f"SKIPPED_NOT_BETTER_THAN_NAIVE: dormant, not eligible ({', '.join(reasons)})")
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
            "forecast_eligibility": (self.eligibility or {}).get("status", "NOT_REQUIRED"),
        }
