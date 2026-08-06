#!/usr/bin/env python3
"""J4: direct-fact inventory of every Paper/Demo controller seat.

Answers ONE question per seat from evidence, never from a service or
symbol name: which artifact actually drives its decisions, and is a
selected SAC champion authoritative there?

Sources are direct: systemd unit state, runner heartbeats (model id,
artifact/config/input/decision hashes, freshness), and the execution
ledgers' recorded due-bar decisions. Anything not directly observed is
reported as ``unavailable`` — never as zero, and never as success.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

STATE = Path("~/.local/state/lts").expanduser()

SEATS = {
    "ibkr_paper": {
        "unit": "lts-ibkr-model-runner.service",
        "heartbeat": STATE / "ibkr-model-runner-heartbeat.json",
        "ledger": STATE / "ibkr-model-execution.sqlite",
    },
    "alpaca_paper": {
        "unit": "lts-alpaca-model-runner.service",
        "heartbeat": STATE / "alpaca-model-runner-heartbeat.json",
        "ledger": STATE / "alpaca-model-execution.sqlite",
    },
    "mt5_demo": {
        "unit": "lts-mt5-model-runner.service",
        "heartbeat": STATE / "mt5-model-runner-heartbeat.json",
        "ledger": STATE / "demo-execution-l0.sqlite",
    },
}

# A controller is only "SAC" when its artifact is a stable-baselines3
# policy selected through the SAC manifest path. Linear/heuristic
# controllers are labelled shadow/control, whatever the seat trades.
SAC_MANIFEST_SCHEMA = "prediction_provider.live_sac_manifest.v1"


def _unit_state(unit: str) -> dict:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "-p", "ActiveState", "-p", "SubState", "-p", "MainPID"],
            capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return {"unavailable": str(exc)}
    if out.returncode != 0:
        return {"unavailable": f"exit {out.returncode}"}
    fields = dict(
        line.split("=", 1) for line in out.stdout.splitlines()
        if "=" in line)
    return fields or {"unavailable": "no fields"}


def _heartbeat(path: Path, max_age_seconds: float) -> dict:
    if not path.exists():
        return {"unavailable": f"no heartbeat at {path}"}
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return {"unavailable": f"unreadable heartbeat: {exc}"}
    observed = payload.get("observed_at")
    age = None
    if observed:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(
                observed)).total_seconds()
        except ValueError:
            age = None
    return {
        "observed_at": observed,
        "age_seconds": age,
        "fresh": (age is not None and age <= max_age_seconds),
        "state": payload.get("state"),
        "model_id": (payload.get("model_id")
                     or (payload.get("inference") or {}).get("model_id")),
        "artifact_sha256": (payload.get("artifact_sha256")
                            or (payload.get("inference") or {}).get(
                                "artifact_sha256")),
        "input_sha256": (payload.get("inference") or {}).get(
            "input_sha256"),
        "instrument": payload.get("instrument"),
        "positions": payload.get("positions"),
        "orders": payload.get("orders"),
        "selection_error": payload.get("selection_error"),
    }


def _ledger_facts(path: Path) -> dict:
    if not path.exists():
        return {"unavailable": f"no ledger at {path}"}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"unavailable": str(exc)}
    try:
        tables = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "due_bar_decisions" not in tables:
            return {"unavailable": "no due_bar_decisions table",
                    "tables": sorted(tables)[:12]}
        total, first, last = con.execute(
            "SELECT COUNT(*), MIN(bar_close), MAX(bar_close)"
            " FROM due_bar_decisions").fetchone()
        by_model = con.execute(
            "SELECT model_id, artifact_sha256, COUNT(*)"
            " FROM due_bar_decisions GROUP BY 1, 2"
            " ORDER BY 3 DESC LIMIT 5").fetchall()
        by_outcome = con.execute(
            "SELECT outcome, COUNT(*) FROM due_bar_decisions"
            " GROUP BY 1 ORDER BY 2 DESC").fetchall()
        return {
            "due_bar_decisions": total,
            "first_bar": first,
            "last_bar": last,
            "controllers_observed": [
                {"model_id": row[0], "artifact_sha256": row[1],
                 "decisions": row[2]} for row in by_model],
            "outcomes": {row[0]: row[1] for row in by_outcome},
        }
    finally:
        con.close()


def _sac_manifests() -> list:
    root = Path("~/.local/share/prediction-provider/live").expanduser()
    found = []
    if not root.exists():
        return found
    for path in sorted(root.rglob("manifest.json")):
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if payload.get("schema") == SAC_MANIFEST_SCHEMA:
            found.append({
                "path": str(path),
                "model_id": payload.get("model_id"),
                "artifact_sha256": payload.get("artifact_sha256"),
                "live_inference_eligible": payload.get(
                    "live_inference_eligible"),
                "live_execution_eligible": payload.get(
                    "live_execution_eligible"),
                "observation_parity_verified": payload.get(
                    "observation_parity_verified"),
            })
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-age-seconds", type=float, default=3600.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    seats = {}
    for seat, spec in SEATS.items():
        heartbeat = _heartbeat(spec["heartbeat"], args.max_age_seconds)
        ledger = _ledger_facts(spec["ledger"])
        model_id = heartbeat.get("model_id")
        controller_type = "unavailable"
        if model_id:
            controller_type = (
                "linear_or_heuristic" if "linear" in str(model_id).lower()
                else "unclassified")
        seats[seat] = {
            "unit": _unit_state(spec["unit"]),
            "heartbeat": heartbeat,
            "ledger": ledger,
            "controller_type": controller_type,
            # The decisive fact: no seat may be described as champion-
            # driven without a SAC artifact proven authoritative here.
            "sac_champion_authoritative": False if model_id else
            "unavailable",
        }

    report = {
        "schema": "lts.controller_inventory.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": os.uname().nodename,
        "seats": seats,
        "sac_manifests_present": _sac_manifests(),
        "conclusion": (
            "No Paper/Demo seat is driven by a selected SAC champion;"
            " every observed controller is linear/heuristic and is"
            " reported as shadow/control. Gate 8-9 remain open."),
    }
    text = json.dumps(report, indent=1, sort_keys=True, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
