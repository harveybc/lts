#!/usr/bin/env python3
"""Deterministic live-versus-simulation replay (order C2, WO2 v2 lineage).

Takes a venue and a decision window, joins each normalized due-bar
decision fact (C1) to its broker lifecycle BY LINEAGE (decision id →
effect/command id → journaled broker facts — never timestamp
coincidence), replays the same as-of inputs through the pinned mechanics
feature pipeline when a bars source is available, and writes joinable
residual facts:

- decision divergence: recomputed feature/input hash and (when the
  pinned model artifact is on disk and its SHA-256 matches the recorded
  one) the recomputed simulator action versus the recorded ones (any
  mismatch of asset, timeframe, model, config or input hash REJECTS the
  row — it is reported, never silently joined);
- decision-to-effect latency; requested-vs-quote spread at decision;
  entry slippage against the recorded quote when fill facts exist;
  holding time and exit reason from terminal facts;
- explicit, TYPED gaps: a decision whose as-of evidence is missing,
  merely pending, or contradicted names the reason (and the durable
  incident) instead of fabricating agreement or reporting generic
  missing data.

As-of bar sources:

- ``asof_ledger`` (default) — the append-only ``as_of_input_bars_v2``
  rows the IBKR/Alpaca runners bind to each due-decision identity
  (venue + account fingerprint + instrument + decision id), verified
  against the decision's model/artifact/config/timeframe/bar-close and
  input hash before a single number is derived from them;
- ``mt5_bridge`` — the MT5 bridge ``account_snapshots``/``bar_snapshots``
  tables: the newest snapshot at or before ``decided_at`` whose closed
  bars reproduce the recorded input hash (candidates are tried
  newest-first; a non-reproducing window is a divergence fact, never a
  silent join).

Descriptive only; exact period labels; nothing is annualized; this
window is never used for optimization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import as_of_lineage  # noqa: E402

SCHEMA = "lts.live_sim_replay.v2"

# typed as-of gaps, consumed by tools/sim_vs_live_window.py
NO_ASOF_BARS = "NO_ASOF_BARS"
AS_OF_PENDING_UNRESOLVED = "AS_OF_PENDING_UNRESOLVED"
AS_OF_LINEAGE_INCIDENT = "AS_OF_LINEAGE_INCIDENT"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _query(conn: sqlite3.Connection, sql: str, params=()) -> list:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


# ------------------------------------------------------------- as-of v2

def lineage_incidents(execution_conn: sqlite3.Connection,
                      ledger_path: Path) -> list[dict]:
    """Open as-of lineage incidents from BOTH durable sinks: the ledger's
    append-only incident table and the sidecar journal beside it (which is
    the only sink that survives a ledger that cannot be written)."""
    events = [dict(row) for row in _query(
        execution_conn,
        "SELECT event, recorded_at, incident_key, identity_sha256,"
        " reason_code, venue, account_fingerprint, instrument, decision_id,"
        " detail_json FROM as_of_lineage_incidents ORDER BY seq")]
    events.extend(as_of_lineage.read_journal(
        as_of_lineage.journal_path_for(ledger_path)))
    return as_of_lineage.open_incidents(events)


def load_bars_asof(execution_conn: sqlite3.Connection, decision: dict, *,
                   incidents: list[dict] | None = None) -> dict:
    """The exact bound as-of bar set for this due-decision identity.

    Returns ``{"bars": [...]}`` only when a BOUND row exists whose lineage
    equals the decision's. Every other outcome is a typed gap naming what
    is wrong — a pending-only linkage, a durable contradiction incident, or
    a plain absence — because finding 260's failure mode was exactly a
    silent slide into generic 'missing data'.
    """
    projected = as_of_lineage.identity_of_decision(decision)
    identity = projected["identity_sha256"]
    rows = _query(
        execution_conn,
        "SELECT row_state, model_id, artifact_sha256, config_sha256,"
        " timeframe, bar_close, input_sha256, feature_contract, bars_sha256,"
        " bars_json, source, recorded_at FROM as_of_input_bars_v2"
        " WHERE identity_sha256=?", (identity,))
    by_state = {str(row["row_state"]): row for row in rows}
    incident = next((item for item in (incidents or [])
                     if item.get("identity_sha256") == identity), None)
    if incident is not None:
        return {"bars": None, "reason_code": AS_OF_LINEAGE_INCIDENT,
                "incident": _incident_summary(incident),
                "reason": "as-of evidence for this due decision carries a"
                          " durable lineage incident"}
    bound = by_state.get(as_of_lineage.BOUND)
    if bound is None:
        if as_of_lineage.PENDING in by_state:
            return {"bars": None, "reason_code": AS_OF_PENDING_UNRESOLVED,
                    "reason": "as-of linkage is still PENDING: the bars were"
                              " captured but never bound to this decision"}
        return {"bars": None, "reason_code": NO_ASOF_BARS,
                "reason": "no persisted as-of bars for this decision identity"}
    drift = sorted(
        key for key in as_of_lineage.LINEAGE_FIELDS + ("input_sha256",)
        if str(bound[key]) != projected[key])
    if drift:
        return {"bars": None, "reason_code": AS_OF_LINEAGE_INCIDENT,
                "incident": {"reason_code": as_of_lineage.REASON_CONTRADICTION,
                             "identity_sha256": identity,
                             "detail": {"diverging_fields": drift}},
                "reason": "the bound as-of row disagrees with the decision"
                          f" lineage on {drift}"}
    try:
        bars = json.loads(bound["bars_json"])
    except (json.JSONDecodeError, TypeError):
        return {"bars": None, "reason_code": NO_ASOF_BARS,
                "reason": "bound as-of row is unreadable"}
    if not isinstance(bars, list) or not bars:
        return {"bars": None, "reason_code": NO_ASOF_BARS,
                "reason": "bound as-of row holds no bars"}
    return {"bars": bars, "as_of_source": bound["source"],
            "as_of_bars_sha256": bound["bars_sha256"],
            "as_of_recorded_at": bound["recorded_at"]}


def _incident_summary(incident: dict) -> dict:
    detail = incident.get("detail_json")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            detail = {"raw": detail[:400]}
    return {
        "incident_key": incident.get("incident_key"),
        "identity_sha256": incident.get("identity_sha256"),
        "reason_code": incident.get("reason_code"),
        "recorded_at": incident.get("recorded_at"),
        "decision_id": incident.get("decision_id"),
        "detail": detail if isinstance(detail, dict) else {},
    }


def load_bars_mt5(bridge_conn: sqlite3.Connection, *, symbol: str,
                  timeframe: str, decided_at: str, bar_close: str,
                  count: int, expected_input_sha256: str | None = None,
                  max_snapshots: int = 8) -> list[dict] | None:
    """As-of bars from the MT5 bridge snapshot tables (closed bars only,
    strictly at or before the decision bar close — no lookahead).

    The runner decided from the newest bridge snapshot available at
    ``decided_at``; candidate snapshots are tried newest-first and the
    first whose window reproduces ``expected_input_sha256`` wins. When
    none reproduces it, the newest complete candidate is returned so the
    divergence is REPORTED as an input-hash mismatch, never hidden."""
    from prediction_provider_mechanics import build_closed_bar_features

    snapshots = _query(
        bridge_conn,
        "SELECT id FROM account_snapshots WHERE received_at <= ?"
        " ORDER BY id DESC LIMIT ?",
        (_parse_time(decided_at).isoformat(), max_snapshots))
    close_limit = _parse_time(bar_close)
    fallback: list[dict] | None = None
    for snapshot in snapshots:
        rows = _query(
            bridge_conn,
            "SELECT bar_time, open, high, low, close, volume"
            " FROM bar_snapshots WHERE snapshot_id=? AND symbol=?"
            " AND timeframe=? ORDER BY bar_time",
            (snapshot["id"], symbol, timeframe))
        bars = [
            {"time": _parse_time(r["bar_time"]).isoformat(),
             "open": r["open"], "high": r["high"], "low": r["low"],
             "close": r["close"], "volume": r["volume"], "complete": True}
            for r in rows
            if _parse_time(r["bar_time"]) <= close_limit
        ]
        window = bars[-count:]
        if len(window) < count:
            continue
        if fallback is None:
            fallback = window
        if expected_input_sha256 is None:
            return window
        try:
            observation = build_closed_bar_features(window)
        except Exception:
            continue
        if observation["input_sha256"] == expected_input_sha256:
            return window
    return fallback


def _load_verified_artifact(artifact_file: str | None,
                            expected_sha256: str):
    """Load the pinned linear policy ONLY when its content hash equals the
    recorded decision artifact hash; anything else is typed, not used."""
    if not artifact_file:
        return None, "unavailable: no artifact_file configured"
    path = Path(os.path.expanduser(artifact_file))
    if not path.is_file():
        return None, f"unavailable: artifact file missing ({path.name})"
    from prediction_provider_mechanics import LiveLinearPolicy

    try:
        policy = LiveLinearPolicy.load(path, expected_sha256=expected_sha256)
    except Exception as exc:
        return None, f"unavailable: {exc}"[:200]
    return policy, None


def replay_decision(decision: dict, as_of: dict,
                    artifact_file: str | None = None) -> dict:
    """Replay one decision's inputs through the pinned mechanics feature
    pipeline. Hash agreement proves the same as-of data; disagreement is
    a divergence fact. When the pinned artifact is resolvable and hash-
    verified, the simulator action is truly recomputed (never inferred
    from determinism claims)."""
    bars = as_of.get("bars")
    if bars is None:
        gap = {"replay": "unavailable",
               "reason_code": as_of.get("reason_code", NO_ASOF_BARS),
               "reason": as_of.get("reason", "no persisted as-of bars source"
                                             " for this venue/window")}
        if as_of.get("incident"):
            gap["as_of_incident"] = as_of["incident"]
        return gap
    from prediction_provider_mechanics import build_closed_bar_features

    try:
        observation = build_closed_bar_features(bars)
    except Exception as exc:
        return {"replay": "failed",
                "reason": f"feature pipeline refused bars: {exc}"[:240]}
    recomputed = observation["input_sha256"]
    result = {
        "replay": "computed",
        "recomputed_input_sha256": recomputed,
        "input_hash_matches": recomputed == decision["input_sha256"],
    }
    for key in ("as_of_source", "as_of_bars_sha256", "as_of_recorded_at"):
        if as_of.get(key) is not None:
            result[key] = as_of[key]
    policy, why_not = _load_verified_artifact(
        artifact_file, decision["artifact_sha256"])
    if policy is None:
        result["sim_action_source"] = why_not
        return result
    try:
        sim = policy.predict(observation)
    except Exception as exc:
        result["sim_action_source"] = f"unavailable: predict failed:" \
                                      f" {exc}"[:200]
        return result
    result.update({
        "sim_action_source": "artifact_repredict",
        "sim_action": sim["action"],
        "sim_probability_up": sim["probability_up"],
        "sim_action_matches": sim["action"] == decision["action"],
    })
    return result


def _join_mt5_command(execution_conn: sqlite3.Connection, decision: dict,
                      command_id: str) -> dict:
    """MT5 lineage join: due-bar decision → execution command →
    result-linked trade events. Never a timestamp join."""
    rows = _query(execution_conn,
                  "SELECT * FROM execution_commands WHERE command_id=?",
                  (command_id,))
    if not rows:
        return {"lifecycle": "missing",
                "note": f"recorded id {command_id} not found — reported,"
                        " never joined by timestamp"}
    command = rows[0]
    decided_at = _parse_time(decision["decided_at"])
    created_at = _parse_time(command["created_at"])
    lifecycle: dict = {
        "effect_id": command_id,
        "state": command["state"],
        "decision_to_effect_seconds": round(
            (created_at - decided_at).total_seconds(), 3),
    }
    if command["completed_at"]:
        lifecycle["effect_to_completion_seconds"] = round(
            (_parse_time(command["completed_at"]) - created_at)
            .total_seconds(), 3)
    result: dict = {"lifecycle": lifecycle}
    payload = {}
    if command["result_json"]:
        try:
            payload = json.loads(command["result_json"])
        except json.JSONDecodeError:
            payload = {}
    if payload.get("result_code") is not None:
        lifecycle["broker_result_code"] = payload["result_code"]
    quote = json.loads(decision.get("quote_json") or "null")
    mid = None
    if isinstance(quote, dict) and quote.get("bid") and quote.get("ask"):
        result["quoted_spread"] = round(
            float(quote["ask"]) - float(quote["bid"]), 8)
        mid = (float(quote["ask"]) + float(quote["bid"])) / 2.0
    order_ticket = str(payload.get("order_ticket") or "")
    if order_ticket and order_ticket != "0":
        deals = _query(
            execution_conn,
            "SELECT price, volume, terminal_observed_at FROM trade_events"
            " WHERE order_ticket=? AND event_type='TRADE_TRANSACTION_DEAL_ADD'"
            " ORDER BY received_at LIMIT 1", (order_ticket,))
        if deals:
            deal = deals[0]
            result["fill_price"] = float(deal["price"])
            result["fill_volume"] = float(deal["volume"])
            if mid is not None:
                result["entry_slippage_vs_mid"] = round(
                    float(deal["price"]) - mid, 8)
    return result


def join_lifecycle(execution_conn: sqlite3.Connection,
                   decision: dict) -> dict:
    """Join by lineage only: the recorded effect/command id."""
    effect_id = decision.get("effect_or_command_id")
    if not effect_id:
        return {"lifecycle": None,
                "note": "decision produced no lifecycle (HOLD/refusal)"}
    if str(effect_id).startswith("mt5-"):
        return _join_mt5_command(execution_conn, decision, str(effect_id))
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
    ledger_path = Path(os.path.expanduser(spec["execution_ledger"]))
    execution_conn = _connect(ledger_path)
    # a ledger without the C1 table simply holds no due-bar facts — an
    # empty window is reported, never fabricated
    decisions = [dict(row) for row in _query(
        execution_conn,
        "SELECT * FROM due_bar_decisions WHERE venue=? AND bar_close >= ?"
        " AND bar_close < ? ORDER BY bar_close", (venue, since, until))]
    incidents = lineage_incidents(execution_conn, ledger_path)
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
            rejected.append({
                "decision_id": decision["decision_id"],
                "errors": identity_errors,
                "bar_close": decision["bar_close"],
                "timeframe": decision["timeframe"],
                "model_id": decision["model_id"],
                "artifact_sha256": decision["artifact_sha256"],
                "config_sha256": decision["config_sha256"],
                "input_sha256": decision["input_sha256"],
            })
            continue
        source = spec.get("bars_source", "asof_ledger")
        if source == "mt5_bridge":
            bars = load_bars_mt5(
                execution_conn,
                symbol=spec["instrument"],
                timeframe=decision["timeframe"],
                decided_at=decision["decided_at"],
                bar_close=decision["bar_close"],
                count=int(spec.get("bars_count", 60)),
                expected_input_sha256=decision["input_sha256"])
            as_of = ({"bars": bars, "as_of_source": "mt5_bridge_snapshot"}
                     if bars else
                     {"bars": None, "reason_code": NO_ASOF_BARS,
                      "reason": "no MT5 bridge snapshot at or before this"
                                " decision reproduces a complete window"})
        else:
            as_of = load_bars_asof(execution_conn, decision,
                                   incidents=incidents)
        entry = {
            "decision_id": decision["decision_id"],
            "bar_close": decision["bar_close"],
            "decided_at": decision["decided_at"],
            "action": decision["action"],
            "outcome": decision["outcome"],
            "reason": decision["reason"],
            "model_id": decision["model_id"],
            "artifact_sha256": decision["artifact_sha256"],
            "config_sha256": decision["config_sha256"],
            "input_sha256": decision["input_sha256"],
            "account_fingerprint": decision.get("account_fingerprint"),
            "instrument": decision.get("instrument"),
            "risk_envelope_json": decision.get("risk_envelope_json"),
            "quote_json": decision.get("quote_json"),
            **replay_decision(decision, as_of,
                              artifact_file=spec.get("artifact_file")),
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
        "as_of_lineage_incidents": [_incident_summary(item)
                                    for item in incidents],
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
