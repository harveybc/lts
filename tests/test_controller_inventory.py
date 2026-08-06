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
FULL_SAC_MANIFEST = {
    "schema": "prediction_provider.live_sac_manifest.v1",
    "model_id": "eth-sac-champ-v9",
    "artifact_sha256": "a" * 64,
    "config_sha256": "c" * 64,
    "input_feature_sha256": "f" * 64,
    "preprocessing_sha256": "p" * 64,
    "manifest_sha256": "m" * 64,
    "live_inference_eligible": True,
    "live_execution_eligible": True,
    "observation_parity_verified": True,
}
SAC_MANIFEST_ELIGIBLE = json.dumps(FULL_SAC_MANIFEST)
FULL_SAC_HB = {
    "observed_at": NOW, "state": "monitoring",
    "model_id": "eth-sac-champ-v9",
    "artifact_sha256": "a" * 64,
    "config_sha256": "c" * 64,
    "input_feature_sha256": "f" * 64,
    "preprocessing_sha256": "p" * 64,
    "manifest_sha256": "m" * 64,
}


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


def _sac_responses(manifest=None, heartbeat=None, unit_active=True):
    manifest = manifest if manifest is not None else FULL_SAC_MANIFEST
    heartbeat = heartbeat if heartbeat is not None else FULL_SAC_HB
    state = "active" if unit_active else "inactive"
    return {
        ("dragon", "systemctl"): (
            0, f"ActiveState={state}\nSubState=running\n", ""),
        ("dragon", "cat"): (0, json.dumps(heartbeat), ""),
        ("dragon", "for f in"): (
            0, "===/m/manifest.json\n" + json.dumps(manifest), ""),
        (None, "systemctl"): (0, "ActiveState=active\n", ""),
        (None, "cat"): (1, "", ""),
        (None, "for f in"): (0, "", ""),
    }


def _seat(responses):
    return inv.collect(run_on=_fake_transport(responses))[
        "seats"]["mt5_demo"]


def test_full_exact_join_grants_authority():
    seat = _seat(_sac_responses())
    assert seat["controller_type"] == "sac"
    assert seat["sac_champion_authoritative"] is True
    assert all(v in ("match", True)
               for v in seat["join"]["checks"].values())


def test_stale_heartbeat_denies_authority():
    """Musashi reproducer `incomplete_authority_join`: a STALE
    heartbeat must never grant authority."""
    stale = dict(FULL_SAC_HB,
                 observed_at="2020-01-01T00:00:00+00:00")
    seat = _seat(_sac_responses(heartbeat=stale))
    assert seat["sac_champion_authoritative"] is not True
    assert any("not fresh" in r
               for r in seat["join"]["blocking_reasons"])


def test_inactive_unit_denies_authority():
    seat = _seat(_sac_responses(unit_active=False))
    assert seat["sac_champion_authoritative"] is not True
    assert any("not active" in r
               for r in seat["join"]["blocking_reasons"])


def test_config_or_input_hash_mismatch_denies_authority():
    for field in ("config_sha256", "input_feature_sha256",
                  "preprocessing_sha256", "manifest_sha256",
                  "model_id"):
        heartbeat = dict(FULL_SAC_HB, **{field: "WRONG"})
        seat = _seat(_sac_responses(heartbeat=heartbeat))
        assert seat["sac_champion_authoritative"] is False, field
        assert seat["join"]["checks"][field] == "mismatch"


def test_missing_fields_are_unavailable_never_true():
    heartbeat = {k: v for k, v in FULL_SAC_HB.items()
                 if k != "config_sha256"}
    seat = _seat(_sac_responses(heartbeat=heartbeat))
    assert seat["sac_champion_authoritative"] == "unavailable"
    assert seat["join"]["checks"]["config_sha256"] == "unavailable"


def test_failed_eligibility_predicates_deny_authority():
    for predicate in ("live_inference_eligible",
                      "live_execution_eligible",
                      "observation_parity_verified"):
        manifest = dict(FULL_SAC_MANIFEST, **{predicate: False})
        seat = _seat(_sac_responses(manifest=manifest))
        assert seat["sac_champion_authoritative"] is False, predicate
        assert any(predicate in r
                   for r in seat["join"]["blocking_reasons"])


def test_artifact_matching_no_manifest_is_not_authoritative():
    manifest = dict(FULL_SAC_MANIFEST, artifact_sha256="b" * 64)
    seat = _seat(_sac_responses(manifest=manifest))
    assert seat["sac_champion_authoritative"] is False
    assert seat["controller_type"] == "unclassified"
