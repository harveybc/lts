"""Shared, socket-free fixtures for the succession tests.

Not a test module: pytest collects ``test_*.py`` only. Everything here
builds ISOLATED temporary ledgers, manifests, capability stores and owner
keys — no live store, no live manifest, no venue socket, ever.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.champion_succession import (
    ActionContract,
    CandidateContract,
    ExecutionContract,
    FeatureProvisioningContract,
    PromotionBinding,
    PROMOTION_OPERATION,
    PROMOTION_SCHEMA_VERSION,
    SeatContract,
    VenueFacts,
)

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)

LINEAR_FEATURES = (
    "return_1", "return_2", "return_3", "return_5", "return_10",
    "ema_ratio_5_20", "ema_ratio_10_50", "volatility_5", "volatility_20",
    "range_fraction_1", "volume_z20",
)

INCUMBENT = {"model_id": "incumbent-linear-v1",
             "artifact_sha256": "1" * 64, "config_sha256": "2" * 64}
FINGERPRINT = "f1e2d3c4b5a69788"

GOOD_STRATEGY = {"stop_fraction": 0.02, "take_profit_fraction": 0.03}
GOOD_CAPABILITY_FLAGS = {"native_stop_loss": True,
                         "native_take_profit": True, "native_bracket": True}

SESSIONS_SCHEMA = """
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
"""


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def provisioning() -> FeatureProvisioningContract:
    descriptor = {"feature_contract": "test.closed_bars.linear.v1",
                  "feature_names": list(LINEAR_FEATURES)}
    return FeatureProvisioningContract(
        contract_id="test.closed_bars.linear.v1",
        feature_names=LINEAR_FEATURES,
        preprocessing_sha256=hashlib.sha256(canonical(descriptor)).hexdigest(),
        observation_dim=len(LINEAR_FEATURES),
    )


def make_seat(tmp_path: Path) -> SeatContract:
    manifest = tmp_path / "seat" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(
        {"schema": "prediction_provider.live_linear_manifest.v1",
         "model_id": INCUMBENT["model_id"],
         "artifact_sha256": INCUMBENT["artifact_sha256"]}, indent=1) + "\n")
    return SeatContract(
        venue="alpaca_paper", asset_id="equity:SPY", instrument="SPY",
        timeframe="1d", manifest_file=str(manifest),
        provisioning=provisioning(),
        action=ActionContract(kind="probability_threshold", threshold=0.5),
        execution=ExecutionContract(
            native_stop_loss=True, native_take_profit=True,
            native_bracket=True, sl_tp_geometry="fraction_of_reference",
            transfer_policy="close_all"),
    )


def make_candidate(tmp_path: Path, **overrides) -> CandidateContract:
    artifact = tmp_path / "candidate_model.json"
    if not artifact.exists():
        artifact.write_text(json.dumps({"model": "compatible-fixture"}))
    values = dict(
        model_id="challenger-linear-v2", model_kind="linear",
        artifact_file=str(artifact),
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        config_sha256="c" * 64, asset_id="equity:SPY", timeframe="1d",
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


TARGET_MANIFEST = {
    "schema": "prediction_provider.live_linear_manifest.v1",
    "model_id": "challenger-linear-v2",
    "artifact_sha256": "3" * 64,
}


class FakeVenue:
    """Transport double implementing the real ``SuccessionVenue`` shape."""

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
        self.reasons: list[str] = []
        self.observations = 0
        self.drained = False

    def fetch_facts(self) -> VenueFacts:
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


def seed_incumbent_session(olap, seat, balance=100000.0) -> None:
    olap._con.execute(
        "INSERT INTO live_model_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("model-session-incumbent0000", seat.venue, FINGERPRINT,
         seat.instrument, INCUMBENT["model_id"],
         INCUMBENT["artifact_sha256"], INCUMBENT["config_sha256"],
         NOW.isoformat(), balance, balance, None, None, None, "active"))


def seed_incumbent_due_bars(olap, seat, count=3) -> None:
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


def shadow_infer(row):
    return {"action": "long",
            "input_sha256": hashlib.sha256(
                (row["bar_close"] + ":challenger").encode()).hexdigest(),
            "score": 0.58}


def make_signer(tmp_path: Path):
    key = tmp_path / "owner_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                    "-f", str(key)], check=True)
    pub = (tmp_path / "owner_key.pub").read_text().split()
    signers = tmp_path / "allowed_signers"
    signers.write_text(
        f'owner namespaces="lts-paper-promotion" {pub[0]} {pub[1]}\n')
    signers.chmod(0o644)
    return key, signers


def sign(key: Path, capability: Path) -> Path:
    subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key),
                    "-n", "lts-paper-promotion", str(capability)],
                   check=True, capture_output=True)
    return capability.parent / (capability.name + ".sig")


def capability_payload(seat, candidate, binding_shas, *, now=NOW,
                       **overrides) -> dict:
    payload = {
        "schema_version": PROMOTION_SCHEMA_VERSION,
        "operation": PROMOTION_OPERATION,
        "venue": seat.venue, "asset_id": seat.asset_id,
        "instrument": seat.instrument, "timeframe": seat.timeframe,
        "incumbent_model_id": INCUMBENT["model_id"],
        "incumbent_artifact_sha256": INCUMBENT["artifact_sha256"],
        "candidate_model_id": candidate.model_id,
        "candidate_artifact_sha256": candidate.artifact_sha256,
        "candidate_config_sha256": candidate.config_sha256,
        "compatibility_report_sha256": binding_shas[0],
        "shadow_report_sha256": binding_shas[1],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=900)).isoformat(),
        "nonce": secrets.token_hex(32),
    }
    payload.update(overrides)
    return payload


def write_capability(store_dir: Path, name: str, payload: dict) -> Path:
    path = store_dir / name
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    path.chmod(0o600)
    return path


def make_binding(seat, candidate, shas) -> PromotionBinding:
    return PromotionBinding(
        seat=seat, candidate=candidate,
        incumbent_model_id=INCUMBENT["model_id"],
        incumbent_artifact_sha256=INCUMBENT["artifact_sha256"],
        compatibility_report_sha256=shas[0],
        shadow_report_sha256=shas[1])
