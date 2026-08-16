#!/usr/bin/env python3
"""WO3 stage-1 CLI: typed artifact-compatibility preflight for the seats.

Builds the SeatContract for each current Paper/Demo seat from its live
manifest plus the RUNTIME feature-provisioning truth (the closed-bar
linear contract is what live provisioning can actually produce today),
then runs ``preflight_candidate`` against a candidate descriptor and
prints the typed verdicts. An INCOMPATIBLE verdict with named
incompatibilities (e.g. ``NO_COMPATIBLE_FEATURE_PROVISIONING`` listing
the missing features) is the honest deliverable — this CLI promotes
nothing and writes nothing outside ``--json``.

Candidate sources:
- ``--candidate-descriptor PATH``: a JSON file mirroring
  CandidateContract fields; or
- ``--v2-post-easy``: build the best currently materialized v2-shape
  candidate from the running P1LR v2 decision's persisted facts
  (identity cdf30aebf585385b; 2660-dim observation contract =
  32 bars x 83 ordered features + 4 agent-state dims).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prediction_provider_mechanics.live_linear_policy import (  # noqa: E402
    FEATURE_NAMES,
)

from app.champion_succession import (  # noqa: E402
    ActionContract,
    CandidateContract,
    ExecutionContract,
    FeatureProvisioningContract,
    SeatContract,
    preflight_candidate,
)

LIVE_MANIFEST_ROOT = Path.home() / ".local/share/prediction-provider/live"
LINEAR_FEATURE_CONTRACT = "prediction_provider.closed_bars.linear.v1"

#: seat key -> (manifest dir name, venue, instrument)
SEATS = {
    "spy": ("spy_daily_linear_v1", "alpaca_paper", "SPY"),
    "usdcad": ("usdcad_4h_linear_v1", "ibkr_paper", "USD.CAD"),
    "ethusd": ("ethusdt_4h_linear_v1", "mt5_demo", "ETHUSD"),
}

V2_RUN_ROOT = Path.home() / (
    ".local/share/agent-multi/"
    "p1_difficulty_lr_factorial_20260815_v2_decision/cdf30aebf585385b")
V2_OBSERVATION_DIM = 2660          # 32 * 83 + 4 (finding 235, order §6)
V2_WINDOW = 32
V2_AGENT_STATE_DIMS = 4


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def linear_provisioning_contract() -> FeatureProvisioningContract:
    """What live provisioning can ACTUALLY produce for every seat today:
    the 11-feature closed-bar linear contract."""
    descriptor = {
        "feature_contract": LINEAR_FEATURE_CONTRACT,
        "feature_names": list(FEATURE_NAMES),
    }
    return FeatureProvisioningContract(
        contract_id=LINEAR_FEATURE_CONTRACT,
        feature_names=tuple(FEATURE_NAMES),
        preprocessing_sha256=hashlib.sha256(
            _canonical(descriptor)).hexdigest(),
        observation_dim=len(FEATURE_NAMES),
    )


def build_seat(key: str, manifest_root: Path) -> SeatContract:
    directory, venue, instrument = SEATS[key]
    manifest_file = manifest_root / directory / "manifest.json"
    manifest = json.loads(manifest_file.read_bytes())
    return SeatContract(
        venue=venue,
        asset_id=str(manifest["asset_id"]),
        instrument=instrument,
        timeframe=str(manifest["timeframe"]),
        manifest_file=str(manifest_file),
        provisioning=linear_provisioning_contract(),
        action=ActionContract(
            kind="probability_threshold",
            threshold=float(
                (manifest.get("champion") or {}).get(
                    "probability_threshold", 0.5)),
        ),
        execution=ExecutionContract(
            native_stop_loss=True, native_take_profit=True,
            native_bracket=True,
            sl_tp_geometry="fraction_of_reference",
            transfer_policy="close_all",
        ),
    )


def build_v2_post_easy_candidate() -> CandidateContract:
    """The best currently materialized v2-shape candidate: the seed101
    P1N_LR1E4 post-easy checkpoint of the RUNNING decision. Evidence
    shape only — the decision has no terminal selected champion yet."""
    attempt = (V2_RUN_ROOT / "seed101" / "P1N_LR1E4"
               / "attempt-b6662e13fc45db01-01")
    meta_path = attempt / "model.post_easy.zip.meta.json"
    meta = json.loads(meta_path.read_bytes())
    trace_meta = json.loads(
        (attempt / "return_traces"
         / "train_epoch_return_trace.csv.meta.json").read_bytes())

    def find_key(node, key):
        if isinstance(node, dict):
            if key in node:
                return node[key]
            for value in node.values():
                found = find_key(value, key)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = find_key(value, key)
                if found is not None:
                    return found
        return None

    feature_columns = find_key(trace_meta, "feature_columns")
    if not feature_columns:
        raise SystemExit(
            "v2 trace metadata lacks feature_columns — cannot build the"
            " candidate descriptor from persisted facts")
    split_manifest = json.loads(
        (attempt / "nested_splits"
         / "nested_split_manifest.json").read_bytes())
    threshold = find_key(meta, "continuous_action_threshold") or 0.1
    return CandidateContract(
        model_id="p1lr-v2-cdf30aeb-seed101-P1N_LR1E4-post_easy",
        model_kind="sb3_sac",
        artifact_file=str(meta["artifact"]),
        artifact_sha256=str(meta["artifact_sha256"]),
        config_sha256=hashlib.sha256(meta_path.read_bytes()).hexdigest(),
        asset_id="crypto:ETHUSD",
        timeframe="4h",
        observation_dim=V2_OBSERVATION_DIM,
        feature_names=tuple(feature_columns),
        preprocessing_sha256=str(split_manifest["contract_sha256"]),
        action=ActionContract(kind="continuous_threshold",
                              threshold=float(threshold)),
        execution=ExecutionContract(
            native_stop_loss=True, native_take_profit=True,
            native_bracket=True,
            sl_tp_geometry="fraction_of_reference",
            transfer_policy="close_all",
        ),
    )


def load_candidate_descriptor(path: Path) -> CandidateContract:
    data = json.loads(path.read_bytes())
    return CandidateContract(
        model_id=str(data["model_id"]),
        model_kind=str(data["model_kind"]),
        artifact_file=str(data["artifact_file"]),
        artifact_sha256=str(data["artifact_sha256"]),
        config_sha256=str(data["config_sha256"]),
        asset_id=str(data["asset_id"]),
        timeframe=str(data["timeframe"]),
        observation_dim=int(data["observation_dim"]),
        feature_names=tuple(data["feature_names"]),
        preprocessing_sha256=str(data["preprocessing_sha256"]),
        action=ActionContract(**data["action"]) if not isinstance(
            data["action"], ActionContract) else data["action"],
        execution=ExecutionContract(**data["execution"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seat", default="all",
                        choices=[*SEATS.keys(), "all"])
    parser.add_argument("--manifest-root", type=Path,
                        default=LIVE_MANIFEST_ROOT)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--candidate-descriptor", type=Path)
    source.add_argument("--v2-post-easy", action="store_true")
    parser.add_argument("--no-artifact-bytes", action="store_true",
                        help="skip hashing the artifact bytes (contract"
                        " checks only)")
    parser.add_argument("--json", type=Path,
                        help="also write the full typed reports here")
    args = parser.parse_args()

    candidate = (build_v2_post_easy_candidate() if args.v2_post_easy
                 else load_candidate_descriptor(args.candidate_descriptor))
    keys = list(SEATS) if args.seat == "all" else [args.seat]
    reports = {}
    for key in keys:
        seat = build_seat(key, args.manifest_root)
        report = preflight_candidate(
            seat, candidate,
            verify_artifact_bytes=not args.no_artifact_bytes)
        reports[key] = report
        codes = [item["code"] for item in report["incompatibilities"]]
        print(f"[{key}] seat={seat.venue}/{seat.instrument}"
              f"@{seat.timeframe} candidate={candidate.model_id}")
        print(f"  verdict: {report['verdict']}"
              + (f"  codes: {codes}" if codes else ""))
        for item in report["incompatibilities"]:
            facts = dict(item["facts"])
            missing = facts.pop("missing_features", None)
            print(f"    - {item['code']}: {item['detail']}")
            if missing:
                print(f"      missing ({len(missing)}):"
                      f" {', '.join(missing[:12])}"
                      + (" …" if len(missing) > 12 else ""))
        print(f"  report_sha256: {report['report_sha256']}")
    if args.json:
        args.json.write_text(json.dumps(
            {"schema": "lts.succession.preflight_run.v1",
             "candidate": candidate.to_dict(), "reports": reports},
            indent=1, sort_keys=True) + "\n")
        print(f"full reports written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
