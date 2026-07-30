#!/usr/bin/env python3
"""Emit a sanitized evidence packet for the Hermes Paper/Shadow review."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ALPACA_DB = Path.home() / ".local/state/lts/alpaca-paper-lab.sqlite"
MONITOR_DB = Path.home() / ".local/state/lts/paper-execution-monitor.sqlite"
LATEST = Path.home() / ".local/state/lts/paper-execution-watchdog/latest.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _quote_summary(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT symbol,observed_at,mid,spread_bps
            FROM quote_observations
            WHERE observed_at >= ?
            ORDER BY symbol,observed_at
            """,
            (cutoff,),
        ).fetchall()
    finally:
        connection.close()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["symbol"]), []).append(row)
    result: list[dict[str, Any]] = []
    for symbol, observations in sorted(grouped.items()):
        mids = [float(row["mid"]) for row in observations if row["mid"]]
        spreads = [
            float(row["spread_bps"])
            for row in observations
            if row["spread_bps"] is not None
        ]
        result.append(
            {
                "symbol": symbol,
                "observations_24h": len(observations),
                "first_observed_at": observations[0]["observed_at"],
                "last_observed_at": observations[-1]["observed_at"],
                "mid_return_24h_sample": (
                    mids[-1] / mids[0] - 1.0 if len(mids) >= 2 and mids[0] else None
                ),
                "spread_bps_mean": (
                    sum(spreads) / len(spreads) if spreads else None
                ),
                "spread_bps_max": max(spreads) if spreads else None,
            }
        )
    return result


def _event_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"transitions_24h": 0, "by_transition": {}, "by_category": {}}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    connection = sqlite3.connect(path)
    try:
        transitions = dict(
            connection.execute(
                """
                SELECT transition,COUNT(*) FROM monitor_events
                WHERE observed_at >= ? GROUP BY transition
                """,
                (cutoff,),
            ).fetchall()
        )
        categories = dict(
            connection.execute(
                """
                SELECT category,COUNT(*) FROM monitor_events
                WHERE observed_at >= ? GROUP BY category
                """,
                (cutoff,),
            ).fetchall()
        )
    finally:
        connection.close()
    return {
        "transitions_24h": sum(int(value) for value in transitions.values()),
        "by_transition": transitions,
        "by_category": categories,
    }


def main() -> int:
    latest = _read_json(LATEST)
    if not latest:
        print(json.dumps({"wakeAgent": False, "reason": "watchdog_has_no_data"}))
        return 0
    packet = {
        "wakeAgent": True,
        "schema": "lts.hermes.paper_business_review.v1",
        "generated_at": _utc_now(),
        "evidence_only": True,
        "policy": {
            "orders_allowed": False,
            "risk_changes_allowed": False,
            "job_enqueue_allowed": False,
            "model_promotion_allowed": False,
            "human_review_required": True,
        },
        "current_status": {
            "generated_at": latest.get("generated_at"),
            "active_event_keys": latest.get("active_event_keys", []),
            "discussion_event_keys": latest.get("discussion_event_keys", []),
            "alpaca_session_status": (latest.get("alpaca") or {}).get("status"),
            "alpaca_complete_sessions": (latest.get("alpaca") or {}).get(
                "complete_sessions"
            ),
            "ibkr_online": (latest.get("ibkr") or {}).get("available"),
            "oanda_configured": (latest.get("oanda") or {}).get("configured"),
            "oanda_available": (latest.get("oanda") or {}).get("available"),
        },
        "quote_evidence_24h": _quote_summary(ALPACA_DB),
        "monitor_events_24h": _event_summary(MONITOR_DB),
    }
    print(json.dumps(packet, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
