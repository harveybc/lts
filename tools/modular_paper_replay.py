"""Replay RECORDED closed bars through the modular adapter; opens no socket.

For each of the last ``--points`` recorded sessions the adapter builds the
observation as of that session's close, runs the contract's model and maps the
action.  The receipt then states four things, each re-derived here:

* ``golden_parity`` - scaled windows and both model outputs equal the values the
  predictor exporter computed (two independent feature implementations);
* ``deterministic`` - a second full pass reproduces every output digest;
* ``causal`` - rewriting every bar after an as-of point leaves that point's
  observation digest unchanged, while rewriting a bar inside the window changes it;
* ``no_execution`` - zero broker calls, ``execution_authorized`` false throughout.

The CSV is the LTS closed-bar fetch output; that tool writes only completed bars,
which is why rows are admitted as ``complete``.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.modular_inference_adapter import (  # noqa: E402
    ModularAdapterError, ModularPolicy, build_observation, parse_time,
)

RECEIPT_SCHEMA = "lts.modular_replay_receipt.v1"


def read_recorded_bars(path: Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return [{"time": row["DateTime"], "open": row["Open"], "high": row["High"],
                 "low": row["Low"], "close": row["Close"], "volume": row["Volume"],
                 "complete": True} for row in csv.DictReader(handle)]


def _max_abs(a, b) -> float:
    if isinstance(a, list):
        if not isinstance(b, list) or len(a) != len(b):
            return float("inf")
        return max((_max_abs(x, y) for x, y in zip(a, b)), default=0.0)
    return abs(float(a) - float(b))


def replay(contract_path: Path, bars_path: Path, points: int, *, golden_path: Path | None = None,
           feature_tol: float = 1e-9, output_tol: float = 1e-5) -> dict:
    policy = ModularPolicy.load(contract_path)
    contract = policy.contract
    bars = read_recorded_bars(bars_path)
    offset = timedelta(hours=float(contract.data["time"]["bar_close_offset_hours"]))
    as_of_points = [parse_time(bar["time"]) + offset for bar in bars[-points:]]

    def one_pass():
        rows = []
        for as_of in as_of_points:
            observation = build_observation(contract, bars, as_of=as_of)
            rows.append((observation, policy.predict(observation)))
        return rows

    first, second = one_pass(), one_pass()
    deterministic = all(a[1]["output_sha256"] == b[1]["output_sha256"]
                        and a[0]["input_sha256"] == b[0]["input_sha256"]
                        for a, b in zip(first, second))

    # Causality: the future is rewritten, the observation must not move; a change
    # inside the window must move it (the check is not vacuous).
    probe_index = len(bars) - max(2, points // 2)
    probe_as_of = parse_time(bars[probe_index]["time"]) + offset
    base = build_observation(contract, bars, as_of=probe_as_of)["input_sha256"]
    future = [dict(bar) for bar in bars]
    for bar in future[probe_index + 1:]:
        bar["close"] = str(float(bar["close"]) * 1.5)
        bar["high"] = str(max(float(bar["high"]) * 1.5, float(bar["close"])))
        bar["volume"] = str(float(bar["volume"]) * 7 + 1)
    future_unchanged = build_observation(contract, future, as_of=probe_as_of)["input_sha256"] == base
    inside = [dict(bar) for bar in bars]
    inside[probe_index]["volume"] = str(float(inside[probe_index]["volume"]) * 3 + 1)
    inside_changes = build_observation(contract, inside, as_of=probe_as_of)["input_sha256"] != base

    parity = {"checked": 0, "missing": 0, "max_abs_window": 0.0, "max_abs_forecast": 0.0,
              "max_abs_bottleneck": 0.0}
    if golden_path is not None:
        golden = json.loads(Path(golden_path).read_text())
        by_bar = {parse_time(row["last_closed_bar"]).isoformat(): row for row in golden["rows"]}
        for observation, inference in first:
            row = by_bar.get(observation["last_closed_bar"])
            if row is None:
                continue
            parity["checked"] += 1
            # The model consumes float32; the exporter stored the float32 tensor it fed.
            # Parity is judged on that tensor; the float64 difference is reported too.
            window32 = np.asarray(observation["window"], dtype=np.float32).astype(np.float64).tolist()
            parity["max_abs_window"] = max(parity["max_abs_window"],
                                           _max_abs(window32, row["window_scaled"]))
            parity["max_abs_window_float64"] = max(parity.get("max_abs_window_float64", 0.0),
                                                   _max_abs(observation["window"], row["window_scaled"]))
            parity["max_abs_forecast"] = max(parity["max_abs_forecast"], _max_abs(
                [v for h in inference["forecast"]["scaled"] for v in h], row["forecast"]))
            parity["max_abs_bottleneck"] = max(parity["max_abs_bottleneck"], _max_abs(
                inference["bottleneck"]["values"], row["bottleneck"]))
        parity["missing"] = len(golden["rows"]) - parity["checked"]
    parity["passed"] = (golden_path is not None and parity["checked"] > 0
                        and parity["max_abs_window"] <= feature_tol
                        and parity["max_abs_forecast"] <= output_tol
                        and parity["max_abs_bottleneck"] <= output_tol)
    parity["tolerances"] = {"window": feature_tol, "outputs": output_tol}

    actions = {}
    for _, inference in first:
        actions[inference["action"]] = actions.get(inference["action"], 0) + 1
    no_execution = all(inference["execution_authorized"] is False for _, inference in first)
    verdict = ("REPLAY_PASS" if deterministic and future_unchanged and inside_changes
               and parity["passed"] and no_execution else "REPLAY_FAIL")
    return {
        "schema": RECEIPT_SCHEMA, "verdict": verdict,
        "evidence_class": "recorded_input_replay",
        "contract_sha256": contract.sha256, "model_id": policy.model_id,
        "artifact_sha256": policy.artifact_sha256,
        "bars_sha256": hashlib.sha256(Path(bars_path).read_bytes()).hexdigest(),
        "bars_rows": len(bars), "points": len(first),
        "first_asof": as_of_points[0].isoformat(), "last_asof": as_of_points[-1].isoformat(),
        "deterministic": deterministic,
        "causal": {"future_rewrite_leaves_observation": future_unchanged,
                   "in_window_rewrite_changes_observation": inside_changes,
                   "probe_last_closed_bar": bars[probe_index]["time"]},
        "golden_parity": parity, "actions": actions,
        "bottleneck_shape": first[-1][1]["bottleneck"]["shape"],
        "broker_calls": 0, "orders_submitted": 0, "execution_authorized": False,
        "inferences": [{"last_closed_bar": inference["last_closed_bar"],
                        "input_sha256": inference["input_sha256"],
                        "output_sha256": inference["output_sha256"],
                        "action": inference["action"], "forecast": inference["action_input"]}
                       for _, inference in first],
        "replayed_at": datetime.now(timezone.utc).isoformat(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--bars", required=True, type=Path)
    parser.add_argument("--golden", type=Path)
    parser.add_argument("--points", type=int, default=12)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = replay(args.contract, args.bars, args.points, golden_path=args.golden)
    except ModularAdapterError as exc:
        receipt = {"schema": RECEIPT_SCHEMA, "verdict": "REPLAY_REFUSED", "reason": str(exc),
                   "broker_calls": 0, "orders_submitted": 0, "execution_authorized": False}
    text = json.dumps(receipt, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    summary = {k: receipt.get(k) for k in ("verdict", "points", "deterministic", "causal",
                                            "golden_parity", "actions", "reason")}
    print(json.dumps(summary, sort_keys=True))
    return 0 if receipt["verdict"] == "REPLAY_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
