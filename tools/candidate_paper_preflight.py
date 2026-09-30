"""Offline candidate custody and runner-interface preflight; never contacts a venue."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

# Resolve `app` from THIS checkout, not whichever checkout an editable install points at.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ROUTES = {
    "alpaca_spy": ("equity:SPY", "1d", "alpaca_paper"),
    "mt5_usdcad": ("fx:USD/CAD", "4h", "mt5_demo"),
}


def _read_json(path: Path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _check_file(spec, label, reasons):
    if not isinstance(spec, dict):
        reasons.append(f"{label}_missing")
        return None
    name, expected = spec.get("path"), spec.get("sha256")
    if not isinstance(name, str) or not name or not isinstance(expected, str) or len(expected) != 64:
        reasons.append(f"{label}_identity_missing")
        return None
    try:
        int(expected, 16)
        path = Path(name).expanduser()
        if not path.is_file():
            reasons.append(f"{label}_missing")
            return None
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        reasons.append(f"{label}_unreadable")
        return None
    if actual != expected.lower():
        reasons.append(f"{label}_hash_mismatch")
        return None
    return path


def _valid_metrics(doc):
    values = doc.get("validation") if isinstance(doc, dict) else None
    return isinstance(values, dict) and bool(values) and all(
        isinstance(v, (int, float)) and not isinstance(v, bool)
        and math.isfinite(v) for v in values.values()
    )


def preflight(candidate_file: str | Path, route: str) -> dict:
    """Validate local custody; interface compatibility is not an execution grant."""
    reasons = []
    result = {
        "schema": "lts.paper_candidate_preflight.v1", "route": route,
        "artifact_evidence": "refused", "paper_path": "refused",
        "promotion_authorized": False,
        "orders_submitted": 0, "orders_cancelled": 0, "reasons": reasons,
    }
    try:
        candidate = _read_json(Path(candidate_file))
    except (OSError, ValueError, json.JSONDecodeError):
        reasons.append("candidate_unreadable")
        return result
    if candidate.get("schema") != "lts.paper_candidate_handoff.v1":
        reasons.append("candidate_schema_unsupported")
    if not isinstance(candidate.get("model_id"), str) or not candidate["model_id"]:
        reasons.append("model_id_missing")
    weights = _check_file(candidate.get("weights"), "weights", reasons)
    metrics = _check_file(candidate.get("metrics"), "metrics", reasons)
    provenance = _check_file(candidate.get("provenance"), "provenance", reasons)
    if metrics:
        try:
            if not _valid_metrics(_read_json(metrics)):
                reasons.append("metrics_invalid")
        except (OSError, ValueError):
            reasons.append("metrics_invalid")
    if provenance:
        try:
            proof = _read_json(provenance)
            weight_spec = candidate.get("weights")
            metric_spec = candidate.get("metrics")
            if (proof.get("schema") != "lts.candidate_provenance.v1"
                    or proof.get("status") != "verified"
                    or proof.get("model_id") != candidate.get("model_id")
                    or not isinstance(weight_spec, dict)
                    or not isinstance(metric_spec, dict)
                    or proof.get("weights_sha256") != weight_spec.get("sha256")
                    or proof.get("metrics_sha256") != metric_spec.get("sha256")):
                reasons.append("provenance_not_verified")
        except (OSError, ValueError):
            reasons.append("provenance_not_verified")
    if reasons:
        return result
    result["artifact_evidence"] = "locally_hash_consistent"
    if route not in ROUTES:
        reasons.append("route_unsupported")
        return result
    asset, timeframe, venue = ROUTES[route]
    if candidate.get("asset_id") != asset or candidate.get("timeframe") != timeframe:
        reasons.append("route_mismatch")
        return result
    if candidate.get("family") == "modular" and "inference_contract" in candidate:
        return _modular_shadow(candidate, asset, timeframe, result, reasons)
    if candidate.get("family") != "linear":
        reasons.append("unsupported_model_family")
        return result
    manifest = candidate.get("selection_manifest")
    if not isinstance(manifest, str) or not manifest:
        reasons.append("selection_manifest_missing")
        return result
    # The active runner uses this exact selector, including its tier and hashes.
    try:
        from app.live_model_selection import SelectedLinearPolicy
        selector = SelectedLinearPolicy(
            manifest_file=manifest, expected_asset_id=asset,
            expected_timeframe=timeframe, execution_tier="demo_research_canary",
        )
        if (selector.policy.model_id != candidate["model_id"]
                or selector.policy.artifact_sha256 != candidate["weights"]["sha256"]):
            reasons.append("selection_identity_mismatch")
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            ImportError, RuntimeError):
        reasons.append("selection_refused")
    if reasons:
        return result
    if venue == "mt5_demo":
        reasons.append("mt5_bridge_evidence_required")
        return result
    result["paper_path"] = "interface_compatible_only"
    return result


def _modular_shadow(candidate, asset, timeframe, result, reasons):
    """A modular candidate reaches the adapter's shadow tier, never the runner.

    The contract digest, its engine/artifact/metrics/golden digests and the
    route are verified without loading TensorFlow. The handoff's weights must
    be the very archive the contract names. No runner consumes this tier.
    """
    contract_path = _check_file(candidate.get("inference_contract"), "inference_contract", reasons)
    if contract_path is None:
        return result
    try:
        from app.modular_inference_adapter import ModularAdapterError, load_contract
    except ImportError:
        reasons.append("modular_adapter_unavailable")
        return result
    try:
        contract = load_contract(contract_path)
    except ModularAdapterError:
        reasons.append("inference_contract_refused")
        return result
    if (contract.model_id != candidate.get("model_id") or contract.asset_id != asset
            or contract.timeframe != timeframe
            or contract.data["artifact"]["sha256"] != candidate["weights"]["sha256"]):
        reasons.append("inference_contract_identity_mismatch")
        return result
    result["paper_path"] = "shadow_inference_only"
    result["inference_contract_sha256"] = contract.sha256
    reasons.append("modular_runner_integration_not_wired")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--route", required=True, choices=sorted(ROUTES))
    args = parser.parse_args()
    report = preflight(args.candidate, args.route)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["paper_path"] == "interface_compatible_only" else 2


if __name__ == "__main__":
    raise SystemExit(main())
