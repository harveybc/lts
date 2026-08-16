"""End-to-end runner proofs for findings 259/260, socket-free.

The IBKR model runner is driven through a real tick with a fake broker to
prove that (a) the due-decision fact and its exact as-of bars are written
as ONE logical operation, (b) a persistence failure degrades COMPARABILITY
health without touching trading safety, and (c) that degradation is
durable — a restart still reports it, and the heartbeat file carries the
typed reason.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from prediction_provider_mechanics import FEATURE_NAMES

from app import as_of_lineage
from app.ibkr_l1_broker import FakeIbkrClient
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_model_authority import MANDATE_SCHEMA, ContinuousPaperProfile
from app.ibkr_model_runner import IbkrModelRunner

ACCOUNT = "DU-ASOF-LINEAGE"
FINGERPRINT = hashlib.sha256(ACCOUNT.encode()).hexdigest()[:16]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("network operation attempted in runner test")
    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)


def _json(path, value):
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ModelBroker(FakeIbkrClient):
    def account_balance(self):
        return {"equity": 1_000_000.0, "cash": 1_000_000.0,
                "available_funds": 1_000_000.0}

    def current_quote(self, _instrument):
        return {"conId": 15016062, "symbol": "USD", "currency": "CAD",
                "secType": "CASH", "bid": 1.4045, "ask": 1.4046,
                "observed_at": datetime.now(timezone.utc)}

    def historical_closed_bars(self, _instrument, *, timeframe, count):
        now = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0)
        return [{
            "time": (now - timedelta(hours=4 * (60 - index))).isoformat(),
            "open": 1.39 + index * 0.0002,
            "high": 1.391 + index * 0.0002,
            "low": 1.389 + index * 0.0002,
            "close": 1.3905 + index * 0.0002,
            "volume": 0.0, "complete": True,
        } for index in range(count)]


def _runner_config(tmp_path):
    profile_path = tmp_path / "profile.json"
    _json(profile_path, {
        "schema_version": "lts.ibkr.paper.model_profile.v1",
        "venue": "ibkr_paper", "environment": "paper",
        "host": "127.0.0.1", "port": 7497, "client_id": 78,
        "account_fingerprint_algorithm": "account_id_sha256_16",
        "account_fingerprint": FINGERPRINT,
        "instrument": "USD.CAD", "asset_id": "fx:USD/CAD",
        "max_entries_per_day": 4, "quantity_ceiling": 20000,
        "stop_distance_price_max": 0.003,
        "take_profit_distance_price_max": 0.006,
        "max_spread_price": 0.0003, "contract_con_id": None,
    })
    profile = ContinuousPaperProfile.load(profile_path)
    training_path = tmp_path / "training.json"
    config_sha = _json(training_path, {"route": "USD.CAD"})
    artifact_path = tmp_path / "model.json"
    artifact_sha = _json(artifact_path, {
        "schema": "prediction_provider.live_linear_policy.v1",
        "model_id": "usdcad-asof-v1", "asset_id": "fx:USD/CAD",
        "timeframe": "4h", "feature_names": list(FEATURE_NAMES),
        "means": [0.0] * len(FEATURE_NAMES),
        "scales": [1.0] * len(FEATURE_NAMES),
        "coefficients": [0.0] * len(FEATURE_NAMES),
        "intercept": 10.0, "probability_threshold": 0.5,
    })
    manifest_path = tmp_path / "manifest.json"
    _json(manifest_path, {
        "schema": "prediction_provider.live_linear_manifest.v1",
        "model_id": "usdcad-asof-v1", "asset_id": "fx:USD/CAD",
        "timeframe": "4h", "artifact_file": str(artifact_path),
        "artifact_sha256": artifact_sha, "config_file": str(training_path),
        "config_sha256": config_sha, "research_validated": True,
        "live_inference_eligible": False, "live_execution_eligible": False,
    })
    mandate_path = tmp_path / "mandate.json"
    now = datetime.now(timezone.utc)
    _json(mandate_path, {
        "schema": MANDATE_SCHEMA, "environment": "paper",
        "venue": "ibkr_paper", "profile_hash": profile.profile_hash,
        "asset_id": "fx:USD/CAD", "instrument": "USD.CAD",
        "execution_tier": "demo_research_canary",
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "max_risk_fraction_at_stop": 0.0000625,
        "quantity_ceiling": 20000, "max_entries_per_day": 4,
        "mandate_id": "asof-lineage-mandate",
    })
    os.chmod(mandate_path, 0o600)
    return {
        "schema": "lts.ibkr.model_runner.v1",
        "profile_file": str(profile_path),
        "mandate_file": str(mandate_path),
        "model": {"manifest_file": str(manifest_path),
                  "expected_timeframe": "4h",
                  "execution_tier": "demo_research_canary"},
        "route": {"minimum_units": 20000, "unit_step": 1000,
                  "margin_rate": 1},
        "strategy": {"stop_fraction": 0.0015,
                     "take_profit_fraction": 0.003},
        "price_decimals": 5, "quantity_decimals": 0,
        "max_decision_age_seconds": 300, "loop_seconds": 60,
        "heartbeat_path": str(tmp_path / "heartbeat.json"),
        "service": {
            "venue": "ibkr_paper", "account_fingerprint": FINGERPRINT,
            "environment": "paper",
            "database_path": str(tmp_path / "state" / "runner.sqlite"),
            "risk_fraction_at_stop": 0.00005, "max_overshoot_ratio": 0.25,
            "gross_notional_fraction_max": 0.029,
            "margin_fraction_max": 0.029,
            "daily_loss_budget_fraction": 0.0002,
            "max_concurrent_positions": 1, "signal_max_age_seconds": 28800,
            "owner_issuer_allowlist": ["owner"], "command_phrases": {},
            "asset_instrument_bindings": {"fx:USD/CAD": "USD.CAD"},
        },
    }


def _heartbeat(config):
    return json.loads(
        open(config["heartbeat_path"], encoding="utf-8").read())


def test_due_decision_and_as_of_bars_are_written_as_one_operation(tmp_path):
    config = _runner_config(tmp_path)
    runner = IbkrModelRunner(config,
                             client_factory=lambda _p: ModelBroker(ACCOUNT))
    try:
        result = runner.tick()
        assert result["state"] == "decided"
        runner.write_heartbeat(result)
        decisions = runner.olap.due_bar_decisions(venue="ibkr_paper")
        bound = runner.olap.as_of_rows(row_state="bound")
        assert len(decisions) == 1 and len(bound) == 1
        # the as-of row binds the SAME normalized identity as the decision
        projected = as_of_lineage.identity_of_decision(decisions[0])
        assert bound[0]["identity_sha256"] == projected["identity_sha256"]
        assert bound[0]["account_fingerprint"] == FINGERPRINT
        assert bound[0]["decision_id"] == decisions[0]["decision_id"]
        assert bound[0]["origin"] == "runner_atomic_bind"
        assert len(json.loads(bound[0]["bars_json"])) == 60
        # the pending linkage that preceded it is resolved, not an orphan
        assert runner.olap.unresolved_as_of_pendings() == []
        beat = _heartbeat(config)
        assert beat["comparison_lineage_state"] == "healthy"
        assert beat["comparison_lineage_reason"] is None
        assert beat["comparison_lineage_open_incidents"] == 0
    finally:
        runner.close()


def test_persistence_failure_degrades_health_without_touching_trading(
        tmp_path):
    config = _runner_config(tmp_path)
    broker = ModelBroker(ACCOUNT)
    runner = IbkrModelRunner(config, client_factory=lambda _p: broker)
    try:
        real_insert = runner.olap._insert_as_of

        def explode(normalized, *, row_state, origin):
            if row_state == as_of_lineage.BOUND:
                raise sqlite3.OperationalError("injected as-of write failure")
            return real_insert(normalized, row_state=row_state, origin=origin)

        runner.olap._insert_as_of = explode
        result = runner.tick()
        runner.olap._insert_as_of = real_insert
        runner.write_heartbeat(result)

        # trading safety is untouched: the protected bracket still went out
        assert result["state"] == "decided"
        assert result["decision"]["outcome"] == "would_be_order"
        assert len([f for name, f in broker.calls
                    if name == "place_order"]) == 3

        # but the comparison product lost this bar's lineage, atomically:
        # neither the decision fact nor the as-of row survives the rollback
        assert runner.olap.due_bar_decisions(venue="ibkr_paper") == []
        assert runner.olap.as_of_rows(row_state="bound") == []

        beat = _heartbeat(config)
        assert beat["comparison_lineage_state"] == "degraded"
        assert beat["comparison_lineage_reason"] == \
            "as_of_persistence_failure"
        assert beat["comparison_lineage_open_incidents"] == 1
        last = beat["comparison_lineage_last_incident"]
        assert last["venue"] == "ibkr_paper"
        assert last["instrument"] == "USD.CAD"
        assert last["reason_code"] == "as_of_persistence_failure"
        assert beat["state"] == "decided"      # the venue itself looks fine
    finally:
        runner.close()


def test_degradation_is_durable_across_a_runner_restart(tmp_path):
    config = _runner_config(tmp_path)
    runner = IbkrModelRunner(config,
                             client_factory=lambda _p: ModelBroker(ACCOUNT))
    try:
        real_insert = runner.olap._insert_as_of

        def explode(normalized, *, row_state, origin):
            if row_state == as_of_lineage.BOUND:
                raise sqlite3.OperationalError("injected as-of write failure")
            return real_insert(normalized, row_state=row_state, origin=origin)

        runner.olap._insert_as_of = explode
        runner.tick()
        runner.olap._insert_as_of = real_insert
    finally:
        runner.close()

    restarted = IbkrModelRunner(
        config, client_factory=lambda _p: ModelBroker(ACCOUNT))
    try:
        health = restarted.comparison_lineage_health()
        assert health["comparison_lineage_state"] == "degraded"
        assert health["comparison_lineage_reason"] == \
            "as_of_persistence_failure"
        restarted.write_heartbeat({"state": "restarted"})
        assert _heartbeat(config)["comparison_lineage_state"] == "degraded"
    finally:
        restarted.close()


def test_a_second_tick_on_the_same_bar_is_idempotent_and_heals(tmp_path):
    config = _runner_config(tmp_path)
    broker = ModelBroker(ACCOUNT)
    runner = IbkrModelRunner(config, client_factory=lambda _p: broker)
    try:
        real_insert = runner.olap._insert_as_of

        def explode(normalized, *, row_state, origin):
            if row_state == as_of_lineage.BOUND:
                raise sqlite3.OperationalError("injected as-of write failure")
            return real_insert(normalized, row_state=row_state, origin=origin)

        runner.olap._insert_as_of = explode
        runner.tick()
        runner.olap._insert_as_of = real_insert
        assert runner.comparison_lineage_health()[
            "comparison_lineage_state"] == "degraded"
    finally:
        runner.close()

    # the same bar is re-decided by a healthy process: the identity's
    # evidence becomes whole again and the incident closes itself
    ledger = config["service"]["database_path"]
    store = L1ExecutionOlap(ledger)
    try:
        pending = store.as_of_rows(row_state="pending")
        assert len(pending) == 1
        fact = {key: pending[0][key] for key in (
            as_of_lineage.IDENTITY_FIELDS + as_of_lineage.LINEAGE_FIELDS
            + ("input_sha256", "feature_contract", "source"))}
        fact["bars_json"] = pending[0]["bars_json"]
        decision = {
            **{key: pending[0][key] for key in (
                "venue", "account_fingerprint", "instrument", "decision_id",
                "model_id", "artifact_sha256", "config_sha256", "timeframe",
                "bar_close", "input_sha256")},
            "asset_id": "fx:USD/CAD",
            "decided_at": "2026-08-11T00:00:05+00:00",
            "action": "long", "outcome": "would_be_order",
        }
        store.record_due_bar_decision_with_as_of(decision, fact)
        assert store.as_of_lineage_health()["comparison_lineage_state"] \
            == "healthy"
        assert len(store.as_of_rows(row_state="bound")) == 1
    finally:
        store.close()


# --------------------------------------------------------- Alpaca runner

class _Stub:
    def __init__(self, **fields):
        self.__dict__.update(fields)


def _alpaca_runner(tmp_path):
    """The Alpaca runner's as-of code path without a broker or credentials:
    only the attributes its lineage methods touch are provided."""
    from app.alpaca_model_runner import AlpacaModelRunner

    runner = object.__new__(AlpacaModelRunner)
    runner.config = {"model": {"expected_timeframe": "1d"}}
    runner.store = L1ExecutionOlap(tmp_path / "alpaca.sqlite")
    runner.profile = _Stub(account_fingerprint="0123456789abcdef",
                           symbol="SPY")
    runner.policy = _Stub(model_id="spy-daily-linear-live-v1",
                          artifact_sha256="c" * 64, asset_id="equity:SPY")
    runner.manifest = {"config_sha256": "b" * 64,
                       "manifest_sha256": "d" * 64}
    runner._as_of = None
    runner._as_of_outcome = {}
    return runner


def test_alpaca_runner_binds_as_of_bars_to_the_same_identity(tmp_path):
    runner = _alpaca_runner(tmp_path)
    try:
        bars = [{
            "time": f"2026-08-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00",
            "open": 500.0 + i, "high": 501.0 + i, "low": 499.0 + i,
            "close": 500.5 + i, "volume": 1000.0, "complete": True,
        } for i in range(60)]
        observation = {
            "last_closed_bar": bars[-1]["time"],
            "input_sha256": "e" * 64,
            "feature_contract": "prediction_provider.closed_bars.linear.v1",
        }
        inference = {"last_closed_bar": bars[-1]["time"],
                     "input_sha256": "e" * 64, "action": "long",
                     "probability_up": 0.62}
        runner._open_as_of(observation, bars)
        assert runner._as_of_outcome["ok"] is True
        assert len(runner.store.as_of_rows(row_state="pending")) == 1
        runner._record_due_bar(inference, outcome="would_be_order",
                               quote={"bid": 500.4, "ask": 500.6})

        decisions = runner.store.due_bar_decisions(venue="alpaca_paper")
        bound = runner.store.as_of_rows(row_state="bound")
        assert len(decisions) == 1 and len(bound) == 1
        assert bound[0]["identity_sha256"] == as_of_lineage \
            .identity_of_decision(decisions[0])["identity_sha256"]
        assert bound[0]["source"] == "alpaca_iex_daily_bars"
        assert bound[0]["instrument"] == "SPY"
        assert runner.comparison_lineage_health()[
            "comparison_lineage_state"] == "healthy"

        # a contradictory second window for the same bar refuses and lands
        # exactly one durable incident, while the C1 fact stays intact
        runner._open_as_of(observation,
                           [{**bar, "close": bar["close"] + 5.0}
                            for bar in bars])
        assert runner._as_of_outcome["ok"] is False
        assert runner._as_of_outcome["diverging"] == ["bars_sha256"]
        health = runner.comparison_lineage_health()
        assert health["comparison_lineage_state"] == "degraded"
        assert health["comparison_lineage_reason"] == \
            "as_of_lineage_contradiction"
        assert health["comparison_lineage_open_incidents"] == 1
        assert len(runner.store.as_of_rows(row_state="bound")) == 1
    finally:
        runner.store.close()
