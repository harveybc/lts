#!/usr/bin/env python3
"""Deterministic TWS Paper continuity monitor (Musashi order 2026-08-04 P0).

Combines process, API-port, functional-heartbeat, runner-restart and
execution-ledger evidence into normalized incident observations for the
fleet incident ledger. A listening port alone is never healthy evidence:
health requires the process, the port, a fresh heartbeat in a connected
state, and a consistent ledger, together.

Severity doctrine:

- TWS unavailable with a nonterminal effect or open exposure  -> P0
  (``broker unavailable with unresolved exposure``);
- TWS unavailable while the ledger proves flat                -> P1;
- decision clock stale while TWS is healthy                   -> P1;
- persistent runner restart loop while TWS is healthy         -> P1;
  a restart loop *during* a TWS outage stays inside the single
  tws_unavailable incident payload (one incident, never one message
  per restart).

Empty or unreadable broker/ledger evidence is always ``unknown`` and
therefore unresolved — never flat, never protected, never recovered.

The monitor is read-only toward TWS, the runner and the ledger. Its only
writes are its own state file and incident observations through the
agent-multi incident-ledger CLI. Exit 0 on a completed pass (healthy or
not), 1 when the pass itself failed (probe or emission error).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "lts.tws_continuity_monitor.v1"
DEFAULT_CONFIG = (
    Path(__file__).resolve().parent.parent
    / "examples/configs/tws_continuity_monitor_v1.json"
)

NONTERMINAL_EFFECT_STATES = (
    "journaled_pending", "submitted_pending_ack", "effect_unknown",
    "recovering", "acknowledged",
)
HEALTHY_HEARTBEAT_STATES = ("monitoring", "decided")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def load_config(path: Path | None) -> dict:
    config_path = path or DEFAULT_CONFIG
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("schema") != "lts.tws_continuity_config.v1":
        raise SystemExit(f"config schema mismatch: {config_path}")
    return config


# ---------------------------------------------------------------- probes


def probe_process(match_substrings: list[str]) -> dict:
    """Scan /proc cmdlines for the TWS java process."""
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cmdline = (pid_dir / "cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        text = cmdline.decode("utf-8", errors="replace")
        if all(part in text for part in match_substrings):
            return {"present": True, "pid": int(pid_dir.name)}
    return {"present": False, "pid": None}


def probe_port(host: str, port: int, timeout: float = 3.0) -> dict:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return {"listening": sock.connect_ex((host, port)) == 0}


def probe_heartbeat(path: Path, now: datetime) -> dict:
    try:
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"readable": False}
    observed_raw = heartbeat.get("observed_at")
    try:
        observed = datetime.fromisoformat(observed_raw)
        age = (now - observed).total_seconds()
    except (TypeError, ValueError):
        return {"readable": False}
    inference = heartbeat.get("inference") or {}
    return {
        "readable": True,
        "age_seconds": age,
        "state": heartbeat.get("state"),
        "schema": heartbeat.get("schema"),
        "account_fingerprint": heartbeat.get("account_fingerprint"),
        "last_closed_bar": inference.get("last_closed_bar"),
        "timeframe": inference.get("timeframe"),
        "error": heartbeat.get("error"),
        # Route facts a monitoring-state heartbeat must carry coherently
        # for the clock-omission exception (finding 106).
        "position": heartbeat.get("position"),
        "orders": heartbeat.get("orders"),
        "model_id": heartbeat.get("model_id"),
    }


def probe_restarts(unit: str) -> dict:
    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "NRestarts"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0 or "=" not in result.stdout:
        return {"readable": False}
    return {"readable": True,
            "n_restarts": int(result.stdout.strip().split("=", 1)[1])}


def probe_ledger(db_path: Path) -> dict:
    """Read-only execution-ledger consistency: halt, effects, exposure."""
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    except sqlite3.Error:
        return {"readable": False}
    try:
        halt_row = conn.execute(
            "SELECT value FROM service_state WHERE key='halt'").fetchone()
        effects = dict(conn.execute(
            "SELECT state, COUNT(*) FROM l1_effects GROUP BY state"))
        open_exposures = conn.execute(
            "SELECT COUNT(*) FROM exposures WHERE closed_at IS NULL"
        ).fetchone()[0]
    except sqlite3.Error:
        return {"readable": False}
    finally:
        conn.close()
    return {
        "readable": True,
        "halt": halt_row[0] if halt_row else None,
        "effect_states": effects,
        "open_exposures": open_exposures,
    }


# ------------------------------------------------------------ assessment


def exposure_state(ledger_probe: dict) -> str:
    """unknown | unresolved | flat — empty evidence is never flat."""
    if not ledger_probe.get("readable"):
        return "unknown"
    effects = ledger_probe.get("effect_states") or {}
    nonterminal = sum(
        count for state, count in effects.items()
        if state in NONTERMINAL_EFFECT_STATES
    )
    if nonterminal or ledger_probe.get("open_exposures"):
        return "unresolved"
    return "flat"


def session_state(probes: dict, heartbeat_connected: bool) -> str:
    """Classify the operator-visible TWS session without guessing auth state.

    A running Java process with no listening API socket is the common state
    when TWS is at its login screen, socket clients are disabled, or startup
    has not completed. The monitor cannot distinguish those cases safely, so
    it reports the exact bounded action they share.
    """
    if not probes["process"].get("present"):
        return "process_absent"
    if not probes["port"].get("listening"):
        return "login_or_api_required"
    if not heartbeat_connected:
        return "api_session_degraded"
    return "authenticated"


def outage_timing(now: datetime, previous_state: dict,
                  healthy: bool) -> tuple[str | None, float]:
    """Preserve the first observed outage time across repeated monitor runs."""
    if healthy:
        return None, 0.0
    started_raw = previous_state.get("outage_started_at")
    try:
        started = datetime.fromisoformat(started_raw)
        if started.tzinfo is None:
            raise ValueError("naive outage timestamp")
    except (TypeError, ValueError):
        started = now
    return iso(started), max(0.0, (now - started).total_seconds())


def assess(probes: dict, config: dict, now: datetime,
           previous_state: dict) -> tuple[list[dict], dict]:
    """Pure decision core: probes -> (emissions, next monitor state)."""
    emissions: list[dict] = []
    heartbeat = probes["heartbeat"]
    ledger_probe = probes["ledger"]
    restarts = probes["restarts"]
    exposure = exposure_state(ledger_probe)

    heartbeat_fresh = (
        heartbeat.get("readable")
        and heartbeat.get("schema") == config["heartbeat_schema"]
        and heartbeat.get("age_seconds", 1e9)
        <= float(config["heartbeat_max_age_seconds"])
    )
    heartbeat_connected = (
        heartbeat_fresh
        and heartbeat.get("state") in HEALTHY_HEARTBEAT_STATES
    )
    current_session_state = session_state(probes, heartbeat_connected)

    reasons = []
    if not probes["process"]["present"]:
        reasons.append("tws process absent")
    if not probes["port"]["listening"]:
        reasons.append("api port not listening")
    if not heartbeat.get("readable"):
        reasons.append("runner heartbeat unreadable")
    elif not heartbeat_fresh:
        reasons.append("runner heartbeat stale")
    elif not heartbeat_connected:
        reasons.append(
            f"runner heartbeat state {heartbeat.get('state')!r}")

    tws_healthy = not reasons
    outage_started_at, outage_age_seconds = outage_timing(
        now, previous_state, tws_healthy)

    restart_delta = None
    if restarts.get("readable"):
        last = previous_state.get("n_restarts")
        if last is not None:
            restart_delta = restarts["n_restarts"] - last

    identity = {
        "source": "tws_continuity_monitor",
        "front": "front2",
        "machine": config["machine"],
        "affected_object": config["venue_object"],
    }

    if not tws_healthy:
        severity = "P0" if exposure in ("unresolved", "unknown") else "P1"
        emissions.append({
            "action": "observe",
            "event_code": "tws_unavailable",
            "severity": severity,
            "payload": {
                "reasons": reasons,
                "exposure_state": exposure,
                "protection_state": (
                    "unknown" if exposure != "flat" else "flat-none-needed"),
                "halt": ledger_probe.get("halt"),
                "account_fingerprint": heartbeat.get("account_fingerprint"),
                "restart_count": restarts.get("n_restarts"),
                "restart_delta": restart_delta,
                "session_state": current_session_state,
                "outage_started_at": outage_started_at,
                "outage_age_seconds": round(outage_age_seconds, 1),
                "operator_action": config["operator_action"],
                "summary": "TWS Paper continuity lost: " + "; ".join(reasons),
            },
            **identity,
        })
    else:
        emissions.append({
            "action": "recover",
            "event_code": "tws_unavailable",
            "evidence": {
                "process_pid": probes["process"]["pid"],
                "port_listening": True,
                "heartbeat_state": heartbeat.get("state"),
                "heartbeat_age_seconds": round(
                    heartbeat.get("age_seconds", -1), 1),
                "session_state": current_session_state,
                "exposure_state": exposure,
                "halt": ledger_probe.get("halt"),
            },
            **identity,
        })

        # Decision clock: only meaningful while TWS itself is healthy.
        # Finding 106: omitting the inference clock is valid ONLY for the
        # explicitly enumerated `monitoring` state carrying coherent
        # current route facts (numeric position, integer order count and a
        # bound model id). A `decided`, unknown, malformed or partial
        # heartbeat with a missing clock is stale, never suppressed.
        timeframe = heartbeat.get("timeframe")
        last_closed_bar = heartbeat.get("last_closed_bar")
        bar_seconds = config["timeframe_seconds"].get(timeframe or "", None)
        if timeframe is None and last_closed_bar is None:
            route_coherent = (
                heartbeat.get("state") == "monitoring"
                and isinstance(heartbeat.get("position"), (int, float))
                and not isinstance(heartbeat.get("position"), bool)
                and isinstance(heartbeat.get("orders"), int)
                and not isinstance(heartbeat.get("orders"), bool)
                and bool(heartbeat.get("model_id"))
            )
            clock_stale = not route_coherent
        elif bar_seconds is not None and last_closed_bar:
            try:
                last_bar = datetime.fromisoformat(last_closed_bar)
                lag = (now - last_bar).total_seconds()
                clock_stale = lag > bar_seconds * float(
                    config["decision_clock_grace_factor"])
            except ValueError:
                clock_stale = True
        else:
            clock_stale = True    # unknown timeframe or half-missing clock
        if clock_stale:
            emissions.append({
                "action": "observe",
                "event_code": "decision_clock_stale",
                "severity": "P1",
                "payload": {
                    "last_closed_bar": heartbeat.get("last_closed_bar"),
                    "timeframe": heartbeat.get("timeframe"),
                    "exposure_state": exposure,
                    "summary": "model decision clock is stale",
                    "operator_action": config["operator_action_clock"],
                },
                **identity,
            })
        else:
            emissions.append({
                "action": "recover",
                "event_code": "decision_clock_stale",
                "evidence": {
                    "last_closed_bar": heartbeat.get("last_closed_bar"),
                    "timeframe": heartbeat.get("timeframe"),
                },
                **identity,
            })

        loop_threshold = int(config["restart_loop_threshold"])
        if restart_delta is not None and restart_delta >= loop_threshold:
            emissions.append({
                "action": "observe",
                "event_code": "runner_restart_loop",
                "severity": "P1",
                "payload": {
                    "restart_delta": restart_delta,
                    "restart_count": restarts.get("n_restarts"),
                    "exposure_state": exposure,
                    "summary": "runner is restart-looping while TWS is up",
                    "operator_action": config["operator_action_loop"],
                },
                **identity,
            })
        elif restart_delta == 0:
            emissions.append({
                "action": "recover",
                "event_code": "runner_restart_loop",
                "evidence": {"restart_delta": 0,
                             "restart_count": restarts.get("n_restarts")},
                **identity,
            })

    next_state = {
        "schema": SCHEMA,
        "checked_at": iso(now),
        "n_restarts": restarts.get("n_restarts",
                                   previous_state.get("n_restarts")),
        "tws_healthy": tws_healthy,
        "session_state": current_session_state,
        "outage_started_at": outage_started_at,
        "outage_age_seconds": round(outage_age_seconds, 1),
        "exposure_state": exposure,
    }
    return emissions, next_state


# -------------------------------------------------------------- emission


def emit(emissions: list[dict], config: dict, now: datetime,
         dry_run: bool = False) -> int:
    """Deliver emissions through the incident-ledger CLI. Returns the
    number of failures."""
    script = str(Path(config["incident_repo"]) / "tools"
                 / "incident_ledger.py")
    failures = 0
    for emission in emissions:
        base = [sys.executable, script]
        if config.get("incident_config"):
            base += ["--config", os.path.expanduser(
                config["incident_config"])]
        if config.get("incident_db_override"):
            base += ["--db", os.path.expanduser(
                config["incident_db_override"])]
        identity = ["--source", emission["source"],
                    "--front", emission["front"],
                    "--machine", emission["machine"],
                    "--event-code", emission["event_code"],
                    "--object", emission["affected_object"]]
        if emission["action"] == "observe":
            command = base + ["observe", *identity,
                              "--severity", emission["severity"],
                              "--evidence-at", iso(now),
                              "--payload-json",
                              json.dumps(emission["payload"],
                                         sort_keys=True)]
        else:
            command = base + ["recover", *identity,
                              "--state-json",
                              json.dumps(emission["evidence"],
                                         sort_keys=True)]
        if dry_run:
            print(json.dumps({"would_emit": emission["action"],
                              "event_code": emission["event_code"]},
                             sort_keys=True))
            continue
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=30)
        if result.returncode != 0:
            failures += 1
            print(f"emission failed ({emission['event_code']}):"
                  f" {result.stderr.strip()[:300]}", file=sys.stderr)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    now = utcnow()

    state_path = Path(os.path.expanduser(config["state_file"]))
    try:
        previous_state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous_state = {}

    try:
        probes = {
            "process": probe_process(config["process_match"]),
            "port": probe_port(config["api_host"], int(config["api_port"])),
            "heartbeat": probe_heartbeat(
                Path(os.path.expanduser(config["heartbeat_file"])), now),
            "restarts": probe_restarts(config["runner_unit"]),
            "ledger": probe_ledger(
                Path(os.path.expanduser(config["execution_ledger"]))),
        }
    except Exception as exc:
        print(f"probe failure: {exc}", file=sys.stderr)
        return 1

    emissions, next_state = assess(probes, config, now, previous_state)
    failures = emit(emissions, config, now, dry_run=args.dry_run)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(state_path.name + ".tmp")
    temporary.write_text(json.dumps(next_state, indent=2, sort_keys=True)
                         + "\n", encoding="utf-8")
    temporary.replace(state_path)
    print(json.dumps({"tws_healthy": next_state["tws_healthy"],
                      "exposure_state": next_state["exposure_state"],
                      "emissions": len(emissions),
                      "failures": failures}, sort_keys=True))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
