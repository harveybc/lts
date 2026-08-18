"""Atomic, filesystem-local heartbeat for continuous model runners."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def linear_model_identity(selector: Any) -> dict[str, Any]:
    """Content identity for the exact linear policy currently in memory.

    The feature and preprocessing hashes are derived from the immutable policy
    fields used by inference, rather than from paths or labels.  This makes a
    monitoring heartbeat independently joinable to the selected manifest and
    prevents a model id from standing in for artifact custody.
    """
    policy = selector.policy
    manifest = selector.manifest

    def digest(payload: Mapping[str, Any]) -> str:
        body = json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return hashlib.sha256(body).hexdigest()

    return {
        "model_id": policy.model_id,
        "artifact_sha256": policy.artifact_sha256,
        "config_sha256": str(manifest["config_sha256"]),
        "manifest_sha256": str(selector.manifest_sha256),
        "input_feature_sha256": digest({
            "feature_contract": "prediction_provider.closed_bars.linear.v1",
            "feature_names": list(policy.feature_names),
        }),
        "preprocessing_sha256": digest({
            "schema": "prediction_provider.live_linear.standardization.v1",
            "feature_names": list(policy.feature_names),
            "means": list(policy.means),
            "scales": list(policy.scales),
        }),
    }


def selected_model_identity(selector: Any) -> dict[str, Any]:
    """Content identity for either a linear control or an SAC policy."""
    policy = selector.policy
    if hasattr(policy, "feature_names"):
        return linear_model_identity(selector)
    contract = selector.manifest.get("observation_contract") or {}

    def digest(payload: Mapping[str, Any]) -> str:
        body = json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return hashlib.sha256(body).hexdigest()

    return {
        "model_id": policy.model_id,
        "artifact_sha256": policy.artifact_sha256,
        "config_sha256": str(selector.manifest["config_sha256"]),
        "manifest_sha256": str(selector.manifest_sha256),
        "input_feature_sha256": str(
            contract.get("feature_columns_sha256") or ""
        ),
        "preprocessing_sha256": digest({
            key: contract.get(key) for key in (
                "preprocessor_plugin", "feature_scaling",
                "feature_scaling_window", "feature_clip", "window_size",
                "agent_state_contract", "holding_duration_scale_bars",
            )
        }),
    }


def write_runner_heartbeat(
    path: str | Path, *, schema: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    destination = Path(os.path.expandvars(str(path))).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema": schema,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        **dict(payload),
    }
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(body, sort_keys=True, indent=1, default=str), encoding="utf-8"
    )
    temporary.replace(destination)
    return body
