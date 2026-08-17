"""WO3 executable champion succession — socket-free fixture proofs.

Everything runs against ISOLATED temporary stores and ledgers; the live
owner stores, live manifests and decision GPUs are never touched. Proven
here, one test per ordered claim:

- an incompatible candidate refuses with NAMED incompatibilities
  (the 2660-dim SAC vs 11-feature linear reality included);
- a compatible fixture passes the preflight;
- shadow replay lands typed due-bar rows bound by artifact hash and
  due-bar ids, idempotently, with typed refusals for unreproducible bars;
- the promotion capability is single-use, owner-signed, store-bound,
  byte-snapshot (TOCTOU) protected and symlink-ineligible;
- drain carries the ACTUAL post-close balance (session-carry precedent);
- the manifest switch is atomic with a one-operation typed rollback;
- a successor without native SL/TP refuses; and
- outgoing-champion shadow rows land inside the window and refuse
  outside it.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.champion_succession import (
    ACTION_CONTRACT_MISMATCH,
    ARTIFACT_HASH_MISMATCH,
    CARRY_SCHEMA,
    FEATURE_ORDER_MISMATCH,
    MISSING_NATIVE_PROTECTION,
    NO_COMPATIBLE_FEATURE_PROVISIONING,
    OBSERVATION_DIM_MISMATCH,
    PREPROCESSING_CONTRACT_MISMATCH,
    PROMOTION_OPERATION,
    PROMOTION_SCHEMA_VERSION,
    SYMBOL_MISMATCH,
    TIMEFRAME_MISMATCH,
    VERDICT_COMPATIBLE,
    VERDICT_INCOMPATIBLE,
    ActionContract,
    CandidateContract,
    ExecutionContract,
    FeatureProvisioningContract,
    PromotionBinding,
    SeatContract,
    SuccessionError,
    VenueFacts,
    assert_native_protection,
    candidate_activity_report,
    candidate_shadow_replay,
    classify_promotion_store,
    drain_and_carry_session,
    outgoing_shadow_status,
    preflight_candidate,
    promote_paper_champion,
    record_outgoing_shadow_decision,
    register_outgoing_shadow,
    require_compatible,
    require_shadow_evidence,
    rollback_manifest,
    select_promotion_capability,
    switch_manifest_atomically,
    validate_promotion_capability,
)
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_l1_resume import (
    ENTRY_CONSUMED,
    ENTRY_MALFORMED,
    ENTRY_UNSIGNED,
    ENTRY_VALID,
)

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)

LINEAR_FEATURES = (
    "return_1", "return_2", "return_3", "return_5", "return_10",
    "ema_ratio_5_20", "ema_ratio_10_50", "volatility_5", "volatility_20",
    "range_fraction_1", "volume_z20",
)
SAC_FEATURES = tuple(
    f"feat_{index:02d}" for index in range(80)
) + ("return_1", "return_5", "return_10")     # 83, three shared names


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def olap(tmp_path):
    ledger = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    yield ledger
    ledger.close()


@pytest.fixture()
def sessions_schema(olap):
    """The live_model_sessions schema exactly as ModelSessionStore makes
    it (same connection, same statements)."""
    olap._con.executescript("""
    CREATE TABLE IF NOT EXISTS live_model_sessions (
        session_id TEXT PRIMARY KEY,
        venue TEXT NOT NULL,
        account_fingerprint TEXT NOT NULL,
        symbol TEXT NOT NULL,
        model_id TEXT NOT NULL,
        artifact_sha256 TEXT NOT NULL,
        config_sha256 TEXT NOT NULL,
        started_at TEXT NOT NULL,
        starting_balance REAL NOT NULL,
        starting_equity REAL NOT NULL,
        ended_at TEXT,
        ending_balance REAL,
        ending_equity REAL,
        state TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS one_active_model_per_route
    ON live_model_sessions(venue,account_fingerprint,symbol)
    WHERE state='active';
    """)
    return olap


def provisioning() -> FeatureProvisioningContract:
    descriptor = {"feature_contract": "test.closed_bars.linear.v1",
                  "feature_names": list(LINEAR_FEATURES)}
    return FeatureProvisioningContract(
        contract_id="test.closed_bars.linear.v1",
        feature_names=LINEAR_FEATURES,
        preprocessing_sha256=hashlib.sha256(canonical(descriptor)).hexdigest(),
        observation_dim=len(LINEAR_FEATURES),
    )


@pytest.fixture()
def seat(tmp_path) -> SeatContract:
    manifest = tmp_path / "seat" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(
        {"schema": "prediction_provider.live_linear_manifest.v1",
         "model_id": "incumbent-linear-v1",
         "artifact_sha256": "1" * 64}, indent=1) + "\n")
    return SeatContract(
        venue="alpaca_paper",
        asset_id="equity:SPY",
        instrument="SPY",
        timeframe="1d",
        manifest_file=str(manifest),
        provisioning=provisioning(),
        action=ActionContract(kind="probability_threshold", threshold=0.5),
        execution=ExecutionContract(
            native_stop_loss=True, native_take_profit=True,
            native_bracket=True, sl_tp_geometry="fraction_of_reference",
            transfer_policy="close_all"),
    )


def make_candidate(tmp_path, **overrides) -> CandidateContract:
    """A candidate whose declared contracts EXACTLY match the seat."""
    artifact = tmp_path / "candidate_model.json"
    if not artifact.exists():
        artifact.write_text(json.dumps({"model": "compatible-fixture"}))
    values = dict(
        model_id="challenger-linear-v2",
        model_kind="linear",
        artifact_file=str(artifact),
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        config_sha256="c" * 64,
        asset_id="equity:SPY",
        timeframe="1d",
        observation_dim=len(LINEAR_FEATURES),
        feature_names=LINEAR_FEATURES,
        preprocessing_sha256=provisioning().preprocessing_sha256,
        action=ActionContract(kind="probability_threshold", threshold=0.5),
        execution=ExecutionContract(
            native_stop_loss=True, native_take_profit=True,
            native_bracket=True, sl_tp_geometry="fraction_of_reference",
            transfer_policy="close_all"),
    )
    values.update(overrides)
    return CandidateContract(**values)


def make_sac_candidate(tmp_path) -> CandidateContract:
    """Today's reality: a 2660-dim SAC candidate against an 11-feature
    linear live contract."""
    artifact = tmp_path / "sac_model.zip"
    artifact.write_bytes(b"sac-artifact-fixture")
    return make_candidate(
        tmp_path,
        model_id="p1lr-v2-shape-sac",
        model_kind="sb3_sac",
        artifact_file=str(artifact),
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        asset_id="crypto:ETHUSD",
        timeframe="4h",
        observation_dim=2660,
        feature_names=SAC_FEATURES,
        preprocessing_sha256="a" * 64,
        action=ActionContract(kind="continuous_threshold", threshold=0.1),
    )


@pytest.fixture()
def signer(tmp_path):
    key = tmp_path / "owner_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                    "-f", str(key)], check=True)
    pub = (tmp_path / "owner_key.pub").read_text().split()
    signers = tmp_path / "allowed_signers"
    signers.write_text(
        f'owner namespaces="lts-paper-promotion" {pub[0]} {pub[1]}\n')
    signers.chmod(0o644)
    return key, signers


@pytest.fixture()
def cap_store(tmp_path):
    store_dir = tmp_path / "promotion-store"
    store_dir.mkdir(mode=0o700)
    return store_dir


def sign(key: Path, capability: Path) -> Path:
    subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key),
                    "-n", "lts-paper-promotion", str(capability)],
                   check=True, capture_output=True)
    return capability.parent / (capability.name + ".sig")


def write_capability(store_dir: Path, name: str, payload: dict) -> Path:
    path = store_dir / name
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    path.chmod(0o600)
    return path


INCUMBENT = {"model_id": "incumbent-linear-v1",
             "artifact_sha256": "1" * 64, "config_sha256": "2" * 64}
FINGERPRINT = "f1e2d3c4b5a69788"


def capability_payload(seat, candidate, binding_shas, **overrides) -> dict:
    payload = {
        "schema_version": PROMOTION_SCHEMA_VERSION,
        "operation": PROMOTION_OPERATION,
        "venue": seat.venue,
        "asset_id": seat.asset_id,
        "instrument": seat.instrument,
        "timeframe": seat.timeframe,
        "incumbent_model_id": INCUMBENT["model_id"],
        "incumbent_artifact_sha256": INCUMBENT["artifact_sha256"],
        "candidate_model_id": candidate.model_id,
        "candidate_artifact_sha256": candidate.artifact_sha256,
        "candidate_config_sha256": candidate.config_sha256,
        "compatibility_report_sha256": binding_shas[0],
        "shadow_report_sha256": binding_shas[1],
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(seconds=900)).isoformat(),
        "nonce": secrets.token_hex(32),
    }
    payload.update(overrides)
    return payload


def make_binding(seat, candidate, shas) -> PromotionBinding:
    return PromotionBinding(
        seat=seat, candidate=candidate,
        incumbent_model_id=INCUMBENT["model_id"],
        incumbent_artifact_sha256=INCUMBENT["artifact_sha256"],
        compatibility_report_sha256=shas[0],
        shadow_report_sha256=shas[1],
    )


def seed_incumbent_due_bars(olap, seat, count=3):
    for index in range(count):
        bar = f"2026-08-{10 + index:02d}T20:00:00+00:00"
        olap.record_due_bar_decision({
            "venue": seat.venue, "account_fingerprint": FINGERPRINT,
            "asset_id": seat.asset_id, "instrument": seat.instrument,
            "timeframe": seat.timeframe, "bar_close": bar,
            "decided_at": NOW.isoformat(), "feature_cutoff": bar,
            "input_sha256": hashlib.sha256(bar.encode()).hexdigest(),
            "config_sha256": INCUMBENT["config_sha256"],
            "model_id": INCUMBENT["model_id"],
            "artifact_sha256": INCUMBENT["artifact_sha256"],
            "action": "long", "score": 0.61, "outcome": "decided",
            "decision_id": f"{INCUMBENT['model_id']}:{bar}",
        })


def seed_incumbent_session(olap, seat, balance=100000.0):
    olap._con.execute(
        "INSERT INTO live_model_sessions VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("model-session-incumbent0000", seat.venue, FINGERPRINT,
         seat.instrument, INCUMBENT["model_id"],
         INCUMBENT["artifact_sha256"], INCUMBENT["config_sha256"],
         NOW.isoformat(), balance, balance, None, None, None, "active"),
    )


class FakeVenue:
    """Test double for the TRANSPORT only.

    It implements the same ``SuccessionVenue`` interface the real
    adapters in ``app.succession_venue`` implement, so every fact the
    orchestrator uses still arrives through ``fetch_facts()`` and every
    drain still goes through the venue object. ``after_drain`` lets a
    test make the venue tell a DIFFERENT truth once the drain has run —
    which is the only way to prove that pre-drain snapshots never
    authorize a switch.
    """

    venue = "alpaca_paper"

    def __init__(self, *, cash="98750.25", equity="98901.10",
                 open_orders=(), positions=(), after_drain=None,
                 instrument="SPY", fingerprint=FINGERPRINT,
                 capability=None):
        self.state = {"cash": cash, "equity": equity,
                      "open_orders": tuple(open_orders),
                      "positions": tuple(positions)}
        self.after_drain = after_drain
        self.instrument = instrument
        self.fingerprint = fingerprint
        self.capability = capability or dict(GOOD_CAPABILITY_FLAGS)
        self.reasons = []
        self.observations = 0
        self.drained = False

    def fetch_facts(self):
        state = dict(self.state)
        if self.drained and self.after_drain:
            state.update(self.after_drain)
        self.observations += 1
        return VenueFacts(
            venue=self.venue, account_fingerprint=self.fingerprint,
            instrument=self.instrument,
            observed_at=NOW + timedelta(seconds=self.observations),
            cash=state["cash"], equity=state["equity"],
            open_orders=tuple(state["open_orders"]),
            positions=tuple(state["positions"]),
            instrument_capability=self.capability,
            source="fixture:in-memory")

    def drain_for_succession(self, *, reason, incumbent_session_id,
                             successor_artifact_sha256, now):
        self.reasons.append(reason)
        self.drained = True
        return [{"effect_id": "l1e-fixture", "actions": ["cancel"]}]


GOOD_STRATEGY = {"stop_fraction": 0.02, "take_profit_fraction": 0.03}
GOOD_CAPABILITY_FLAGS = {"native_stop_loss": True,
                         "native_take_profit": True, "native_bracket": True}


# ── stage 1: preflight ─────────────────────────────────────────────────


def test_compatible_fixture_passes_preflight(tmp_path, seat):
    report = preflight_candidate(seat, make_candidate(tmp_path), now=NOW)
    assert report["verdict"] == VERDICT_COMPATIBLE
    assert report["incompatibilities"] == []
    # digest binds the document
    assert require_compatible(report, make_candidate(tmp_path))


def test_v2_shape_sac_refuses_with_named_incompatibilities(tmp_path, seat):
    """Today's exact reality: 2660-dim SAC vs 11-feature linear seat."""
    candidate = make_sac_candidate(tmp_path)
    report = preflight_candidate(seat, candidate, now=NOW)
    assert report["verdict"] == VERDICT_INCOMPATIBLE
    codes = {item["code"] for item in report["incompatibilities"]}
    assert {SYMBOL_MISMATCH, TIMEFRAME_MISMATCH, OBSERVATION_DIM_MISMATCH,
            NO_COMPATIBLE_FEATURE_PROVISIONING,
            PREPROCESSING_CONTRACT_MISMATCH,
            ACTION_CONTRACT_MISMATCH} <= codes
    provisioningless = next(
        item for item in report["incompatibilities"]
        if item["code"] == NO_COMPATIBLE_FEATURE_PROVISIONING)
    assert provisioningless["facts"]["missing_count"] == 80
    assert "feat_00" in provisioningless["facts"]["missing_features"]
    # the three name-shared features are NOT reported missing …
    assert "return_1" not in provisioningless["facts"]["missing_features"]
    # … but the preprocessing hash mismatch still blocks them semantically
    with pytest.raises(SuccessionError, match="INCOMPATIBLE"):
        require_compatible(report, candidate)


def test_artifact_hash_mismatch_is_typed(tmp_path, seat):
    candidate = make_candidate(tmp_path, artifact_sha256="9" * 64)
    report = preflight_candidate(seat, candidate, now=NOW)
    codes = [item["code"] for item in report["incompatibilities"]]
    assert ARTIFACT_HASH_MISMATCH in codes


def test_feature_order_mismatch_is_typed(tmp_path, seat):
    reordered = LINEAR_FEATURES[::-1]
    candidate = make_candidate(tmp_path, feature_names=reordered)
    report = preflight_candidate(seat, candidate, now=NOW)
    codes = [item["code"] for item in report["incompatibilities"]]
    assert FEATURE_ORDER_MISMATCH in codes
    assert NO_COMPATIBLE_FEATURE_PROVISIONING not in codes


def test_missing_native_protection_refuses_in_preflight(tmp_path, seat):
    candidate = make_candidate(tmp_path, execution=ExecutionContract(
        native_stop_loss=True, native_take_profit=False,
        native_bracket=True, sl_tp_geometry="fraction_of_reference"))
    report = preflight_candidate(seat, candidate, now=NOW)
    assert report["verdict"] == VERDICT_INCOMPATIBLE
    assert MISSING_NATIVE_PROTECTION in {
        item["code"] for item in report["incompatibilities"]}


def test_tampered_compatibility_report_refuses(tmp_path, seat):
    candidate = make_candidate(tmp_path)
    report = preflight_candidate(seat, candidate, now=NOW)
    report["verdict"] = VERDICT_COMPATIBLE     # unchanged text …
    report["checked_at"] = "2027-01-01T00:00:00+00:00"   # … changed body
    with pytest.raises(SuccessionError, match="digest mismatch"):
        require_compatible(report, candidate)


# ── stage 2: shadow replay ─────────────────────────────────────────────


def shadow_infer(row):
    return {"action": "short", "score": -0.2,
            "input_sha256": row["input_sha256"]}


def test_shadow_replay_binds_and_lands_rows(olap, seat, tmp_path):
    candidate = make_candidate(tmp_path)
    seed_incumbent_due_bars(olap, seat, count=3)
    report = candidate_shadow_replay(
        olap, seat=seat, candidate=candidate, infer=shadow_infer, now=NOW)
    assert report["counts"] == {"incumbent_bars": 3, "shadowed": 3,
                                "refused": 0}
    assert report["candidate"]["artifact_sha256"] == candidate.artifact_sha256
    assert all(bar["input_match"] for bar in report["bars"])
    rows = [r for r in olap.due_bar_decisions(venue=seat.venue)
            if r["model_id"] == candidate.model_id]
    assert len(rows) == 3 and {r["outcome"] for r in rows} == {"shadow"}
    assert require_shadow_evidence(report, candidate)
    # idempotent: replay again, no duplicates, same coverage
    again = candidate_shadow_replay(
        olap, seat=seat, candidate=candidate, infer=shadow_infer, now=NOW)
    assert again["counts"]["shadowed"] == 3
    rows = [r for r in olap.due_bar_decisions(venue=seat.venue)
            if r["model_id"] == candidate.model_id]
    assert len(rows) == 3


def test_shadow_replay_types_refusals_never_guesses(olap, seat, tmp_path):
    candidate = make_candidate(tmp_path)
    seed_incumbent_due_bars(olap, seat, count=2)

    def flaky(row):
        if row["bar_close"].startswith("2026-08-10"):
            raise RuntimeError("persisted inputs unavailable")
        return shadow_infer(row)

    report = candidate_shadow_replay(
        olap, seat=seat, candidate=candidate, infer=flaky, now=NOW)
    assert report["counts"] == {"incumbent_bars": 2, "shadowed": 1,
                                "refused": 1}
    assert "persisted inputs unavailable" in report["refusals"][0]["reason"]
    rows = [r for r in olap.due_bar_decisions(venue=seat.venue)
            if r["model_id"] == candidate.model_id]
    assert len(rows) == 1        # no fabricated row for the refused bar


def test_zero_shadow_evidence_refuses_promotion_gate(olap, seat, tmp_path):
    candidate = make_candidate(tmp_path)
    report = candidate_shadow_replay(
        olap, seat=seat, candidate=candidate, infer=shadow_infer, now=NOW)
    with pytest.raises(SuccessionError, match="zero shadowed"):
        require_shadow_evidence(report, candidate)


# ── stage 3: promotion capability ──────────────────────────────────────


SHAS = ("d" * 64, "e" * 64)


def test_capability_valid_despite_typed_side_files(
        tmp_path, seat, cap_store, signer, olap):
    key, signers = signer
    candidate = make_candidate(tmp_path)
    binding = make_binding(seat, candidate, SHAS)
    valid = write_capability(cap_store, "promotion_valid.json",
                             capability_payload(seat, candidate, SHAS))
    sign(key, valid)
    write_capability(cap_store, "promotion_unsigned.json",
                     capability_payload(seat, candidate, SHAS))
    chosen, ignored = select_promotion_capability(
        cap_store, binding=binding, olap=olap, now=NOW,
        allowed_signers=signers, require_root_pin=False)
    assert chosen.kind == ENTRY_VALID and chosen.path == valid
    assert [(e.path.name, e.kind) for e in ignored] == [
        ("promotion_unsigned.json", ENTRY_UNSIGNED)]


def test_capability_byte_snapshot_toctou(tmp_path, seat, cap_store,
                                         signer, olap):
    """Any byte change AFTER signing refuses — the signature is checked
    against the exact snapshot that is later validated."""
    key, signers = signer
    candidate = make_candidate(tmp_path)
    binding = make_binding(seat, candidate, SHAS)
    payload = capability_payload(seat, candidate, SHAS)
    path = write_capability(cap_store, "promotion_tampered.json", payload)
    sign(key, path)
    tampered = dict(payload, candidate_model_id="evil-model")
    path.write_text(json.dumps(tampered, indent=1, sort_keys=True) + "\n")
    path.chmod(0o600)
    entries = classify_promotion_store(
        cap_store, binding=binding, olap=olap, now=NOW,
        allowed_signers=signers, require_root_pin=False)
    assert [e.kind for e in entries] == [ENTRY_UNSIGNED]


def test_capability_symlink_is_ineligible(tmp_path, seat, cap_store,
                                          signer, olap):
    key, signers = signer
    candidate = make_candidate(tmp_path)
    binding = make_binding(seat, candidate, SHAS)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(
        capability_payload(seat, candidate, SHAS)) + "\n")
    outside.chmod(0o600)
    (cap_store / "promotion_link.json").symlink_to(outside)
    entries = classify_promotion_store(
        cap_store, binding=binding, olap=olap, now=NOW,
        allowed_signers=signers, require_root_pin=False)
    assert [e.kind for e in entries] == [ENTRY_MALFORMED]
    assert "regular file" in entries[0].detail


def test_capability_single_use_burn(tmp_path, seat, cap_store, signer, olap):
    key, signers = signer
    candidate = make_candidate(tmp_path)
    binding = make_binding(seat, candidate, SHAS)
    path = write_capability(cap_store, "promotion_once.json",
                            capability_payload(seat, candidate, SHAS))
    sign(key, path)
    chosen, _ = select_promotion_capability(
        cap_store, binding=binding, olap=olap, now=NOW,
        allowed_signers=signers, require_root_pin=False)
    record = chosen.record
    olap.consume_capability(record.capability_sha256, record.nonce_sha256,
                            record.metadata, "succession-test")
    entries = classify_promotion_store(
        cap_store, binding=binding, olap=olap, now=NOW,
        allowed_signers=signers, require_root_pin=False)
    assert [e.kind for e in entries] == [ENTRY_CONSUMED]
    with pytest.raises(SuccessionError, match="no valid signed"):
        select_promotion_capability(
            cap_store, binding=binding, olap=olap, now=NOW,
            allowed_signers=signers, require_root_pin=False)


def test_two_valid_capabilities_refuse_ambiguity(tmp_path, seat, cap_store,
                                                 signer, olap):
    key, signers = signer
    candidate = make_candidate(tmp_path)
    binding = make_binding(seat, candidate, SHAS)
    for name in ("promotion_a.json", "promotion_b.json"):
        path = write_capability(cap_store, name,
                                capability_payload(seat, candidate, SHAS))
        sign(key, path)
    with pytest.raises(SuccessionError, match="ambiguity"):
        select_promotion_capability(
            cap_store, binding=binding, olap=olap, now=NOW,
            allowed_signers=signers, require_root_pin=False)


def test_capability_binding_mismatches_refuse(tmp_path, seat):
    candidate = make_candidate(tmp_path)
    binding = make_binding(seat, candidate, SHAS)
    good = capability_payload(seat, candidate, SHAS)
    assert validate_promotion_capability(good, binding=binding, now=NOW)
    for corrupt, match in (
        (dict(good, candidate_artifact_sha256="0" * 64),
         "candidate artifact hash"),
        (dict(good, compatibility_report_sha256="0" * 64),
         "compatibility report"),
        (dict(good, shadow_report_sha256="0" * 64), "shadow report"),
        (dict(good, incumbent_model_id="other"), "different incumbent"),
        (dict(good, venue="alpaca_live"), "not a Paper/Demo venue"),
        (dict(good, extra_key=1), "unknown keys"),
    ):
        with pytest.raises(SuccessionError, match=match):
            validate_promotion_capability(corrupt, binding=binding, now=NOW)


# ── stage 4: drain and real-balance carry ──────────────────────────────


def test_drain_waits_while_not_flat_and_never_carries(
        sessions_schema, seat, tmp_path):
    olap = sessions_schema
    seed_incumbent_session(olap, seat)
    result = drain_and_carry_session(
        store=olap, seat=seat,
        venue=FakeVenue(after_drain={"open_orders": ({"id": "o1"},)}),
        successor={"model_id": "challenger-linear-v2",
                   "artifact_sha256": "3" * 64, "config_sha256": "4" * 64},
        account_fingerprint=FINGERPRINT,
        reason="test_drain", now=NOW)
    assert result["state"] == "draining_for_succession"
    assert result["carried"] is False
    # incumbent session untouched
    row = olap._con.execute(
        "SELECT state FROM live_model_sessions").fetchone()
    assert row[0] == "active"


def test_carry_uses_actual_post_close_balance(sessions_schema, seat):
    """The Alpaca 2026-08-03 precedent: closed at X, opened at X, both
    hashes recorded."""
    olap = sessions_schema
    seed_incumbent_session(olap, seat, balance=100000.0)
    result = drain_and_carry_session(
        store=olap, seat=seat, venue=FakeVenue(),
        successor={"model_id": "challenger-linear-v2",
                   "artifact_sha256": "3" * 64, "config_sha256": "4" * 64},
        account_fingerprint=FINGERPRINT,
        reason="test_carry", now=NOW)
    assert result["schema"] == CARRY_SCHEMA and result["carried"] is True
    assert result["outgoing"]["ending_balance"] == 98750.25
    assert result["incoming"]["starting_balance"] == 98750.25
    assert result["incoming"]["starting_equity"] == 98901.10
    assert result["outgoing"]["artifact_sha256"] == "1" * 64
    assert result["incoming"]["artifact_sha256"] == "3" * 64
    rows = olap._con.execute(
        "SELECT model_id, state, starting_balance, ending_balance "
        "FROM live_model_sessions ORDER BY started_at").fetchall()
    states = {row[0]: row for row in rows}
    assert states["incumbent-linear-v1"][1] == "closed"
    assert states["incumbent-linear-v1"][3] == 98750.25
    assert states["challenger-linear-v2"][1] == "active"
    assert states["challenger-linear-v2"][2] == 98750.25
    # durable audit doc records both hashes
    stored = json.loads(olap.get_state(
        f"last_succession_carry:{seat.venue}:{seat.instrument}"))
    assert stored["outgoing"]["artifact_sha256"] == "1" * 64
    assert stored["incoming"]["artifact_sha256"] == "3" * 64


def test_transfer_refused_under_close_all_contract(sessions_schema, seat):
    olap = sessions_schema
    seed_incumbent_session(olap, seat)
    result = drain_and_carry_session(
        store=olap, seat=seat, venue=FakeVenue(
            cash="1.0", equity="1.0",
            after_drain={"positions": ({"symbol": "SPY", "qty": "5"},)}),
        successor={"model_id": "x", "artifact_sha256": "3" * 64,
                   "config_sha256": "4" * 64},
        account_fingerprint=FINGERPRINT,
        reason="test_transfer_refusal", now=NOW)
    assert result["state"] == "draining_for_succession"
    assert result["transfer_policy"] == "close_all"


def test_missing_account_fact_is_a_refusal(sessions_schema, seat):
    olap = sessions_schema
    seed_incumbent_session(olap, seat)
    with pytest.raises(SuccessionError, match="ACTUAL broker fact"):
        drain_and_carry_session(
            store=olap, seat=seat, venue=FakeVenue(cash=None, equity=None),
            successor={"model_id": "x", "artifact_sha256": "3" * 64,
                       "config_sha256": "4" * 64},
            account_fingerprint=FINGERPRINT,
            reason="test_missing_fact", now=NOW)


# ── stage 5: atomic manifest switch and rollback ───────────────────────


def test_switch_preserves_previous_and_rollback_restores(seat):
    manifest = Path(seat.manifest_file)
    original = manifest.read_bytes()
    record = switch_manifest_atomically(
        manifest, {"schema": "prediction_provider.live_linear_manifest.v1",
                   "model_id": "challenger-linear-v2"}, now=NOW)
    assert json.loads(manifest.read_bytes())["model_id"] == (
        "challenger-linear-v2")
    preserved = Path(record["preserved_path"])
    assert preserved.read_bytes() == original
    assert record["previous_sha256"] == hashlib.sha256(original).hexdigest()
    rollback = rollback_manifest(record, now=NOW)
    assert manifest.read_bytes() == original
    assert rollback["restored_sha256"] == record["previous_sha256"]


def test_switch_cas_guard_refuses_concurrent_change(seat):
    with pytest.raises(SuccessionError, match="changed concurrently"):
        switch_manifest_atomically(
            seat.manifest_file, {"model_id": "x"},
            expected_previous_sha256="0" * 64, now=NOW)


def test_rollback_refuses_tampered_preserved_manifest(seat):
    record = switch_manifest_atomically(
        seat.manifest_file, {"model_id": "challenger"}, now=NOW)
    Path(record["preserved_path"]).write_text('{"model_id": "evil"}\n')
    with pytest.raises(SuccessionError, match="rollback refused"):
        rollback_manifest(record, now=NOW)


# ── stage 6: native SL/TP assertion ────────────────────────────────────


def test_native_protection_passes_and_returns_proof():
    proof = assert_native_protection(
        strategy_config=GOOD_STRATEGY,
        instrument_capability=GOOD_CAPABILITY_FLAGS)
    assert proof["geometry"]["stop_fraction"] == 0.02


@pytest.mark.parametrize("strategy,capability,match", [
    ({}, GOOD_CAPABILITY_FLAGS, "lacks numeric"),
    ({"stop_fraction": 0.02}, GOOD_CAPABILITY_FLAGS, "lacks numeric"),
    ({"stop_fraction": 0.0, "take_profit_fraction": 0.03},
     GOOD_CAPABILITY_FLAGS, "must both be positive"),
    (GOOD_STRATEGY, {"native_stop_loss": True, "native_take_profit": True,
                     "native_bracket": False}, "native_bracket"),
])
def test_missing_native_protection_refuses(strategy, capability, match):
    with pytest.raises(SuccessionError, match=match):
        assert_native_protection(strategy_config=strategy,
                                 instrument_capability=capability)


# ── stage 7: outgoing champion shadow window ───────────────────────────


def test_outgoing_shadow_rows_land_inside_window(olap, seat):
    register_outgoing_shadow(
        olap, seat=seat, outgoing_model_id=INCUMBENT["model_id"],
        outgoing_artifact_sha256=INCUMBENT["artifact_sha256"],
        outgoing_config_sha256=INCUMBENT["config_sha256"], now=NOW)
    assert outgoing_shadow_status(olap, seat=seat, now=NOW)["state"] == (
        "active")
    result = record_outgoing_shadow_decision(
        olap, seat=seat, account_fingerprint=FINGERPRINT,
        inference={"last_closed_bar": "2026-08-15T20:00:00+00:00",
                   "action": "hold", "input_sha256": "b" * 64},
        now=NOW + timedelta(days=1))
    assert result["recorded"] is True
    rows = [r for r in olap.due_bar_decisions(venue=seat.venue)
            if r["outcome"] == "outgoing_shadow"]
    assert len(rows) == 1
    assert rows[0]["model_id"] == INCUMBENT["model_id"]
    assert rows[0]["artifact_sha256"] == INCUMBENT["artifact_sha256"]


def test_outgoing_shadow_refuses_after_window(olap, seat):
    register_outgoing_shadow(
        olap, seat=seat, outgoing_model_id=INCUMBENT["model_id"],
        outgoing_artifact_sha256=INCUMBENT["artifact_sha256"],
        outgoing_config_sha256=INCUMBENT["config_sha256"], now=NOW)
    with pytest.raises(SuccessionError, match="window has elapsed"):
        record_outgoing_shadow_decision(
            olap, seat=seat, account_fingerprint=FINGERPRINT,
            inference={"last_closed_bar": "2026-08-30T20:00:00+00:00",
                       "action": "hold", "input_sha256": "b" * 64},
            now=NOW + timedelta(days=8))


def test_outgoing_shadow_window_below_seven_days_refuses(olap, seat):
    with pytest.raises(SuccessionError, match="at least 7.0 days"):
        register_outgoing_shadow(
            olap, seat=seat, outgoing_model_id="m",
            outgoing_artifact_sha256="a" * 64,
            outgoing_config_sha256="c" * 64, window_days=2.0, now=NOW)


# ── the full owner-gated promotion path ────────────────────────────────


def make_activity_record(candidate, **overrides) -> Path:
    """Write the candidate's terminal cell record next to its artifact:
    the campaign's own activity-eligible checkpoint fact (finding 269).
    Overrides build the adversarial variants."""
    record = {
        "schema": "agent_multi.p1_difficulty_lr_cell_record.v2",
        "activity_status": "active",
        "promotion_eligible": True,
        "inactive_cause": None,
        "best_model_path": candidate.artifact_file,
        "best_model_sha256": candidate.artifact_sha256,
    }
    record.update(overrides)
    path = Path(candidate.artifact_file).parent / (
        f"cell_record_{candidate.model_id}.json")
    path.write_text(json.dumps(record, indent=1))
    return path


def valid_activity_report(candidate) -> dict:
    return candidate_activity_report(
        candidate, make_activity_record(candidate), now=NOW)


def promote(olap, seat, candidate, cap_store, signers, **overrides):
    defaults = dict(
        store=olap, venue=FakeVenue(), seat=seat,
        candidate=candidate,
        activity_report=valid_activity_report(candidate),
        strategy_config=GOOD_STRATEGY,
        capability_store_dir=cap_store,
        new_manifest={"schema":
                      "prediction_provider.live_linear_manifest.v1",
                      "model_id": "challenger-linear-v2"},
        allowed_signers=signers, require_root_pin=False, now=NOW,
    )
    defaults.update(overrides)
    return promote_paper_champion(**defaults)


@pytest.fixture()
def promotion_world(tmp_path, sessions_schema, seat, cap_store, signer):
    """Compatible candidate + real reports + seeded seat + signed
    capability bound to the recomputed report digests."""
    olap = sessions_schema
    key, signers = signer
    candidate = make_candidate(tmp_path)
    seed_incumbent_session(olap, seat)
    seed_incumbent_due_bars(olap, seat, count=3)
    compat = preflight_candidate(seat, candidate, now=NOW)
    shadow = candidate_shadow_replay(
        olap, seat=seat, candidate=candidate, infer=shadow_infer, now=NOW)
    shas = (compat["report_sha256"], shadow["report_sha256"])
    path = write_capability(cap_store, "promotion_live.json",
                            capability_payload(seat, candidate, shas))
    sign(key, path)
    return olap, seat, candidate, compat, shadow, cap_store, signers


def test_full_promotion_happy_path(promotion_world):
    olap, seat, candidate, compat, shadow, cap_store, signers = (
        promotion_world)
    result = promote(olap, seat, candidate, cap_store, signers,
                     compatibility_report=compat, shadow_report=shadow)
    assert result["state"] == "promoted"
    assert result["capability_consumed"] is True
    # session carry at the ACTUAL POST-DRAIN balance
    assert result["audit"]["incoming"]["starting_balance"] == 98750.25
    assert result["audit"]["outgoing"]["ending_balance"] == 98750.25
    # manifest flipped and previous preserved
    manifest = json.loads(Path(seat.manifest_file).read_bytes())
    assert manifest["model_id"] == "challenger-linear-v2"
    assert Path(result["manifest_switch"]["preserved_path"]).is_file()
    # outgoing shadow registered for the displaced incumbent
    status = outgoing_shadow_status(olap, seat=seat, now=NOW)
    assert status["state"] == "active"
    assert status["registration"]["model_id"] == INCUMBENT["model_id"]
    # durable audit doc
    audit = json.loads(olap.get_state(
        f"last_promotion:{seat.venue}:{seat.instrument}"))
    assert audit["capability_sha256"] == result["capability_sha256"]
    # single use: the SAME capability can never promote again
    with pytest.raises(SuccessionError,
                       match="no valid signed|already consumed"):
        promote(olap, seat, candidate, cap_store, signers,
                compatibility_report=compat, shadow_report=shadow)


def test_incompatible_candidate_never_touches_capability(
        tmp_path, promotion_world):
    olap, seat, candidate, compat, shadow, cap_store, signers = (
        promotion_world)
    sac = make_sac_candidate(tmp_path)
    sac_report = preflight_candidate(seat, sac, now=NOW)
    with pytest.raises(SuccessionError, match="INCOMPATIBLE"):
        promote(olap, seat, sac, cap_store, signers,
                compatibility_report=sac_report, shadow_report=shadow)
    # the owner's capability for the REAL candidate is still unconsumed
    binding = PromotionBinding(
        seat=seat, candidate=candidate,
        incumbent_model_id=INCUMBENT["model_id"],
        incumbent_artifact_sha256=INCUMBENT["artifact_sha256"],
        compatibility_report_sha256=compat["report_sha256"],
        shadow_report_sha256=shadow["report_sha256"])
    entries = classify_promotion_store(
        cap_store, binding=binding, olap=olap, now=NOW,
        allowed_signers=signers, require_root_pin=False)
    assert [e.kind for e in entries] == [ENTRY_VALID]


def test_promotion_waits_flat_without_consuming(promotion_world):
    olap, seat, candidate, compat, shadow, cap_store, signers = (
        promotion_world)
    result = promote(olap, seat, candidate, cap_store, signers,
                     compatibility_report=compat, shadow_report=shadow,
                     venue=FakeVenue(after_drain={
                         "positions": ({"symbol": "SPY", "qty": "5"},)}))
    assert result["state"] == "draining_for_succession"
    assert result["capability_consumed"] is False
    # incumbent stays seated, manifest untouched
    manifest = json.loads(Path(seat.manifest_file).read_bytes())
    assert manifest["model_id"] == "incumbent-linear-v1"


def test_promotion_missing_protection_refuses_before_burn(promotion_world):
    olap, seat, candidate, compat, shadow, cap_store, signers = (
        promotion_world)
    with pytest.raises(SuccessionError, match="MISSING_NATIVE_PROTECTION"):
        promote(olap, seat, candidate, cap_store, signers,
                compatibility_report=compat, shadow_report=shadow,
                strategy_config={"stop_fraction": 0.02})
    manifest = json.loads(Path(seat.manifest_file).read_bytes())
    assert manifest["model_id"] == "incumbent-linear-v1"
