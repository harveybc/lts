#!/usr/bin/env python3
"""Can the retained record settle an unknown MT5 effect? Read-only, and it says.

Correction of 2026-09-26. The first ruling let an ``effect_unknown`` leave that
state only through a LIVE read-side broker query. With the terminal silent that
is unsatisfiable, so an unknown effect would stay unknown forever and its route
blocked forever. The correction admits a second source — the lane's own retained
``account_snapshots`` and ``trade_events`` — under conditions strict enough that
the absence of data can never be read as the absence of an order.

This tool answers, for one real database, whether each unknown effect MEETS those
conditions. It is the dry, read-only half of the reconciliation: it never appends
an outcome, never reconciles anything and never contacts a broker or a terminal.
What it prints is the verdict the store would reach if an operator ran the
reconciliation, and the exact reason for every refusal.

Candidates are both:

* commands whose EFFECTIVE state is already ``effect_unknown``; and
* historical ``failed`` rows that the committed classifier types
  ``effect_unknown`` — i.e. the rows the migration has not yet corrected. A
  database running pre-fix code holds its unknown effects under the old label,
  and they are exactly the ones worth asking about.

Safety
------
* the database is opened ``file:<path>?mode=ro``, so it cannot be written even if
  this script were wrong, and no table is created;
* the continuity budget is never defaulted: give ``--max-gap-seconds`` or a
  ``--bridge-config`` whose ``stale_heartbeat_seconds`` declares it;
* the account fingerprint is masked in every output;
* nothing is started, stopped, restarted, retried or applied.

Usage
-----
    python tools/mt5_retained_reconciliation_report.py \\
        --database <path> --max-gap-seconds 180
    python tools/mt5_retained_reconciliation_report.py \\
        --database <path> --bridge-config <path> --json-report <path>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.mt5_execution_bridge import (  # noqa: E402
    read_retained_record_window,
    read_venue_break_command_rows,
)
from app.mt5_unknown_outcome import (  # noqa: E402
    QUERY_RETAINED_RECORDS,
    SOURCE_RETAINED_RECORDS,
    STATE_EFFECT_UNKNOWN,
    STATE_FAILED,
    ReconciliationInconclusive,
    observation_from_retained_records,
    outcome_for_historical_row,
    outcome_from_observation,
)
from app.mt5_venue_break import (  # noqa: E402
    VenueBreakUndeclarable,
    venue_break_from_observed_record,
)

REPORT_SCHEMA = "lts.mt5.retained_reconciliation_report.v1"

VERDICT_ADMITTED = "admissible_retained_records_settle_it"
VERDICT_INCONCLUSIVE = "inconclusive_retained_records_refuse"


def _mask(value: Any) -> str:
    text = str(value or "")
    if len(text) <= 4:
        return "****"
    return text[:4] + "*" * (len(text) - 4)


def _outcomes_table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND"
        " name='execution_command_outcomes'"
    ).fetchone()
    return row is not None


def candidates(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every command whose effect nobody has observed, under either label."""
    connection.row_factory = sqlite3.Row
    has_outcomes = _outcomes_table_exists(connection)
    rows = connection.execute(
        "SELECT * FROM execution_commands ORDER BY created_at,command_id"
    ).fetchall()
    latest: dict[str, str] = {}
    if has_outcomes:
        for row in connection.execute(
            "SELECT command_id,outcome FROM execution_command_outcomes o"
            " WHERE o.id = (SELECT MAX(x.id) FROM execution_command_outcomes x"
            " WHERE x.command_id = o.command_id)"
        ).fetchall():
            latest[str(row["command_id"])] = str(row["outcome"])
    found: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        command_id = str(row["command_id"])
        effective = latest.get(command_id, str(row["state"]))
        if effective == STATE_EFFECT_UNKNOWN:
            found.append({**row, "effective_state": effective,
                          "label_source": "stored_outcome_or_column"})
            continue
        if effective != STATE_FAILED:
            continue
        # A pre-fix database holds its unknown effects under the old label.
        outcome = outcome_for_historical_row({**row, "state": STATE_FAILED})
        if outcome.state == STATE_EFFECT_UNKNOWN:
            found.append({**row, "effective_state": effective,
                          "label_source": "classifier_on_historical_row",
                          "classifier_evidence": outcome.evidence})
    return found


def assess(
    connection: sqlite3.Connection,
    *,
    max_gap_seconds: float,
    now: datetime,
) -> list[dict[str, Any]]:
    """One verdict per candidate. Reads; decides nothing on its own."""
    verdicts: list[dict[str, Any]] = []
    for row in candidates(connection):
        record: dict[str, Any] = {
            "command_id": str(row["command_id"]),
            "symbol": str(row["symbol"]),
            "command_action": str(row["action"]),
            "recorded_state": str(row["state"]),
            "effective_state": row["effective_state"],
            "label_source": row["label_source"],
            "classifier_evidence": row.get("classifier_evidence"),
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }
        window = read_retained_record_window(
            connection, command_id=str(row["command_id"]),
            account_fingerprint=str(row["account_fingerprint"]),
            max_gap_seconds=max_gap_seconds)
        record["window"] = {
            "start": str(window.window_start),
            "end": str(window.window_end),
            "closed_by": window.closed_by,
            "observations_inside": len(window.observations),
            "bracketed_before": window.boundary_before is not None,
            "bracketed_after": window.boundary_after is not None,
            "trade_events": len(window.trade_events),
            "max_gap_budget_seconds": max_gap_seconds,
        }
        try:
            observation = observation_from_retained_records(window, now=now)
        except ReconciliationInconclusive as refusal:
            record["verdict"] = VERDICT_INCONCLUSIVE
            record["refusal"] = str(refusal)
            record["state_after"] = STATE_EFFECT_UNKNOWN
            record["holds_budget_slot_after"] = True
            verdicts.append(record)
            continue
        outcome = outcome_from_observation(observation)
        record["verdict"] = VERDICT_ADMITTED
        record["refusal"] = None
        record["query"] = observation.query
        record["reconciliation_source"] = SOURCE_RETAINED_RECORDS
        record["observed_at"] = observation.observed_at.isoformat()
        record["state_after"] = outcome.state
        record["evidence_after"] = outcome.evidence
        record["holds_budget_slot_after"] = outcome.consumes_budget_slot
        record["observation_detail"] = dict(observation.detail)
        verdicts.append(record)
    return verdicts


def venue_break(connection: sqlite3.Connection) -> dict[str, Any]:
    """The break the record itself establishes, or the reason it does not."""
    try:
        window = venue_break_from_observed_record(
            read_venue_break_command_rows(connection))
    except VenueBreakUndeclarable as refusal:
        return {"derived": False, "reason": str(refusal)}
    return {"derived": True, **window.as_fact()}


def render(verdicts: list[dict[str, Any]], breaks: dict[str, Any]) -> str:
    lines = ["", "MT5 retained-record reconciliation — READ-ONLY REPORT "
                 "(nothing written, nothing reconciled)",
             "=" * 78, ""]
    if not verdicts:
        lines.append("No command in this database has an unobserved effect "
                     "under either label.")
    for record in verdicts:
        lines.append(f"{record['command_id']}  {record['symbol']:<8}"
                     f" {record['command_action']:<11}"
                     f" recorded={record['recorded_state']}"
                     f" ({record['label_source']})")
        window = record["window"]
        lines.append(f"    window {window['start']} -> {window['end']}"
                     f"  closed_by={window['closed_by']}")
        lines.append(f"    observations inside={window['observations_inside']}"
                     f"  bracketed={window['bracketed_before']}/"
                     f"{window['bracketed_after']}"
                     f"  trade_events={window['trade_events']}"
                     f"  gap budget={window['max_gap_budget_seconds']}s")
        lines.append(f"    VERDICT {record['verdict']}")
        if record["refusal"]:
            lines.append(f"      refusal: {record['refusal']}")
        else:
            lines.append(f"      would record {record['state_after']} / "
                         f"{record['evidence_after']} from "
                         f"{QUERY_RETAINED_RECORDS} observed at "
                         f"{record['observed_at']}")
        lines.append(f"      state after: {record['state_after']}, holds "
                     f"budget slot: {record['holds_budget_slot_after']}")
        lines.append("")
    lines.append("venue break derived from this record: "
                 + json.dumps(breaks, sort_keys=True))
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report, read-only, whether the retained record settles "
                    "each unknown MT5 effect.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument(
        "--max-gap-seconds", type=float, default=None,
        help="the declared continuity budget in seconds; no default")
    parser.add_argument(
        "--bridge-config", type=Path, default=None,
        help="read the budget from this bridge config's "
             "stale_heartbeat_seconds instead")
    parser.add_argument("--json-report", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.database.exists():
        parser.error(f"no such database: {args.database}")
    if (args.max_gap_seconds is None) == (args.bridge_config is None):
        parser.error(
            "declare the continuity budget exactly once: --max-gap-seconds or "
            "--bridge-config. It is never defaulted, because a budget nobody "
            "declared is a guess")
    if args.bridge_config is not None:
        declared = json.loads(args.bridge_config.read_text(encoding="utf-8"))
        budget = declared.get("stale_heartbeat_seconds")
        if not isinstance(budget, (int, float)) or budget <= 0:
            parser.error("the bridge config declares no usable "
                         "stale_heartbeat_seconds")
        max_gap_seconds = float(budget)
        budget_source = "bridge_config.stale_heartbeat_seconds"
    else:
        max_gap_seconds = float(args.max_gap_seconds)
        budget_source = "command_line"

    now = datetime.now(timezone.utc)
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    try:
        verdicts = assess(connection, max_gap_seconds=max_gap_seconds, now=now)
        breaks = venue_break(connection)
        fingerprints = [
            _mask(row[0]) for row in connection.execute(
                "SELECT DISTINCT account_fingerprint FROM execution_commands"
            ).fetchall()
        ]
    finally:
        connection.close()

    print(render(verdicts, breaks))
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": now.isoformat(),
        "database": str(args.database),
        "mode": "read_only_report",
        "accounts_masked": fingerprints,
        "continuity_budget_seconds": max_gap_seconds,
        "continuity_budget_source": budget_source,
        "ruling": "an effect_unknown leaves that state through a live read-side "
                  "broker query or through the retained record under its own "
                  "conditions; every refusal keeps the state and the slot",
        "counts": {
            "candidates": len(verdicts),
            VERDICT_ADMITTED: sum(1 for v in verdicts
                                  if v["verdict"] == VERDICT_ADMITTED),
            VERDICT_INCONCLUSIVE: sum(1 for v in verdicts
                                      if v["verdict"] == VERDICT_INCONCLUSIVE),
        },
        "venue_break_from_observed_record": breaks,
        "rows": verdicts,
    }
    if args.json_report is not None:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8")
        print(f"report: {args.json_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
