"""Milestone B: owner capability minting, validation, storage and status.

Owner constraints under test (2026-08-03): privileged authority separation
(TTY-only mint CLI, executor/gate cannot write the store), fixed protected
storage (0700/0600 enforced), one bracket per capability, short expiry,
atomic single-use consumption in the existing L0 ledger, no broker
connectivity anywhere in the mint path.
"""
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.ibkr_l1_adapter import L1AuthorizationError, L1Profile
from app.ibkr_l1_capability import (
    CapabilityGate,
    MAX_VALIDITY_SECONDS,
    capability_digest,
    capability_status,
    validate_capability,
)
from app.ibkr_l1_journal import L1ExecutionOlap

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from mint_paper_capability import mint_payload, write_capability  # noqa: E402

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted in capability path")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


@pytest.fixture()
def profile(tmp_path):
    payload = {
        "schema_version": "lts.ibkr.paper.l1.profile.v2",
        "venue": "ibkr_paper",
        "environment": "paper",
        "host": "127.0.0.1",
        "port": 7497,
        "client_id": 77,
        "account_fingerprint_algorithm": "account_id_sha256_16",
        "account_fingerprint": "c0ff137a3cc1a363",
        "instrument": "EUR.USD",
        "asset_id": "fx:EUR/USD",
        "max_orders_this_activation": 2,
        "quantity_ceiling": 20000.0,
        "stop_distance_price_max": 0.0020,
        "take_profit_distance_price_max": 0.0040,
        "max_spread_price": 0.0003,
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload))
    return L1Profile.load(path)


def _payload(profile, **overrides):
    payload = mint_payload(
        profile, quantity_ceiling=20000.0, max_risk_fraction_at_stop=0.005,
        validity_seconds=900, contract_con_id=None, now=NOW,
    )
    payload.update(overrides)
    return payload


# ── mint + validate round trip ──

def test_minted_capability_validates_and_yields_safe_record(profile):
    payload = _payload(profile)
    record = validate_capability(payload, profile=profile, now=NOW)
    assert record.capability_sha256 == capability_digest(payload)
    assert record.metadata["quantity_ceiling"] == 20000.0
    assert record.metadata["max_entries"] == 1
    assert "nonce" not in record.metadata          # secrets never in metadata


def test_mint_refuses_validity_beyond_one_hour(profile):
    with pytest.raises(ValueError, match="validity"):
        mint_payload(profile, quantity_ceiling=20000.0,
                     max_risk_fraction_at_stop=0.005,
                     validity_seconds=MAX_VALIDITY_SECONDS + 1,
                     contract_con_id=None, now=NOW)


# ── strict schema and binding refusals ──

@pytest.mark.parametrize("mutation,match", [
    ({"schema_version": "v0"}, "schema"),
    ({"venue": "anything"}, "venue"),
    ({"host": "0.0.0.0"}, "loopback"),
    ({"port": 7496}, "7497"),
    ({"account_fingerprint_algorithm": "account_set_sha256_16"}, "algorithm"),
    ({"account_fingerprint": "86aa086401855219"}, "fingerprint"),
    ({"instrument": "USD.CAD"}, "instrument"),
    ({"asset_id": "fx:USD/CAD"}, "asset"),
    ({"profile_hash": "0" * 64}, "profile hash"),
    ({"profile_schema_version": "v1"}, "profile schema"),
    ({"max_risk_fraction_at_stop": 0.02}, "risk"),
    ({"max_risk_fraction_at_stop": 0.0}, "risk"),
    ({"quantity_ceiling": 0.0}, "ceiling"),
    ({"quantity_ceiling": 25000.0}, "ceiling"),
    ({"max_entries": 2}, "one bracket"),
    ({"contract_con_id": -5}, "con_id"),
    ({"nonce": "short"}, "nonce"),
    ({"nonce": "Z" * 64}, "nonce"),
    ({"extra_field": True}, "unknown"),
])
def test_capability_refuses_any_deviation(profile, mutation, match):
    with pytest.raises(L1AuthorizationError, match=match):
        validate_capability(_payload(profile, **mutation), profile=profile, now=NOW)


def test_capability_missing_key_refuses(profile):
    payload = _payload(profile)
    del payload["expires_at"]
    with pytest.raises(L1AuthorizationError, match="missing"):
        validate_capability(payload, profile=profile, now=NOW)


def test_expired_capability_refuses(profile):
    payload = _payload(profile)
    with pytest.raises(L1AuthorizationError, match="expired"):
        validate_capability(payload, profile=profile,
                            now=NOW + timedelta(seconds=901))


def test_future_issued_capability_refuses(profile):
    payload = _payload(profile)
    with pytest.raises(L1AuthorizationError, match="future"):
        validate_capability(payload, profile=profile,
                            now=NOW - timedelta(seconds=120))


def test_stretched_expiry_window_refuses(profile):
    payload = _payload(
        profile,
        expires_at=(NOW + timedelta(seconds=MAX_VALIDITY_SECONDS + 60)).isoformat(),
    )
    with pytest.raises(L1AuthorizationError, match="validity"):
        validate_capability(payload, profile=profile, now=NOW)


# ── fixed protected storage ──

def test_write_capability_sets_protected_modes(profile, tmp_path):
    store = tmp_path / "store"
    path = write_capability(_payload(profile), store)
    assert oct(os.stat(store).st_mode & 0o777) == "0o700"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_write_capability_never_overwrites(profile, tmp_path):
    store = tmp_path / "store"
    payload = _payload(profile)
    write_capability(payload, store)
    with pytest.raises(FileExistsError):
        write_capability(payload, store)


def test_gate_loads_exactly_one_valid_capability(profile, tmp_path):
    store = tmp_path / "store"
    payload = _payload(profile)
    write_capability(payload, store)
    loaded, record = CapabilityGate(store).load(profile, now=NOW)
    assert loaded == payload
    assert record.capability_sha256 == capability_digest(payload)


def test_gate_refuses_empty_or_missing_store(profile, tmp_path):
    with pytest.raises(L1AuthorizationError, match="does not exist"):
        CapabilityGate(tmp_path / "absent").load(profile, now=NOW)
    empty = tmp_path / "empty"
    empty.mkdir(mode=0o700)
    with pytest.raises(L1AuthorizationError, match="no valid"):
        CapabilityGate(empty).load(profile, now=NOW)


def test_gate_refuses_ambiguous_multiple_capabilities(profile, tmp_path):
    store = tmp_path / "store"
    write_capability(_payload(profile), store)
    write_capability(_payload(profile, nonce="a" * 64), store)
    with pytest.raises(L1AuthorizationError, match="ambiguity"):
        CapabilityGate(store).load(profile, now=NOW)


def test_gate_refuses_permissive_file_mode(profile, tmp_path):
    store = tmp_path / "store"
    path = write_capability(_payload(profile), store)
    os.chmod(path, 0o644)
    with pytest.raises(L1AuthorizationError, match="no valid"):
        CapabilityGate(store).load(profile, now=NOW)


def test_gate_refuses_already_consumed_capability(profile, tmp_path):
    store = tmp_path / "store"
    payload = _payload(profile)
    write_capability(payload, store)
    record = validate_capability(payload, profile=profile, now=NOW)
    olap = L1ExecutionOlap(tmp_path / "l1.db")
    olap.create_effect("l1e-b", "idem-b", "bracket_entry", [1, 2, 3],
                       record.capability_sha256)
    olap.consume_capability(record.capability_sha256, record.nonce_sha256,
                            record.metadata, "l1e-b")
    with pytest.raises(L1AuthorizationError, match="consumed"):
        CapabilityGate(store).load(profile, olap=olap, now=NOW)
    olap.close()


# ── durable status classification ──

def test_capability_status_distinguishes_lifecycle(profile, tmp_path):
    record = validate_capability(_payload(profile), profile=profile, now=NOW)
    olap = L1ExecutionOlap(tmp_path / "l1.db")
    assert capability_status(olap, record)["status"] == "issued"

    olap.create_effect("l1e-s", "idem-s", "bracket_entry", [1, 2, 3],
                       record.capability_sha256)
    olap.consume_capability(record.capability_sha256, record.nonce_sha256,
                            record.metadata, "l1e-s")
    status = capability_status(olap, record)
    assert status["status"] == "consumed"
    assert status["classification"] == "consumed_before_effect"

    olap.record_broker_fact("l1e-s", "call_attempt", {"leg": "parent"})
    assert capability_status(olap, record)["classification"] == "effect_unknown"

    olap.advance_effect("l1e-s", "submitted_pending_ack")
    olap.advance_effect("l1e-s", "acknowledged")
    assert capability_status(olap, record)["classification"] == "acknowledged"
    olap.close()


# ── privileged authority separation ──

def test_mint_cli_refuses_without_interactive_terminal(profile, tmp_path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "schema_version": "lts.ibkr.paper.l1.profile.v2",
        "venue": "ibkr_paper", "environment": "paper", "host": "127.0.0.1",
        "port": 7497, "client_id": 77,
        "account_fingerprint_algorithm": "account_id_sha256_16",
        "account_fingerprint": "c0ff137a3cc1a363",
        "instrument": "EUR.USD", "asset_id": "fx:EUR/USD",
        "max_orders_this_activation": 2, "quantity_ceiling": 20000.0,
        "stop_distance_price_max": 0.0020,
        "take_profit_distance_price_max": 0.0040,
        "max_spread_price": 0.0003,
    }))
    result = subprocess.run(
        [sys.executable, "tools/mint_paper_capability.py",
         "--profile", str(profile_path), "--quantity-ceiling", "20000"],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "interactive owner terminal" in result.stderr


def test_gate_and_executor_modules_expose_no_store_writer():
    import app.ibkr_l1_capability as gate_module
    import app.ibkr_l1_executor as executor_module
    for module in (gate_module, executor_module):
        source = Path(module.__file__).read_text()
        assert "write_capability" not in source
        assert "mint_payload" not in source
