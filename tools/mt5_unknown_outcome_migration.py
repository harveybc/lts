#!/usr/bin/env python3
"""Re-read historical MT5 ``failed`` commands and append the corrections.

Owner grant of 2026-09-26. Until today ``Mt5ExecutionStore.complete`` wrote
``failed`` for every unsuccessful result, so the label carries no information
about whether an order exists: a market that was closed (proof that nothing was
placed) and a send that timed out (proof of nothing at all) are the same row.
Every such row is a candidate false negative, and a false negative here frees a
daily budget slot that a live position may still be occupying.

What this migration does
------------------------
For each row recorded ``failed`` it re-reads the row's OWN structured evidence
through ``app.mt5_unknown_outcome.outcome_for_historical_row``:

* a stored result whose retcode proves no order exists   -> ``failed`` confirmed
* a stored result whose retcode leaves it unproven       -> ``effect_unknown``
* a stored result with no machine code                   -> ``effect_unknown``
* a row that was never delivered to the EA               -> ``failed`` confirmed
* a row delivered with no result ever recorded           -> ``effect_unknown``
* a record that supports NEITHER reading                 -> ``effect_unknown``,
  and the appended row SAYS SO, in its evidence and its detail.

How it writes
-------------
It never rewrites a row. Each correction is APPENDED to
``execution_command_outcomes`` as a ``migration_correction`` event with its own
``recorded_at``; the read path prefers the latest record per command. A row
whose label is confirmed gets no record at all, which is what makes a second run
a no-op. Idempotency is by (command_id, migration_correction): a command that
already carries one is reported as ``already_corrected`` and left alone.

Safety
------
* the dry run (the default) opens the database READ-ONLY, so it cannot write
  even if this script were wrong;
* it prints every change it would make BEFORE any change is made;
* ``--apply`` requires ``--attest-bridge-stopped``, because appending to a
  database the live bridge has open is the operator's call and not this
  script's. This script never starts, stops or restarts anything.

Usage
-----
    python tools/mt5_unknown_outcome_migration.py --database <path>
    python tools/mt5_unknown_outcome_migration.py --database <path> \
        --json-report <path>
    python tools/mt5_unknown_outcome_migration.py --database <path> \
        --apply --attest-bridge-stopped
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.mt5_unknown_outcome import (  # noqa: E402
    EVENT_MIGRATION_CORRECTION,
    STATE_EFFECT_UNKNOWN,
    STATE_FAILED,
    Outcome,
    consumes_budget_slot,
    outcome_for_historical_row,
)

REPORT_SCHEMA = "lts.mt5.unknown_outcome_migration.v1"

ACTION_RELABEL_UNKNOWN = "relabel_as_effect_unknown"
ACTION_CONFIRM_FAILED = "confirmed_failed_no_record_written"
ACTION_ALREADY_CORRECTED = "already_corrected"
ACTION_OUT_OF_SCOPE = "out_of_scope_not_a_failed_row"


@dataclass(frozen=True)
class Verdict:
    """One row's reading, before anything is written."""

    command_id: str
    symbol: str
    action: str
    recorded_state: str
    proposed_state: str
    evidence: str
    migration_action: str
    outcome: Optional[Outcome] = None

    def as_row(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "symbol": self.symbol,
            "command_action": self.action,
            "recorded_state": self.recorded_state,
            "proposed_state": self.proposed_state,
            "evidence": self.evidence,
            "migration_action": self.migration_action,
            "holds_budget_slot_after": consumes_budget_slot(
                self.proposed_state),
        }


def _outcomes_table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND"
        " name='execution_command_outcomes'"
    ).fetchone()
    return row is not None


def _corrected_command_ids(connection: sqlite3.Connection) -> set[str]:
    if not _outcomes_table_exists(connection):
        return set()
    rows = connection.execute(
        "SELECT DISTINCT command_id FROM execution_command_outcomes"
        " WHERE event_kind=?", (EVENT_MIGRATION_CORRECTION,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def plan(connection: sqlite3.Connection) -> list[Verdict]:
    """Read the corpus and decide. Writes nothing, ever."""
    connection.row_factory = sqlite3.Row
    corrected = _corrected_command_ids(connection)
    verdicts: list[Verdict] = []
    rows = connection.execute(
        "SELECT * FROM execution_commands ORDER BY created_at,command_id"
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        command_id = str(row.get("command_id"))
        state = str(row.get("state"))
        common = {
            "command_id": command_id,
            "symbol": str(row.get("symbol")),
            "action": str(row.get("action")),
            "recorded_state": state,
        }
        if command_id in corrected:
            verdicts.append(Verdict(
                proposed_state=state, evidence="-",
                migration_action=ACTION_ALREADY_CORRECTED, **common))
            continue
        if state != STATE_FAILED:
            verdicts.append(Verdict(
                proposed_state=state, evidence="-",
                migration_action=ACTION_OUT_OF_SCOPE, **common))
            continue
        outcome = outcome_for_historical_row(row)
        action = (ACTION_RELABEL_UNKNOWN
                  if outcome.state == STATE_EFFECT_UNKNOWN
                  else ACTION_CONFIRM_FAILED)
        verdicts.append(Verdict(
            proposed_state=outcome.state, evidence=outcome.evidence,
            migration_action=action, outcome=outcome, **common))
    return verdicts


def counts(verdicts: list[Verdict]) -> dict[str, Any]:
    by_action: dict[str, int] = {}
    by_evidence: dict[str, int] = {}
    for verdict in verdicts:
        by_action[verdict.migration_action] = by_action.get(
            verdict.migration_action, 0) + 1
        if verdict.migration_action in {ACTION_RELABEL_UNKNOWN,
                                       ACTION_CONFIRM_FAILED}:
            by_evidence[verdict.evidence] = by_evidence.get(
                verdict.evidence, 0) + 1
    slots_recovered = sum(
        1 for verdict in verdicts
        if verdict.migration_action == ACTION_RELABEL_UNKNOWN
        and str(verdict.action) in {"open_long", "open_short"}
    )
    return {
        "commands_examined": len(verdicts),
        "by_migration_action": by_action,
        "by_evidence": by_evidence,
        "entry_budget_slots_reclaimed": slots_recovered,
    }


def render(verdicts: list[Verdict], *, applied: bool) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append("MT5 unknown-outcome migration — "
                 + ("APPLIED" if applied else "DRY RUN (nothing written)"))
    lines.append("=" * 72)
    changing = [v for v in verdicts
                if v.migration_action == ACTION_RELABEL_UNKNOWN]
    if changing:
        lines.append("")
        lines.append("Corrections " + ("appended:" if applied
                                       else "that WOULD be appended:"))
        for verdict in changing:
            lines.append(
                f"  {verdict.command_id}  {verdict.symbol:<8}"
                f" {verdict.recorded_state} -> {verdict.proposed_state}"
                f"   [{verdict.evidence}]")
    else:
        lines.append("")
        lines.append("No correction is required: no historical failed row is "
                     "ambiguous.")
    summary = counts(verdicts)
    lines.append("")
    lines.append(f"commands examined: {summary['commands_examined']}")
    for key in sorted(summary["by_migration_action"]):
        lines.append(f"  {key}: {summary['by_migration_action'][key]}")
    if summary["by_evidence"]:
        lines.append("evidence of the failed rows re-read:")
        for key in sorted(summary["by_evidence"]):
            lines.append(f"  {key}: {summary['by_evidence'][key]}")
    lines.append("entry budget slots reclaimed (now held, not free): "
                 f"{summary['entry_budget_slots_reclaimed']}")
    lines.append("")
    return "\n".join(lines)


def apply(database: Path, verdicts: list[Verdict]) -> list[dict[str, Any]]:
    """Append the corrections. Import is local so a dry run never touches the
    service module, and so a read-only run cannot construct a writer."""
    from app.mt5_execution_bridge import Mt5ExecutionStore

    store = Mt5ExecutionStore(database)
    written: list[dict[str, Any]] = []
    try:
        for verdict in verdicts:
            if verdict.migration_action != ACTION_RELABEL_UNKNOWN:
                continue
            record_id = store.record_migration_correction(
                command_id=verdict.command_id,
                outcome=verdict.outcome,
                previous_state=verdict.recorded_state,
            )
            written.append({"command_id": verdict.command_id,
                            "outcome_record_id": record_id,
                            "state": verdict.proposed_state,
                            "evidence": verdict.evidence})
    finally:
        store.connection.close()
    return written


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-label historical MT5 failed commands whose evidence "
                    "cannot tell a refusal before sending from a send that "
                    "was never confirmed.")
    parser.add_argument("--database", required=True, type=Path,
                        help="the MT5 execution bridge SQLite database")
    parser.add_argument("--apply", action="store_true",
                        help="append the corrections (default: dry run)")
    parser.add_argument(
        "--attest-bridge-stopped", action="store_true",
        help="operator attestation that the MT5 execution bridge is not "
             "running against this database; required with --apply")
    parser.add_argument("--json-report", type=Path, default=None,
                        help="write the machine-readable report here")
    args = parser.parse_args(argv)

    database = args.database
    if not database.exists():
        parser.error(f"no such database: {database}")
    if args.apply and not args.attest_bridge_stopped:
        parser.error(
            "--apply requires --attest-bridge-stopped: appending to a "
            "database the live bridge has open is the operator's decision, "
            "and this tool never starts or stops a service")

    read_only = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        verdicts = plan(read_only)
    finally:
        read_only.close()

    # The plan is printed BEFORE anything is written, in both modes.
    print(render(verdicts, applied=False))
    written: list[dict[str, Any]] = []
    if args.apply:
        written = apply(database, verdicts)
        print(f"appended {len(written)} correcting record(s)")

    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": str(database),
        "mode": "apply" if args.apply else "dry_run",
        "ruling": "an unknown effect consumes its daily budget slot; a slot is "
                  "released only by a positive observation that no order "
                  "exists",
        "counts": counts(verdicts),
        "rows": [verdict.as_row() for verdict in verdicts],
        "written": written,
    }
    if args.json_report is not None:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        print(f"report: {args.json_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
