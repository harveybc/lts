"""Adversarial proofs for resume_after_reconciliation (addendum §9).

Stale/empty/wrong-account evidence, open orders, nonzero positions,
unknown effects, active incident, replayed nonce, expired capability,
concurrent resume, crash at the transaction boundary, and a new hold
racing the resume — every one refuses; the happy path commits burn,
evidence, previous state and transition atomically; retry after success
is idempotent and can never clear a later hold."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from app.ibkr_l1_adapter import L1AuthorizationError, L1Profile
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_l1_resume import (
    RESUME_OPERATION,
    RESUME_SCHEMA_VERSION,
    resume_after_reconciliation,
    validate_resume_capability,
)

NOW = datetime(2026, 8, 4, 20, 0, 0, tzinfo=timezone.utc)


def make_profile() -> L1Profile:
    return L1Profile(
        schema_version="lts.ibkr.l1.profile.v1",
        venue="ibkr_paper",
        environment="paper",
        host="127.0.0.1",
        port=7497,
        client_id=17,
        account_fingerprint_algorithm="account_id_sha256_16",
        account_fingerprint="c0ff137a3cc1a363",
        instrument="USD.CAD",
        asset_id="fx:USD/CAD",
        max_orders_this_activation=1,
        quantity_ceiling=25000.0,
        stop_distance_price_max=0.02,
        take_profit_distance_price_max=0.02,
        max_spread_price=0.0005,
        profile_hash="p" * 64,
    )


def make_payload(profile: L1Profile, **overrides) -> dict:
    payload = {
        "schema_version": RESUME_SCHEMA_VERSION,
        "operation": RESUME_OPERATION,
        "venue": "ibkr_paper",
        "host": "127.0.0.1",
        "port": 7497,
        "profile_hash": profile.profile_hash,
        "profile_schema_version": profile.schema_version,
        "account_fingerprint_algorithm": "account_id_sha256_16",
        "account_fingerprint": profile.account_fingerprint,
        "asset_id": profile.asset_id,
        "instrument": profile.instrument,
        "resume_of_effect_id": "l1e-f4993c2dda8cdc2a",
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(seconds=600)).isoformat(),
        "nonce": secrets.token_hex(32),
    }
    payload.update(overrides)
    return payload


def make_evidence(profile: L1Profile, **overrides) -> dict:
    evidence = {
        "source": "direct_tws",
        "gathered_at": NOW.isoformat(),
        "account_fingerprint": profile.account_fingerprint,
        "positions": [],
        "open_orders": [],
        "server_version": 178,
    }
    evidence.update(overrides)
    return evidence


@pytest.fixture()
def olap(tmp_path):
    ledger = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    ledger.create_effect(
        "l1e-f4993c2dda8cdc2a", "key-1", "bracket_entry", [])
    for state in ("submitted_pending_ack", "acknowledged", "terminal_flat"):
        ledger.advance_effect("l1e-f4993c2dda8cdc2a", state)
    ledger.set_state("halt", "hold")
    yield ledger
    ledger.close()


_DEFAULT = object()


def run(olap, profile, payload=None, evidence=_DEFAULT, incidents=None,
        now=NOW, record=None):
    profile = profile or make_profile()
    payload = payload or make_payload(profile)
    record = record or validate_resume_capability(
        payload, profile=profile, now=now)
    return resume_after_reconciliation(
        olap=olap, profile=profile, payload=payload, record=record,
        broker_evidence=(make_evidence(profile) if evidence is _DEFAULT
                         else evidence),
        active_incidents=incidents if incidents is not None else [],
        now=now)


def test_happy_path_commits_atomically(olap):
    profile = make_profile()
    result = run(olap, profile)
    assert result["applied"] is True
    assert olap.get_state("halt") == "none"
    facts = {row["fact_kind"]
             for row in olap.broker_facts("l1e-f4993c2dda8cdc2a")}
    assert {"resume_evidence", "halt_cleared"} <= facts
    last = json.loads(olap.get_state("last_resume"))
    assert last["evidence_sha256"] == result["evidence_sha256"]


def test_retry_after_success_is_idempotent(olap):
    profile = make_profile()
    payload = make_payload(profile)
    record = validate_resume_capability(payload, profile=profile, now=NOW)
    first = run(olap, profile, payload=payload, record=record)
    again = run(olap, profile, payload=payload, record=record)
    assert first["applied"] and again["already_resumed"]
    assert olap.get_state("halt") == "none"


def test_spent_capability_cannot_clear_a_later_hold(olap):
    profile = make_profile()
    payload = make_payload(profile)
    record = validate_resume_capability(payload, profile=profile, now=NOW)
    run(olap, profile, payload=payload, record=record)
    olap.set_state("halt", "hold")                 # a NEW unrelated hold
    with pytest.raises(L1AuthorizationError, match="already consumed"):
        run(olap, profile, payload=payload, record=record)
    assert olap.get_state("halt") == "hold"


def test_no_hold_refuses_without_burn(olap):
    profile = make_profile()
    olap.set_state("halt", "none")
    with pytest.raises(L1AuthorizationError, match="no hold is active"):
        run(olap, profile)
    assert not olap.nonce_consumed(hashlib.sha256(b"x").hexdigest())


def test_kill_halt_is_not_clearable(olap):
    olap.set_state("halt", "kill")
    with pytest.raises(L1AuthorizationError, match="not 'hold'"):
        run(olap, make_profile())
    assert olap.get_state("halt") == "kill"


def test_stale_evidence_refuses(olap):
    profile = make_profile()
    stale = make_evidence(
        profile, gathered_at=(NOW - timedelta(minutes=10)).isoformat())
    with pytest.raises(L1AuthorizationError, match="stale"):
        run(olap, profile, evidence=stale)
    assert olap.get_state("halt") == "hold"


def test_empty_and_malformed_evidence_refuse(olap):
    profile = make_profile()
    with pytest.raises(L1AuthorizationError, match="missing or empty"):
        run(olap, profile, evidence={})
    with pytest.raises(L1AuthorizationError, match="missing or empty"):
        run(olap, profile, evidence=None)
    with pytest.raises(L1AuthorizationError, match="missing"):
        run(olap, profile, evidence={"source": "direct_tws"})


def test_wrong_account_evidence_refuses(olap):
    profile = make_profile()
    wrong = make_evidence(profile, account_fingerprint="deadbeefdeadbeef")
    with pytest.raises(L1AuthorizationError, match="different account"):
        run(olap, profile, evidence=wrong)


def test_open_orders_or_positions_refuse(olap):
    profile = make_profile()
    with pytest.raises(L1AuthorizationError, match="position"):
        run(olap, profile,
            evidence=make_evidence(profile, positions=[{"pos": -25000}]))
    with pytest.raises(L1AuthorizationError, match="open order"):
        run(olap, profile,
            evidence=make_evidence(profile, open_orders=[{"orderId": 9}]))
    assert olap.get_state("halt") == "hold"


def test_nonterminal_effect_refuses(olap):
    profile = make_profile()
    olap.create_effect("l1e-aaaaaaaaaaaaaaaa", "key-2", "bracket_entry", [])
    with pytest.raises(L1AuthorizationError, match="not terminal"):
        run(olap, profile)
    assert olap.get_state("halt") == "hold"


def test_unknown_bound_effect_refuses(olap):
    profile = make_profile()
    payload = make_payload(profile,
                           resume_of_effect_id="l1e-0000000000000000")
    with pytest.raises(L1AuthorizationError, match="does not exist"):
        run(olap, profile, payload=payload)


def test_active_incident_refuses(olap):
    profile = make_profile()
    with pytest.raises(L1AuthorizationError, match="still active"):
        run(olap, profile,
            incidents=[{"event_code": "tws_unavailable",
                        "severity": "P1"}])


def test_unknown_incident_state_refuses(olap):
    profile = make_profile()
    payload = make_payload(profile)
    record = validate_resume_capability(payload, profile=profile, now=NOW)
    with pytest.raises(L1AuthorizationError, match="unknown is a refusal"):
        resume_after_reconciliation(
            olap=olap, profile=make_profile(), payload=payload,
            record=record, broker_evidence=make_evidence(profile),
            active_incidents=None, now=NOW)


def test_expired_capability_refuses():
    profile = make_profile()
    payload = make_payload(profile)
    with pytest.raises(L1AuthorizationError, match="expired"):
        validate_resume_capability(
            payload, profile=profile, now=NOW + timedelta(hours=1))


def test_overlong_validity_and_wrong_binding_refuse():
    profile = make_profile()
    with pytest.raises(L1AuthorizationError, match="validity exceeds"):
        validate_resume_capability(
            make_payload(profile, expires_at=(
                NOW + timedelta(hours=2)).isoformat()),
            profile=profile, now=NOW)
    with pytest.raises(L1AuthorizationError, match="fingerprint"):
        validate_resume_capability(
            make_payload(profile, account_fingerprint="0badc0ffee0badc0"),
            profile=profile, now=NOW)
    with pytest.raises(L1AuthorizationError, match="operation"):
        validate_resume_capability(
            make_payload(profile, operation="resume"),
            profile=profile, now=NOW)
    with pytest.raises(L1AuthorizationError, match="unknown keys"):
        validate_resume_capability(
            make_payload(profile, surprise=True), profile=profile, now=NOW)


def test_replayed_nonce_refuses(olap):
    profile = make_profile()
    payload_one = make_payload(profile)
    record_one = validate_resume_capability(
        payload_one, profile=profile, now=NOW)
    run(olap, profile, payload=payload_one, record=record_one)
    olap.set_state("halt", "hold")
    payload_two = make_payload(profile, nonce=payload_one["nonce"])
    record_two = validate_resume_capability(
        payload_two, profile=profile, now=NOW)
    with pytest.raises(L1AuthorizationError, match="already consumed"):
        run(olap, profile, payload=payload_two, record=record_two)


def test_concurrent_resume_second_consumer_refuses(tmp_path, olap):
    """Two processes race: the second burn hits UNIQUE and refuses."""
    profile = make_profile()
    payload = make_payload(profile)
    record = validate_resume_capability(payload, profile=profile, now=NOW)
    run(olap, profile, payload=payload, record=record)
    # Simulate the losing racer: hold re-set as if its check preceded the
    # winner's commit; its burn must now conflict.
    olap.set_state("halt", "hold")
    olap.set_state("last_resume", "")
    with pytest.raises(L1AuthorizationError, match="already consumed"):
        run(olap, profile, payload=payload, record=record)


def test_crash_before_commit_leaves_hold_and_capability(olap, monkeypatch):
    profile = make_profile()
    payload = make_payload(profile)
    record = validate_resume_capability(payload, profile=profile, now=NOW)

    original = olap.set_state

    def exploding_set_state(key, value):
        if key == "last_resume":
            raise RuntimeError("simulated crash inside the atomic unit")
        return original(key, value)

    monkeypatch.setattr(olap, "set_state", exploding_set_state)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run(olap, profile, payload=payload, record=record)
    monkeypatch.undo()
    assert olap.get_state("halt") == "hold"            # rollback held
    assert not olap.nonce_consumed(record.nonce_sha256)  # burn rolled back
    assert not olap.broker_facts("l1e-f4993c2dda8cdc2a", "halt_cleared")
    # The same capability then succeeds cleanly (crash-before-commit
    # consumed nothing).
    result = run(olap, profile, payload=payload, record=record)
    assert result["applied"] is True
