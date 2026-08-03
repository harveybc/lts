"""Hash-verified, hot-reloadable model-selection pointer for Paper/Demo routes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from prediction_provider_mechanics import LiveLinearPolicy


class LiveModelSelectionError(RuntimeError):
    pass


def _path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(_path(path).read_bytes()).hexdigest()


class SelectedLinearPolicy:
    """Treat a manifest as an atomic pointer to the selected route champion."""

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
        self.policy: LiveLinearPolicy
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> bool:
        try:
            manifest_bytes = self.manifest_file.read_bytes()
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            if not force and manifest_sha256 == self.manifest_sha256:
                return False
            manifest = json.loads(manifest_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveModelSelectionError("selected model manifest is unreadable") from exc
        if manifest.get("schema") != "prediction_provider.live_linear_manifest.v1":
            raise LiveModelSelectionError("selected model manifest schema is unsupported")
        if manifest.get("asset_id") != self.expected_asset_id:
            raise LiveModelSelectionError("selected model asset does not match the route")
        if manifest.get("timeframe") != self.expected_timeframe:
            raise LiveModelSelectionError("selected model timeframe does not match the route")
        if self.execution_tier == "demo_research_canary":
            if manifest.get("research_validated") is not True:
                raise LiveModelSelectionError("Demo research model lacks validation evidence")
        elif self.execution_tier == "promoted_paper":
            if (
                manifest.get("live_inference_eligible") is not True
                or manifest.get("live_execution_eligible") is not True
            ):
                raise LiveModelSelectionError("model is not promoted for Paper execution")
        else:
            raise LiveModelSelectionError("unknown model execution tier")
        artifact_path = _path(manifest.get("artifact_file", ""))
        config_path = _path(manifest.get("config_file", ""))
        if _sha256(artifact_path) != manifest.get("artifact_sha256"):
            raise LiveModelSelectionError("selected model artifact hash mismatch")
        if _sha256(config_path) != manifest.get("config_sha256"):
            raise LiveModelSelectionError("selected model config hash mismatch")
        policy = LiveLinearPolicy.load(artifact_path, manifest["artifact_sha256"])
        if (
            policy.model_id != manifest.get("model_id")
            or policy.asset_id != self.expected_asset_id
            or policy.timeframe != self.expected_timeframe
        ):
            raise LiveModelSelectionError("manifest and model artifact identities disagree")
        self.manifest = manifest
        self.policy = policy
        self.manifest_sha256 = manifest_sha256
        return True
