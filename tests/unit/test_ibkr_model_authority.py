import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from trading_contracts import AssetIntent, BrokerCapabilitySnapshot, InstrumentCapability, OrderIntentV2

from app.demo_execution_service import DemoExecutionConfig, DemoExecutionService, ZeroNetworkSink
from app.ibkr_l1_adapter import L1AuthorizationError
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_model_authority import (
    MANDATE_SCHEMA,
    ContinuousPaperGate,
    ContinuousPaperProfile,
)


NOW = datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc)
ACCOUNT = "DU-CONTINUOUS-TEST"
FINGERPRINT = hashlib.sha256(ACCOUNT.encode()).hexdigest()[:16]


def _profile(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({
        "schema_version": "lts.ibkr.paper.model_profile.v1",
        "venue": "ibkr_paper", "environment": "paper",
        "host": "127.0.0.1", "port": 7497, "client_id": 78,
        "account_fingerprint_algorithm": "account_id_sha256_16",
        "account_fingerprint": FINGERPRINT,
        "instrument": "USD.CAD", "asset_id": "fx:USD/CAD",
        "max_entries_per_day": 4, "quantity_ceiling": 20000,
        "stop_distance_price_max": 0.003,
        "take_profit_distance_price_max": 0.006,
        "max_spread_price": 0.0003, "contract_con_id": 15016062,
    }), encoding="utf-8")
    return ContinuousPaperProfile.load(path)


def _intent(tmp_path, profile):
    database = tmp_path / "l0.sqlite"
    config = DemoExecutionConfig.from_dict({
        "venue": "ibkr_paper", "account_fingerprint": FINGERPRINT,
        "environment": "paper", "database_path": str(database),
        "risk_fraction_at_stop": 0.00005, "max_overshoot_ratio": 0.25,
        "gross_notional_fraction_max": 0.03, "margin_fraction_max": 0.03,
        "daily_loss_budget_fraction": 0.0002, "max_concurrent_positions": 1,
        "signal_max_age_seconds": 28800, "owner_issuer_allowlist": ["owner"],
        "command_phrases": {},
        "asset_instrument_bindings": {"fx:USD/CAD": "USD.CAD"},
    })
    olap = L1ExecutionOlap(database)
    service = DemoExecutionService(config, olap, ZeroNetworkSink())
    asset = AssetIntent(
        object_id="usdcad-model:bar-1", as_of=NOW,
        valid_until=NOW + timedelta(hours=8),
        producer={"name": "model", "version": "1"}, trace_id="trace-1",
        config_hash="sha256:" + "b" * 64,
        cell_id="fx:USD/CAD@4h:model", asset_id="fx:USD/CAD",
        action="target", target_exposure=1.0, strategy_rel_volume=1.0,
        risk_geometry={"mode": "fixed_price", "stop_price": 1.402,
                       "take_profit_price": 1.408},
        reason_codes=["model:usdcad-model", "input:" + "c" * 64],
        artifact_hash="sha256:" + "a" * 64,
    )
    snapshot = BrokerCapabilitySnapshot(
        object_id="cap", as_of=NOW, producer={"name": "t", "version": "1"},
        trace_id="cap-1", venue="ibkr_paper", account_fingerprint=FINGERPRINT,
        environment="paper", capability_evidence="live_observed",
        source_artifact_hash="sha256:" + "d" * 64, source_observed_at=NOW,
        instruments=[InstrumentCapability(
            instrument="USD.CAD", tradeable=True, shortable=True,
            min_units=20000, unit_step=1000, price_decimals=5, margin_rate=1,
            native_stop_loss=True, native_take_profit=True, native_bracket=True,
        )],
    )
    result = service.process_intent(
        asset, snapshot, equity=1_000_000, reference_price=1.405,
        quote_time=NOW, instrument="USD.CAD", now=NOW,
    )
    assert result["outcome"] == "would_be_order"
    row = olap.l1_pending_decisions("would_be_order")[0]
    return olap, OrderIntentV2.model_validate_json(row["intent_json"])


def _mandate(tmp_path, profile, *, expires_at=None):
    path = tmp_path / "mandate.json"
    path.write_text(json.dumps({
        "schema": MANDATE_SCHEMA, "environment": "paper", "venue": "ibkr_paper",
        "profile_hash": profile.profile_hash, "asset_id": profile.asset_id,
        "instrument": profile.instrument, "execution_tier": "demo_research_canary",
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (expires_at or NOW + timedelta(days=1)).isoformat(),
        "max_risk_fraction_at_stop": 0.0000625,
        "quantity_ceiling": 20000, "max_entries_per_day": 4,
        "mandate_id": "mandate-test-1",
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_continuous_paper_gate_derives_one_bound_capability_per_intent(tmp_path):
    profile = _profile(tmp_path)
    olap, intent = _intent(tmp_path, profile)
    gate = ContinuousPaperGate(_mandate(tmp_path, profile))
    _, first = gate.load_for_intent(profile, intent, olap=olap, now=NOW)
    _, replay = gate.load_for_intent(profile, intent, olap=olap, now=NOW)
    assert replay == first
    assert first.metadata["contract_con_id"] == 15016062
    assert first.metadata["quantity_ceiling"] == 20000


def test_continuous_paper_gate_refuses_expiry_and_permissive_file(tmp_path):
    profile = _profile(tmp_path)
    olap, intent = _intent(tmp_path, profile)
    path = _mandate(tmp_path, profile, expires_at=NOW - timedelta(seconds=1))
    gate = ContinuousPaperGate(path)
    with pytest.raises(L1AuthorizationError, match="outside validity"):
        gate.load_for_intent(profile, intent, olap=olap, now=NOW)
    os.chmod(path, 0o644)
    with pytest.raises(L1AuthorizationError, match="mode 0600"):
        gate.load_for_intent(profile, intent, olap=olap, now=NOW)
