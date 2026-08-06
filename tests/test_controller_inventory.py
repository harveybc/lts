"""AUD-F2-20260806-133: topology-aware inventory with fake transport.

Reproduces the audited Dragon MT5 topology: the seat's evidence host is
Dragon, its runner is active and fresh, and Omega-local absence must
never be read as inactive.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import controller_inventory as inv


NOW = datetime.now(timezone.utc).isoformat()

DRAGON_HEARTBEAT = json.dumps({
    "observed_at": NOW, "state": "monitoring",
    "model_id": "ethusdt-4h-linear-live-v1",
    "positions": 1, "orders": 0,
})
SAC_HB = json.dumps({
    "observed_at": NOW, "state": "monitoring",
    "model_id": "eth-sac-champ-v9",
    "artifact_sha256": "a" * 64,
})
LINEAR_MANIFEST = json.dumps({
    "schema": "prediction_provider.live_linear_manifest.v1",
    "model_id": "ethusdt-4h-linear-live-v1",
    "artifact_sha256": "1" * 64,
})
SAC_MANIFEST_ELIGIBLE = json.dumps({
    "schema": "prediction_provider.live_sac_manifest.v1",
    "model_id": "eth-sac-champ-v9",
    "artifact_sha256": "a" * 64,
    "live_execution_eligible": True,
})


def _fake_transport(responses):
    def run_on(host, command, timeout=20.0):
        # Longest needle wins so "cat" cannot shadow the manifests
        # command, which also contains the word cat.
        for (want_host, needle), reply in sorted(
                responses.items(), key=lambda kv: -len(kv[0][1])):
            if host == want_host and needle in command:
                return reply
        return 1, "", "not found"
    return run_on


def test_dragon_mt5_seat_observed_remotely_not_inactive():
    responses = {
        ("dragon", "systemctl"): (0, "ActiveState=active\nSubState=running\nMainPID=42\n", ""),
        ("dragon", "cat"): (0, DRAGON_HEARTBEAT, ""),
        ("dragon", "for f in"): (
            0, "===/x/manifest.json\n" + LINEAR_MANIFEST, ""),
        (None, "systemctl"): (0, "ActiveState=active\nSubState=running\nMainPID=7\n", ""),
        (None, "cat"): (1, "", "no file"),
        (None, "for f in"): (0, "", ""),
    }
    report = inv.collect(run_on=_fake_transport(responses))
    seat = report["seats"]["mt5_demo"]
    assert seat["evidence_host"] == "dragon"
    assert seat["unit"]["ActiveState"] == "active"
    assert seat["heartbeat"]["model_id"] == "ethusdt-4h-linear-live-v1"
    assert seat["heartbeat"]["fresh"] is True
    # no artifact hash in that heartbeat -> named gap, never authority
    assert seat["sac_champion_authoritative"] == "unavailable"
    assert "NO artifact hash" in seat["join"]["gap"]


def test_unreachable_host_is_unavailable_never_inactive():
    responses = {
        (None, "systemctl"): (0, "ActiveState=active\n", ""),
        (None, "cat"): (1, "", "no file"),
        (None, "for f in"): (0, "", ""),
        ("dragon", "systemctl"): (None, "", "timeout"),
        ("dragon", "cat"): (None, "", "timeout"),
        ("dragon", "for f in"): (None, "", "timeout"),
    }
    report = inv.collect(run_on=_fake_transport(responses))
    seat = report["seats"]["mt5_demo"]
    assert "unavailable" in seat["unit"]
    assert "unavailable" in seat["heartbeat"]
    assert seat["sac_champion_authoritative"] == "unavailable"


def test_sac_authority_requires_exact_hash_and_eligibility():
    responses = {
        ("dragon", "systemctl"): (0, "ActiveState=active\n", ""),
        ("dragon", "cat"): (0, SAC_HB, ""),
        ("dragon", "for f in"): (
            0, "===/m/manifest.json\n" + SAC_MANIFEST_ELIGIBLE, ""),
        (None, "systemctl"): (0, "ActiveState=active\n", ""),
        (None, "cat"): (1, "", ""),
        (None, "for f in"): (0, "", ""),
    }
    report = inv.collect(run_on=_fake_transport(responses))
    seat = report["seats"]["mt5_demo"]
    assert seat["controller_type"] == "sac"
    assert seat["sac_champion_authoritative"] is True

    ineligible = json.loads(SAC_MANIFEST_ELIGIBLE)
    ineligible["live_execution_eligible"] = False
    responses[("dragon", "for f in")] = (
        0, "===/m/manifest.json\n" + json.dumps(ineligible), "")
    report = inv.collect(run_on=_fake_transport(responses))
    assert report["seats"]["mt5_demo"][
        "sac_champion_authoritative"] is False

    mismatched = json.loads(SAC_MANIFEST_ELIGIBLE)
    mismatched["artifact_sha256"] = "b" * 64
    responses[("dragon", "for f in")] = (
        0, "===/m/manifest.json\n" + json.dumps(mismatched), "")
    report = inv.collect(run_on=_fake_transport(responses))
    seat = report["seats"]["mt5_demo"]
    assert seat["sac_champion_authoritative"] is False
    assert seat["controller_type"] == "unclassified"
