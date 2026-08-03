import hashlib
import json
from datetime import datetime, timedelta, timezone

from prediction_provider_mechanics import FEATURE_NAMES

from app.mt5_bridge_lab import SnapshotPayload
from app.mt5_execution_bridge import Mt5ExecutionConfig, Mt5ExecutionStore
from app.mt5_model_runner import Mt5ModelRunner, close_idempotency_key


ACCOUNT = "0123456789abcdef01234567"


def _json(path, value):
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_close_idempotency_is_snapshot_session_and_reason_bound():
    base = {
        "session_id": "session-1",
        "reason": "model_switch",
        "snapshot_received_at": "2026-08-03T12:00:00+00:00",
        "last_bar": "2026-08-03T08:00:00Z",
    }
    first = close_idempotency_key(**base)
    assert close_idempotency_key(**base) == first
    for name, value in (
        ("session_id", "session-2"),
        ("reason", "unprotected_position"),
        ("snapshot_received_at", "2026-08-03T12:01:00+00:00"),
        ("last_bar", "2026-08-03T12:00:00Z"),
    ):
        changed = dict(base)
        changed[name] = value
        assert close_idempotency_key(**changed) != first


def test_closed_bars_drive_one_l0_checked_model_command(tmp_path):
    bridge_path = tmp_path / "bridge.json"
    database_path = tmp_path / "mt5.sqlite"
    bridge = {
        "schema": "lts.mt5.execution_bridge_config.v2",
        "environment": "demo", "execution_enabled": True,
        "database_path": str(database_path), "secret_env": "SECRET",
        "bind_host": "127.0.0.1", "port": 8766,
        "account_fingerprint": ACCOUNT, "allowed_symbols": ["ETHUSD"],
        "max_volume": 0.01, "max_open_commands_per_day": 4,
    }
    _json(bridge_path, bridge)

    artifact_path = tmp_path / "model.json"
    artifact_sha = _json(artifact_path, {
        "schema": "prediction_provider.live_linear_policy.v1",
        "model_id": "eth-test-v1", "asset_id": "crypto:ETHUSD",
        "timeframe": "4h", "feature_names": list(FEATURE_NAMES),
        "means": [0.0] * len(FEATURE_NAMES),
        "scales": [1.0] * len(FEATURE_NAMES),
        "coefficients": [0.0] * len(FEATURE_NAMES),
        "intercept": 10.0, "probability_threshold": 0.5,
    })
    training_config = tmp_path / "training.json"
    config_sha = _json(training_config, {"model": "test"})
    manifest_path = tmp_path / "manifest.json"
    _json(manifest_path, {
        "schema": "prediction_provider.live_linear_manifest.v1",
        "model_id": "eth-test-v1", "asset_id": "crypto:ETHUSD",
        "timeframe": "4h", "artifact_file": str(artifact_path),
        "artifact_sha256": artifact_sha, "config_file": str(training_config),
        "config_sha256": config_sha, "research_validated": True,
        "live_inference_eligible": False, "live_execution_eligible": False,
    })

    now = datetime.now(timezone.utc).replace(microsecond=0)
    bars = []
    for index in range(60):
        close = 1900.0 + index
        bars.append({
            "symbol": "ETHUSD", "timeframe": "4h",
            "time": (now - timedelta(hours=4 * (60 - index))).isoformat(),
            "open": close - 1, "high": close + 2, "low": close - 2,
            "close": close, "volume": 1000 + index,
        })
    store = Mt5ExecutionStore(database_path)
    store.record_snapshot(SnapshotPayload.model_validate({
        "schema": "lts.mt5.snapshot.v1", "account_fingerprint": ACCOUNT,
        "observed_at": now, "currency": "USD", "balance": 10000,
        "equity": 10000, "margin": 0, "free_margin": 10000,
        "positions": [], "orders": [], "bars": bars,
        "symbols": [{
            "symbol": "ETHUSD", "bid": 1958.0, "ask": 1960.0,
            "point": 0.01, "volume_min": 0.01, "volume_max": 65,
            "volume_step": 0.01, "trade_mode": 4, "observed_at": now,
        }],
    }))
    store.close()

    runner_config = {
        "schema": "lts.mt5.model_runner.v1",
        "bridge_config_file": str(bridge_path),
        "model": {
            "manifest_file": str(manifest_path),
            "expected_asset_id": "crypto:ETHUSD",
            "expected_timeframe": "4h",
            "execution_tier": "demo_research_canary",
        },
        "route": {"symbol": "ETHUSD", "timeframe": "4h"},
        "strategy": {"stop_fraction": 0.01, "take_profit_fraction": 0.02},
        "snapshot_max_age_seconds": 120, "loop_seconds": 15,
        "service": {
            "venue": "mt5_demo", "account_fingerprint": ACCOUNT,
            "environment": "demo", "database_path": str(database_path),
            "risk_fraction_at_stop": 0.00002, "max_overshoot_ratio": 0.5,
            "gross_notional_fraction_max": 0.003,
            "margin_fraction_max": 0.003,
            "daily_loss_budget_fraction": 0.00008,
            "max_concurrent_positions": 1, "signal_max_age_seconds": 28800,
            "owner_issuer_allowlist": ["owner"], "command_phrases": {},
            "asset_instrument_bindings": {"crypto:ETHUSD": "ETHUSD"},
        },
    }
    runner = Mt5ModelRunner(runner_config)
    try:
        result = runner.tick()
        assert result["state"] == "command_queued"
        command = runner.bridge_store.connection.execute(
            "SELECT * FROM execution_commands"
        ).fetchone()
        assert command["action"] == "open_long"
        assert command["volume"] == 0.01
        assert command["stop_loss"] < 1959.0 < command["take_profit"]
        assert command["artifact_sha256"] == artifact_sha
        assert len(command["input_sha256"]) == 64
    finally:
        runner.close()
