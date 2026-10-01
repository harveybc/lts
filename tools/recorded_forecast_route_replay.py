#!/usr/bin/env python3
"""Replay the dormant MT5 demo shadow route over recorded bars with a cell's recorded forecasts.

The REAL Mt5ModelRunner runs offline: an isolated bridge store in --workdir receives one
snapshot per bar (the last 60 bars up to that bar), then the runner ticks. It is read-only
(no network) and queues zero commands. The policy's state persists after every bar, so
``--stop-after N`` followed by a second invocation resumes the replay exactly where the
first one stopped. ``--compare`` checks the per-bar targets against a paired-harness
result (heuristic-strategy run_paired_eth4h.py) on the same bars: they must be identical.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ACCOUNT = "0" * 24  # dummy fingerprint of the isolated replay bridge, not an account


def _no_network(*_a, **_k):
    raise RuntimeError("network forbidden in the offline replay")


def load_view(path, start, stop):
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))[start:stop]
    out = []
    for r in rows:
        t = datetime.fromisoformat(r["DATE_TIME"]).replace(tzinfo=timezone.utc).isoformat()
        out.append({"time": t, "open": float(r["OPEN"]), "high": float(r["HIGH"]), "low": float(r["LOW"]),
                    "close": float(r["CLOSE"])})
    return out


def write_contract(workdir: Path, *, predictions: Path, evidence: Path, declared: dict, episode_start: str,
                   model_id: str) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    pred_copy, ev_copy = workdir / "PREDICTIONS.csv", workdir / "EVIDENCE.json"
    if not pred_copy.exists():
        pred_copy.write_bytes(Path(predictions).read_bytes())
        ev_copy.write_bytes(Path(evidence).read_bytes())
    contract = {
        "schema": "lts.recorded_forecast_contract.v1", "model_id": model_id, "asset_id": "crypto:ETHUSD",
        "timeframe": "4h", "execution_tier": "shadow_inference_only", "execution_authorized": False,
        "predictions": {"file": "PREDICTIONS.csv", "sha256": hashlib.sha256(pred_copy.read_bytes()).hexdigest(),
                        "time_column": "DATE_TIME", "time_zone": "UTC", "price_column": "close_hat_h{h}"},
        "evidence": {"record_file": "EVIDENCE.json", "declared": declared},
        "heuristic": {"profit_threshold_frac": 0.005, "min_drawdown_frac": 0.0025, "tp_multiplier": 0.9,
                      "sl_multiplier": 2.0, "direction": "both"},
        "episode": {"start": episode_start}, "state_file": "state.json", "observability_file": "decisions.jsonl"}
    path = workdir / "contract.json"
    path.write_text(json.dumps(contract, indent=1, sort_keys=True))
    return path


def runner_config(workdir: Path, contract: Path) -> dict:
    (workdir / "bridge.json").write_text(json.dumps({
        "schema": "lts.mt5.execution_bridge_config.v2", "environment": "demo", "execution_enabled": True,
        "database_path": str(workdir / "replay-bridge.sqlite"), "secret_env": "UNUSED", "bind_host": "127.0.0.1",
        "port": 8766, "account_fingerprint": ACCOUNT, "allowed_symbols": ["ETHUSD"], "max_volume": 0.01,
        "max_open_commands_per_day": 4}))
    template = json.loads((ROOT / "examples/configs/mt5_eth_4h_modular_shadow_DORMANT.json").read_text())
    service = dict(template["service"], account_fingerprint=ACCOUNT, database_path=str(workdir / "replay-bridge.sqlite"))
    return {"schema": "lts.mt5.model_runner.v1", "bridge_config_file": str(workdir / "bridge.json"),
            "model": {"family": "recorded_forecast", "contract_file": str(contract),
                      "expected_asset_id": "crypto:ETHUSD", "expected_timeframe": "4h",
                      "execution_tier": "shadow_inference_only"},
            "route": {"symbol": "ETHUSD", "timeframe": "4h"}, "strategy": template["strategy"],
            "snapshot_max_age_seconds": 120, "loop_seconds": 15,
            "heartbeat_path": str(workdir / "heartbeat.json"), "service": service}


def replay(bars, config, *, stop_after=None):
    from app.mt5_bridge_lab import SnapshotPayload
    from app.mt5_model_runner import Mt5ModelRunner

    runner = Mt5ModelRunner(config)
    processed_before = runner.policy.state["processed"]
    done, ticks, states = processed_before, 0, set()
    try:
        for t in range(len(bars)):
            if t < processed_before:
                continue  # already decided before the crash; the persisted state covers it
            now = datetime.now(timezone.utc)
            runner.bridge_store.record_snapshot(SnapshotPayload.model_validate({
                "schema": "lts.mt5.snapshot.v1", "account_fingerprint": ACCOUNT, "observed_at": now,
                "currency": "USD", "balance": 10000, "equity": 10000, "margin": 0, "free_margin": 10000,
                "positions": [], "orders": [],
                "bars": [{"symbol": "ETHUSD", "timeframe": "4h", **b, "volume": 1.0} for b in bars[max(0, t - 59):t + 1]],
                "symbols": [{"symbol": "ETHUSD", "bid": bars[t]["close"], "ask": bars[t]["close"], "point": 0.01,
                             "volume_min": 0.01, "volume_max": 65, "volume_step": 0.01, "trade_mode": 4,
                             "observed_at": now}]}))
            result = runner.tick()
            states.add(result["state"])
            ticks += 1
            done = runner.policy.state["processed"]
            if stop_after is not None and ticks >= stop_after:
                break
        runner.write_heartbeat({"state": "replay", "ticks": ticks})
        commands = runner.bridge_store.connection.execute("SELECT COUNT(*) FROM execution_commands").fetchone()[0]
    finally:
        runner.close()
    return {"ticks": ticks, "processed": done, "states": sorted(states), "commands_queued": commands}


def main(argv=None) -> int:
    socket.socket.connect = _no_network  # type: ignore[assignment]  (keeps the class importable)
    socket.socket.connect_ex = _no_network  # type: ignore[assignment]
    socket.create_connection = _no_network  # type: ignore[assignment]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("--view", "--rows", "--predictions", "--evidence", "--declared", "--workdir"):
        parser.add_argument(name, required=True)
    parser.add_argument("--model-id", default="recorded-forecast-cell")
    parser.add_argument("--stop-after", type=int)
    parser.add_argument("--compare")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    start, stop = (int(x) for x in args.rows.split(":"))
    bars = load_view(args.view, start, stop)
    workdir = Path(args.workdir)
    contract = write_contract(workdir, predictions=Path(args.predictions), evidence=Path(args.evidence),
                              declared=json.loads(Path(args.declared).read_text()),
                              episode_start=bars[0]["time"], model_id=args.model_id)
    summary = replay(bars, runner_config(workdir, contract), stop_after=args.stop_after)
    decisions = [json.loads(line) for line in (workdir / "decisions.jsonl").read_text().splitlines()]
    summary.update(bars=len(bars), decisions_logged=len(decisions), execution_authorized=False)
    if args.compare and summary["processed"] == len(bars):
        harness = json.loads(Path(args.compare).read_text())
        mine = [d["target"] for d in decisions]
        theirs = harness["trajectory"]["targets"]
        mismatches = [i for i, (a, b) in enumerate(zip(mine, theirs)) if a != b]
        summary["comparison"] = {"harness_digest": harness.get("digest"), "bars_compared": min(len(mine), len(theirs)),
                                 "length_equal": len(mine) == len(theirs), "mismatches": len(mismatches),
                                 "first_mismatch": mismatches[0] if mismatches else None,
                                 "identical": len(mine) == len(theirs) and not mismatches}
    text = json.dumps(summary, indent=1, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
