"""Eligible, read-only paper smoke for the modular adapter (shadow inference only).

Eligibility is decided before any network call:

1. the contract verifies (engine, artifact, metrics and golden digests);
2. a replay receipt for the SAME contract and artifact says ``REPLAY_PASS``;
3. the Paper observer's own ledger, opened read-only (``mode=ro``), shows a
   completed observer session within ``--observer-max-age-minutes``.

Only then are closed daily bars read through the observer's GET-only
``AlpacaPaperClient`` data endpoint.  Its HTTP session is replaced by a guard
that refuses every method except GET and counts calls, so a mutating request is
structurally impossible here, not merely absent.  No account, position or order
endpoint is called, no ledger is written and no order intent is built.  The
record states ``execution_authorized: false`` and ``orders_submitted: 0``.

Credentials come only from the environment named by the observer config; this
tool never reads a credential file and never prints one.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.alpaca_paper_lab import AlpacaPaperClient, AlpacaPaperLabConfig  # noqa: E402
from app.modular_inference_adapter import (  # noqa: E402
    ModularAdapterError, ModularPolicy, build_observation, load_contract,
)

RECORD_SCHEMA = "lts.modular_paper_smoke.v1"


class GetOnlySession(requests.Session):
    """HTTP session that refuses any non-GET request before it leaves the host."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: dict[str, int] = {}
        self.refused: list[str] = []
        self.paths: list[str] = []

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        verb = str(method).upper()
        if verb != "GET":
            self.refused.append(verb)
            raise PermissionError(f"read-only smoke refuses HTTP {verb}")
        self.calls[verb] = self.calls.get(verb, 0) + 1
        self.paths.append(requests.utils.urlparse(url).path)
        return super().request(method, url, *args, **kwargs)


def observer_health(database: Path, max_age: timedelta, now: datetime) -> dict:
    uri = f"file:{database}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            row = connection.execute(
                "SELECT started_at, ended_at, status FROM lab_sessions "
                "ORDER BY started_at DESC LIMIT 1").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return {"eligible": False, "reason": f"observer_ledger_unreadable:{type(exc).__name__}"}
    if row is None:
        return {"eligible": False, "reason": "observer_ledger_empty"}
    started = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).astimezone(timezone.utc)
    age = now - started
    eligible = age <= max_age and row[1] is not None and str(row[2]).lower() in {"complete"}
    return {"eligible": eligible, "latest_started_at": started.isoformat(),
            "age_seconds": round(age.total_seconds(), 1), "status": row[2],
            "reason": None if eligible else "observer_not_fresh_or_not_completed"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--replay-receipt", required=True, type=Path)
    parser.add_argument("--observer-config", type=Path,
                        default=ROOT / "examples/configs/alpaca_paper_execution_lab_v1.json")
    parser.add_argument("--observer-max-age-minutes", type=float, default=20.0)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--lookback-days", type=int, default=150)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    record = {"schema": RECORD_SCHEMA, "started_at": now.isoformat(), "tier": "shadow_inference_only",
              "execution_authorized": False, "orders_submitted": 0, "orders_cancelled": 0,
              "account_endpoints_called": 0, "verdict": "SMOKE_REFUSED", "gates": {}}

    def finish(code: int) -> int:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "smoke_record.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        print(json.dumps({k: record.get(k) for k in ("verdict", "gates", "http", "inference_summary",
                                                     "execution_authorized", "orders_submitted")},
                         sort_keys=True))
        return code

    try:
        contract = load_contract(args.contract)
        record["gates"]["contract"] = {"eligible": True, "sha256": contract.sha256}
    except ModularAdapterError as exc:
        record["gates"]["contract"] = {"eligible": False, "reason": str(exc)}
        return finish(2)
    try:
        receipt = json.loads(args.replay_receipt.read_text())
    except (OSError, json.JSONDecodeError):
        receipt = {}
    replay_ok = (receipt.get("verdict") == "REPLAY_PASS"
                 and receipt.get("contract_sha256") == contract.sha256
                 and receipt.get("artifact_sha256") == contract.data["artifact"]["sha256"])
    record["gates"]["replay"] = {"eligible": replay_ok,
                                 "receipt_sha256": hashlib.sha256(_bytes(args.replay_receipt)).hexdigest()
                                 if args.replay_receipt.is_file() else None}
    if not replay_ok:
        return finish(2)
    lab = AlpacaPaperLabConfig.load(args.observer_config)
    health = observer_health(lab.database_path, timedelta(minutes=args.observer_max_age_minutes), now)
    record["gates"]["observer"] = health
    if not health["eligible"]:
        return finish(2)

    session = GetOnlySession()
    key, secret = lab.credentials()
    client = AlpacaPaperClient(key, secret, session=session, timeout_seconds=lab.timeout_seconds)
    del key, secret
    start = (now - timedelta(days=args.lookback_days)).strftime("%Y-%m-%dT00:00:00Z")
    raw, token = [], None
    while True:
        page = client.stock_bars(args.symbol, timeframe="1Day", start=start, feed="iex", page_token=token)
        raw.extend(page.get("bars") or [])
        token = page.get("next_page_token")
        if not token:
            break
    bars = []
    for bar in raw:
        stamp = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
        if stamp.date() >= now.date():  # the runner's own closed-bar rule
            continue
        bars.append({"time": stamp.isoformat(), "open": bar["o"], "high": bar["h"], "low": bar["l"],
                     "close": bar["c"], "volume": bar["v"], "complete": True})
    args.out.mkdir(parents=True, exist_ok=True)
    snapshot = args.out / "observed_closed_bars.csv"
    with snapshot.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["DateTime", "Open", "High", "Low", "Close", "Volume"])
        for bar in bars:
            writer.writerow([bar["time"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]])
    record["http"] = {"methods": session.calls, "refused_non_get": session.refused,
                      "paths": sorted(set(session.paths)), "mutating_calls": 0}
    record["bars"] = {"count": len(bars), "first": bars[0]["time"] if bars else None,
                      "last": bars[-1]["time"] if bars else None,
                      "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()}
    try:
        policy = ModularPolicy.load(args.contract)
        observation = build_observation(policy.contract, bars, as_of=now)
        inference = policy.predict(observation)
    except ModularAdapterError as exc:
        record["gates"]["inference"] = {"eligible": False, "reason": str(exc)}
        return finish(2)
    (args.out / "inference.json").write_text(json.dumps(inference, indent=2, sort_keys=True) + "\n")
    record["inference_summary"] = {
        "last_closed_bar": inference["last_closed_bar"], "action": inference["action"],
        "forecast_log_return": inference["action_input"],
        "bottleneck_shape": inference["bottleneck"]["shape"],
        "input_sha256": inference["input_sha256"], "output_sha256": inference["output_sha256"]}
    record["verdict"] = "SMOKE_PASS_SHADOW_ONLY"
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    return finish(0)


def _bytes(path: Path) -> bytes:
    return Path(path).read_bytes()


if __name__ == "__main__":
    raise SystemExit(main())
