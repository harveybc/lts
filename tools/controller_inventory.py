#!/usr/bin/env python3
"""J4: topology-aware, manifest-joined Paper/Demo controller inventory.

AUD-F2-20260806-133 corrections: every seat declares its EVIDENCE HOST
and paths; facts are collected from that host (SSH for remote seats);
unavailable remote evidence is reported as unavailable, never as
inactive. The heartbeat's artifact hash is JOINED to the selected-model
manifests on that same host, and SAC authority is true ONLY on exact
hash equality with an eligible SAC manifest. Controller classification
comes from the matching manifest's schema, never from a name substring
alone.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

STATE = "~/.local/state/lts"
MANIFEST_ROOT = "~/.local/share/prediction-provider/live"

# Deployment registry: seat -> evidence host (None = this host).
SEATS = {
    "ibkr_paper": {
        "host": None,
        "unit": "lts-ibkr-model-runner.service",
        "heartbeat": f"{STATE}/ibkr-model-runner-heartbeat.json",
    },
    "alpaca_paper": {
        "host": None,
        "unit": "lts-alpaca-model-runner.service",
        "heartbeat": f"{STATE}/alpaca-model-runner-heartbeat.json",
    },
    "mt5_demo": {
        "host": "dragon",
        "unit": "lts-mt5-model-runner.service",
        "heartbeat": f"{STATE}/mt5-model-runner-heartbeat.json",
    },
}

SAC_SCHEMA = "prediction_provider.live_sac_manifest.v1"
LINEAR_SCHEMA = "prediction_provider.live_linear_manifest.v1"


def _run_on(host: str | None, command: str, timeout: float = 20.0):
    """Run a command on the seat's evidence host. Returns (rc, stdout,
    stderr); an unreachable host returns rc=None (UNAVAILABLE)."""
    argv = (["bash", "-lc", command] if host is None else
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             host, command])
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
        return done.returncode, done.stdout, done.stderr
    except Exception as exc:
        return None, "", str(exc)


def _unit_state(host: str | None, unit: str) -> dict:
    rc, out, err = _run_on(
        host, f"systemctl --user show {unit} -p ActiveState -p SubState"
              " -p MainPID")
    if rc is None:
        return {"unavailable": f"host unreachable: {err[:120]}"}
    if rc != 0:
        return {"unavailable": f"systemctl exit {rc}: {err[:120]}"}
    return dict(line.split("=", 1) for line in out.splitlines()
                if "=" in line)


def _heartbeat(host: str | None, path: str,
               max_age_seconds: float) -> dict:
    rc, out, err = _run_on(host, f"cat {path}")
    if rc is None:
        return {"unavailable": f"host unreachable: {err[:120]}"}
    if rc != 0:
        return {"unavailable": f"no heartbeat at {path} ({err[:80]})"}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"unavailable": f"unreadable heartbeat: {exc}"}
    observed = payload.get("observed_at")
    age = None
    if observed:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(observed)).total_seconds()
        except ValueError:
            age = None
    inference = payload.get("inference") or {}
    return {
        "observed_at": observed,
        "age_seconds": age,
        "fresh": (age is not None and age <= max_age_seconds),
        "freshness_budget_seconds": max_age_seconds,
        "state": payload.get("state"),
        "model_id": payload.get("model_id") or inference.get("model_id"),
        "artifact_sha256": (payload.get("artifact_sha256")
                            or inference.get("artifact_sha256")),
        "config_sha256": (payload.get("config_sha256")
                          or inference.get("config_sha256")),
        "input_sha256": inference.get("input_sha256"),
        "instrument": payload.get("instrument")
        or inference.get("asset_id"),
        "positions": payload.get("positions"),
        "orders": payload.get("orders"),
        "selection_error": payload.get("selection_error"),
    }


def _manifests_on(host: str | None) -> list | dict:
    command = (
        f"for f in $(ls {MANIFEST_ROOT}/*/manifest.json 2>/dev/null);"
        " do echo \"===$f\"; cat \"$f\"; done")
    rc, out, err = _run_on(host, command)
    if rc is None:
        return {"unavailable": f"host unreachable: {err[:120]}"}
    manifests = []
    for chunk in out.split("===")[1:]:
        lines = chunk.splitlines()
        if not lines:
            continue
        path, body = lines[0], "\n".join(lines[1:])
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        manifests.append({
            "path": path,
            "schema": payload.get("schema"),
            "model_id": payload.get("model_id"),
            "artifact_sha256": payload.get("artifact_sha256"),
            "live_inference_eligible": payload.get(
                "live_inference_eligible"),
            "live_execution_eligible": payload.get(
                "live_execution_eligible"),
            "research_validated": payload.get("research_validated"),
        })
    return manifests


def _join_manifest(heartbeat: dict, manifests) -> dict:
    """Exact hash join: controller class and SAC authority come from
    the manifest whose artifact hash equals the heartbeat's."""
    artifact = heartbeat.get("artifact_sha256")
    if not artifact:
        # Named evidence gap: this runner's heartbeat publishes no
        # artifact hash, so no hash join is possible. A model_id join
        # is reported as UNVERIFIED context only; it can never grant
        # authority and the gap itself is the finding.
        model_id = heartbeat.get("model_id")
        weak = None
        if model_id and isinstance(manifests, list):
            weak = next((m for m in manifests
                         if m.get("model_id") == model_id), None)
        return {"controller_type": (
                    "linear_shadow_control_unverified"
                    if weak and weak.get("schema") == LINEAR_SCHEMA
                    else "unavailable"),
                "sac_champion_authoritative": "unavailable",
                "join": {
                    "gap": ("heartbeat publishes NO artifact hash;"
                            " hash join impossible — runner heartbeat"
                            " must be enriched"),
                    "model_id_join_unverified": (
                        weak.get("path") if weak else None),
                }}
    if isinstance(manifests, dict):                # unavailable
        return {"controller_type": "unclassified",
                "sac_champion_authoritative": "unavailable",
                "join": manifests.get("unavailable")}
    match = next((m for m in manifests
                  if m.get("artifact_sha256") == artifact), None)
    if match is None:
        return {"controller_type": "unclassified",
                "sac_champion_authoritative": False,
                "join": (f"artifact {artifact[:12]} matches no"
                         " manifest on the evidence host")}
    schema = match.get("schema")
    if schema == SAC_SCHEMA:
        authoritative = (
            match.get("live_execution_eligible") is True)
        return {"controller_type": "sac",
                "sac_champion_authoritative": authoritative,
                "join": {"manifest": match["path"],
                         "eligible": authoritative}}
    if schema == LINEAR_SCHEMA:
        return {"controller_type": "linear_shadow_control",
                "sac_champion_authoritative": False,
                "join": {"manifest": match["path"]}}
    return {"controller_type": f"other:{schema}",
            "sac_champion_authoritative": False,
            "join": {"manifest": match["path"]}}


def collect(run_on=_run_on, max_age_seconds: float = 3600.0) -> dict:
    global _run_on
    original = _run_on
    _run_on = run_on
    try:
        seats = {}
        for seat, spec in SEATS.items():
            host = spec["host"]
            heartbeat = _heartbeat(host, spec["heartbeat"],
                                   max_age_seconds)
            manifests = _manifests_on(host)
            join = _join_manifest(heartbeat, manifests)
            seats[seat] = {
                "evidence_host": host or os.uname().nodename,
                "evidence_paths": {
                    "heartbeat": spec["heartbeat"],
                    "manifest_root": MANIFEST_ROOT,
                },
                "evidence_time": datetime.now(
                    timezone.utc).isoformat(),
                "unit": _unit_state(host, spec["unit"]),
                "heartbeat": heartbeat,
                **join,
            }
        return {
            "schema": "lts.controller_inventory.v2",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "collector_host": os.uname().nodename,
            "seats": seats,
        }
    finally:
        _run_on = original


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-age-seconds", type=float, default=3600.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = collect(max_age_seconds=args.max_age_seconds)
    text = json.dumps(report, indent=1, sort_keys=True, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
