"""Runner tests: live-feed consumption, bar idempotency, staleness, restart.

Sockets are booby-trapped for the whole module: the running loop is proven
structurally incapable of a network submission.
"""
import json
import socket
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.demo_execution_runner import (
    DemoExecutionRunner,
    RunnerError,
    bar_time,
    build_capability,
)

NOW = datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted during L0")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


def _quote_db(tmp_path, observed_at, mid=2200.0):
    path = tmp_path / "alpaca-fixture.sqlite"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE IF NOT EXISTS quote_observations ("
        "session_id TEXT, symbol TEXT, broker_time TEXT, observed_at TEXT,"
        "bid REAL, ask REAL, mid REAL, spread REAL, spread_bps REAL,"
        "bid_size REAL, ask_size REAL, quote_json TEXT)"
    )
    con.execute(
        "INSERT INTO quote_observations VALUES "
        "('s-1','ETH/USD',?,?,?,?,?,0.5,2.3,1.0,1.0,'{\"raw\":1}')",
        (observed_at.isoformat(), observed_at.isoformat(),
         mid - 0.25, mid + 0.25, mid),
    )
    con.commit()
    con.close()
    return str(path)


def _runner_config(tmp_path, quote_db_path):
    return {
        "schema": "lts.demo_execution_runner.config.v1",
        "service": {
            "venue": "ibkr_paper",
            "account_fingerprint": "synthetic-ibkr-l0-test",
            "environment": "paper",
            "database_path": str(tmp_path / "l0.sqlite"),
            "risk_fraction_at_stop": 0.005,
            "max_overshoot_ratio": 0.25,
            "gross_notional_fraction_max": 0.10,
            "margin_fraction_max": 0.10,
            "daily_loss_budget_fraction": 0.02,
            "max_concurrent_positions": 3,
            "signal_max_age_seconds": 3900.0,
            "synthetic_equity": 100000.0,
            "owner_issuer_allowlist": ["owner-1"],
            "command_phrases": {"hold": "HOLD ALL DEMO TRADING NOW"},
            "asset_instrument_bindings": {"crypto:ETH/USD": "ETH.USD"},
        },
        "policy": {
            "cell_id": "crypto:ETH/USD@1h:mech:policy",
            "asset_id": "crypto:ETH/USD",
            "target_exposure_magnitude": 0.5,
            "stop_fraction": 0.01,
            "take_profit_fraction": 0.02,
            "validity_hours": 1.0,
            "policy_version": "0.1.0",
        },
        "quote_source": {
            "database_path": quote_db_path,
            "symbol": "ETH/USD",
            "instrument": "ETH.USD",
            "max_age_seconds": 900.0,
        },
        "capability_fixture": {
            "venue": "ibkr_paper",
            "account_fingerprint": "synthetic-ibkr-l0-test",
            "environment": "paper",
            "capability_evidence": "synthetic_fixture",
            "source_artifact_hash": "sha256:" + "1" * 64,
            "source_observed_at": "2026-08-02T00:00:00+00:00",
            "instruments": [{
                "instrument": "ETH.USD", "tradeable": True, "shortable": True,
                "min_units": 0.001, "unit_step": 0.001, "price_decimals": 2,
                "margin_rate": 0.3, "native_stop_loss": True,
                "native_take_profit": True, "native_bracket": True,
            }],
        },
        "bar_seconds": 3600,
        "loop_seconds": 60,
        "heartbeat_path": str(tmp_path / "hb" / "heartbeat.json"),
    }


def test_tick_produces_protected_would_be_decision_and_heartbeat(tmp_path):
    quotes = _quote_db(tmp_path, NOW - timedelta(seconds=30))
    runner = DemoExecutionRunner(_runner_config(tmp_path, quotes))
    heartbeat = runner.tick(now=NOW)
    assert heartbeat["outcome"] == "would_be_order"
    assert heartbeat["network_submissions_session"] == 0
    assert heartbeat["would_be_orders_session"] == 1
    assert heartbeat["capability_evidence"] == "synthetic_fixture"
    saved = json.loads(
        (tmp_path / "hb" / "heartbeat.json").read_text()
    )
    assert saved["outcome"] == "would_be_order"
    row = runner.service.olap._con.execute(
        "SELECT payload_json FROM decisions WHERE outcome='would_be_order'"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["bracket"] is not None
    assert payload["instrument"] == "ETH.USD"


def test_same_bar_is_idempotent_across_ticks_and_restart(tmp_path):
    quotes = _quote_db(tmp_path, NOW - timedelta(seconds=30))
    config = _runner_config(tmp_path, quotes)
    runner = DemoExecutionRunner(config)
    first = runner.tick(now=NOW)
    second = runner.tick(now=NOW + timedelta(minutes=1))
    assert first["outcome"] == "would_be_order"
    assert second["replayed"] is True
    runner.service.olap.close()
    reborn = DemoExecutionRunner(config)
    third = reborn.tick(now=NOW + timedelta(minutes=2))
    assert third["replayed"] is True  # restart replays the same bar decision
    count = reborn.service.olap._con.execute(
        "SELECT COUNT(*) FROM decisions"
    ).fetchone()[0]
    assert count == 1


def test_stale_quote_produces_no_decision(tmp_path):
    quotes = _quote_db(tmp_path, NOW - timedelta(hours=2))
    runner = DemoExecutionRunner(_runner_config(tmp_path, quotes))
    heartbeat = runner.tick(now=NOW)
    assert heartbeat["outcome"] == "quote_stale"
    count = runner.service.olap._con.execute(
        "SELECT COUNT(*) FROM decisions"
    ).fetchone()[0]
    assert count == 0


def test_missing_quote_source_degrades_not_crash(tmp_path):
    config = _runner_config(tmp_path, str(tmp_path / "absent.sqlite"))
    runner = DemoExecutionRunner(config)
    heartbeat = runner.tick(now=NOW)
    assert heartbeat["outcome"] == "no_quote_available"


def test_live_observed_fixture_is_refused_for_l0(tmp_path):
    fixture = _runner_config(tmp_path, "x")["capability_fixture"]
    fixture["capability_evidence"] = "live_observed"
    with pytest.raises(RunnerError, match="synthetic_fixture"):
        build_capability(fixture, NOW)


def test_finding_058_future_quote_rejected_with_alert(tmp_path):
    quotes = _quote_db(tmp_path, NOW + timedelta(hours=6))
    runner = DemoExecutionRunner(_runner_config(tmp_path, quotes))
    heartbeat = runner.tick(now=NOW)
    assert heartbeat["outcome"] == "quote_invalid_future_timestamp"
    assert "quote_future_timestamp" in heartbeat["alerts"]
    count = runner.service.olap._con.execute(
        "SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert count == 0


def test_finding_056_lifecycle_driver_prevents_saturation(tmp_path):
    quotes = _quote_db(tmp_path, NOW - timedelta(seconds=30))
    config = _runner_config(tmp_path, quotes)
    config["lifecycle_driver"] = {"enabled": True, "hold_bars": 1}
    runner = DemoExecutionRunner(config)

    progression = []
    first = runner.tick(now=NOW)
    assert first["outcome"] == "would_be_order"
    progression += [a.get("to") for a in first["lifecycle_advanced"]]
    second = runner.tick(now=NOW + timedelta(minutes=1))
    progression += [a.get("to") for a in second["lifecycle_advanced"]]
    third = runner.tick(now=NOW + timedelta(minutes=2))
    progression += [a.get("to") for a in third["lifecycle_advanced"]]
    assert progression[:2] == ["accepted", "filled"]   # deterministic order
    day = NOW.date().isoformat()
    totals = runner.service.olap.active_totals(day)
    assert totals["positions"] == 1.0                  # exposure, conserved

    # after the hold period the position closes and capacity frees;
    # the next bar produces a NEW decision instead of repeating a cap
    # rejection (the saturation Musashi observed live)
    later = NOW + timedelta(hours=1, minutes=5)
    con = sqlite3.connect(quotes)
    con.execute(
        "INSERT INTO quote_observations VALUES "
        "('s-2','ETH/USD',?,?,?,?,?,0.5,2.3,1.0,1.0,'{\"raw\":2}')",
        ((later - timedelta(seconds=20)).isoformat(),
         (later - timedelta(seconds=20)).isoformat(), 2199.75, 2200.25, 2200.0),
    )
    con.commit()
    con.close()
    fourth = runner.tick(now=later)
    assert any(a.get("to") == "position_closed"
               for a in fourth["lifecycle_advanced"])
    assert fourth["outcome"] == "would_be_order"       # new bar decided
    assert fourth["would_be_orders_session"] == 2
    assert runner.service.sink.network_submissions == 0


def test_runner_resumes_journaled_effects_at_startup(tmp_path, monkeypatch):
    """Finding 055 end-to-end: crash between kill acceptance and emission,
    then the next runner startup completes the effects automatically."""
    from trading_contracts import OwnerCommand
    quotes = _quote_db(tmp_path, NOW - timedelta(seconds=30))
    config = _runner_config(tmp_path, quotes)
    config["service"]["command_phrases"]["kill"] = "KILL ALL DEMO TRADING NOW"
    runner = DemoExecutionRunner(config)
    runner.tick(now=NOW)

    def explode(intent):
        raise RuntimeError("crash during effect emission")

    monkeypatch.setattr(runner.service.sink, "serialize", explode)
    command = OwnerCommand(
        object_id="cmd-r055", as_of=NOW,
        producer={"name": "telegram.deterministic", "version": "0"},
        trace_id="t-1", command="kill", issuer_id="owner-1",
        exact_phrase="KILL ALL DEMO TRADING NOW", nonce="n-r055",
        expires_at=NOW + timedelta(minutes=5), idempotency_key="ck-r055",
    )
    with pytest.raises(RuntimeError):
        runner.service.apply_owner_command(command, now=NOW)
    runner.service.olap.close()

    reborn = DemoExecutionRunner(config)               # startup resume
    kinds = sorted(e["kind"] for e in reborn.resumed_effects)
    assert kinds == ["cancel"]                          # pending entry cancelled
    assert reborn.service.olap.get_state("effects_due", "") == ""
    heartbeat = reborn.tick(now=NOW + timedelta(minutes=1))
    assert "halted:kill" in heartbeat["alerts"]


def test_bar_clock_is_deterministic():
    inside = datetime(2026, 8, 2, 12, 59, 59, tzinfo=timezone.utc)
    assert bar_time(inside, 3600) == datetime(
        2026, 8, 2, 12, 0, tzinfo=timezone.utc
    )
