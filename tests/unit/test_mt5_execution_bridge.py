import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.mt5_bridge_lab import (
    Mt5BridgeError,
    SnapshotPayload,
    create_signed_headers,
)
from app.mt5_execution_bridge import (
    ExecutionResultPayload,
    Mt5ExecutionConfig,
    Mt5ExecutionStore,
    _response_signature,
    create_mt5_execution_app,
)


SECRET = b"s" * 64
ACCOUNT = "0123456789abcdef01234567"
EA_PATH = (
    Path(__file__).resolve().parents[2]
    / "mt5" / "MQL5" / "Experts" / "LtsMt5ModelBridge.mq5"
)


def test_execution_ea_source_keeps_demo_native_protection_contract():
    source = EA_PATH.read_text(encoding="utf-8")
    assert "InpExecutionEnabled = false" in source
    assert "ACCOUNT_TRADE_MODE_DEMO" in source
    assert source.count("OrderSend(request, result)") == 2
    assert "request.sl = stop_loss;" in source
    assert "request.tp = take_profit;" in source
    assert "sl_tp_not_anchored_to_direct_quote" in source
    assert "close_route_outside_ea_mandate" in source
    assert "PositionGetInteger(POSITION_MAGIC) == InpMagic" in source


def test_execution_ea_bar_json_concatenates_closing_brace():
    source = EA_PATH.read_text(encoding="utf-8")
    assert (
        '"\\\"volume\\\":" + DoubleToString((double)bar.tick_volume, 0) +\n'
        '      "}";'
    ) in source


def test_execution_ea_handles_empty_get_hash_and_header_case():
    source = EA_PATH.read_text(encoding="utf-8")
    assert (
        "e3b0c44298fc1c149afbf4c8996fb924"
        "27ae41e4649b934ca495991b7852b855"
    ) in source
    assert 'if(value == "")' in source
    assert "StringToLower(normalized_headers);" in source
    assert "StringToLower(normalized_name);" in source


def test_execution_ea_accepts_successful_zero_retcode_order_check():
    source = EA_PATH.read_text(encoding="utf-8")
    assert source.count("if(!OrderCheck(request, check))") == 2
    assert "check.retcode != TRADE_RETCODE_DONE" not in source


def _config(tmp_path):
    return Mt5ExecutionConfig(
        database_path=tmp_path / "mt5.sqlite", secret_env="SECRET",
        bind_host="127.0.0.1", port=8766, max_clock_skew_seconds=90,
        nonce_retention_seconds=900, stale_heartbeat_seconds=180,
        account_fingerprint=ACCOUNT, allowed_symbols=("USDCAD",),
        max_volume=0.01, max_open_commands_per_day=2,
        delivery_retry_seconds=30,
    )


def _enqueue(store, config, **overrides):
    values = dict(
        config=config, idempotency_key="model:bar:open", action="open_long",
        symbol="USDCAD", volume=0.01, stop_loss=1.34, take_profit=1.36,
        model_id="usdcad-model-v1", artifact_sha256="a" * 64,
        config_sha256="b" * 64, input_sha256="c" * 64,
    )
    values.update(overrides)
    return store.enqueue(**values)


def test_queue_requires_native_sl_tp_and_model_evidence(tmp_path):
    config = _config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    with pytest.raises(Mt5BridgeError, match="SL/TP"):
        _enqueue(store, config, stop_loss=0, take_profit=0)
    with pytest.raises(Mt5BridgeError, match="SHA-256"):
        _enqueue(store, config, artifact_sha256="nope")
    assert store.command_counts() == {}


def test_queue_is_idempotent_and_allows_only_one_unresolved_route_command(tmp_path):
    config = _config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    first = _enqueue(store, config)
    replay = _enqueue(store, config)
    assert replay["command_id"] == first["command_id"]
    assert replay["replayed"] is True
    with pytest.raises(Mt5BridgeError, match="unresolved"):
        _enqueue(store, config, idempotency_key="another")


def test_signed_delivery_and_exact_result_lifecycle(tmp_path):
    config = _config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    command = _enqueue(store, config)
    client = TestClient(create_mt5_execution_app(config, store, SECRET))

    path = "/v2/commands/next"
    headers = create_signed_headers(
        SECRET, "GET", path, nonce="delivery-nonce-1234"
    )
    response = client.get(
        path, params={"account_fingerprint": ACCOUNT}, headers=headers
    )
    assert response.status_code == 200
    fields = response.text.split("|")
    assert fields[:4] == ["v1", command["command_id"], "open_long", "USDCAD"]
    assert response.headers["X-LTS-Response-Signature"] == _response_signature(
        SECRET, "delivery-nonce-1234", response.content
    )

    payload = {
        "schema": "lts.mt5.execution_result.v1",
        "command_id": command["command_id"],
        "account_fingerprint": ACCOUNT,
        "success": True,
        "result_code": 10009,
        "order_ticket": "100",
        "deal_ticket": "101",
        "message": "done",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    response = client.post(
        "/v2/commands/result", content=body,
        headers={
            "Content-Type": "application/json",
            **create_signed_headers(SECRET, "POST", "/v2/commands/result", body),
        },
    )
    assert response.status_code == 200
    assert response.json()["state"] == "succeeded"
    assert store.command_counts() == {"succeeded": 1}


def test_execution_status_reports_v2_and_not_read_only(tmp_path):
    config = _config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    client = TestClient(create_mt5_execution_app(config, store, SECRET))

    status = client.get("/v1/status").json()

    assert status["bridge_version"] == "lts.mt5.bridge.execution.v2"
    assert status["read_only"] is False
    assert status["execution_enabled"] is True


def test_execution_status_reconciles_protected_model_position(tmp_path):
    config = _config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    command = _enqueue(store, config)
    store.complete(ExecutionResultPayload.model_validate({
        "schema": "lts.mt5.execution_result.v1",
        "command_id": command["command_id"],
        "account_fingerprint": ACCOUNT,
        "success": True,
        "result_code": 10009,
        "order_ticket": "100",
        "deal_ticket": "101",
        "message": "protected_entry_accepted",
        "observed_at": datetime.now(timezone.utc),
    }))
    store.record_snapshot(SnapshotPayload.model_validate({
        "schema": "lts.mt5.snapshot.v1",
        "account_fingerprint": ACCOUNT,
        "observed_at": datetime.now(timezone.utc),
        "currency": "USD",
        "balance": 10_000,
        "equity": 10_000,
        "margin": 10,
        "free_margin": 9_990,
        "positions": [{
            "ticket": "100", "symbol": "USDCAD", "side": "long",
            "volume": 0.01, "price_open": 1.35,
            "stop_loss": 1.34, "take_profit": 1.36, "profit": 0,
        }],
    }))
    client = TestClient(create_mt5_execution_app(config, store, SECRET))

    status = client.get("/v1/status").json()

    assert status["exposure_reconciliation"] == {
        "available": True,
        "positions_total": 1,
        "orders_total": 0,
        "authorized_positions": 1,
        "unexpected_positions": 0,
        "unexpected_orders": 0,
        "all_authorized": True,
    }


def test_execution_status_refuses_altered_or_foreign_position(tmp_path):
    config = _config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    command = _enqueue(store, config)
    store.complete(ExecutionResultPayload.model_validate({
        "schema": "lts.mt5.execution_result.v1",
        "command_id": command["command_id"],
        "account_fingerprint": ACCOUNT,
        "success": True,
        "result_code": 10009,
        "order_ticket": "100",
        "observed_at": datetime.now(timezone.utc),
    }))
    store.record_snapshot(SnapshotPayload.model_validate({
        "schema": "lts.mt5.snapshot.v1",
        "account_fingerprint": ACCOUNT,
        "observed_at": datetime.now(timezone.utc),
        "currency": "USD", "balance": 10_000, "equity": 10_000,
        "margin": 10, "free_margin": 9_990,
        "positions": [{
            "ticket": "foreign", "symbol": "USDCAD", "side": "long",
            "volume": 0.01, "price_open": 1.35,
            "stop_loss": 0, "take_profit": 1.36, "profit": 0,
        }],
    }))

    reconciliation = store.exposure_reconciliation()

    assert reconciliation["all_authorized"] is False
    assert reconciliation["unexpected_positions"] == 1


def test_wrong_account_and_unsigned_poll_never_receive_commands(tmp_path):
    config = _config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    _enqueue(store, config)
    client = TestClient(create_mt5_execution_app(config, store, SECRET))
    assert client.get(
        "/v2/commands/next", params={"account_fingerprint": ACCOUNT}
    ).status_code == 401
    headers = create_signed_headers(SECRET, "GET", "/v2/commands/next")
    assert client.get(
        "/v2/commands/next",
        params={"account_fingerprint": "f" * 24}, headers=headers,
    ).status_code == 403


def test_open_budget_counts_only_new_commands(tmp_path):
    config = _config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    for number in range(2):
        command = _enqueue(store, config, idempotency_key=f"entry-{number}")
        payload = {
            "schema": "lts.mt5.execution_result.v1",
            "command_id": command["command_id"],
            "account_fingerprint": ACCOUNT,
            "success": True,
            "result_code": 10009,
            "observed_at": datetime.now(timezone.utc),
        }
        from app.mt5_execution_bridge import ExecutionResultPayload
        store.complete(ExecutionResultPayload.model_validate(payload))
    with pytest.raises(Mt5BridgeError, match="daily Demo entry budget"):
        _enqueue(store, config, idempotency_key="third")


# --- Dual-symbol routing (order 2026-08-23: ETHUSD + USDCAD) ---------


def _dual_config(tmp_path):
    return Mt5ExecutionConfig(
        database_path=tmp_path / "mt5.sqlite", secret_env="SECRET",
        bind_host="127.0.0.1", port=8766, max_clock_skew_seconds=90,
        nonce_retention_seconds=900, stale_heartbeat_seconds=180,
        account_fingerprint=ACCOUNT,
        allowed_symbols=("ETHUSD", "USDCAD"),
        max_volume=0.01, max_open_commands_per_day=4,
        delivery_retry_seconds=30,
    )


def test_cross_symbol_command_theft_is_impossible(tmp_path):
    """A USDCAD command must never be delivered to the ETHUSD chart EA."""
    config = _dual_config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    command = _enqueue(store, config, symbol="USDCAD")
    assert store.next_command(
        ACCOUNT, retry_seconds=30, symbol="ETHUSD") is None
    delivered = store.next_command(
        ACCOUNT, retry_seconds=30, symbol="USDCAD")
    assert delivered["command_id"] == command["command_id"]
    # and not deliverable twice inside the retry window
    assert store.next_command(
        ACCOUNT, retry_seconds=30, symbol="USDCAD") is None


def test_multi_symbol_poll_requires_chart_symbol(tmp_path):
    config = _dual_config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    client = TestClient(create_mt5_execution_app(config, store, SECRET))
    path = "/v2/commands/next"
    resp = client.get(
        path, params={"account_fingerprint": ACCOUNT},
        headers=create_signed_headers(SECRET, "GET", path,
                                      nonce="dual-nonce-0001"))
    assert resp.status_code == 400
    resp = client.get(
        path, params={"account_fingerprint": ACCOUNT,
                      "symbol": "EURUSD"},
        headers=create_signed_headers(SECRET, "GET", path,
                                      nonce="dual-nonce-0002"))
    assert resp.status_code == 403
    resp = client.get(
        path, params={"account_fingerprint": ACCOUNT,
                      "symbol": "USDCAD"},
        headers=create_signed_headers(SECRET, "GET", path,
                                      nonce="dual-nonce-0003"))
    assert resp.status_code == 204  # empty queue, correctly scoped


def test_single_symbol_mandate_resolves_absent_symbol(tmp_path):
    config = _config(tmp_path)  # ("USDCAD",)
    store = Mt5ExecutionStore(config.database_path)
    command = _enqueue(store, config)
    client = TestClient(create_mt5_execution_app(config, store, SECRET))
    path = "/v2/commands/next"
    resp = client.get(
        path, params={"account_fingerprint": ACCOUNT},
        headers=create_signed_headers(SECRET, "GET", path,
                                      nonce="single-nonce-0001"))
    assert resp.status_code == 200
    assert resp.text.split("|")[1] == command["command_id"]


def test_unresolved_route_is_per_symbol_not_per_account(tmp_path):
    """An unresolved ETHUSD command must not block USDCAD enqueue."""
    config = _dual_config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    _enqueue(store, config, idempotency_key="eth:open",
             symbol="ETHUSD", model_id="eth-model-v1")
    other = _enqueue(store, config, idempotency_key="cad:open",
                     symbol="USDCAD", model_id="usdcad-model-v1")
    assert other["symbol"] == "USDCAD"
    with pytest.raises(Mt5BridgeError, match="unresolved"):
        _enqueue(store, config, idempotency_key="cad:open2",
                 symbol="USDCAD")


def test_nonce_replay_across_ea_clients_is_refused(tmp_path):
    config = _dual_config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    client = TestClient(create_mt5_execution_app(config, store, SECRET))
    path = "/v2/commands/next"
    headers = create_signed_headers(SECRET, "GET", path,
                                    nonce="replayed-nonce-0001")
    first = client.get(
        path, params={"account_fingerprint": ACCOUNT,
                      "symbol": "ETHUSD"}, headers=headers)
    assert first.status_code in (200, 204)
    # the second client replays the SAME nonce (cross-client replay)
    second = client.get(
        path, params={"account_fingerprint": ACCOUNT,
                      "symbol": "USDCAD"}, headers=headers)
    assert second.status_code == 401


def test_duplicate_fill_and_altered_result_identity(tmp_path):
    config = _dual_config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    command = _enqueue(store, config, symbol="USDCAD")
    store.next_command(ACCOUNT, retry_seconds=30, symbol="USDCAD")

    observed = datetime.now(timezone.utc).isoformat()

    def _result(**over):
        values = dict(
            schema="lts.mt5.execution_result.v1",
            command_id=command["command_id"],
            account_fingerprint=ACCOUNT, success=True,
            result_code=10009, order_ticket="100", deal_ticket="101",
            message="done",
            observed_at=observed,
        )
        values.update(over)
        return ExecutionResultPayload.model_validate(values)

    assert store.complete(_result())["duplicate"] is False
    assert store.complete(_result())["duplicate"] is True
    with pytest.raises(Mt5BridgeError, match="identity collision"):
        store.complete(_result(deal_ticket="999"))


def test_restart_idempotency_preserves_route_state(tmp_path):
    config = _dual_config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    command = _enqueue(store, config, symbol="USDCAD")
    del store
    reopened = Mt5ExecutionStore(config.database_path)
    replay = _enqueue(reopened, config, symbol="USDCAD")
    assert replay["command_id"] == command["command_id"]
    assert replay["replayed"] is True
    delivered = reopened.next_command(
        ACCOUNT, retry_seconds=30, symbol="USDCAD")
    assert delivered["command_id"] == command["command_id"]


def test_one_symbol_failure_leaves_the_other_healthy(tmp_path):
    config = _dual_config(tmp_path)
    store = Mt5ExecutionStore(config.database_path)
    cad = _enqueue(store, config, idempotency_key="cad:open",
                   symbol="USDCAD")
    store.next_command(ACCOUNT, retry_seconds=30, symbol="USDCAD")
    failed = ExecutionResultPayload.model_validate(dict(
        schema="lts.mt5.execution_result.v1",
        command_id=cad["command_id"], account_fingerprint=ACCOUNT,
        success=False, result_code=10013, order_ticket="",
        deal_ticket="", message="rejected",
        observed_at=datetime.now(timezone.utc).isoformat(),
    ))
    assert store.complete(failed)["state"] == "failed"
    eth = _enqueue(store, config, idempotency_key="eth:open",
                   symbol="ETHUSD", model_id="eth-model-v1")
    delivered = store.next_command(
        ACCOUNT, retry_seconds=30, symbol="ETHUSD")
    assert delivered["command_id"] == eth["command_id"]


def test_ea_source_declares_symbol_and_refuses_wrong_chart():
    """Mixed-magic / wrong-symbol defense in depth, pinned in source:
    each EA instance polls with its own chart symbol, fails a
    mis-delivered command VISIBLY, and only ever manages positions
    carrying its own magic."""
    source = EA_PATH.read_text(encoding="utf-8")
    assert '+ "&symbol=" + Symbol();' in source
    assert "wrong_symbol_for_this_chart" in source
    assert "PositionGetInteger(POSITION_MAGIC) == InpMagic" in source
