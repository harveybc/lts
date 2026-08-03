import hashlib
import json
import os
import socket
from datetime import datetime, timedelta, timezone

import pytest
from prediction_provider_mechanics import FEATURE_NAMES

from app.ibkr_l1_broker import FakeIbkrClient
from app.ibkr_l1_adapter import L1ExecutionError
from app.ibkr_model_runner import IbkrModelRunner
from app.ibkr_model_authority import ContinuousPaperProfile, MANDATE_SCHEMA


ACCOUNT = "DU-MODEL-RUNNER"
FINGERPRINT = hashlib.sha256(ACCOUNT.encode()).hexdigest()[:16]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("network operation attempted in model-runner test")
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
        return {
            "conId": 15016062, "symbol": "USD", "currency": "CAD",
            "secType": "CASH", "bid": 1.4045, "ask": 1.4046,
            "observed_at": datetime.now(timezone.utc),
        }

    def historical_closed_bars(self, _instrument, *, timeframe, count):
        assert timeframe == "4h" and count == 60
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        return [
            {
                "time": (now - timedelta(hours=4 * (60 - index))).isoformat(),
                "open": 1.39 + index * 0.0002,
                "high": 1.391 + index * 0.0002,
                "low": 1.389 + index * 0.0002,
                "close": 1.3905 + index * 0.0002,
                "volume": 0.0, "complete": True,
            }
            for index in range(60)
        ]


class QuoteUnavailableBroker(ModelBroker):
    def current_quote(self, _instrument):
        raise L1ExecutionError("TWS FX quote is unavailable or invalid")


def test_selected_model_reaches_one_exact_protected_ibkr_paper_bracket(tmp_path):
    profile_path = tmp_path / "profile.json"
    profile_payload = {
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
    }
    _json(profile_path, profile_payload)
    profile = ContinuousPaperProfile.load(profile_path)

    training_path = tmp_path / "training.json"
    config_sha = _json(training_path, {"route": "USD.CAD"})
    artifact_path = tmp_path / "model.json"
    artifact_sha = _json(artifact_path, {
        "schema": "prediction_provider.live_linear_policy.v1",
        "model_id": "usdcad-test-v1", "asset_id": "fx:USD/CAD",
        "timeframe": "4h", "feature_names": list(FEATURE_NAMES),
        "means": [0.0] * len(FEATURE_NAMES),
        "scales": [1.0] * len(FEATURE_NAMES),
        "coefficients": [0.0] * len(FEATURE_NAMES),
        "intercept": 10.0, "probability_threshold": 0.5,
    })
    manifest_path = tmp_path / "manifest.json"
    _json(manifest_path, {
        "schema": "prediction_provider.live_linear_manifest.v1",
        "model_id": "usdcad-test-v1", "asset_id": "fx:USD/CAD",
        "timeframe": "4h", "artifact_file": str(artifact_path),
        "artifact_sha256": artifact_sha, "config_file": str(training_path),
        "config_sha256": config_sha, "research_validated": True,
        "live_inference_eligible": False, "live_execution_eligible": False,
    })

    mandate_path = tmp_path / "mandate.json"
    now = datetime.now(timezone.utc)
    _json(mandate_path, {
        "schema": MANDATE_SCHEMA, "environment": "paper", "venue": "ibkr_paper",
        "profile_hash": profile.profile_hash, "asset_id": "fx:USD/CAD",
        "instrument": "USD.CAD", "execution_tier": "demo_research_canary",
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "max_risk_fraction_at_stop": 0.0000625,
        "quantity_ceiling": 20000, "max_entries_per_day": 4,
        "mandate_id": "runner-test-mandate",
    })
    os.chmod(mandate_path, 0o600)

    config = {
        "schema": "lts.ibkr.model_runner.v1",
        "profile_file": str(profile_path), "mandate_file": str(mandate_path),
        "model": {"manifest_file": str(manifest_path),
                  "expected_timeframe": "4h",
                  "execution_tier": "demo_research_canary"},
        "route": {"minimum_units": 20000, "unit_step": 1000, "margin_rate": 1},
        "strategy": {"stop_fraction": 0.0015, "take_profit_fraction": 0.003},
        "price_decimals": 5, "quantity_decimals": 0,
        "max_decision_age_seconds": 300, "loop_seconds": 60,
        "heartbeat_path": str(tmp_path / "heartbeat.json"),
        "service": {
            "venue": "ibkr_paper", "account_fingerprint": FINGERPRINT,
            "environment": "paper",
            "database_path": str(tmp_path / "new-state" / "runner.sqlite"),
            "risk_fraction_at_stop": 0.00005, "max_overshoot_ratio": 0.25,
            "gross_notional_fraction_max": 0.029, "margin_fraction_max": 0.029,
            "daily_loss_budget_fraction": 0.0002, "max_concurrent_positions": 1,
            "signal_max_age_seconds": 28800,
            "owner_issuer_allowlist": ["owner"], "command_phrases": {},
            "asset_instrument_bindings": {"fx:USD/CAD": "USD.CAD"},
        },
    }
    broker = ModelBroker(account=ACCOUNT)
    assert not (tmp_path / "new-state").exists()
    runner = IbkrModelRunner(config, client_factory=lambda _profile: broker)
    try:
        assert (tmp_path / "new-state" / "runner.sqlite").is_file()
        result = runner.tick()
        assert result["state"] == "decided"
        assert result["decision"]["outcome"] == "would_be_order"
        assert result["entries"][0]["state"] == "acknowledged"
        calls = [fact for name, fact in broker.calls if name == "place_order"]
        assert len(calls) == 3
        assert [fact["orderType"] for fact in calls] == ["MKT", "LMT", "STP"]
        assert [fact["transmit"] for fact in calls] == [False, False, True]
        assert calls[1]["lmtPrice"] is not None
        assert calls[2]["auxPrice"] is not None
    finally:
        runner.close()


def test_quote_unavailable_is_degraded_without_restart_or_order(tmp_path):
    profile_path = tmp_path / "profile.json"
    profile_payload = {
        "schema_version": "lts.ibkr.paper.model_profile.v1",
        "venue": "ibkr_paper", "environment": "paper",
        "host": "127.0.0.1", "port": 7497, "client_id": 78,
        "account_fingerprint_algorithm": "account_id_sha256_16",
        "account_fingerprint": FINGERPRINT,
        "instrument": "USD.CAD", "asset_id": "fx:USD/CAD",
        "max_entries_per_day": 4, "quantity_ceiling": 25000,
        "stop_distance_price_max": 0.003,
        "take_profit_distance_price_max": 0.006,
        "max_spread_price": 0.0003, "contract_con_id": None,
    }
    _json(profile_path, profile_payload)
    profile = ContinuousPaperProfile.load(profile_path)
    training_path = tmp_path / "training.json"
    config_sha = _json(training_path, {"route": "USD.CAD"})
    artifact_path = tmp_path / "model.json"
    artifact_sha = _json(artifact_path, {
        "schema": "prediction_provider.live_linear_policy.v1",
        "model_id": "usdcad-test-v1", "asset_id": "fx:USD/CAD",
        "timeframe": "4h", "feature_names": list(FEATURE_NAMES),
        "means": [0.0] * len(FEATURE_NAMES),
        "scales": [1.0] * len(FEATURE_NAMES),
        "coefficients": [0.0] * len(FEATURE_NAMES),
        "intercept": 10.0, "probability_threshold": 0.5,
    })
    manifest_path = tmp_path / "manifest.json"
    _json(manifest_path, {
        "schema": "prediction_provider.live_linear_manifest.v1",
        "model_id": "usdcad-test-v1", "asset_id": "fx:USD/CAD",
        "timeframe": "4h", "artifact_file": str(artifact_path),
        "artifact_sha256": artifact_sha, "config_file": str(training_path),
        "config_sha256": config_sha, "research_validated": True,
        "live_inference_eligible": False, "live_execution_eligible": False,
    })
    mandate_path = tmp_path / "mandate.json"
    now = datetime.now(timezone.utc)
    _json(mandate_path, {
        "schema": MANDATE_SCHEMA, "environment": "paper", "venue": "ibkr_paper",
        "profile_hash": profile.profile_hash, "asset_id": "fx:USD/CAD",
        "instrument": "USD.CAD", "execution_tier": "demo_research_canary",
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "max_risk_fraction_at_stop": 0.0000625,
        "quantity_ceiling": 25000, "max_entries_per_day": 4,
        "mandate_id": "runner-test-mandate",
    })
    os.chmod(mandate_path, 0o600)
    config = {
        "schema": "lts.ibkr.model_runner.v1",
        "profile_file": str(profile_path), "mandate_file": str(mandate_path),
        "model": {"manifest_file": str(manifest_path),
                  "expected_timeframe": "4h",
                  "execution_tier": "demo_research_canary"},
        "route": {"minimum_units": 25000, "unit_step": 1000, "margin_rate": 1},
        "strategy": {"stop_fraction": 0.0015, "take_profit_fraction": 0.003},
        "price_decimals": 5, "quantity_decimals": 0,
        "max_decision_age_seconds": 300, "loop_seconds": 60,
        "heartbeat_path": str(tmp_path / "heartbeat.json"),
        "service": {
            "venue": "ibkr_paper", "account_fingerprint": FINGERPRINT,
            "environment": "paper", "database_path": str(tmp_path / "runner.sqlite"),
            "risk_fraction_at_stop": 0.00005, "max_overshoot_ratio": 0.25,
            "gross_notional_fraction_max": 0.04, "margin_fraction_max": 0.04,
            "daily_loss_budget_fraction": 0.0002, "max_concurrent_positions": 1,
            "signal_max_age_seconds": 28800,
            "owner_issuer_allowlist": ["owner"], "command_phrases": {},
            "asset_instrument_bindings": {"fx:USD/CAD": "USD.CAD"},
        },
    }
    broker = QuoteUnavailableBroker(account=ACCOUNT)
    runner = IbkrModelRunner(config, client_factory=lambda _profile: broker)
    try:
        result = runner.tick()
        assert result["state"] == "waiting_for_quote"
        assert result["orders_submitted"] == 0
        assert "unavailable" in result["reason"]
        assert not [fact for name, fact in broker.calls if name == "place_order"]
        assert runner.tick()["state"] == "waiting_for_quote"
    finally:
        runner.close()
