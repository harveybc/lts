"""End-to-end shadow run of the Alpaca SPY 1d runner with a modular policy, offline.

The REAL ``AlpacaModelRunner.__init__`` and ``tick()`` run.  Only the broker is
replaced: ``ReplayBrokerClient`` serves recorded closed daily bars through
``stock_bars`` and raises on every other attribute, so any account, position,
quote, clock or order call is a recorded test failure, not a network effect.
Credentials are dummy values under dedicated variable names; the account
fingerprint is a dummy; the ledger is a fresh file in ``--workdir``.

For each as-of point the client serves only bars up to that session and the
runner decides once.  The receipt reads the decisions back from the runner's
own ``due_bar_decisions`` table and declares the contract, the population and
the mandate's sizing (unused: the shadow tier never sizes an order).
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RECEIPT_SCHEMA = "lts.modular_runner_shadow_e2e.v1"
FAKE_KEY_ENV, FAKE_SECRET_ENV = "LTS_SHADOW_E2E_FAKE_KEY", "LTS_SHADOW_E2E_FAKE_SECRET"
DUMMY_FINGERPRINT = "0" * 16
READ_METHODS = {"stock_bars"}


class ReplayBrokerClient:
    """Serves recorded bars; every other broker attribute is refused and logged."""

    calls: list = []
    bars: list = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.calls = ReplayBrokerClient.calls

    def stock_bars(self, symbol, *, timeframe, start, feed="iex", page_token=None, **_kw):
        self.calls.append("stock_bars")
        if timeframe != "1Day" or feed != "iex":
            raise AssertionError("unexpected bar request")
        return {"bars": list(ReplayBrokerClient.bars), "next_page_token": None}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        ReplayBrokerClient.calls.append(name)
        raise AssertionError(f"shadow path reached broker method {name!r}")


def to_wire(bars):
    """CSV/adapter bars -> Alpaca wire bars as the runner's _bars() reads them."""
    return [{"t": b["time"], "o": float(b["open"]), "h": float(b["high"]), "l": float(b["low"]),
             "c": float(b["close"]), "v": float(b["volume"])} for b in bars]


def read_recorded(path: Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return [{"time": r["DateTime"], "open": r["Open"], "high": r["High"], "low": r["Low"],
                 "close": r["Close"], "volume": r["Volume"]} for r in csv.DictReader(handle)]


def shadow_config(contract: Path, workdir: Path, *, tier="shadow_inference_only",
                  family="modular") -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    base = json.loads((ROOT / "examples/configs/alpaca_spy_model_runner_v1.json").read_text())
    profile = json.loads((ROOT / "examples/configs/alpaca_spy_l1_profile_v1.json").read_text())
    profile["account_fingerprint"] = DUMMY_FINGERPRINT
    profile_path = workdir / "profile.json"
    profile_path.write_text(json.dumps(profile, indent=2))
    config = copy.deepcopy(base)
    config["profile_file"] = str(profile_path)
    config["secrets"] = {"api_key_env": FAKE_KEY_ENV, "api_secret_env": FAKE_SECRET_ENV}
    config["model"] = {"family": family, "contract_file": str(contract),
                       "expected_asset_id": "equity:SPY", "expected_timeframe": "1d",
                       "execution_tier": tier}
    config["service"]["account_fingerprint"] = DUMMY_FINGERPRINT
    config["service"]["database_path"] = str(workdir / "shadow-ledger.sqlite")
    config["heartbeat_path"] = str(workdir / "heartbeat.json")
    return config


def build_runner(config: dict, bars: list[dict]):
    import app.alpaca_model_runner as runner_module

    os.environ.setdefault(FAKE_KEY_ENV, "fake-key")
    os.environ.setdefault(FAKE_SECRET_ENV, "fake-secret")
    ReplayBrokerClient.calls = []
    ReplayBrokerClient.bars = to_wire(bars)
    original = runner_module.AlpacaPaperTradingClient
    runner_module.AlpacaPaperTradingClient = ReplayBrokerClient
    try:
        runner = runner_module.AlpacaModelRunner(config)
    finally:
        runner_module.AlpacaPaperTradingClient = original
    return runner


def decisions(database: Path) -> list[dict]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            "SELECT bar_close, model_id, action, outcome, reason, input_sha256, config_sha256,"
            " artifact_sha256, score FROM due_bar_decisions ORDER BY bar_close")]
    finally:
        connection.close()


def run(contract: Path, bars_path: Path, points: int, workdir: Path, *,
        replay_receipt: Path | None = None) -> dict:
    bars = read_recorded(bars_path)
    config = shadow_config(contract, workdir)
    runner = build_runner(config, bars[:len(bars) - points + 1])
    results = []
    try:
        for end in range(len(bars) - points + 1, len(bars) + 1):
            ReplayBrokerClient.bars = to_wire(bars[:end])
            # Execution is requested on purpose: the shadow tier must ignore it.
            result = runner.tick(allow_execution=True)
            runner.write_heartbeat(result)
            results.append(result)
        identity = runner.selector.identity()
        contract_doc = runner.policy.contract.data
    finally:
        runner.close()
    rows = decisions(Path(config["service"]["database_path"]))
    calls = sorted(set(ReplayBrokerClient.calls))
    heartbeat = json.loads(Path(config["heartbeat_path"]).read_text())
    parity = None
    if replay_receipt is not None:
        receipt = json.loads(Path(replay_receipt).read_text())
        by_bar = {r["last_closed_bar"]: r for r in receipt.get("inferences", [])}
        matched = [r for r in results if r.get("inference", {}).get("last_closed_bar") in by_bar]
        parity = {
            "replay_receipt_sha256": hashlib.sha256(Path(replay_receipt).read_bytes()).hexdigest(),
            "points_matched": len(matched),
            "input_sha256_equal": all(by_bar[r["inference"]["last_closed_bar"]]["input_sha256"]
                                      == r["inference"]["input_sha256"] for r in matched),
            "output_sha256_equal": all(by_bar[r["inference"]["last_closed_bar"]]["output_sha256"]
                                       == r["inference"]["output_sha256"] for r in matched),
        }
    states = sorted({r["state"] for r in results})
    ok = (states == ["shadow_inference_only"] and len(rows) == points
          and all(r["outcome"] == "shadow_inference_only" for r in rows)
          and calls == ["stock_bars"]
          and all(r["orders_submitted"] == 0 and r["execution_authorized"] is False for r in results)
          and heartbeat.get("read_only") is True
          and (parity is None or (parity["points_matched"] == points
                                  and parity["input_sha256_equal"] and parity["output_sha256_equal"])))
    profile = json.loads((ROOT / "examples/configs/alpaca_spy_l1_profile_v1.json").read_text())
    return {
        "schema": RECEIPT_SCHEMA, "verdict": "RUNNER_SHADOW_PASS" if ok else "RUNNER_SHADOW_FAIL",
        "runner": "app.alpaca_model_runner.AlpacaModelRunner (real __init__ and tick)",
        "contract": {"sha256": identity["contract_sha256"], "model_id": identity["model_id"],
                     "artifact_sha256": identity["artifact_sha256"],
                     "engine_source_commit": identity["engine_source_commit"],
                     "engine_previous": contract_doc["engine"].get("previous"),
                     "keras_version": identity["keras_version"],
                     "evidence": contract_doc["evidence"]},
        "population": {"asset_id": "equity:SPY", "timeframe": "1d", "feed": "alpaca_iex (recorded)",
                       "bars_sha256": hashlib.sha256(Path(bars_path).read_bytes()).hexdigest(),
                       "bars_rows": len(bars), "first_bar": bars[0]["time"], "last_bar": bars[-1]["time"],
                       "decision_points": points,
                       "first_decision_bar": rows[0]["bar_close"] if rows else None,
                       "last_decision_bar": rows[-1]["bar_close"] if rows else None},
        "scale": {"mandate": "alpaca paper L1 profile (example config)",
                  "quantity_ceiling": profile.get("quantity_ceiling"),
                  "max_orders_per_day": profile.get("max_orders_per_day"),
                  "used": False, "reason": "shadow_inference_only never sizes or submits an order"},
        "bottleneck": {
            "shape": contract_doc["outputs"]["bottleneck"]["shape"],
            "semantics": "transformer_conv core latent: two causal-attention Transformer blocks over "
                         "the 12-step fused branch grid, then three learned compression stages to "
                         "6 right-edge tokens x 8 channels (engine 556c5f3e / 64a91a74).",
            "new_core": "PENDING: lane A's integrated residual Conv1D core over 24-step branches "
                        "(also (6, 8), different semantics) is not yet published.",
        },
        "states": states, "ticks": len(results), "decisions": rows,
        "actions": {a: sum(1 for r in rows if r["action"] == a) for a in sorted({r["action"] for r in rows})},
        "broker_attributes_touched": calls, "mutating_calls": 0 if calls == ["stock_bars"] else None,
        "orders_submitted": 0, "execution_authorized": False,
        "allow_execution_requested": True, "heartbeat_read_only": heartbeat.get("read_only"),
        "replay_parity": parity,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--bars", required=True, type=Path)
    parser.add_argument("--points", type=int, default=12)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--replay-receipt", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    receipt = run(args.contract, args.bars, args.points, args.workdir,
                  replay_receipt=args.replay_receipt)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: receipt[k] for k in ("verdict", "states", "actions",
                                               "broker_attributes_touched", "replay_parity")},
                     sort_keys=True))
    return 0 if receipt["verdict"] == "RUNNER_SHADOW_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
