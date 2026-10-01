"""ETH 4h demo route (MT5 runner) consuming a modular policy: shadow only, dormant until eligible.

The real Mt5ModelRunner reads recorded bars from its bridge store's latest snapshot.
With ``require_forecast_eligibility`` the selector refuses (the route stays dormant)
unless the contract's frozen forecast-vs-naive record passes for the horizon the
action consumes. No command is ever queued, and the network is forbidden.
"""
import hashlib
import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from app.live_model_selection import LiveModelSelectionError
from app.mt5_bridge_lab import SnapshotPayload
from app.mt5_execution_bridge import Mt5ExecutionStore
from app.mt5_model_runner import Mt5ModelRunner
from tests.unit.test_forecast_naive_gate import _horizon
from tests.unit.test_forecast_naive_gate import seal as _seal
from tests.unit.test_modular_inference_adapter import _contract, _export, needs_engine

ACCOUNT = "0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*_a, **_k):
        raise AssertionError("network operation attempted")
    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


def _bridge(tmp_path):
    database = tmp_path / "mt5.sqlite"
    bridge = {"schema": "lts.mt5.execution_bridge_config.v2", "environment": "demo", "execution_enabled": True,
              "database_path": str(database), "secret_env": "SECRET", "bind_host": "127.0.0.1", "port": 8766,
              "account_fingerprint": ACCOUNT, "allowed_symbols": ["ETHUSD"], "max_volume": 0.01,
              "max_open_commands_per_day": 4}
    (tmp_path / "bridge.json").write_text(json.dumps(bridge))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    bars = [{"symbol": "ETHUSD", "timeframe": "4h", "time": (now - timedelta(hours=4 * (60 - i))).isoformat(),
             "open": 1900 + i - 1, "high": 1900 + i + 2, "low": 1900 + i - 2, "close": 1900 + i,
             "volume": 1000 + 7 * (i % 5)} for i in range(60)]
    store = Mt5ExecutionStore(database)
    store.record_snapshot(SnapshotPayload.model_validate({
        "schema": "lts.mt5.snapshot.v1", "account_fingerprint": ACCOUNT, "observed_at": now, "currency": "USD",
        "balance": 10000, "equity": 10000, "margin": 0, "free_margin": 10000, "positions": [], "orders": [],
        "bars": bars, "symbols": [{"symbol": "ETHUSD", "bid": 1958.0, "ask": 1960.0, "point": 0.01,
                                   "volume_min": 0.01, "volume_max": 65, "volume_step": 0.01, "trade_mode": 4,
                                   "observed_at": now}]}))
    store.close()
    return database, bars


def _record(model_mae, naive_mae, *, model_sha="e" * 64):
    return _seal({
        "schema": "predictor.forecast_naive_evidence.v1",
        "frozen_metric": {"primary": "MAE", "secondary": "MSE", "frozen": "declared before selection"},
        "artifact": {"model_sha256": model_sha},
        "population": {"dataset_id": "eth", "asset": "crypto:ETHUSD", "targets": ["CLOSE"], "rows": 500,
                       "row_ids_sha256": "a" * 64, "first_origin": "2024-01-01", "last_origin": "2024-12-31",
                       "sample_hours": 4, "horizon_unit": "steps of 4 h"},
        "scale": {"metric_space": "z_train", "scaler_identity": "eth-4h-train"},
        "split": {"provenance": "chronological_oof", "test_used": False, "reserved_trading_test": False},
        "naive": {"definition": "persistence"},
        "per_horizon": [_horizon(1, model_mae, naive_mae, rows=500), _horizon(2, 0.9, 0.8, rows=500)]})


def _eth_contract(path, data, *, record=None, horizons=(1,)):
    data["asset_id"], data["timeframe"] = "crypto:ETHUSD", "4h"
    data["time"].update(sample_hours=4, bar_close_offset_hours=4, max_gap_hours=12)
    data["modular_config"]["sample_hours"] = 4
    if record is not None:
        (path.parent / "evidence.json").write_text(json.dumps(record))
        data["evidence"]["forecast_naive"] = {"record_file": "evidence.json", "declared": {
            "asset": "crypto:ETHUSD", "families": {"forecast": {
                "evidence_sha256": record["evidence_sha256"], "model_sha256": "e" * 64, "period_hours": 4,
                "metric_space": "z_train", "scaler_identity": "eth-4h-train", "horizons": list(horizons)}}}}
    path.write_text(json.dumps(data))
    return path


def _dummy_contract(tmp_path, **kw):
    engine, model = tmp_path / "engine.py", tmp_path / "model.keras"
    engine.write_text("# engine\n")
    model.write_bytes(b"model")
    data = _contract(**{"engine.path": str(engine), "engine.sha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
                        "engine.keras_version": "3.13.2",
                        "artifact.file": str(model), "artifact.sha256": hashlib.sha256(b"model").hexdigest()})
    return _eth_contract(tmp_path / "contract.json", data, **kw)


def _config(tmp_path, contract, *, tier="shadow_inference_only", require=True):
    database = tmp_path / "mt5.sqlite"
    return {"schema": "lts.mt5.model_runner.v1", "bridge_config_file": str(tmp_path / "bridge.json"),
            "model": {"family": "modular", "contract_file": str(contract), "expected_asset_id": "crypto:ETHUSD",
                      "expected_timeframe": "4h", "execution_tier": tier,
                      "require_forecast_eligibility": require},
            "route": {"symbol": "ETHUSD", "timeframe": "4h"},
            "strategy": {"stop_fraction": 0.01, "take_profit_fraction": 0.02},
            "snapshot_max_age_seconds": 120, "loop_seconds": 15,
            "heartbeat_path": str(tmp_path / "heartbeat.json"),
            "service": {"venue": "mt5_demo", "account_fingerprint": ACCOUNT, "environment": "demo",
                        "database_path": str(database), "risk_fraction_at_stop": 0.00002,
                        "max_overshoot_ratio": 0.5, "gross_notional_fraction_max": 0.003,
                        "margin_fraction_max": 0.003, "daily_loss_budget_fraction": 0.00008,
                        "max_concurrent_positions": 1, "signal_max_age_seconds": 28800,
                        "owner_issuer_allowlist": ["owner"], "command_phrases": {},
                        "asset_instrument_bindings": {"crypto:ETHUSD": "ETHUSD"}}}


def _commands(database):
    store = Mt5ExecutionStore(database)
    try:
        return store.connection.execute("SELECT COUNT(*) FROM execution_commands").fetchone()[0]
    finally:
        store.close()


# ------------------------------------------------------------ dormant until eligible (no TensorFlow)


@pytest.mark.parametrize("case, reason", [
    ("no_evidence", "evidence_not_configured"),
    ("failing", "not_better_than_naive"),
    ("tie", "tie_with_naive"),
    ("wrong_horizon", "consumed_horizon_mismatch"),
])
def test_route_stays_dormant_unless_the_consumed_horizon_beats_persistence(tmp_path, case, reason):
    database, _ = _bridge(tmp_path)
    record = {"no_evidence": None, "failing": _record(1.1, 1.0), "tie": _record(1.0, 1.0),
              "wrong_horizon": _record(0.5, 1.0)}[case]
    contract = _dummy_contract(tmp_path, record=record, horizons=(2,) if case == "wrong_horizon" else (1,))
    with pytest.raises(LiveModelSelectionError, match="dormant") as error:
        Mt5ModelRunner(_config(tmp_path, contract))
    assert reason in str(error.value) or case == "wrong_horizon"
    assert _commands(database) == 0


def test_modular_route_refuses_any_tier_but_shadow(tmp_path):
    _bridge(tmp_path)
    contract = _dummy_contract(tmp_path, record=_record(0.5, 1.0))
    with pytest.raises(LiveModelSelectionError, match="shadow_inference_only"):
        Mt5ModelRunner(_config(tmp_path, contract, tier="demo_research_canary"))


# ------------------------------------------------------------ eligible: shadow decisions from recorded bars


def _real_eth_contract(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    (tmp_path / "m").mkdir()
    contract_path, _, _ = _export(tmp_path / "m")
    data = json.loads(contract_path.read_text())
    return _eth_contract(contract_path, data, record=record)


@needs_engine
def test_eligible_route_decides_in_shadow_from_recorded_bars_and_queues_nothing(tmp_path, monkeypatch):
    database, bars = _bridge(tmp_path)
    contract = _real_eth_contract(tmp_path, monkeypatch, _record(0.5, 1.0))
    runner = Mt5ModelRunner(_config(tmp_path, contract))
    try:
        result = runner.tick()
        runner.write_heartbeat(result)
    finally:
        runner.close()
    assert result["state"] == "shadow_inference_only" and result["commands_queued"] == 0
    assert result["inference"]["execution_authorized"] is False
    assert result["inference"]["last_closed_bar"] == datetime.fromisoformat(bars[-1]["time"]).isoformat()
    assert _commands(database) == 0
    heartbeat = json.loads((tmp_path / "heartbeat.json").read_text())
    assert heartbeat["read_only"] is True and heartbeat["forecast_eligibility"] == "ELIGIBLE"


@needs_engine
def test_shadow_without_the_eligibility_requirement_still_never_queues(tmp_path, monkeypatch):
    database, _ = _bridge(tmp_path)
    contract = _real_eth_contract(tmp_path, monkeypatch, None)
    runner = Mt5ModelRunner(_config(tmp_path, contract, require=False))
    try:
        assert runner.tick()["state"] == "shadow_inference_only"
    finally:
        runner.close()
    assert _commands(database) == 0
