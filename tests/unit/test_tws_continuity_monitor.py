"""Adversarial proofs for the TWS continuity monitor (Musashi order §6:
items 10, 11, 12): process loss with exposure is P0, proven flat is P1,
a restart loop stays one incident, and empty broker/ledger evidence is
unknown — never flat, never protected, never recovered. A listening port
alone is never healthy evidence."""
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "tws_continuity_monitor",
    REPO_ROOT / "tools" / "tws_continuity_monitor.py")
monitor = importlib.util.module_from_spec(_SPEC)
sys.modules["tws_continuity_monitor"] = monitor
_SPEC.loader.exec_module(monitor)

NOW = datetime(2026, 8, 4, 18, 0, 0, tzinfo=timezone.utc)
CONFIG = {
    "schema": "lts.tws_continuity_config.v1",
    "machine": "omega",
    "venue_object": "ibkr_paper",
    "heartbeat_schema": "lts.ibkr.model_runner.heartbeat.v1",
    "heartbeat_max_age_seconds": 300,
    "timeframe_seconds": {"4h": 14400},
    "decision_clock_grace_factor": 2.5,
    "restart_loop_threshold": 5,
    "operator_action": "authenticate TWS Paper",
    "operator_action_clock": "inspect runner journal",
    "operator_action_loop": "inspect crash cause",
}


def healthy_probes(**overrides):
    probes = {
        "process": {"present": True, "pid": 1391469},
        "port": {"listening": True},
        "heartbeat": {
            "readable": True,
            "age_seconds": 30.0,
            "state": "monitoring",
            "schema": "lts.ibkr.model_runner.heartbeat.v1",
            "account_fingerprint": "c0ff137a3cc1a363",
            "last_closed_bar": "2026-08-04T12:00:00+00:00",
            "timeframe": "4h",
            "error": None,
        },
        "restarts": {"readable": True, "n_restarts": 10},
        "ledger": {
            "readable": True,
            "halt": None,
            "effect_states": {"terminal_flat": 2},
            "open_exposures": 0,
        },
    }
    probes.update(overrides)
    return probes


def _observed(emissions, event_code):
    return [e for e in emissions
            if e["action"] == "observe" and e["event_code"] == event_code]


def _recovered(emissions, event_code):
    return [e for e in emissions
            if e["action"] == "recover" and e["event_code"] == event_code]


def test_tws_loss_with_open_exposure_is_p0():
    probes = healthy_probes(
        process={"present": False, "pid": None},
        port={"listening": False},
        ledger={"readable": True, "halt": "hold",
                "effect_states": {"effect_unknown": 1, "terminal_flat": 1},
                "open_exposures": 1},
    )
    emissions, state = monitor.assess(probes, CONFIG, NOW, {})
    incident = _observed(emissions, "tws_unavailable")[0]
    assert incident["severity"] == "P0"
    assert incident["payload"]["exposure_state"] == "unresolved"
    assert not state["tws_healthy"]


def test_tws_loss_with_acknowledged_position_is_p0():
    probes = healthy_probes(
        process={"present": False, "pid": None},
        port={"listening": False},
        ledger={"readable": True, "halt": None,
                "effect_states": {"acknowledged": 1},
                "open_exposures": 1},
    )
    emissions, _ = monitor.assess(probes, CONFIG, NOW, {})
    assert _observed(emissions, "tws_unavailable")[0]["severity"] == "P0"


def test_tws_loss_proven_flat_is_p1():
    probes = healthy_probes(
        process={"present": False, "pid": None},
        port={"listening": False},
    )
    emissions, _ = monitor.assess(probes, CONFIG, NOW, {})
    incident = _observed(emissions, "tws_unavailable")[0]
    assert incident["severity"] == "P1"
    assert incident["payload"]["exposure_state"] == "flat"


def test_empty_ledger_evidence_is_unknown_never_flat():
    probes = healthy_probes(
        process={"present": False, "pid": None},
        port={"listening": False},
        ledger={"readable": False},
    )
    emissions, _ = monitor.assess(probes, CONFIG, NOW, {})
    incident = _observed(emissions, "tws_unavailable")[0]
    assert incident["severity"] == "P0"           # unknown escalates
    assert incident["payload"]["exposure_state"] == "unknown"
    assert incident["payload"]["protection_state"] == "unknown"


def test_listening_port_alone_is_not_healthy():
    probes = healthy_probes(
        heartbeat={"readable": True, "age_seconds": 40.0,
                   "state": "degraded_error",
                   "schema": "lts.ibkr.model_runner.heartbeat.v1",
                   "account_fingerprint": "c0ff137a3cc1a363",
                   "last_closed_bar": None, "timeframe": None,
                   "error": "ConnectionError: Not connected"},
    )
    emissions, state = monitor.assess(probes, CONFIG, NOW, {})
    assert _observed(emissions, "tws_unavailable")
    assert not state["tws_healthy"]


def test_stale_heartbeat_is_unhealthy_even_with_port():
    probes = healthy_probes()
    probes["heartbeat"]["age_seconds"] = 50000.0
    emissions, _ = monitor.assess(probes, CONFIG, NOW, {})
    incident = _observed(emissions, "tws_unavailable")[0]
    assert "stale" in " ".join(incident["payload"]["reasons"])


def test_restart_loop_during_outage_stays_one_incident():
    previous = {"n_restarts": 100}
    probes = healthy_probes(
        process={"present": False, "pid": None},
        port={"listening": False},
        restarts={"readable": True, "n_restarts": 2295},
    )
    emissions, _ = monitor.assess(probes, CONFIG, NOW, previous)
    assert len(_observed(emissions, "tws_unavailable")) == 1
    assert _observed(emissions, "runner_restart_loop") == []
    payload = _observed(emissions, "tws_unavailable")[0]["payload"]
    assert payload["restart_delta"] == 2195       # count kept in payload


def test_restart_loop_while_tws_up_is_p1():
    previous = {"n_restarts": 10}
    probes = healthy_probes(
        restarts={"readable": True, "n_restarts": 30})
    emissions, _ = monitor.assess(probes, CONFIG, NOW, previous)
    loop = _observed(emissions, "runner_restart_loop")[0]
    assert loop["severity"] == "P1"


def test_healthy_pass_recovers_all_codes():
    previous = {"n_restarts": 10}
    emissions, state = monitor.assess(healthy_probes(), CONFIG, NOW,
                                      previous)
    assert _recovered(emissions, "tws_unavailable")
    assert _recovered(emissions, "decision_clock_stale")
    assert _recovered(emissions, "runner_restart_loop")
    assert _observed(emissions, "tws_unavailable") == []
    assert state["tws_healthy"]
    evidence = _recovered(emissions, "tws_unavailable")[0]["evidence"]
    assert evidence["exposure_state"] == "flat"


def test_decision_clock_stale_while_tws_healthy_is_p1():
    probes = healthy_probes()
    probes["heartbeat"]["last_closed_bar"] = "2026-08-01T00:00:00+00:00"
    emissions, _ = monitor.assess(probes, CONFIG, NOW, {})
    stale = _observed(emissions, "decision_clock_stale")[0]
    assert stale["severity"] == "P1"


def test_unknown_timeframe_refuses_to_call_clock_healthy():
    probes = healthy_probes()
    probes["heartbeat"]["timeframe"] = "13m"
    emissions, _ = monitor.assess(probes, CONFIG, NOW, {})
    assert _observed(emissions, "decision_clock_stale")
