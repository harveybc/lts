#!/usr/bin/env python3
"""Same-window simulation-versus-Paper/Demo comparison rows (WO2, F-P1-02).

This is the OPERATING wrapper around the existing deterministic
comparator ``tools/live_sim_replay.py`` — it is NOT a second comparator.
Every join, replay and residual fact comes from ``live_sim_replay``; this
tool only (a) types each joined fact as ``COMPARABLE`` or
``NOT_SUBTRACTABLE`` with reason codes, (b) persists one append-only row
per due decision keyed by (venue, seat, symbol, timeframe, due-bar
timestamp, model artifact hash, config hash, input-feature hash),
(c) appends completion rows when a lifecycle horizon closes (old rows are
NEVER edited), and (d) renders a seven-day rolling report whose numeric
aggregates are computed ONLY over comparable rows.

The 2026-08-10 mistake — subtracting shadow NAV from seat P&L — is
structurally impossible here: a numeric delta can only be requested
through :func:`subtractable_payload`, which raises
:class:`NotSubtractableError` for any refused row, and the report never
aggregates a refused row's numbers.

Telegram is never contacted directly: actionable per-seat STATE CHANGES
are handed to the fleet incident router (``agent-multi
tools/incident_ledger.py``), which owns dedup, recovery windows and
paging. The first observation of a seat initializes state silently.

Usage:
    python tools/sim_vs_live_window.py collect --config CFG \
        [--as-of ISO] [--no-emit] [--output PATH]
    python tools/sim_vs_live_window.py report --config CFG \
        [--as-of ISO] [--output PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CONFIG_SCHEMA = "lts.sim_vs_live_window_config.v1"
ROW_SCHEMA = "lts.sim_vs_live_comparison_row.v1"
REPORT_SCHEMA = "lts.sim_vs_live_rolling_report.v1"
STATE_SCHEMA = "lts.sim_vs_live_state.v1"

COMPARABLE = "COMPARABLE"
NOT_SUBTRACTABLE = "NOT_SUBTRACTABLE"

# Typed refusal reasons. Never invent an untyped one.
REASONS = frozenset({
    "NO_ASOF_BARS",
    "AS_OF_PENDING_UNRESOLVED",
    "AS_OF_LINEAGE_INCIDENT",
    "INPUT_LINEAGE_DIVERGENCE",
    "REPLAY_PIPELINE_REFUSED",
    "MODEL_IDENTITY_MISMATCH",
    "CONFIG_IDENTITY_MISMATCH",
    "ASSET_IDENTITY_MISMATCH",
    "TIMEFRAME_IDENTITY_MISMATCH",
    "ECONOMIC_ASSUMPTIONS_UNPINNED",
    "LEDGER_UNAVAILABLE",
    "NO_DUE_DECISION_IN_WINDOW",
})

_REJECT_ERROR_TO_REASON = {
    "timeframe mismatch": "TIMEFRAME_IDENTITY_MISMATCH",
    "model mismatch": "MODEL_IDENTITY_MISMATCH",
    "asset mismatch": "ASSET_IDENTITY_MISMATCH",
    "config mismatch": "CONFIG_IDENTITY_MISMATCH",
}

TIMEFRAME_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}

UNAVAILABLE = "unavailable"


class NotSubtractableError(RuntimeError):
    """Raised whenever a numeric delta is requested across a refused row."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def load_replay_module():
    """Load the existing comparator from this tool's own directory —
    reuse, never reimplementation."""
    if "live_sim_replay" in sys.modules:
        return sys.modules["live_sim_replay"]
    spec = importlib.util.spec_from_file_location(
        "live_sim_replay", Path(__file__).resolve().parent
        / "live_sim_replay.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_sim_replay"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- storage

_ROWS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS comparison_rows (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    schema TEXT NOT NULL,
    row_kind TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    venue TEXT NOT NULL,
    seat TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_close TEXT NOT NULL,
    model_artifact_sha256 TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    comparability TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    UNIQUE(row_kind, venue, seat, account_fingerprint, symbol, decision_id,
           timeframe, bar_close, model_artifact_sha256, config_sha256,
           input_sha256)
);
"""


def open_comparison_db(path: Path | str) -> sqlite3.Connection:
    path = _expand(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_ROWS_SCHEMA_SQL)
    conn.commit()
    return conn


def append_row(conn: sqlite3.Connection, *, row_kind: str, venue: str,
               seat: str, symbol: str, timeframe: str, bar_close: str,
               artifact_sha256: str, config_sha256: str, input_sha256: str,
               comparability: str, reasons: list[str],
               payload: dict[str, Any], now: datetime,
               account_fingerprint: str = UNAVAILABLE,
               decision_id: str = UNAVAILABLE) -> bool:
    """INSERT OR IGNORE on the full row identity: a restart or a repeated
    window can never duplicate a row, and an existing row is never
    edited. Returns True only when a NEW row was appended."""
    unknown = [reason for reason in reasons if reason not in REASONS]
    if unknown:
        raise ValueError(f"untyped refusal reason(s): {unknown}")
    if comparability == NOT_SUBTRACTABLE and not reasons:
        raise ValueError("NOT_SUBTRACTABLE requires at least one reason")
    if comparability == COMPARABLE and reasons:
        raise ValueError("COMPARABLE rows carry no refusal reasons")
    payload_json = json.dumps(payload, sort_keys=True,
                              separators=(",", ":"), default=str)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO comparison_rows"
        " (schema, row_kind, recorded_at, venue, seat, account_fingerprint,"
        "  symbol, decision_id, timeframe, bar_close,"
        "  model_artifact_sha256, config_sha256, input_sha256,"
        "  comparability, reason_codes_json, payload_json, payload_sha256)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ROW_SCHEMA, row_kind, now.isoformat(), venue, seat,
         account_fingerprint, symbol, decision_id, timeframe, bar_close,
         artifact_sha256, config_sha256, input_sha256, comparability,
         json.dumps(sorted(reasons)), payload_json,
         hashlib.sha256(payload_json.encode()).hexdigest()),
    )
    conn.commit()
    return cursor.rowcount == 1


def subtractable_payload(row: dict[str, Any]) -> dict[str, Any]:
    """The ONLY sanctioned path to a row's numbers for delta/aggregate
    computation. A refused row raises — a numeric delta across
    incomparable series is structurally impossible, not just discouraged."""
    if row["comparability"] != COMPARABLE:
        raise NotSubtractableError(
            "row is NOT_SUBTRACTABLE (" + row["reason_codes_json"] + ")")
    return json.loads(row["payload_json"])


# ---------------------------------------------------------------- collect

def expected_due_bar(now: datetime, timeframe: str) -> str:
    """Deterministic most-recent timeframe-grid boundary at ``now`` (UTC)
    — the identity key for a typed 'no due decision' refusal, so restarts
    dedup onto the same row."""
    seconds = TIMEFRAME_SECONDS.get(timeframe)
    if seconds is None:
        return f"unknown-grid:{timeframe}:{now.date().isoformat()}"
    epoch = int(now.timestamp())
    floored = epoch - (epoch % seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc).isoformat()


def _seat_replay_config(seat: dict[str, Any]) -> dict[str, Any]:
    venue_spec = {
        "venue": seat["venue"],
        "timeframe": seat["timeframe"],
        "instrument": seat["instrument"],
        "execution_ledger": seat["execution_ledger"],
    }
    for key in ("expected_asset_id", "expected_model_id",
                "expected_config_sha256", "bars_source", "bars_count",
                "artifact_file"):
        if seat.get(key) is not None:
            venue_spec[key] = seat[key]
    return {"schema": "lts.live_sim_replay_config.v1",
            "venues": [venue_spec]}


def classify_joined_row(entry: dict[str, Any],
                        seat: dict[str, Any]) -> tuple[str, list[str]]:
    """Type one lineage-joined replay row. Identity is already proven by
    the replay join (mismatches were rejected there); what remains is the
    as-of input lineage and the pinned economic assumptions.

    An as-of gap keeps the EXACT typed reason the comparator produced —
    a durable lineage incident and a still-pending linkage are not the
    same fact as 'no bars', and finding 260 was precisely the collapse of
    those three into one silent state.
    """
    reasons: list[str] = []
    if not seat.get("economic_assumptions"):
        reasons.append("ECONOMIC_ASSUMPTIONS_UNPINNED")
    replay_state = entry.get("replay")
    if replay_state == "unavailable":
        code = str(entry.get("reason_code") or "NO_ASOF_BARS")
        reasons.append(code if code in REASONS else "NO_ASOF_BARS")
    elif replay_state == "failed":
        reasons.append("REPLAY_PIPELINE_REFUSED")
    elif not entry.get("input_hash_matches"):
        reasons.append("INPUT_LINEAGE_DIVERGENCE")
    return (NOT_SUBTRACTABLE, sorted(reasons)) if reasons \
        else (COMPARABLE, [])


def _row_payload(entry: dict[str, Any], seat: dict[str, Any],
                 window: dict[str, str]) -> dict[str, Any]:
    simulation = {
        "replay": entry.get("replay"),
        "recomputed_input_sha256": entry.get("recomputed_input_sha256",
                                             UNAVAILABLE),
        "input_hash_matches": entry.get("input_hash_matches"),
        "sim_action_source": entry.get("sim_action_source", UNAVAILABLE),
        "economic_assumptions": seat.get("economic_assumptions")
        or UNAVAILABLE,
    }
    for key in ("sim_action", "sim_probability_up", "sim_action_matches",
                "reason", "reason_code", "as_of_source", "as_of_bars_sha256",
                "as_of_recorded_at"):
        if entry.get(key) is not None:
            simulation[key] = entry[key]
    # 260: the report names the durable incident instead of degrading into
    # generic missing data
    if entry.get("as_of_incident"):
        simulation["as_of_incident"] = entry["as_of_incident"]
    broker = {
        "lifecycle": entry.get("lifecycle"),
        "note": entry.get("note"),
        "quoted_spread": entry.get("quoted_spread", UNAVAILABLE),
        "entry_slippage_vs_mid": entry.get("entry_slippage_vs_mid",
                                           UNAVAILABLE),
        "fill_price": entry.get("fill_price", UNAVAILABLE),
        "fill_volume": entry.get("fill_volume", UNAVAILABLE),
        "fees": "unavailable: not journaled per effect",
    }
    return {
        "schema": ROW_SCHEMA,
        "decision": {
            "decision_id": entry.get("decision_id"),
            "decided_at": entry.get("decided_at"),
            "action": entry.get("action"),
            "outcome": entry.get("outcome"),
            "reason": entry.get("reason"),
            "proposed_geometry_json": entry.get("risk_envelope_json"),
            "quote_json": entry.get("quote_json"),
        },
        "simulation": simulation,
        "broker": broker,
        "window": window,
    }


def collect_seat(conn: sqlite3.Connection, seat: dict[str, Any],
                 now: datetime, replay_module) -> list[dict[str, Any]]:
    """One read-only pass for one seat: reuse ``build_replay`` over the
    seat's current window, type every row, append what is new."""
    produced: list[dict[str, Any]] = []
    seat_name = seat["seat"]
    lookback = float(seat.get("lookback_hours", 72))
    since = (now - timedelta(hours=lookback)).isoformat()
    until = now.isoformat()
    window = {"since": since, "until": until,
              "label": f"{since}..{until} (native window; descriptive"
                       " only; never annualized)"}

    def _record(row_kind, bar_close, artifact, config, input_hash,
                comparability, reasons, payload, *,
                account_fingerprint=UNAVAILABLE, decision_id=UNAVAILABLE):
        inserted = append_row(
            conn, row_kind=row_kind, venue=seat["venue"], seat=seat_name,
            symbol=seat["instrument"], timeframe=seat["timeframe"],
            bar_close=bar_close, artifact_sha256=artifact,
            config_sha256=config, input_sha256=input_hash,
            comparability=comparability, reasons=reasons,
            payload=payload, now=now,
            account_fingerprint=account_fingerprint,
            decision_id=decision_id)
        produced.append({
            "row_kind": row_kind, "seat": seat_name,
            "venue": seat["venue"], "symbol": seat["instrument"],
            "account_fingerprint": account_fingerprint,
            "decision_id": decision_id,
            "timeframe": seat["timeframe"], "bar_close": bar_close,
            "model_artifact_sha256": artifact, "config_sha256": config,
            "input_sha256": input_hash, "comparability": comparability,
            "reason_codes": reasons, "appended": inserted,
            "payload": payload,
        })

    ledger = _expand(seat["execution_ledger"])
    if not ledger.is_file():
        _record("window_refusal",
                expected_due_bar(now, seat["timeframe"]),
                UNAVAILABLE, UNAVAILABLE, UNAVAILABLE,
                NOT_SUBTRACTABLE, ["LEDGER_UNAVAILABLE"],
                {"schema": ROW_SCHEMA, "window": window,
                 "fact": f"execution ledger not readable: {ledger}"})
        return produced

    report = replay_module.build_replay(
        _seat_replay_config(seat), venue=seat["venue"], since=since,
        until=until)

    for rejected in report.get("decisions_rejected", []):
        reasons = sorted({
            _REJECT_ERROR_TO_REASON.get(error, "MODEL_IDENTITY_MISMATCH")
            for error in rejected.get("errors", [])
        })
        _record("decision", rejected.get("bar_close", UNAVAILABLE),
                rejected.get("artifact_sha256", UNAVAILABLE),
                rejected.get("config_sha256", UNAVAILABLE),
                rejected.get("input_sha256", UNAVAILABLE),
                NOT_SUBTRACTABLE, reasons,
                {"schema": ROW_SCHEMA, "window": window,
                 "decision": {"decision_id": rejected.get("decision_id")},
                 "identity_errors": rejected.get("errors", [])},
                decision_id=rejected.get("decision_id") or UNAVAILABLE)

    for entry in report.get("rows", []):
        comparability, reasons = classify_joined_row(entry, seat)
        _record("decision", entry["bar_close"], entry["artifact_sha256"],
                entry["config_sha256"], entry["input_sha256"],
                comparability, reasons, _row_payload(entry, seat, window),
                account_fingerprint=entry.get("account_fingerprint")
                or UNAVAILABLE,
                decision_id=entry.get("decision_id") or UNAVAILABLE)

    if not report.get("rows") and not report.get("decisions_rejected"):
        _record("window_refusal",
                expected_due_bar(now, seat["timeframe"]),
                UNAVAILABLE, UNAVAILABLE, UNAVAILABLE,
                NOT_SUBTRACTABLE, ["NO_DUE_DECISION_IN_WINDOW"],
                {"schema": ROW_SCHEMA, "window": window,
                 "fact": "no due-bar decision fact in this window for"
                         " this seat (a seat holding a position does not"
                         " emit new due decisions by design)"})
    return produced


def append_completions(conn: sqlite3.Connection, seat: dict[str, Any],
                       now: datetime, replay_module) -> list[dict[str, Any]]:
    """Later rows may complete earlier ones ONLY by appending completion
    rows. A decision row whose lifecycle has since reached a terminal
    state gains exactly one completion row; nothing is ever edited."""
    completed: list[dict[str, Any]] = []
    ledger = _expand(seat["execution_ledger"])
    if not ledger.is_file():
        return completed
    open_rows = conn.execute(
        "SELECT * FROM comparison_rows AS d WHERE d.row_kind='decision'"
        " AND d.seat=? AND NOT EXISTS (SELECT 1 FROM comparison_rows AS c"
        "  WHERE c.row_kind='completion' AND c.venue=d.venue"
        "  AND c.seat=d.seat AND c.symbol=d.symbol"
        "  AND c.account_fingerprint=d.account_fingerprint"
        "  AND c.decision_id=d.decision_id"
        "  AND c.timeframe=d.timeframe AND c.bar_close=d.bar_close"
        "  AND c.model_artifact_sha256=d.model_artifact_sha256"
        "  AND c.config_sha256=d.config_sha256"
        "  AND c.input_sha256=d.input_sha256)",
        (seat["seat"],),
    ).fetchall()
    if not open_rows:
        return completed
    execution_conn = replay_module._connect(ledger)
    try:
        for row in open_rows:
            payload = json.loads(row["payload_json"])
            decision = payload.get("decision") or {}
            lifecycle = (payload.get("broker") or {}).get("lifecycle")
            if not isinstance(lifecycle, dict):
                continue
            effect_id = lifecycle.get("effect_id")
            if not effect_id or str(effect_id).startswith("mt5-"):
                # MT5 close evidence is not yet lineage-joinable from the
                # command journal alone; typed absence, never guessed.
                continue
            stub = {
                "effect_or_command_id": effect_id,
                "decided_at": decision.get("decided_at")
                or row["recorded_at"],
                "quote_json": decision.get("quote_json"),
            }
            joined = replay_module.join_lifecycle(execution_conn, stub)
            state = (joined.get("lifecycle") or {}).get("state") \
                if isinstance(joined.get("lifecycle"), dict) else None
            if not state or not str(state).startswith("terminal_"):
                continue
            completion_payload = {
                "schema": ROW_SCHEMA,
                "completes": {"row_kind": "decision",
                              "bar_close": row["bar_close"]},
                "lifecycle": joined.get("lifecycle"),
                "entry_slippage_vs_mid": joined.get(
                    "entry_slippage_vs_mid", UNAVAILABLE),
                "quoted_spread": joined.get("quoted_spread", UNAVAILABLE),
                "realized": {
                    "holding_seconds": (joined.get("lifecycle") or {})
                    .get("holding_seconds", UNAVAILABLE),
                    "exit_reason": (joined.get("lifecycle") or {})
                    .get("exit_reason", UNAVAILABLE),
                    "realized_pnl": "unavailable: per-effect realized"
                                    " P&L is not journaled; seat session"
                                    " equity is a different series and is"
                                    " never subtracted here",
                },
            }
            inserted = append_row(
                conn, row_kind="completion", venue=row["venue"],
                seat=row["seat"], symbol=row["symbol"],
                account_fingerprint=row["account_fingerprint"],
                decision_id=row["decision_id"],
                timeframe=row["timeframe"], bar_close=row["bar_close"],
                artifact_sha256=row["model_artifact_sha256"],
                config_sha256=row["config_sha256"],
                input_sha256=row["input_sha256"],
                comparability=row["comparability"],
                reasons=json.loads(row["reason_codes_json"]),
                payload=completion_payload, now=now)
            if inserted:
                completed.append({"seat": row["seat"],
                                  "bar_close": row["bar_close"],
                                  "effect_id": effect_id,
                                  "state": state})
    finally:
        execution_conn.close()
    return completed


# ------------------------------------------------------- incident routing

def seat_state(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse one seat's freshly typed rows into one routing state:
    the newest decision row wins; a window refusal stands in when the
    window held no decision at all."""
    decisions = [r for r in rows if r["row_kind"] == "decision"]
    chosen = max(decisions, key=lambda r: r["bar_close"]) if decisions \
        else next((r for r in rows if r["row_kind"] == "window_refusal"),
                  None)
    if chosen is None:
        return {"state": "NO_ROWS", "reasons": []}
    if chosen["comparability"] == COMPARABLE:
        return {"state": COMPARABLE, "reasons": [],
                "bar_close": chosen["bar_close"]}
    return {"state": NOT_SUBTRACTABLE + ":"
            + ",".join(chosen["reason_codes"]),
            "reasons": chosen["reason_codes"],
            "bar_close": chosen["bar_close"]}


def emission_plan(previous: dict[str, Any], current: dict[str, Any],
                  ) -> list[dict[str, Any]]:
    """Emit ONLY on actionable state changes. First sight of a seat
    initializes silently; the incident router owns recovery windows and
    Telegram — this tool never pages directly."""
    emissions = []
    for seat, state in sorted(current.items()):
        prior = previous.get(seat)
        if prior is None or prior.get("state") == state["state"]:
            continue
        if state["state"] == COMPARABLE:
            emissions.append({
                "action": "recover", "event_code": "sim_vs_live_seat",
                "affected_object": seat,
                "evidence": {"from": prior.get("state"),
                             "to": state["state"],
                             "bar_close": state.get("bar_close")},
            })
        else:
            emissions.append({
                "action": "observe", "event_code": "sim_vs_live_seat",
                "severity": "P2", "affected_object": seat,
                "payload": {"from": prior.get("state"),
                            "to": state["state"],
                            "reasons": state.get("reasons", []),
                            "bar_close": state.get("bar_close")},
            })
    return emissions


def emit_via_incident_router(emissions: list[dict[str, Any]],
                             incident: dict[str, Any], now: datetime,
                             runner=subprocess.run) -> int:
    """Deliver through the fleet incident-ledger CLI; failures never kill
    the collector. Returns the number of failed deliveries."""
    script = _expand(incident["repo"]) / "tools" / "incident_ledger.py"
    failures = 0
    for emission in emissions:
        base = [sys.executable, str(script)]
        if incident.get("config"):
            base += ["--config", str(_expand(incident["config"]))]
        if incident.get("db_override"):
            base += ["--db", str(_expand(incident["db_override"]))]
        identity = ["--source", incident.get("source", "lts_sim_vs_live"),
                    "--front", incident.get("front", "front2"),
                    "--machine", incident.get("machine", "unknown"),
                    "--event-code", emission["event_code"],
                    "--object", emission["affected_object"]]
        if emission["action"] == "observe":
            command = base + [
                "observe", *identity,
                "--severity", emission.get("severity", "P2"),
                "--evidence-at", now.isoformat(),
                "--payload-json",
                json.dumps(emission["payload"], sort_keys=True)]
        else:
            command = base + [
                "recover", *identity,
                "--state-json",
                json.dumps(emission["evidence"], sort_keys=True)]
        try:
            result = runner(command, capture_output=True, text=True,
                            timeout=30)
            returncode, stderr = result.returncode, result.stderr or ""
        except Exception as exc:
            returncode, stderr = 1, str(exc)
        if returncode != 0:
            failures += 1
            print(f"incident emission failed"
                  f" ({emission['event_code']}): {stderr.strip()[:300]}",
                  file=sys.stderr)
    return failures


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("seats", {}) if data.get("schema") == STATE_SCHEMA \
        else {}


def save_state(path: Path, seats: dict[str, Any], now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": STATE_SCHEMA, "updated_at": now.isoformat(),
               "seats": seats}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- orchestration

def collect(config: dict[str, Any], *, now: datetime | None = None,
            replay_module=None, emit: bool = True,
            emit_runner=subprocess.run) -> dict[str, Any]:
    now = now or _utc_now()
    replay_module = replay_module or load_replay_module()
    conn = open_comparison_db(config["comparison_db"])
    summary: dict[str, Any] = {
        "schema": "lts.sim_vs_live_collect.v1",
        "generated_at": now.isoformat(),
        "seats": {}, "completions": [], "emissions": [],
    }
    current_states: dict[str, Any] = {}
    try:
        for seat in config["seats"]:
            rows = collect_seat(conn, seat, now, replay_module)
            completions = append_completions(conn, seat, now,
                                             replay_module)
            state = seat_state(rows)
            state["updated_at"] = now.isoformat()
            current_states[seat["seat"]] = state
            summary["seats"][seat["seat"]] = {
                "rows": rows, "state": state,
                "completions_appended": completions,
            }
            summary["completions"].extend(completions)
    finally:
        conn.close()

    state_path = _expand(config["state_file"])
    previous = load_state(state_path)
    emissions = emission_plan(previous, current_states)
    summary["emissions"] = emissions
    if emissions and emit and config.get("incident"):
        summary["emission_failures"] = emit_via_incident_router(
            emissions, config["incident"], now, runner=emit_runner)
    merged = dict(previous)
    merged.update(current_states)
    save_state(state_path, merged, now)
    return summary


# ------------------------------------------------------------------ report

def build_rolling_report(conn: sqlite3.Connection, *, as_of: datetime,
                         days: int = 7) -> dict[str, Any]:
    """Seven-day rolling summary per seat. Old rows never change; this is
    a pure read. Every numeric aggregate is computed ONLY over rows that
    pass :func:`subtractable_payload`."""
    since = (as_of - timedelta(days=days)).isoformat()
    until = as_of.isoformat()
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "generated_at": as_of.isoformat(),
        "period_start": since,
        "period_end": until,
        "period_label": f"{since}..{until} (native {days}-day window;"
                        " descriptive only; never annualized; append-only"
                        " source rows)",
        "seats": {},
    }
    seats = [row[0] for row in conn.execute(
        "SELECT DISTINCT seat FROM comparison_rows ORDER BY seat")]
    for seat in seats:
        rows = conn.execute(
            "SELECT * FROM comparison_rows WHERE seat=?"
            " AND recorded_at >= ? AND recorded_at < ?"
            " ORDER BY bar_close, seq", (seat, since, until),
        ).fetchall()
        decision_rows = [r for r in rows if r["row_kind"] == "decision"]
        refusal_rows = [r for r in rows
                        if r["row_kind"] == "window_refusal"]
        completion_rows = [r for r in rows
                           if r["row_kind"] == "completion"]
        comparable = [r for r in decision_rows
                      if r["comparability"] == COMPARABLE]
        refused = [r for r in decision_rows + refusal_rows
                   if r["comparability"] == NOT_SUBTRACTABLE]
        by_reason: dict[str, int] = {}
        incidents: dict[str, dict[str, Any]] = {}
        for row in refused:
            for reason in json.loads(row["reason_codes_json"]):
                by_reason[reason] = by_reason.get(reason, 0) + 1
            # 260: a lost/contradicted as-of lineage is REPORTED as its
            # durable incident, not as generic missing data
            incident = ((json.loads(row["payload_json"]).get("simulation")
                         or {}).get("as_of_incident"))
            if isinstance(incident, dict):
                key = str(incident.get("incident_key")
                          or incident.get("identity_sha256") or "unkeyed")
                entry = incidents.setdefault(key, {**incident, "rows": 0})
                entry["rows"] += 1
                entry.setdefault("bar_closes", []).append(row["bar_close"])

        match_evaluated = match_agreed = 0
        spreads: list[float] = []
        slippages: list[float] = []
        latencies: list[float] = []
        rejections = 0
        fills = 0
        no_fill_evidence = 0
        for row in comparable:
            payload = subtractable_payload(dict(row))
            simulation = payload.get("simulation", {})
            if simulation.get("sim_action_source") == "artifact_repredict":
                match_evaluated += 1
                if simulation.get("sim_action_matches"):
                    match_agreed += 1
            broker = payload.get("broker", {})
            decision = payload.get("decision", {})
            if decision.get("outcome") == "rejected":
                rejections += 1
            spread = broker.get("quoted_spread")
            if isinstance(spread, (int, float)):
                spreads.append(float(spread))
            slip = broker.get("entry_slippage_vs_mid")
            if isinstance(slip, (int, float)):
                slippages.append(float(slip))
                fills += 1
            elif decision.get("outcome") == "would_be_order":
                no_fill_evidence += 1
            lifecycle = broker.get("lifecycle")
            if isinstance(lifecycle, dict):
                latency = lifecycle.get("decision_to_effect_seconds")
                if isinstance(latency, (int, float)):
                    latencies.append(float(latency))

        def _mean(values):
            return round(sum(values) / len(values), 8) if values \
                else UNAVAILABLE

        report["seats"][seat] = {
            "comparable_rows": len(comparable),
            "not_subtractable_rows": len(refused),
            "not_subtractable_by_reason": dict(sorted(by_reason.items())),
            "as_of_lineage_incidents": [incidents[key]
                                        for key in sorted(incidents)],
            "completion_rows": len(completion_rows),
            "decision_match": {
                "evaluated": match_evaluated,
                "agreed": match_agreed,
                "rate": (round(match_agreed / match_evaluated, 4)
                         if match_evaluated else UNAVAILABLE),
                "note": "only comparable rows with a hash-verified"
                        " artifact re-prediction are evaluated",
            },
            "fill_quality": {
                "rows_with_fill_evidence": fills,
                "would_be_orders_without_fill_evidence": no_fill_evidence,
                "rejections": rejections,
                "mean_quoted_spread": _mean(spreads),
                "mean_entry_slippage_vs_mid": _mean(slippages),
                "max_abs_entry_slippage_vs_mid": (
                    round(max(abs(s) for s in slippages), 8)
                    if slippages else UNAVAILABLE),
                "mean_decision_to_effect_seconds": _mean(latencies),
                "note": "computed ONLY over comparable rows; refused rows"
                        " never enter any aggregate",
            },
        }
    return report


# --------------------------------------------------------------------- cli

def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema") != CONFIG_SCHEMA:
        raise SystemExit("config schema mismatch")
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("collect", "report"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", type=Path, required=True)
        cmd.add_argument("--as-of", default=None)
        cmd.add_argument("--output", type=Path, default=None)
    sub.choices["collect"].add_argument("--no-emit", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    as_of = (datetime.fromisoformat(args.as_of) if args.as_of
             else _utc_now())

    if args.mode == "collect":
        result = collect(config, now=as_of, emit=not args.no_emit)
    else:
        conn = open_comparison_db(config["comparison_db"])
        try:
            result = build_rolling_report(conn, as_of=as_of)
        finally:
            conn.close()

    text = json.dumps(result, indent=1, sort_keys=True, default=str)
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
