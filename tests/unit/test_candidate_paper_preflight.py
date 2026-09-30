import hashlib
import json
import subprocess
import sys
from pathlib import Path

from tools.candidate_paper_preflight import preflight


def _write(path, value):
    path.write_text(json.dumps(value))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path, *, family="modular"):
    weights = tmp_path / "weights.bin"
    weights.write_bytes(b"fixture weights")
    weight_hash = hashlib.sha256(weights.read_bytes()).hexdigest()
    metrics = tmp_path / "metrics.json"
    metric_hash = _write(metrics, {"validation": {"mae": 0.12}})
    provenance = tmp_path / "provenance.json"
    provenance_hash = _write(provenance, {
        "schema": "lts.candidate_provenance.v1", "status": "verified",
        "model_id": "candidate-1", "weights_sha256": weight_hash,
        "metrics_sha256": metric_hash,
    })
    candidate = tmp_path / "candidate.json"
    _write(candidate, {
        "schema": "lts.paper_candidate_handoff.v1", "model_id": "candidate-1",
        "family": family, "asset_id": "equity:SPY", "timeframe": "1d",
        "weights": {"path": str(weights), "sha256": weight_hash},
        "metrics": {"path": str(metrics), "sha256": metric_hash},
        "provenance": {"path": str(provenance), "sha256": provenance_hash},
    })
    return candidate, weights, metrics, provenance


def test_verified_modular_candidate_is_not_runner_compatible(tmp_path):
    candidate, _, _, _ = _fixture(tmp_path)
    result = preflight(candidate, "alpaca_spy")
    assert result["artifact_evidence"] == "locally_hash_consistent"
    assert result["paper_path"] == "refused"
    assert "unsupported_model_family" in result["reasons"]
    assert result["orders_submitted"] == result["orders_cancelled"] == 0
    assert result["promotion_authorized"] is False


def test_linear_fixture_reaches_interface_compatible_without_promotion(tmp_path):
    from prediction_provider_mechanics import FEATURE_NAMES

    candidate, weights, _, provenance = _fixture(tmp_path, family="linear")
    model = {
        "schema": "prediction_provider.live_linear_policy.v1",
        "model_id": "candidate-1", "asset_id": "equity:SPY", "timeframe": "1d",
        "feature_names": list(FEATURE_NAMES),
        "means": [0.0] * len(FEATURE_NAMES),
        "scales": [1.0] * len(FEATURE_NAMES),
        "coefficients": [0.0] * len(FEATURE_NAMES),
        "intercept": 1.0, "probability_threshold": 0.5,
    }
    weight_hash = _write(weights, model)
    proof = json.loads(provenance.read_text())
    proof["weights_sha256"] = weight_hash
    proof_hash = _write(provenance, proof)
    config = tmp_path / "config.json"
    config_hash = _write(config, {"model_id": "candidate-1"})
    manifest = tmp_path / "manifest.json"
    _write(manifest, {
        "schema": "prediction_provider.live_linear_manifest.v1",
        "model_id": "candidate-1", "asset_id": "equity:SPY", "timeframe": "1d",
        "artifact_file": str(weights), "artifact_sha256": weight_hash,
        "config_file": str(config), "config_sha256": config_hash,
        "research_validated": True, "live_inference_eligible": False,
        "live_execution_eligible": False,
    })
    handoff = json.loads(candidate.read_text())
    handoff["weights"]["sha256"] = weight_hash
    handoff["provenance"]["sha256"] = proof_hash
    handoff["selection_manifest"] = str(manifest)
    _write(candidate, handoff)
    result = preflight(candidate, "alpaca_spy")
    assert result["paper_path"] == "interface_compatible_only"
    assert result["promotion_authorized"] is False
    run = subprocess.run(
        [sys.executable, "tools/candidate_paper_preflight.py", "--candidate",
         str(candidate), "--route", "alpaca_spy"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
    )
    assert run.returncode == 0
    assert json.loads(run.stdout)["promotion_authorized"] is False


def test_missing_weights_or_metrics_refuses_before_compatibility(tmp_path):
    candidate, weights, metrics, _ = _fixture(tmp_path)
    weights.unlink()
    result = preflight(candidate, "alpaca_spy")
    assert result["artifact_evidence"] == "refused"
    assert "weights_missing" in result["reasons"]
    weights.write_bytes(b"fixture weights")
    metrics.unlink()
    assert "metrics_missing" in preflight(candidate, "alpaca_spy")["reasons"]


def test_tamper_and_unverified_provenance_refuse(tmp_path):
    candidate, weights, _, provenance = _fixture(tmp_path)
    weights.write_bytes(b"changed")
    assert "weights_hash_mismatch" in preflight(candidate, "alpaca_spy")["reasons"]
    weights.write_bytes(b"fixture weights")
    doc = json.loads(provenance.read_text())
    doc["status"] = "pending"
    _write(provenance, doc)
    handoff = json.loads(candidate.read_text())
    handoff["provenance"]["sha256"] = hashlib.sha256(provenance.read_bytes()).hexdigest()
    _write(candidate, handoff)
    assert "provenance_not_verified" in preflight(candidate, "alpaca_spy")["reasons"]


def test_route_and_metric_semantics_refuse(tmp_path):
    candidate, _, metrics, _ = _fixture(tmp_path)
    assert "route_mismatch" in preflight(candidate, "mt5_usdcad")["reasons"]
    _write(metrics, {"validation": {"mae": "nan"}})
    handoff = json.loads(candidate.read_text())
    handoff["metrics"]["sha256"] = hashlib.sha256(metrics.read_bytes()).hexdigest()
    _write(candidate, handoff)
    assert "metrics_invalid" in preflight(candidate, "alpaca_spy")["reasons"]


def test_malformed_handoff_refuses_without_exception(tmp_path):
    candidate, _, _, _ = _fixture(tmp_path)
    handoff = json.loads(candidate.read_text())
    handoff["weights"] = "not a file object"
    _write(candidate, handoff)
    assert preflight(candidate, "alpaca_spy")["paper_path"] == "refused"


def test_cli_is_read_only_and_nonzero_on_refusal(tmp_path):
    candidate, _, _, _ = _fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    run = subprocess.run(
        [sys.executable, "tools/candidate_paper_preflight.py", "--candidate",
         str(candidate), "--route", "alpaca_spy"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
    )
    assert run.returncode == 2
    assert json.loads(run.stdout)["paper_path"] == "refused"
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def _with_contract(tmp_path, weights, *, artifact_sha=None, asset="equity:SPY"):
    from tests.unit.test_modular_inference_adapter import _contract

    engine = tmp_path / "engine.py"
    engine.write_text("# engine\n")
    data = _contract(**{
        "model_id": "candidate-1", "asset_id": asset,
        "engine.path": str(engine), "engine.sha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
        "artifact.file": str(weights),
        "artifact.sha256": artifact_sha or hashlib.sha256(weights.read_bytes()).hexdigest()})
    contract = tmp_path / "contract.json"
    return {"path": str(contract), "sha256": _write(contract, data)}


def test_modular_candidate_with_verified_contract_reaches_shadow_tier_only(tmp_path):
    candidate, weights, _, _ = _fixture(tmp_path)
    doc = json.loads(candidate.read_text())
    doc["inference_contract"] = _with_contract(tmp_path, weights)
    _write(candidate, doc)
    result = preflight(candidate, "alpaca_spy")
    assert result["paper_path"] == "shadow_inference_only"
    assert result["promotion_authorized"] is False and result["orders_submitted"] == 0
    assert "modular_runner_integration_not_wired" in result["reasons"]
    assert "execution_authorized" not in result or result["execution_authorized"] is False


def test_modular_contract_must_name_the_handed_off_weights(tmp_path):
    candidate, weights, _, _ = _fixture(tmp_path)
    other = tmp_path / "other.keras"
    other.write_bytes(b"other")
    doc = json.loads(candidate.read_text())
    doc["inference_contract"] = _with_contract(tmp_path, other)
    _write(candidate, doc)
    result = preflight(candidate, "alpaca_spy")
    assert result["paper_path"] == "refused"
    assert "inference_contract_identity_mismatch" in result["reasons"]
    doc["inference_contract"]["sha256"] = "0" * 64
    _write(candidate, doc)
    assert "inference_contract_hash_mismatch" in preflight(candidate, "alpaca_spy")["reasons"]
