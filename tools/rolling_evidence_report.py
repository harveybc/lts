#!/usr/bin/env python3
"""Rolling 24-hour / 7-day business-evidence report (order C4).

One versioned, reproducible report per venue and model from persisted
facts only: due-bar decision coverage (C1 facts), decisions/HOLDs/
refusals/submissions, lifecycle outcomes, protected duration, reconnect
and incident counts, and unresolved facts. Periods are labeled exactly;
nothing is annualized; a value that is not observable in the ledgers is
reported as unavailable, never invented. Negative Paper/Demo profit is
acceptable evidence; missing lineage or protection is not.

Usage:
    python tools/rolling_evidence_report.py --config \
        examples/configs/rolling_evidence_report_v1.json \
        [--as-of ISO] [--output PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = "lts.rolling_evidence_report.v1"
WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
TIMEFRAME_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}


def _connect(path: Path) -> sqlite3.Connection | None:
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return None


def _count(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchone()[0]
    except sqlite3.Error:
        return None


def venue_window_report(conn: sqlite3.Connection, *, venue: str,
                        timeframe: str, since: str, until: str) -> dict:
    report: dict = {"period_start": since, "period_end": until,
                    "period_label": f"{since}..{until} (native window;"
                                    " not annualized)"}
    bar_seconds = TIMEFRAME_SECONDS.get(timeframe)
    if bar_seconds:
        span = (datetime.fromisoformat(until)
                - datetime.fromisoformat(since)).total_seconds()
        report["expected_bars_upper_bound"] = int(span // bar_seconds)
        report["expected_bars_note"] = (
            "calendar upper bound; venue sessions/weekends reduce it")
    decisions = {}
    try:
        decisions = dict(conn.execute(
            "SELECT outcome, COUNT(*) FROM due_bar_decisions"
            " WHERE venue=? AND bar_close >= ? AND bar_close < ?"
            " GROUP BY outcome", (venue, since, until)))
    except sqlite3.Error:
        report["due_bar_facts"] = "unavailable"
    else:
        report["due_bar_facts"] = {
            "delivered_bars": sum(decisions.values()),
            "by_outcome": decisions,
        }
        models = [row[0] for row in conn.execute(
            "SELECT DISTINCT model_id FROM due_bar_decisions"
            " WHERE venue=? AND bar_close >= ? AND bar_close < ?",
            (venue, since, until))]
        report["models"] = models
    report["ledger_decisions"] = {
        "total": _count(conn, "SELECT COUNT(*) FROM decisions"
                        " WHERE decided_at >= ? AND decided_at < ?",
                        (since, until)),
        "would_be_order": _count(
            conn, "SELECT COUNT(*) FROM decisions WHERE outcome="
            "'would_be_order' AND decided_at >= ? AND decided_at < ?",
            (since, until)),
        "rejected": _count(
            conn, "SELECT COUNT(*) FROM decisions WHERE outcome='rejected'"
            " AND decided_at >= ? AND decided_at < ?", (since, until)),
        "superseded": _count(
            conn, "SELECT COUNT(*) FROM decisions WHERE outcome="
            "'superseded' AND decided_at >= ? AND decided_at < ?",
            (since, until)),
    }
    effects = {}
    try:
        effects = dict(conn.execute(
            "SELECT state, COUNT(*) FROM l1_effects"
            " WHERE created_at >= ? AND created_at < ? GROUP BY state",
            (since, until)))
    except sqlite3.Error:
        pass
    report["lifecycles"] = {
        "created": sum(effects.values()),
        "by_state": effects,
    }
    protected = []
    try:
        for row in conn.execute(
            "SELECT effect_id, created_at, updated_at, state FROM"
            " l1_effects WHERE created_at >= ? AND created_at < ?",
                (since, until)):
            if str(row[3]).startswith("terminal_") or row[3] == "acknowledged":
                start = datetime.fromisoformat(row[1])
                end = datetime.fromisoformat(row[2])
                protected.append((end - start).total_seconds())
    except sqlite3.Error:
        pass
    if protected:
        report["lifecycle_duration_seconds"] = {
            "count": len(protected),
            "total": round(sum(protected), 1),
            "max": round(max(protected), 1),
        }
    fills = _count(conn, "SELECT COUNT(*) FROM l1_broker_facts WHERE"
                   " fact_kind IN ('position_reconciled',"
                   "'recovery_flatten_result') AND recorded_at >= ?"
                   " AND recorded_at < ?", (since, until))
    report["fill_or_reconcile_facts"] = fills
    report["connectivity_or_recovery_facts"] = _count(
        conn, "SELECT COUNT(*) FROM l1_broker_facts WHERE fact_kind IN"
        " ('suspect_cache', 'cache_convergence', 'recovery_hold',"
        "'protection_health_failure') AND recorded_at >= ?"
        " AND recorded_at < ?", (since, until))
    report["unresolved"] = {
        "nonterminal_effects": _count(
            conn, "SELECT COUNT(*) FROM l1_effects WHERE state NOT LIKE"
            " 'terminal_%'"),
        "open_exposures": _count(
            conn, "SELECT COUNT(*) FROM exposures WHERE state='open'"),
        "active_reservations": _count(
            conn, "SELECT COUNT(*) FROM reservations WHERE"
            " state='active'"),
        "halt": (conn.execute(
            "SELECT value FROM service_state WHERE key='halt'"
        ).fetchone() or [None])[0],
    }
    return report


def incident_counts(incident_db: Path, *, machine_or_venue: str,
                    since: str, until: str) -> dict | str:
    conn = _connect(incident_db)
    if conn is None:
        return "unavailable"
    try:
        rows = dict(conn.execute(
            "SELECT severity, COUNT(*) FROM incidents WHERE"
            " (venue_or_machine=? OR affected_object=?)"
            " AND first_observed_at >= ? AND first_observed_at < ?"
            " GROUP BY severity",
            (machine_or_venue, machine_or_venue, since, until)))
        return rows
    except sqlite3.Error:
        return "unavailable"
    finally:
        conn.close()


def build_report(config: dict, as_of: datetime) -> dict:
    report = {
        "schema": SCHEMA,
        "generated_at": as_of.isoformat(),
        "windows": {},
    }
    for label, span in WINDOWS.items():
        since = (as_of - span).isoformat()
        until = as_of.isoformat()
        venues: dict = {}
        for venue in config["venues"]:
            db_path = Path(os.path.expanduser(venue["execution_ledger"]))
            conn = _connect(db_path)
            if conn is None:
                venues[venue["venue"]] = {
                    "available": False,
                    "reason": f"ledger unreadable: {db_path.name}"}
                continue
            try:
                entry = venue_window_report(
                    conn, venue=venue["venue"],
                    timeframe=venue["timeframe"], since=since, until=until)
            finally:
                conn.close()
            entry["available"] = True
            entry["incidents"] = incident_counts(
                Path(os.path.expanduser(config["incident_ledger"])),
                machine_or_venue=venue.get("incident_object",
                                           venue["venue"]),
                since=since, until=until)
            venues[venue["venue"]] = entry
        report["windows"][label] = venues
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if config.get("schema") != "lts.rolling_evidence_config.v1":
        raise SystemExit("config schema mismatch")
    as_of = (datetime.fromisoformat(args.as_of) if args.as_of
             else datetime.now(timezone.utc))
    report = build_report(config, as_of)
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(json.dumps({"written": str(args.output)}))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
