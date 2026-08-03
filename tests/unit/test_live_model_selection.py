import hashlib
import json

import pytest
from prediction_provider_mechanics import FEATURE_NAMES

from app.live_model_selection import LiveModelSelectionError, SelectedLinearPolicy


def _write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _selection(tmp_path, *, model_id="model-v1", research_validated=True):
    config_path = tmp_path / f"{model_id}-config.json"
    config_sha = _write_json(config_path, {"model_id": model_id})
    artifact_path = tmp_path / f"{model_id}.json"
    artifact_sha = _write_json(artifact_path, {
        "schema": "prediction_provider.live_linear_policy.v1",
        "model_id": model_id,
        "asset_id": "equity:SPY",
        "timeframe": "1d",
        "feature_names": list(FEATURE_NAMES),
        "means": [0.0] * len(FEATURE_NAMES),
        "scales": [1.0] * len(FEATURE_NAMES),
        "coefficients": [0.0] * len(FEATURE_NAMES),
        "intercept": 1.0,
        "probability_threshold": 0.5,
    })
    return {
        "schema": "prediction_provider.live_linear_manifest.v1",
        "model_id": model_id,
        "asset_id": "equity:SPY",
        "timeframe": "1d",
        "artifact_file": str(artifact_path),
        "artifact_sha256": artifact_sha,
        "config_file": str(config_path),
        "config_sha256": config_sha,
        "research_validated": research_validated,
        "live_inference_eligible": False,
        "live_execution_eligible": False,
    }


def test_demo_selector_hot_reloads_only_a_fully_verified_pointer(tmp_path):
    manifest_path = tmp_path / "selected.json"
    first = _selection(tmp_path, model_id="model-v1")
    _write_json(manifest_path, first)
    selector = SelectedLinearPolicy(
        manifest_file=manifest_path,
        expected_asset_id="equity:SPY",
        expected_timeframe="1d",
        execution_tier="demo_research_canary",
    )
    assert selector.policy.model_id == "model-v1"
    assert selector.refresh() is False

    second = _selection(tmp_path, model_id="model-v2")
    _write_json(manifest_path, second)
    assert selector.refresh() is True
    assert selector.policy.model_id == "model-v2"

    broken = dict(second, artifact_sha256="f" * 64)
    _write_json(manifest_path, broken)
    with pytest.raises(LiveModelSelectionError, match="artifact hash"):
        selector.refresh()
    assert selector.policy.model_id == "model-v2"


def test_selection_tier_is_explicit_and_fail_closed(tmp_path):
    manifest_path = tmp_path / "selected.json"
    _write_json(manifest_path, _selection(
        tmp_path, model_id="unvalidated", research_validated=False
    ))
    with pytest.raises(LiveModelSelectionError, match="validation evidence"):
        SelectedLinearPolicy(
            manifest_file=manifest_path,
            expected_asset_id="equity:SPY",
            expected_timeframe="1d",
            execution_tier="demo_research_canary",
        )

    _write_json(manifest_path, _selection(tmp_path, model_id="research-only"))
    with pytest.raises(LiveModelSelectionError, match="not promoted"):
        SelectedLinearPolicy(
            manifest_file=manifest_path,
            expected_asset_id="equity:SPY",
            expected_timeframe="1d",
            execution_tier="promoted_paper",
        )
