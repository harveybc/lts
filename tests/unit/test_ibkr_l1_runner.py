"""Milestone E: the disabled-by-default L1 canary runner.

Proves fail-closed construction (disabled config never touches a client;
the Milestone-F-less default factory degrades to an alert), deterministic
ledger-derived heartbeats/alerts/events, and a full heartbeat-driven canary
cycle on the fake client.
"""
import hashlib
import json
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_contracts import (
    AssetIntent,
    BrokerCapabilitySnapshot,
    InstrumentCapability,
    OwnerCommand,
)

from app.demo_execution_service import (
    DemoExecutionConfig,
    DemoExecutionService,
    ZeroNetworkSink,
)
from app.ibkr_l1_broker import FakeIbkrClient
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_l1_runner import (
    HEARTBEAT_SCHEMA,
    IbkrL1Runner,
    L1RunnerError,
    load_l1_runner_config,
)

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from mint_paper_capability import mint_payload, write_capability  # noqa: E402

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
ACCOUNT = "DU-TEST-1"
FINGERPRINT = hashlib.sha256(ACCOUNT.encode()).hexdigest()[:16]
ARTIFACT = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted in the L1 runner")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


def _service_config(tmp_path):
    return {
        "venue": "ibkr_paper",
        "account_fingerprint": FINGERPRINT,
        "environment": "paper",
        "database_path": str(tmp_path / "l1_demo.sqlite"),
        "risk_fraction_at_stop": 0.005,
        "max_overshoot_ratio": 0.25,
        "gross_notional_fraction_max": 0.10,
        "margin_fraction_max": 0.10,
        "daily_loss_budget_fraction": 0.02,
        "max_concurrent_positions": 3,
        "signal_max_age_seconds": 300.0,
        "owner_issuer_allowlist": ["owner-1"],
        "command_phrases": {
            "hold": "HOLD ALL DEMO TRADING NOW",
            "kill": "KILL ALL DEMO TRADING NOW",
            "flatten_all": "FLATTEN ALL DEMO POSITIONS NOW",
            "cancel_pending": "CANCEL ALL PENDING DEMO ENTRIES NOW",
        },
        "asset_instrument_bindings": {"fx:EUR/USD": "EUR.USD"},
    }


def _profile_file(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({
        "schema_version": "lts.ibkr.paper.l1.profile.v2",
        "venue": "ibkr_paper", "environment": "paper", "host": "127.0.0.1",
        "port": 7497, "client_id": 77,
        "account_fingerprint_algorithm": "account_id_sha256_16",
        "account_fingerprint": FINGERPRINT,
        "instrument": "EUR.USD", "asset_id": "fx:EUR/USD",
        "max_orders_this_activation": 2, "quantity_ceiling": 20000.0,
        "stop_distance_price_max": 0.0020,
        "take_profit_distance_price_max": 0.0040,
        "max_spread_price": 0.0003,
    }))
    return path


def _write_quote(db_path, *, observed_at, bid=1.08790, ask=1.08810):
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS quote_observations "
        "(symbol TEXT, mid REAL, bid REAL, ask REAL, observed_at TEXT,"
        " quote_json TEXT)"
    )
    con.execute(
        "INSERT INTO quote_observations VALUES (?,?,?,?,?,?)",
        ("EUR.USD", (bid + ask) / 2, bid, ask, observed_at.isoformat(), "{}"),
    )
    con.commit()
    con.close()


def _runner_config(tmp_path, enabled=True, **overrides):
    config = {
        "enabled": enabled,
        "service": _service_config(tmp_path),
        "profile_path": str(_profile_file(tmp_path)),
        "quote_database_path": str(tmp_path / "quotes.sqlite"),
        "quote_symbol": "EUR.USD",
        "quote_max_age_seconds": 60.0,
        "loop_seconds": 0.1,
        "heartbeat_path": str(tmp_path / "hb" / "heartbeat.json"),
        "price_decimals": 5,
        "quantity_decimals": 0,
        "capability_store_dir": str(tmp_path / "capstore"),
    }
    config.update(overrides)
    return config


class RunnerEnv:
    """L0 service + fake client + runner over one shared ledger."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.config = _runner_config(tmp_path)
        self.olap = L1ExecutionOlap(self.config["service"]["database_path"])
        self.service = DemoExecutionService(
            DemoExecutionConfig.from_dict(self.config["service"]),
            self.olap, ZeroNetworkSink(),
        )
        self.client = FakeIbkrClient(account=ACCOUNT,
                                     auto_fill_market_orders=True)
        self.runner = IbkrL1Runner(
            self.config, client_factory=lambda profile: self.client)
        from app.ibkr_l1_adapter import L1Profile
        self.profile = L1Profile.load(self.config["profile_path"])

    def mint(self, now=None):
        payload = mint_payload(
            self.profile, quantity_ceiling=20000.0,
            max_risk_fraction_at_stop=0.005, validity_seconds=900,
            contract_con_id=None, now=now or NOW,
        )
        return write_capability(
            payload, Path(self.config["capability_store_dir"]))

    def decide(self, object_id="ai-r-1", exposure=0.5, sl=1.0860, tp=1.0910,
               now=None):
        now = now or NOW + timedelta(seconds=1)
        intent = AssetIntent(
            object_id=object_id, as_of=now,
            valid_until=now + timedelta(hours=4),
            producer={"name": "provider.mechanics", "version": "0"},
            trace_id="t-r", cell_id="fx:EUR/USD@4h:mech:policy",
            asset_id="fx:EUR/USD", action="target", target_exposure=exposure,
            risk_geometry={"mode": "fixed_price", "stop_price": sl,
                           "take_profit_price": tp},
            artifact_hash=ARTIFACT,
        )
        snapshot = BrokerCapabilitySnapshot(
            object_id="cap-r", as_of=now, producer={"name": "t", "version": "0"},
            trace_id="t-r", venue="ibkr_paper",
            account_fingerprint=FINGERPRINT, environment="paper",
            capability_evidence="live_observed",
            source_artifact_hash="sha256:" + "f" * 64, source_observed_at=now,
            instruments=[InstrumentCapability(
                instrument="EUR.USD", tradeable=True, shortable=True,
                min_units=20000.0, unit_step=20000.0, price_decimals=5,
                margin_rate=0.03, native_stop_loss=True,
                native_take_profit=True, native_bracket=True,
            )],
        )
        return self.service.process_intent(
            intent, snapshot, equity=250_000.0, reference_price=1.0880,
            instrument="EUR.USD", now=now + timedelta(seconds=1),
        )

    def close(self):
        self.olap.close()


@pytest.fixture()
def env(tmp_path):
    environment = RunnerEnv(tmp_path)
    yield environment
    environment.close()


def _heartbeat_on_disk(config):
    return json.loads(Path(config["heartbeat_path"]).read_text())


# ── config strictness ──

def test_config_missing_and_unknown_keys_refuse(tmp_path):
    config = _runner_config(tmp_path)
    del config["heartbeat_path"]
    path = tmp_path / "c1.json"
    path.write_text(json.dumps(config))
    with pytest.raises(L1RunnerError, match="missing"):
        load_l1_runner_config(path)
    config = _runner_config(tmp_path)
    config["surprise"] = True
    path.write_text(json.dumps(config))
    with pytest.raises(L1RunnerError, match="unknown"):
        load_l1_runner_config(path)


# ── fail-closed construction ──

def test_disabled_runner_never_touches_a_client(tmp_path):
    config = _runner_config(tmp_path, enabled=False)

    def forbidden_factory(profile):
        raise AssertionError("client factory invoked while disabled")

    runner = IbkrL1Runner(config, client_factory=forbidden_factory)
    heartbeat = runner.tick(now=NOW)
    assert heartbeat["state"] == "disabled"
    assert _heartbeat_on_disk(config)["schema"] == HEARTBEAT_SCHEMA
    assert _heartbeat_on_disk(config)["enabled"] is False


def test_missing_milestone_f_client_degrades_to_alert(tmp_path):
    config = _runner_config(tmp_path, enabled=True)
    runner = IbkrL1Runner(config)                      # default factory
    heartbeat = runner.tick(now=NOW)
    assert heartbeat["state"] == "degraded_no_client"
    assert any(a.startswith("client_unavailable") for a in heartbeat["alerts"])
    assert _heartbeat_on_disk(config)["state"] == "degraded_no_client"


# ── the heartbeat-driven canary cycle ──

def test_runner_drives_entry_fill_and_flatten_cycle(env):
    env.mint()
    assert env.decide()["outcome"] == "would_be_order"
    _write_quote(env.config["quote_database_path"],
                 observed_at=NOW + timedelta(seconds=2))

    first = env.runner.tick(now=NOW + timedelta(seconds=3))
    assert first["state"] == "active"
    assert first["tick"]["entries"] == ["acknowledged"]
    assert first["effect_state_counts"] == {"acknowledged": 1}
    assert first["orders_submitted_by_this_runner"] == 3
    assert any(e.startswith("l1_entry:") for e in first["events"])

    parent_id = env.olap.nonterminal_effects()[0]["order_ids"][0]
    env.client.fill_parent(parent_id, 20000.0)
    second = env.runner.tick(now=NOW + timedelta(seconds=4))
    assert len(second["tick"]["fills"]) == 1
    assert env.olap.open_exposures()[0]["units_open"] == 20000.0

    env.service.apply_owner_command(OwnerCommand(
        object_id="oc-r-1", as_of=NOW + timedelta(seconds=5),
        producer={"name": "owner", "version": "0"}, trace_id="t-own",
        command="flatten_all", issuer_id="owner-1",
        exact_phrase="FLATTEN ALL DEMO POSITIONS NOW", nonce="n-r-1",
        expires_at=NOW + timedelta(minutes=5), idempotency_key="cmd-r-1",
    ), now=NOW + timedelta(seconds=5))
    third = env.runner.tick(now=NOW + timedelta(seconds=6))
    assert third["tick"]["flattens"] == ["terminal_flat"]
    assert env.client.position_facts() == []
    assert env.olap.nonterminal_effects() == []
    assert any(e.startswith("l1_flatten:") for e in third["events"])


# ── deterministic degraded-state alerts ──

def test_stale_quote_defers_and_alerts_without_destroying_decision(env):
    env.mint()
    env.decide()
    _write_quote(env.config["quote_database_path"],
                 observed_at=NOW - timedelta(seconds=300))
    heartbeat = env.runner.tick(now=NOW + timedelta(seconds=3))
    assert any(a.startswith("quote_stale") for a in heartbeat["alerts"])
    assert any(a.startswith("entry_deferred") for a in heartbeat["alerts"])
    assert heartbeat["effect_state_counts"] == {}     # decision untouched
    # fresh quote on the next tick executes the same pending decision
    _write_quote(env.config["quote_database_path"],
                 observed_at=NOW + timedelta(seconds=9))
    retry = env.runner.tick(now=NOW + timedelta(seconds=10))
    assert retry["tick"]["entries"] == ["acknowledged"]


def test_missing_quote_source_alerts_and_defers(env):
    env.mint()
    env.decide()
    heartbeat = env.runner.tick(now=NOW + timedelta(seconds=3))
    assert "quote_source_unavailable" in heartbeat["alerts"]
    assert heartbeat["tick"]["entries"] == ["deferred"]


def test_halt_state_is_alerted_and_blocks_entries(env):
    env.mint()
    env.decide()
    env.olap.set_state("halt", "hold")
    _write_quote(env.config["quote_database_path"],
                 observed_at=NOW + timedelta(seconds=2))
    heartbeat = env.runner.tick(now=NOW + timedelta(seconds=3))
    assert "halted:hold" in heartbeat["alerts"]
    assert heartbeat["tick"]["entries"] == ["deferred"]
    assert heartbeat["effect_state_counts"] == {}


def test_no_capability_alert_names_the_missing_authority(env):
    env.decide()
    _write_quote(env.config["quote_database_path"],
                 observed_at=NOW + timedelta(seconds=2))
    heartbeat = env.runner.tick(now=NOW + timedelta(seconds=3))
    assert any("no_capability" in a for a in heartbeat["alerts"])


def test_heartbeat_write_is_atomic(env):
    env.runner.tick(now=NOW)
    hb_dir = Path(env.config["heartbeat_path"]).parent
    assert [p.name for p in hb_dir.iterdir()] == ["heartbeat.json"]


def test_run_once_exits_cleanly(env):
    assert env.runner.run(once=True) == 0
