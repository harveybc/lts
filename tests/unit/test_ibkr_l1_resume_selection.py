"""Finding AUD-F2-20260811-227: typed capability-store classification.

The defect: the resume CLI counted EVERY top-level JSON in the store
before signature/expiry classification, so a second unsigned/expired
side file denied a valid owner operation with "2 capability file(s)".

Proven here, on ISOLATED temporary stores only (the live owner store
``~/.config/lts/ibkr-resume-capabilities`` is never touched):

- one valid signed capability passes despite unsigned, expired and
  malformed side files (each typed and reported, never counted);
- two valid signed current capabilities still refuse (ambiguity);
- an explicit ``--capability`` outside the store, or one that is
  unsigned, expired or consumed, refuses with a typed reason;
- an explicit ``--capability`` resolves a two-valid ambiguity by naming;
- the separate archival operation moves ONLY typed expired/consumed
  files (never the valid one, never unsigned/malformed evidence), and
  the default selection flow moves nothing.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.ibkr_l1_resume as resume_module
from app.ibkr_l1_adapter import L1AuthorizationError, L1Profile
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_l1_resume import (
    ENTRY_CONSUMED,
    ENTRY_EXPIRED,
    ENTRY_MALFORMED,
    ENTRY_UNSIGNED,
    ENTRY_VALID,
    RESUME_OPERATION,
    RESUME_SCHEMA_VERSION,
    archive_invalid_capabilities,
    classify_resume_store,
    select_resume_capability,
    validate_resume_capability,
)

NOW = datetime(2026, 8, 11, 20, 0, 0, tzinfo=timezone.utc)


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


@pytest.fixture()
def signer(tmp_path):
    """Throwaway ed25519 owner key + pinned allowed_signers, tmp-only."""
    key = tmp_path / "owner_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                    "-f", str(key)], check=True)
    pub = (tmp_path / "owner_key.pub").read_text().split()
    signers = tmp_path / "allowed_signers"
    signers.write_text(
        f'owner namespaces="lts-ibkr-resume" {pub[0]} {pub[1]}\n')
    signers.chmod(0o644)
    return key, signers


@pytest.fixture()
def store(tmp_path):
    """An ISOLATED temporary capability store; never the live one."""
    store_dir = tmp_path / "resume-store"
    store_dir.mkdir(mode=0o700)
    return store_dir


@pytest.fixture()
def olap(tmp_path):
    ledger = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    yield ledger
    ledger.close()


def write_capability(store_dir, name, payload) -> Path:
    path = store_dir / name
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    path.chmod(0o600)
    return path


def sign(key, capability):
    subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key),
                    "-n", "lts-ibkr-resume", str(capability)],
                   check=True, capture_output=True)
    return capability.parent / (capability.name + ".sig")


def expired_payload(profile) -> dict:
    return make_payload(
        profile,
        issued_at=(NOW - timedelta(seconds=700)).isoformat(),
        expires_at=(NOW - timedelta(seconds=100)).isoformat(),
    )


def select(store_dir, signers, olap, **kwargs):
    return select_resume_capability(
        store_dir, profile=make_profile(), olap=olap, now=NOW,
        allowed_signers=signers, require_root_pin=False, **kwargs)


# ── side files can never deny one valid signed capability ──


def test_valid_with_unsigned_side_file_passes(store, signer, olap):
    """The audit's exact regression: 2 files, one unsigned — the valid
    signed owner capability must still be usable."""
    key, signers = signer
    profile = make_profile()
    valid = write_capability(store, "resume_valid.json",
                             make_payload(profile))
    sign(key, valid)
    write_capability(store, "resume_unsigned.json", make_payload(profile))

    chosen, ignored = select(store, signers, olap)
    assert chosen.path == valid
    assert chosen.kind == ENTRY_VALID
    assert [(e.path.name, e.kind) for e in ignored] == [
        ("resume_unsigned.json", ENTRY_UNSIGNED)]


def test_valid_with_expired_side_file_passes(store, signer, olap):
    key, signers = signer
    profile = make_profile()
    valid = write_capability(store, "resume_valid.json",
                             make_payload(profile))
    sign(key, valid)
    stale = write_capability(store, "resume_expired.json",
                             expired_payload(profile))
    sign(key, stale)                      # signed but expired → typed expired

    chosen, ignored = select(store, signers, olap)
    assert chosen.path == valid
    assert [(e.path.name, e.kind) for e in ignored] == [
        ("resume_expired.json", ENTRY_EXPIRED)]


def test_valid_with_malformed_side_file_passes(store, signer, olap):
    key, signers = signer
    valid = write_capability(store, "resume_valid.json",
                             make_payload(make_profile()))
    sign(key, valid)
    garbage = store / "resume_garbage.json"
    garbage.write_text("{not json")
    garbage.chmod(0o600)

    chosen, ignored = select(store, signers, olap)
    assert chosen.path == valid
    assert [(e.path.name, e.kind) for e in ignored] == [
        ("resume_garbage.json", ENTRY_MALFORMED)]


def test_two_valid_signed_current_still_refuse(store, signer, olap):
    key, signers = signer
    profile = make_profile()
    for name in ("resume_one.json", "resume_two.json"):
        sign(key, write_capability(store, name, make_payload(profile)))

    with pytest.raises(L1AuthorizationError, match="ambiguity"):
        select(store, signers, olap)


def test_empty_store_refuses(store, signer, olap):
    _key, signers = signer
    with pytest.raises(L1AuthorizationError, match="store is empty"):
        select(store, signers, olap)


# ── explicit --capability selection ──


def test_explicit_capability_outside_store_refuses(
        store, signer, olap, tmp_path):
    key, signers = signer
    profile = make_profile()
    sign(key, write_capability(store, "resume_valid.json",
                               make_payload(profile)))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    outside = write_capability(elsewhere, "resume_outside.json",
                               make_payload(profile))
    sign(key, outside)

    with pytest.raises(L1AuthorizationError,
                       match="outside the protected store"):
        select(store, signers, olap, explicit_path=outside)


def test_explicit_unsigned_capability_refuses(store, signer, olap):
    _key, signers = signer
    unsigned = write_capability(store, "resume_unsigned.json",
                                make_payload(make_profile()))
    with pytest.raises(L1AuthorizationError, match="unsigned"):
        select(store, signers, olap, explicit_path=unsigned)


def test_explicit_expired_capability_refuses(store, signer, olap):
    key, signers = signer
    stale = write_capability(store, "resume_expired.json",
                             expired_payload(make_profile()))
    sign(key, stale)
    with pytest.raises(L1AuthorizationError, match="expired"):
        select(store, signers, olap, explicit_path=stale)


def test_explicit_consumed_capability_refuses(store, signer, olap):
    key, signers = signer
    profile = make_profile()
    payload = make_payload(profile)
    spent = write_capability(store, "resume_spent.json", payload)
    sign(key, spent)
    record = validate_resume_capability(payload, profile=profile, now=NOW)
    olap.consume_capability(record.capability_sha256, record.nonce_sha256,
                            {"consumed_for": "resume"}, "l1e-old")

    with pytest.raises(L1AuthorizationError, match="consumed"):
        select(store, signers, olap, explicit_path=spent)


def test_explicit_missing_file_refuses(store, signer, olap):
    _key, signers = signer
    write_capability(store, "resume_valid.json", make_payload(make_profile()))
    with pytest.raises(L1AuthorizationError, match="does not exist"):
        select(store, signers, olap,
               explicit_path=store / "resume_absent.json")


def test_explicit_selection_resolves_two_valid_ambiguity(
        store, signer, olap):
    """Requirement: PREFER the explicit owner-selected file — naming one
    of two valid capabilities is not ambiguous."""
    key, signers = signer
    profile = make_profile()
    first = write_capability(store, "resume_one.json", make_payload(profile))
    sign(key, first)
    second = write_capability(store, "resume_two.json", make_payload(profile))
    sign(key, second)

    chosen, ignored = select(store, signers, olap, explicit_path=second)
    assert chosen.path == second
    assert chosen.kind == ENTRY_VALID
    assert [e.path.name for e in ignored] == ["resume_one.json"]


# ── archival: separate, explicit, evidence-preserving ──


def _full_store(store, signer, olap):
    """valid + signed-expired + consumed + unsigned, all typed."""
    key, _signers = signer
    profile = make_profile()
    valid = write_capability(store, "resume_valid.json",
                             make_payload(profile))
    sign(key, valid)
    stale = write_capability(store, "resume_expired.json",
                             expired_payload(profile))
    sign(key, stale)
    spent_payload = make_payload(profile)
    spent = write_capability(store, "resume_spent.json", spent_payload)
    sign(key, spent)
    record = validate_resume_capability(
        spent_payload, profile=make_profile(), now=NOW)
    olap.consume_capability(record.capability_sha256, record.nonce_sha256,
                            {"consumed_for": "resume"}, "l1e-old")
    unsigned = write_capability(store, "resume_unsigned.json",
                                make_payload(profile))
    return valid, stale, spent, unsigned


def test_archival_moves_only_typed_expired_and_consumed(
        store, signer, olap):
    _key, signers = signer
    valid, stale, spent, unsigned = _full_store(store, signer, olap)

    report = archive_invalid_capabilities(
        store, profile=make_profile(), olap=olap, now=NOW,
        allowed_signers=signers, require_root_pin=False)

    archive = store / "archive"
    assert sorted(report["archived"]) == [
        "resume_expired.json: expired", "resume_spent.json: consumed"]
    assert not stale.exists() and (archive / stale.name).is_file()
    assert not spent.exists() and (archive / spent.name).is_file()
    # Signatures travel with their capability files.
    assert (archive / (stale.name + ".sig")).is_file()
    assert (archive / (spent.name + ".sig")).is_file()
    # The valid capability is NEVER moved; unsigned stays as evidence.
    assert valid.is_file() and (valid.parent / (valid.name + ".sig")).is_file()
    assert unsigned.is_file()
    # The store afterwards selects the valid capability cleanly.
    chosen, ignored = select(store, signers, olap)
    assert chosen.path == valid
    assert [(e.path.name, e.kind) for e in ignored] == [
        ("resume_unsigned.json", ENTRY_UNSIGNED)]


def test_default_selection_flow_moves_and_deletes_nothing(
        store, signer, olap):
    _key, signers = signer
    _full_store(store, signer, olap)
    before = sorted(p.name for p in store.iterdir())

    chosen, ignored = select(store, signers, olap)

    assert chosen.path.name == "resume_valid.json"
    assert sorted(p.name for p in store.iterdir()) == before
    kinds = {e.path.name: e.kind for e in ignored}
    assert kinds == {"resume_expired.json": ENTRY_EXPIRED,
                     "resume_spent.json": ENTRY_CONSUMED,
                     "resume_unsigned.json": ENTRY_UNSIGNED}


def test_classification_types_every_file_before_ambiguity(
        store, signer, olap):
    _key, signers = signer
    _full_store(store, signer, olap)
    entries = classify_resume_store(
        store, profile=make_profile(), olap=olap, now=NOW,
        allowed_signers=signers, require_root_pin=False)
    kinds = {e.path.name: e.kind for e in entries}
    assert kinds == {"resume_valid.json": ENTRY_VALID,
                     "resume_expired.json": ENTRY_EXPIRED,
                     "resume_spent.json": ENTRY_CONSUMED,
                     "resume_unsigned.json": ENTRY_UNSIGNED}


def test_classification_parses_the_exact_bytes_verified(
        store, signer, olap, monkeypatch):
    """A replacement between signature verification and parsing cannot
    authorize bytes other than the immutable snapshot that was verified."""
    _key, signers = signer
    profile = make_profile()
    original_payload = make_payload(profile, nonce="a" * 64)
    capability = write_capability(
        store, "resume_original.json", original_payload)

    def replace_after_capture(path, signature_path, *, capability_bytes,
                              **_kwargs):
        assert capability_bytes == capability.read_bytes()
        replacement = make_payload(profile, nonce="b" * 64)
        path.write_text(json.dumps(replacement, sort_keys=True) + "\n")
        path.chmod(0o600)
        return {"verified": True}

    monkeypatch.setattr(
        resume_module, "verify_owner_signature", replace_after_capture)
    entries = classify_resume_store(
        store, profile=profile, olap=olap, now=NOW,
        allowed_signers=signers, require_root_pin=False)

    assert len(entries) == 1
    assert entries[0].kind == ENTRY_VALID
    assert entries[0].payload == original_payload
    assert entries[0].record.nonce_sha256 == hashlib.sha256(
        ("a" * 64).encode()).hexdigest()


def test_symlinked_capability_is_never_eligible(
        store, signer, olap, tmp_path):
    key, signers = signer
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(make_payload(make_profile())) + "\n")
    outside.chmod(0o600)
    sign(key, outside)
    (store / "resume_link.json").symlink_to(outside)
    (store / "resume_link.json.sig").symlink_to(
        outside.with_name(outside.name + ".sig"))

    entries = classify_resume_store(
        store, profile=make_profile(), olap=olap, now=NOW,
        allowed_signers=signers, require_root_pin=False)

    assert [(entry.path.name, entry.kind) for entry in entries] == [
        ("resume_link.json", ENTRY_MALFORMED)]
    assert "regular file" in entries[0].detail
