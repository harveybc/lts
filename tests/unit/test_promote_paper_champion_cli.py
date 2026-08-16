"""AUD-F2-20260816-257: the production entry point exists and is reached.

The finding: ``promote_paper_champion`` was called only from a unit-test
helper; the shipped CLI performed preflight and promoted nothing.

These tests drive ``tools/promote_paper_champion.py`` — the real,
installed, non-test entry point — through its whole call path:

    main() -> run() -> build_venue() -> venue.fetch_facts()
                    -> venue.bind_executor()
                    -> candidate_shadow_replay()
                    -> promote_paper_champion() -> the saga

The MT5 seat needs no injected transport at all: its direct fact source
IS the execution bridge database the terminal posts into, so this whole
path runs socket-free against a temporary bridge.

They also prove the entry point's two hard refusals: operator-supplied
broker truth is impossible (exit 2), and an incompatible candidate is
refused before any broker session is opened (exit 3) — which is exactly
what all three live seats do today.
"""
from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.promote_paper_champion import REFUSED_FACT_FLAGS, build_parser, main

from succession_fixtures import capability_payload, make_signer, sign, write_capability
from test_succession_venue_e2e import (       # the same real MT5 assembly
    MT5_ACCOUNT,
    _candidate,
    _linear_model,
    _mt5_config,
    _seed_seat,
)

from app.succession_venue import (
    Mt5SuccessionVenue,
    seat_contract_from_runner_config,
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("the succession CLI opened a socket")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)


def _write_candidate_descriptor(path: Path, candidate) -> Path:
    path.write_text(json.dumps(candidate.to_dict(), indent=1,
                               sort_keys=True) + "\n")
    return path


@pytest.fixture()
def seat_world(tmp_path):
    """A whole MT5 Demo seat on disk: bridge, manifest, models, ledger."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    incumbent = _linear_model(tmp_path, "eth-incumbent-v1",
                              "crypto:ETHUSD", "4h", intercept=1.0)
    challenger = _linear_model(tmp_path, "eth-challenger-v2",
                               "crypto:ETHUSD", "4h", intercept=-1.0)
    config = _mt5_config(tmp_path, incumbent)
    config_path = tmp_path / "runner_config.json"
    config_path.write_text(json.dumps(config, indent=1, sort_keys=True))

    # seed the seat's incumbent session and its own due bars, through the
    # real runner assembly, then let go of the ledger
    venue = Mt5SuccessionVenue.from_config(config)
    seat = seat_contract_from_runner_config(config)
    bars = venue.historical_closed_bars()
    _seed_seat(venue.runner.l0, seat, incumbent, MT5_ACCOUNT,
               [bar["time"] for bar in bars[-3:]], now)
    venue.close()

    candidate = _candidate(challenger, "crypto:ETHUSD", "4h")
    descriptor = _write_candidate_descriptor(
        tmp_path / "candidate.json", candidate)
    return {"config": config, "config_path": config_path, "seat": seat,
            "candidate": candidate, "descriptor": descriptor,
            "incumbent": incumbent, "challenger": challenger,
            "tmp_path": tmp_path, "now": now}


def _argv(world, *extra, action="promote"):
    return [
        "--runner-config", str(world["config_path"]),
        "--candidate-descriptor", str(world["descriptor"]),
        "--action", action,
        "--evidence-dir", str(world["tmp_path"] / "evidence"),
        "--json", str(world["tmp_path"] / "report.json"),
        *extra,
    ]


def _report(world) -> dict:
    return json.loads((world["tmp_path"] / "report.json").read_bytes())


# ── the two hard refusals ──────────────────────────────────────────────


def test_help_is_the_documented_entry_point():
    text = build_parser().format_help()
    assert "promote_paper_champion.py" in text
    assert "Broker truth is never operator-supplied" in text
    assert "--runner-config" in text
    for action in ("promote", "preflight", "status", "resume-complete",
                   "resume-rollback"):
        assert action in text


@pytest.mark.parametrize("flag", REFUSED_FACT_FLAGS)
def test_operator_supplied_broker_truth_is_refused(flag, capsys, tmp_path):
    """A ``--facts-file`` that bypasses the venue is exactly what the
    order forbids: here it cannot even parse."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--runner-config", str(tmp_path / "nope.json"),
              flag, str(tmp_path / "fake_facts.json")])
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    printed = captured.out + captured.err
    assert "OPERATOR_SUPPLIED_BROKER_TRUTH" in printed
    assert flag in printed


def test_incompatible_candidate_is_refused_before_any_broker_session(
        seat_world, monkeypatch, capsys):
    """Today's live reality on every seat: a 2660-dim SAC candidate
    against an 11-feature linear contract."""
    world = seat_world
    sac = dict(world["candidate"].to_dict())
    sac.update({
        "model_id": "p1lr-v2-shape-sac", "model_kind": "sb3_sac",
        "observation_dim": 2660,
        "feature_names": [f"feat_{index:02d}" for index in range(83)],
        "preprocessing_sha256": "a" * 64,
        "action": {"kind": "continuous_threshold", "threshold": 0.1,
                   "actions": ["long", "short", "hold"]},
    })
    descriptor = world["tmp_path"] / "sac_candidate.json"
    descriptor.write_text(json.dumps(sac, indent=1, sort_keys=True))
    world["descriptor"] = descriptor

    import app.succession_venue as venue_module

    def refuse(*_args, **_kwargs):
        raise AssertionError("a broker session was opened for an"
                             " incompatible candidate")

    monkeypatch.setattr(venue_module, "build_venue", refuse)
    monkeypatch.setattr("tools.promote_paper_champion.build_venue", refuse)

    assert main(_argv(world)) == 3
    report = _report(world)
    assert report["code"] == "CANDIDATE_INCOMPATIBLE"
    assert set(report["incompatibility_codes"]) >= {
        "OBSERVATION_DIM_MISMATCH", "NO_COMPATIBLE_FEATURE_PROVISIONING",
        "PREPROCESSING_CONTRACT_MISMATCH", "ACTION_CONTRACT_MISMATCH"}
    assert "no broker session was opened" in report["detail"]
    assert [stage["stage"] for stage in report["stages"]] == [
        "seat_and_candidate_loaded", "compatibility_pre_gate"]


# ── the production call path ───────────────────────────────────────────


def test_cli_reaches_the_orchestrator_and_refuses_without_a_capability(
        seat_world):
    """The whole path runs — facts, executor, shadow replay, orchestrator
    — and stops exactly where the owner gate is, printing the digests the
    owner must mint against."""
    world = seat_world
    store_dir = world["tmp_path"] / "promotion-store"
    store_dir.mkdir(mode=0o700)
    _, signers = make_signer(world["tmp_path"])
    assert main(_argv(world, "--capability-store", str(store_dir),
                      "--allowed-signers", str(signers),
                      "--no-root-pin")) == 3
    report = _report(world)
    assert report["code"] == "SUCCESSION_REFUSED"
    assert "no valid signed promotion capability" in report["detail"]
    stages = [stage["stage"] for stage in report["stages"]]
    assert stages == ["seat_and_candidate_loaded", "compatibility_pre_gate",
                      "seat_exclusivity", "direct_venue_facts",
                      "executor_bound", "shadow_replay"]
    facts_stage = report["stages"][3]
    assert facts_stage["source"] == "mt5_demo:execution_bridge:v2"
    assert facts_stage["balance_available"] is True
    assert "cash" not in facts_stage           # counts, never balances
    assert report["stages"][4]["executor"] == (
        "app.mt5_execution_bridge.Mt5ExecutionStore")
    binding = report["mint_binding"]
    assert binding["compatibility_report_sha256"]
    assert binding["shadow_report_sha256"]
    # the manifest is untouched by a refused promotion
    manifest = json.loads(Path(world["seat"].manifest_file).read_bytes())
    assert manifest["model_id"] == "eth-incumbent-v1"


def test_cli_promotes_end_to_end_with_an_owner_signed_capability(
        seat_world):
    """The acceptance proof: a NON-TEST inbound call path that reaches
    ``promote_paper_champion`` and switches a real seat manifest."""
    world = seat_world
    store_dir = world["tmp_path"] / "promotion-store"
    store_dir.mkdir(mode=0o700)
    key, signers = make_signer(world["tmp_path"])
    argv = _argv(world, "--capability-store", str(store_dir),
                 "--allowed-signers", str(signers), "--no-root-pin")

    # run 1: the owner learns the exact digests to bind
    assert main(argv) == 3
    binding = _report(world)["mint_binding"]

    payload = capability_payload(
        world["seat"], world["candidate"],
        (binding["compatibility_report_sha256"],
         binding["shadow_report_sha256"]),
        now=datetime.now(timezone.utc),
        incumbent_model_id=world["incumbent"]["model_id"],
        incumbent_artifact_sha256=world["incumbent"]["artifact_sha256"])
    path = write_capability(store_dir, "promotion.json", payload)
    sign(key, path)

    # run 2: the same command, now authorized
    assert main(argv) == 0
    report = _report(world)
    assert report["state"] == "promoted"
    assert report["promotion"]["saga_state"] == "completed"
    manifest = json.loads(Path(world["seat"].manifest_file).read_bytes())
    assert manifest["model_id"] == "eth-challenger-v2"
    assert manifest["artifact_sha256"] == (
        world["challenger"]["artifact_sha256"])
    assert manifest["live_execution_eligible"] is False

    # run 3: the same capability can never promote again
    assert main(argv) == 3
    assert "already consumed" in _report(world)["detail"] or (
        "no valid signed" in _report(world)["detail"])


def test_cli_status_and_resume_are_reachable(seat_world):
    world = seat_world
    assert main(_argv(world, action="status")) == 0
    report = _report(world)
    assert report["succession_pending"] is None
    assert report["outgoing_shadow"]["state"] == "none"
    assert main(_argv(world, action="resume-complete")) == 3
    assert _report(world)["code"] == "NO_OPEN_SAGA"


def test_cli_refuses_while_the_seat_runner_is_alive(seat_world):
    """A promotion never races the runner it is replacing."""
    world = seat_world
    heartbeat = Path(world["config"]["heartbeat_path"])
    heartbeat.write_text(json.dumps({
        "schema": "lts.mt5.model_runner.heartbeat.v1", "state": "monitoring",
        "observed_at": datetime.now(timezone.utc).isoformat()}))
    assert main(_argv(world)) == 3
    report = _report(world)
    assert report["code"] == "SEAT_HELD_BY_LIVE_RUNNER"
    assert report["live_runner"]["runner_state"] == "monitoring"
    # a stale heartbeat does not hold the seat
    heartbeat.write_text(json.dumps({
        "schema": "lts.mt5.model_runner.heartbeat.v1", "state": "monitoring",
        "observed_at": (datetime.now(timezone.utc)
                        - timedelta(hours=2)).isoformat()}))
    assert main(_argv(world)) == 3
    assert _report(world)["code"] == "SUCCESSION_REFUSED"


def test_cli_refuses_to_start_a_second_saga(seat_world, monkeypatch):
    """An interrupted promotion is resumed, never re-started."""
    world = seat_world
    from app.champion_succession import (
        SAGA_MANIFEST_PENDING, SAGA_SCHEMA_VERSION, ensure_saga_schema,
    )
    from app.ibkr_l1_journal import L1ExecutionOlap

    store = L1ExecutionOlap(world["config"]["service"]["database_path"])
    ensure_saga_schema(store)
    store._con.execute(
        "INSERT INTO promotion_saga (saga_id, schema_version, state, venue,"
        " account_fingerprint, instrument, timeframe, capability_sha256,"
        " nonce_sha256, capability_metadata_json, incumbent_session_id,"
        " incumbent_model_id, incumbent_artifact_sha256,"
        " incumbent_config_sha256, successor_model_id,"
        " successor_artifact_sha256, successor_config_sha256,"
        " carry_balance, carry_equity, manifest_file,"
        " manifest_previous_sha256, manifest_previous_bytes,"
        " manifest_target_sha256, manifest_target_bytes,"
        " outgoing_shadow_json, facts_json, audit_json, created_at,"
        " updated_at) VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("promotion-saga-fixture", SAGA_SCHEMA_VERSION,
         SAGA_MANIFEST_PENDING, "mt5_demo", MT5_ACCOUNT, "ETHUSD", "4h",
         "a" * 64, "b" * 64, "{}", "session-1", "eth-incumbent-v1",
         "1" * 64, "2" * 64, "eth-challenger-v2", "3" * 64, "4" * 64,
         1.0, 1.0, world["seat"].manifest_file,
         hashlib.sha256(b"previous").hexdigest(), b"previous",
         hashlib.sha256(b"target").hexdigest(), b"target",
         "{}", "{}", "{}", world["now"].isoformat(),
         world["now"].isoformat()))
    store.close()

    assert main(_argv(world)) == 3
    report = _report(world)
    assert report["code"] == "PROMOTION_SAGA_OPEN"
    assert report["succession"]["state"] == SAGA_MANIFEST_PENDING
    # and status reports the same split state
    assert main(_argv(world, action="status")) == 0
    assert _report(world)["succession_pending"]["split_authority"] is True
