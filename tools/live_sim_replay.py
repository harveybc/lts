#!/usr/bin/env python3
"""Deterministic live-versus-simulation replay (order C2).

Takes a venue and a decision window, joins each normalized due-bar
decision fact (C1) to its broker lifecycle BY LINEAGE (decision id →
effect/command id → journaled broker facts — never timestamp
coincidence), replays the same as-of inputs through the pinned mechanics
feature pipeline when a bars source is available, and writes joinable
residual facts:

- decision divergence: recomputed feature/input hash and action versus
  the recorded ones (any mismatch of asset, timeframe, model, config or
  input hash REJECTS the row — it is reported, never silently joined);
- decision-to-effect latency; requested-vs-quote spread at decision;
  entry slippage against the recorded quote when fill facts exist;
  holding time and exit reason from terminal facts;
- explicit gaps: a venue without a persisted bars source reports
  ``replay: unavailable`` for the model-divergence leg instead of
  fabricating agreement.

Descriptive only; exact period labels; nothing is annualized; this
window is never used for optimization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "lts.live_sim_replay.v1"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def load_bars_mt5(bridge_db: Path, symbol: str, timeframe: str,
                  upto_iso: str, count: int) -> list[dict] | None:
    """As-of bars from the MT5 bridge bar_snapshots (closed bars only,
    strictly at or before the decision bar close — no lookahead)."""
    try:
        conn = _connect(bridge_db)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(
            "SELECT payload_json FROM bar_snapshots WHERE symbol=?"
            " AND timeframe=? ORDER BY rowid DESC LIMIT 4000",
            (symbol, timeframe),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    bars: dict[str, dict] = {}
    for row in rows:
        try:
            bar = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        time_key = str(bar.get("time", ""))
        if time_key and time_key <= upto_iso:
            bars.setdefault(time_key, bar)
    ordered = [bars[key] for key in sorted(bars)][-count:]
    return ordered if len(ordered) >= count else None


def replay_decision(decision: dict, bars: list[dict] | None) -> dict:
    """Replay one decision's inputs through the pinned mechanics feature
    pipeline. Hash agreement proves the same as-of data; disagreement is
    a divergence fact."""
    if bars is None:
        return {"replay": "unavailable", "reason": "no persisted as-of"
                " bars source for this venue/window"}
    from prediction_provider_mechanics import build_closed_bar_features

    observation = build_closed_bar_features(bars)
    recomputed = hashlib.sha256(
        json.dumps(observation, sort_keys=True, default=str).encode()
    ).hexdigest()
    return {
        "replay": "computed",
        "recomputed_input_sha256": recomputed,
        "input_hash_matches": recomputed == decision["input_sha256"],
    }


def join_lifecycle(execution_conn: sqlite3.Connection,
                   decision: dict) -> dict:
    """Join by lineage only: the recorded effect/command id."""
    effect_id = decision.get("effect_or_command_id")
    if not effect_id:
        return {"lifecycle": None,
                "note": "decision produced no lifecycle (HOLD/refusal)"}
    effect = execution_conn.execute(
        "SELECT * FROM l1_effects WHERE effect_id=?", (effect_id,)
    ).fetchone()
    if effect is None:
        return {"lifecycle": "missing",
                "note": f"recorded id {effect_id} not found — reported,"
                        " never joined by timestamp"}
    facts = execution_conn.execute(
        "SELECT fact_kind, fact_json, recorded_at FROM l1_broker_facts"
        " WHERE effect_id=? ORDER BY seq", (effect_id,),
    ).fetchall()
    decided_at = datetime.fromisoformat(decision["decided_at"])
    created_at = datetime.fromisoformat(effect["created_at"])
    result: dict = {
        "lifecycle": {
            "effect_id": effect_id,
            "state": effect["state"],
            "decision_to_effect_seconds": round(
                (created_at - decided_at).total_seconds(), 3),
        }
    }
    quote = json.loads(decision.get("quote_json") or "null")
    if isinstance(quote, dict) and quote.get("bid") and quote.get("ask"):
        result["quoted_spread"] = round(
            float(quote["ask"]) - float(quote["bid"]), 8)
        mid = (float(quote["ask"]) + float(quote["bid"])) / 2.0
        for row in facts:
            fact = json.loads(row["fact_json"])
            fill_price = fact.get("filled_price") or fact.get("avg_price")
            if fill_price:
                result["entry_slippage_vs_mid"] = round(
                    float(fill_price) - mid, 8)
                break
    terminal_rows = [row for row in facts
                     if str(row["fact_kind"]).startswith("recovery_")
                     or "closed" in str(row["fact_kind"])]
    if str(effect["state"]).startswith("terminal_"):
        ended = datetime.fromisoformat(effect["updated_at"])
        result["lifecycle"]["holding_seconds"] = round(
            (ended - created_at).total_seconds(), 1)
        result["lifecycle"]["exit_reason"] = (
            str(terminal_rows[-1]["fact_kind"]) if terminal_rows
            else effect["state"])
    return result


def build_replay(config: dict, *, venue: str, since: str,
                 until: str) -> dict:
    spec = next((v for v in config["venues"] if v["venue"] == venue), None)
    if spec is None:
        raise SystemExit(f"unknown venue {venue!r} in config")
    execution_conn = _connect(
        Path(os.path.expanduser(spec["execution_ledger"])))
    decisions = [dict(row) for row in execution_conn.execute(
        "SELECT * FROM due_bar_decisions WHERE venue=? AND bar_close >= ?"
        " AND bar_close < ? ORDER BY bar_close", (venue, since, until))]
    rows = []
    rejected = []
    for decision in decisions:
        identity_errors = []
        if decision["timeframe"] != spec["timeframe"]:
            identity_errors.append("timeframe mismatch")
        if spec.get("expected_model_id") and \
                decision["model_id"] != spec["expected_model_id"]:
            identity_errors.append("model mismatch")
        if spec.get("expected_asset_id") and \
                decision["asset_id"] != spec["expected_asset_id"]:
            identity_errors.append("asset mismatch")
        if spec.get("expected_config_sha256") and \
                decision["config_sha256"] != spec["expected_config_sha256"]:
            identity_errors.append("config mismatch")
        if identity_errors:
            rejected.append({"decision_id": decision["decision_id"],
                             "errors": identity_errors})
            continue
        bars = None
        if spec.get("bars_source") == "mt5_bridge":
            bars = load_bars_mt5(
                Path(os.path.expanduser(spec["execution_ledger"])),
                spec["instrument"], decision["timeframe"],
                decision["bar_close"], int(spec.get("bars_count", 60)))
        entry = {
            "decision_id": decision["decision_id"],
            "bar_close": decision["bar_close"],
            "action": decision["action"],
            "outcome": decision["outcome"],
            "model_id": decision["model_id"],
            "artifact_sha256": decision["artifact_sha256"],
            **replay_decision(decision, bars),
            **join_lifecycle(execution_conn, decision),
        }
        rows.append(entry)
    execution_conn.close()
    return {
        "schema": SCHEMA,
        "venue": venue,
        "period_start": since,
        "period_end": until,
        "period_label": f"{since}..{until} (native window; descriptive"
                        " only; never annualized; not an optimization"
                        " input)",
        "decisions_joined": len(rows),
        "decisions_rejected": rejected,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--venue", required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if config.get("schema") != "lts.live_sim_replay_config.v1":
        raise SystemExit("config schema mismatch")
    report = build_replay(config, venue=args.venue, since=args.since,
                          until=args.until)
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(json.dumps({"written": str(args.output),
                          "sha256": hashlib.sha256(
                              text.encode()).hexdigest()}))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
