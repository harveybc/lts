"""Executable champion succession machinery (WO3, Musashi order 2026-08-15).

Completes the 2026-08-04 succession design (agent-multi work_plan doc 32
S1-S3/S7 and the C3 drain lesson) instead of replacing it. Seven stages,
all fail-closed, all typed, none of which promotes anything by itself:

1. ``preflight_candidate``            — artifact compatibility preflight
   (observation dims + ordered feature list + preprocessing hashes vs the
   live feature-provisioning contract, symbol, timeframe, action contract,
   execution/SL-TP geometry). Refuses with NAMED incompatibilities; the
   COMPATIBLE verdict is the "one valid candidate compatibility proof".
2. ``candidate_shadow_replay``        — the candidate re-decides the SAME
   due bars the incumbent decided, through the existing due-bar lineage
   machinery (``L1ExecutionOlap.record_due_bar_decision``; zero orders,
   no new comparator). Produces a typed report bound by artifact hash and
   due-bar ids.
3. promotion capability                — owner-approved, Paper-only,
   single-use, signed, store-bound, audit-logged. Mirrors the
   ``app.ibkr_l1_resume`` pattern with the 227/240 lessons: typed
   classification of EVERY store file before any ambiguity check, byte-
   snapshot TOCTOU protection, symlink ineligibility, root-pinned
   allowed-signers, UNIQUE digest/nonce burn inside one ledger unit.
   Minting requires the owner (``tools/mint_promotion_capability.py``);
   a minted file stays inert until the owner SIGNS it.
4. ``drain_and_carry_session``        — stop new incumbent risk through the
   journaled ``drain_for_succession`` lifecycle; close (or transfer ONLY
   where the seat contract explicitly permits); the successor's starting
   balance/equity are the ACTUAL post-close broker facts — the proven
   Alpaca 2026-08-03 session-carry precedent (closed at X, opened at X,
   both model hashes recorded).
5. ``switch_manifest_atomically`` / ``rollback_manifest`` — the seat's
   manifest pointer flips via tmp+fsync+rename; the previous manifest is
   preserved; rollback is ONE typed operation.
6. ``assert_native_protection``       — every opening order of the
   successor must carry native SL and TP; a successor whose strategy
   config or venue capability lacks native protection refuses.
7. ``register_outgoing_shadow`` / ``record_outgoing_shadow_decision`` —
   the displaced incumbent keeps producing zero-order shadow decisions on
   the same due bars for a configured window (doc 32 S3: >= 7 days).

``promote_paper_champion`` sequences 1-7. Promotion itself remains
owner-gated: without a freshly minted, owner-signed, unconsumed
capability bound to the exact seat, candidate, incumbent and report
digests, every call refuses. Tests exercise ONLY isolated temporary
stores and ledgers.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from app.ibkr_l1_capability import _parse_utc, capability_digest
from app.ibkr_l1_executor import CapabilityRecord
from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_l1_resume import (
    ENTRY_CONSUMED,
    ENTRY_EXPIRED,
    ENTRY_MALFORMED,
    ENTRY_UNSIGNED,
    ENTRY_VALID,
    StoreEntry,
)

# ── schemas and constants ──────────────────────────────────────────────

COMPATIBILITY_REPORT_SCHEMA = "lts.succession.compatibility_report.v1"
SHADOW_REPORT_SCHEMA = "lts.succession.shadow_report.v1"
PROMOTION_RESULT_SCHEMA = "lts.succession.promotion.v1"
CARRY_SCHEMA = "lts.succession.session_carry.v1"
SWITCH_SCHEMA = "lts.succession.manifest_switch.v1"
ROLLBACK_SCHEMA = "lts.succession.manifest_rollback.v1"
OUTGOING_SHADOW_SCHEMA = "lts.succession.outgoing_shadow.v1"

PROMOTION_SCHEMA_VERSION = "lts.paper_promotion_capability.v1"
PROMOTION_OPERATION = "promote_paper_champion"
PROMOTION_STORE = Path.home() / ".config/lts/paper-promotion-capabilities"
PROMOTION_SIGNATURE_NAMESPACE = "lts-paper-promotion"
PROMOTION_ALLOWED_SIGNERS = Path("/etc/lts/promotion_allowed_signers")
OWNER_PRINCIPAL = "owner"
MAX_PROMOTION_VALIDITY_SECONDS = 3600
_ISSUED_AT_SKEW_SECONDS = 120.0

#: Paper/Demo venues a promotion capability may ever name. Live venues
#: are structurally impossible here — a Live promotion authority does
#: not exist in this module by design.
PROMOTION_VENUES = frozenset({
    "alpaca_paper", "ibkr_paper", "mt5_demo", "oanda_practice",
    "capital_demo", "binance_paper",
})

#: doc 32 S3: the outgoing champion shadows for at least seven days.
OUTGOING_SHADOW_DEFAULT_DAYS = 7.0

# Typed incompatibility codes (stage 1). Every refusal is named.
ARTIFACT_MISSING = "ARTIFACT_MISSING"
ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
TIMEFRAME_MISMATCH = "TIMEFRAME_MISMATCH"
OBSERVATION_DIM_MISMATCH = "OBSERVATION_DIM_MISMATCH"
NO_COMPATIBLE_FEATURE_PROVISIONING = "NO_COMPATIBLE_FEATURE_PROVISIONING"
FEATURE_ORDER_MISMATCH = "FEATURE_ORDER_MISMATCH"
PREPROCESSING_CONTRACT_MISMATCH = "PREPROCESSING_CONTRACT_MISMATCH"
ACTION_CONTRACT_MISMATCH = "ACTION_CONTRACT_MISMATCH"
EXECUTION_CONTRACT_MISMATCH = "EXECUTION_CONTRACT_MISMATCH"
MISSING_NATIVE_PROTECTION = "MISSING_NATIVE_PROTECTION"

VERDICT_COMPATIBLE = "COMPATIBLE"
VERDICT_INCOMPATIBLE = "INCOMPATIBLE"


class SuccessionError(RuntimeError):
    """Typed refusal. Unknown is a refusal; nothing here degrades."""


class PromotionCapabilityExpired(SuccessionError):
    """Typed expiry so an expired side file is ignorable/archivable
    instead of an anonymous refusal (finding 227)."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      default=str).encode()


def _doc_sha256(doc: Mapping[str, Any], *, exclude: str = "report_sha256"
                ) -> str:
    body = {key: value for key, value in doc.items() if key != exclude}
    return hashlib.sha256(_canonical(body)).hexdigest()


# ── typed contracts ────────────────────────────────────────────────────


@dataclass(frozen=True)
class FeatureProvisioningContract:
    """What the seat's live feeder can ACTUALLY provision, verbatim."""

    contract_id: str
    feature_names: tuple[str, ...]
    preprocessing_sha256: str
    observation_dim: int

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["feature_names"] = list(self.feature_names)
        return data


@dataclass(frozen=True)
class ActionContract:
    kind: str                      # "probability_threshold" | "continuous_threshold"
    threshold: float
    actions: tuple[str, ...] = ("long", "short", "hold")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["actions"] = list(self.actions)
        return data


@dataclass(frozen=True)
class ExecutionContract:
    native_stop_loss: bool
    native_take_profit: bool
    native_bracket: bool
    sl_tp_geometry: str            # "fraction_of_reference" | "fixed_price"
    transfer_policy: str = "close_all"   # "close_all" | "transfer_permitted"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SeatContract:
    """Doc 32 S1 exact seat key plus the live provisioning facts."""

    venue: str
    asset_id: str
    instrument: str
    timeframe: str
    manifest_file: str
    provisioning: FeatureProvisioningContract
    action: ActionContract
    execution: ExecutionContract

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue, "asset_id": self.asset_id,
            "instrument": self.instrument, "timeframe": self.timeframe,
            "manifest_file": self.manifest_file,
            "provisioning": self.provisioning.to_dict(),
            "action": self.action.to_dict(),
            "execution": self.execution.to_dict(),
        }


@dataclass(frozen=True)
class CandidateContract:
    """One challenger artifact, hashed, with its declared contracts."""

    model_id: str
    model_kind: str                # "linear" | "sb3_sac" | ...
    artifact_file: str
    artifact_sha256: str
    config_sha256: str
    asset_id: str
    timeframe: str
    observation_dim: int
    feature_names: tuple[str, ...]
    preprocessing_sha256: str
    action: ActionContract
    execution: ExecutionContract

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id, "model_kind": self.model_kind,
            "artifact_file": self.artifact_file,
            "artifact_sha256": self.artifact_sha256,
            "config_sha256": self.config_sha256,
            "asset_id": self.asset_id, "timeframe": self.timeframe,
            "observation_dim": self.observation_dim,
            "feature_names": list(self.feature_names),
            "preprocessing_sha256": self.preprocessing_sha256,
            "action": self.action.to_dict(),
            "execution": self.execution.to_dict(),
        }


# ── stage 1: artifact compatibility preflight ──────────────────────────


def _incompatibility(code: str, detail: str, **facts: Any) -> dict[str, Any]:
    return {"code": code, "detail": detail, "facts": facts}


def preflight_candidate(
    seat: SeatContract,
    candidate: CandidateContract,
    *,
    verify_artifact_bytes: bool = True,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Typed compatibility preflight — the future "one valid candidate
    compatibility proof" gate. Returns a canonical, hashable report whose
    verdict is COMPATIBLE only when EVERY named check passes. Never
    raises for an incompatibility: incompatibility is data, not an
    exception; only a malformed contract raises."""
    now = now or _utc_now()
    problems: list[dict[str, Any]] = []

    artifact = Path(os.path.expandvars(candidate.artifact_file)).expanduser()
    if verify_artifact_bytes:
        if not artifact.is_file():
            problems.append(_incompatibility(
                ARTIFACT_MISSING,
                f"candidate artifact {artifact} does not exist",
                artifact_file=str(artifact)))
        else:
            actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
            if actual != candidate.artifact_sha256:
                problems.append(_incompatibility(
                    ARTIFACT_HASH_MISMATCH,
                    "candidate artifact bytes do not hash to the declared"
                    " artifact_sha256",
                    declared=candidate.artifact_sha256, actual=actual))

    if candidate.asset_id != seat.asset_id:
        problems.append(_incompatibility(
            SYMBOL_MISMATCH,
            f"candidate asset {candidate.asset_id!r} is not the seat asset"
            f" {seat.asset_id!r}",
            seat=seat.asset_id, candidate=candidate.asset_id))
    if candidate.timeframe != seat.timeframe:
        problems.append(_incompatibility(
            TIMEFRAME_MISMATCH,
            f"candidate timeframe {candidate.timeframe!r} is not the seat"
            f" timeframe {seat.timeframe!r}",
            seat=seat.timeframe, candidate=candidate.timeframe))

    provisioning = seat.provisioning
    if candidate.observation_dim != provisioning.observation_dim:
        problems.append(_incompatibility(
            OBSERVATION_DIM_MISMATCH,
            f"candidate expects a {candidate.observation_dim}-dim"
            f" observation; live provisioning produces"
            f" {provisioning.observation_dim} dims",
            candidate_dim=candidate.observation_dim,
            provisioned_dim=provisioning.observation_dim))

    provided = set(provisioning.feature_names)
    missing = [name for name in candidate.feature_names
               if name not in provided]
    if missing:
        problems.append(_incompatibility(
            NO_COMPATIBLE_FEATURE_PROVISIONING,
            f"live provisioning lacks {len(missing)} of the"
            f" {len(candidate.feature_names)} features the candidate"
            " requires",
            missing_count=len(missing), missing_features=missing,
            provisioned_contract=provisioning.contract_id,
            provisioned_count=len(provisioning.feature_names)))
    elif tuple(candidate.feature_names) != tuple(provisioning.feature_names):
        problems.append(_incompatibility(
            FEATURE_ORDER_MISMATCH,
            "every candidate feature is provisioned but the ORDERED lists"
            " differ — feature order is part of the observation contract",
            candidate_order=list(candidate.feature_names),
            provisioned_order=list(provisioning.feature_names)))

    if candidate.preprocessing_sha256 != provisioning.preprocessing_sha256:
        problems.append(_incompatibility(
            PREPROCESSING_CONTRACT_MISMATCH,
            "candidate preprocessing hash does not equal the live"
            " provisioning preprocessing hash — same names never prove"
            " same semantics",
            candidate=candidate.preprocessing_sha256,
            provisioned=provisioning.preprocessing_sha256))

    if (
        candidate.action.kind != seat.action.kind
        or tuple(candidate.action.actions) != tuple(seat.action.actions)
    ):
        problems.append(_incompatibility(
            ACTION_CONTRACT_MISMATCH,
            f"candidate action contract {candidate.action.kind!r}"
            f"/{list(candidate.action.actions)} is not the seat contract"
            f" {seat.action.kind!r}/{list(seat.action.actions)}",
            candidate=candidate.action.to_dict(),
            seat=seat.action.to_dict()))

    if candidate.execution.sl_tp_geometry != seat.execution.sl_tp_geometry:
        problems.append(_incompatibility(
            EXECUTION_CONTRACT_MISMATCH,
            f"candidate SL/TP geometry"
            f" {candidate.execution.sl_tp_geometry!r} is not the seat"
            f" geometry {seat.execution.sl_tp_geometry!r}",
            candidate=candidate.execution.sl_tp_geometry,
            seat=seat.execution.sl_tp_geometry))

    for owner, contract in (("candidate", candidate.execution),
                            ("seat", seat.execution)):
        lacking = [name for name, ok in (
            ("native_stop_loss", contract.native_stop_loss),
            ("native_take_profit", contract.native_take_profit),
            ("native_bracket", contract.native_bracket),
        ) if not ok]
        if lacking:
            problems.append(_incompatibility(
                MISSING_NATIVE_PROTECTION,
                f"{owner} execution contract lacks {lacking} — every"
                " opening order must carry native SL and TP",
                owner=owner, lacking=lacking))

    report = {
        "schema": COMPATIBILITY_REPORT_SCHEMA,
        "verdict": VERDICT_COMPATIBLE if not problems
        else VERDICT_INCOMPATIBLE,
        "incompatibilities": problems,
        "seat": seat.to_dict(),
        "candidate": candidate.to_dict(),
        "checked_at": now.isoformat(),
    }
    report["report_sha256"] = _doc_sha256(report)
    return report


def require_compatible(report: Mapping[str, Any],
                       candidate: CandidateContract) -> str:
    """Re-verify a compatibility report at consumption time (TOCTOU: the
    digest is recomputed from the presented document, never trusted).
    Returns the verified report digest."""
    if report.get("schema") != COMPATIBILITY_REPORT_SCHEMA:
        raise SuccessionError("compatibility report schema is unsupported")
    digest = _doc_sha256(report)
    if digest != report.get("report_sha256"):
        raise SuccessionError(
            "compatibility report digest mismatch — the document changed"
            " after it was produced")
    bound = (report.get("candidate") or {}).get("artifact_sha256")
    if bound != candidate.artifact_sha256:
        raise SuccessionError(
            "compatibility report binds a different candidate artifact")
    if report.get("verdict") != VERDICT_COMPATIBLE:
        codes = sorted({item.get("code") for item in
                        report.get("incompatibilities", [])})
        raise SuccessionError(
            f"candidate is {report.get('verdict')}: {codes} — promotion"
            " requires a COMPATIBLE preflight")
    return digest


# ── stage 2: candidate shadow replay on the incumbent's due bars ───────


def candidate_shadow_replay(
    store: L1ExecutionOlap,
    *,
    seat: SeatContract,
    candidate: CandidateContract,
    infer: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    since: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Replay the candidate over the SAME due bars the incumbent decided.

    Reuses the existing due-bar lineage machinery verbatim: incumbent
    rows come from ``due_bar_decisions`` and every candidate decision is
    persisted through ``record_due_bar_decision`` under the candidate's
    own model identity (the UNIQUE (venue, model_id, timeframe,
    bar_close) key makes replay idempotent and can never collide with
    the incumbent's rows). Zero orders by construction — nothing here
    touches an executor.

    ``infer`` receives the full persisted incumbent row and must
    reproduce the candidate's decision from PERSISTED inputs, returning
    at least ``{"action", "input_sha256"}``. A bar whose inputs cannot
    be reproduced is a typed refusal in the report, never a guess and
    never a fabricated row.
    """
    now = now or _utc_now()
    rows = store.due_bar_decisions(venue=seat.venue, since=since)
    incumbent_rows = [
        row for row in rows
        if row["instrument"] == seat.instrument
        and row["timeframe"] == seat.timeframe
        and row["model_id"] != candidate.model_id
        and row["outcome"] not in ("shadow", "outgoing_shadow")
    ]
    bars: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    incumbents: set[tuple[str, str]] = set()
    for row in incumbent_rows:
        incumbents.add((row["model_id"], row["artifact_sha256"]))
        try:
            inference = dict(infer(row))
            action = str(inference["action"])
            input_sha256 = str(inference["input_sha256"])
            if action not in candidate.action.actions:
                raise SuccessionError(
                    f"candidate action {action!r} is outside the declared"
                    f" action contract {list(candidate.action.actions)}")
            if not input_sha256:
                raise SuccessionError(
                    "candidate inference carries no input lineage hash")
        except Exception as exc:  # typed refusal, never a fabricated row
            refusals.append({"bar_close": row["bar_close"],
                             "reason": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        decision_id = f"shadow:{candidate.model_id}:{row['bar_close']}"
        store.record_due_bar_decision({
            "venue": seat.venue,
            "account_fingerprint": row["account_fingerprint"],
            "asset_id": seat.asset_id,
            "instrument": seat.instrument,
            "timeframe": seat.timeframe,
            "bar_close": row["bar_close"],
            "decided_at": now.isoformat(),
            "feature_cutoff": row.get("feature_cutoff"),
            "input_sha256": input_sha256,
            "config_sha256": candidate.config_sha256,
            "model_id": candidate.model_id,
            "artifact_sha256": candidate.artifact_sha256,
            "action": action,
            "score": inference.get("score"),
            "outcome": "shadow",
            "reason": f"shadow_of:{row['decision_id']}",
            "decision_id": decision_id,
        })
        bars.append({
            "bar_close": row["bar_close"],
            "incumbent_decision_id": row["decision_id"],
            "incumbent_model_id": row["model_id"],
            "incumbent_input_sha256": row["input_sha256"],
            "shadow_decision_id": decision_id,
            "shadow_action": action,
            "shadow_input_sha256": input_sha256,
            "input_match": input_sha256 == row["input_sha256"],
        })
    total = len(incumbent_rows)
    report = {
        "schema": SHADOW_REPORT_SCHEMA,
        "venue": seat.venue,
        "instrument": seat.instrument,
        "timeframe": seat.timeframe,
        "candidate": {
            "model_id": candidate.model_id,
            "artifact_sha256": candidate.artifact_sha256,
            "config_sha256": candidate.config_sha256,
        },
        "incumbents": sorted(
            [{"model_id": model, "artifact_sha256": artifact}
             for model, artifact in incumbents],
            key=lambda item: (item["model_id"], item["artifact_sha256"])),
        "bars": bars,
        "refusals": refusals,
        "counts": {
            "incumbent_bars": total,
            "shadowed": len(bars),
            "refused": len(refusals),
        },
        "coverage_fraction": (len(bars) / total) if total else 0.0,
        "generated_at": now.isoformat(),
    }
    report["report_sha256"] = _doc_sha256(report)
    return report


def require_shadow_evidence(report: Mapping[str, Any],
                            candidate: CandidateContract) -> str:
    """Re-verify a shadow report at consumption time. Returns its digest."""
    if report.get("schema") != SHADOW_REPORT_SCHEMA:
        raise SuccessionError("shadow report schema is unsupported")
    digest = _doc_sha256(report)
    if digest != report.get("report_sha256"):
        raise SuccessionError(
            "shadow report digest mismatch — the document changed after"
            " it was produced")
    bound = (report.get("candidate") or {}).get("artifact_sha256")
    if bound != candidate.artifact_sha256:
        raise SuccessionError(
            "shadow report binds a different candidate artifact")
    counts = report.get("counts") or {}
    if not counts.get("shadowed"):
        raise SuccessionError(
            "shadow report contains zero shadowed due bars — no operating"
            " evidence, no promotion")
    return digest


# ── stage 3: owner promotion capability (ibkr_l1_resume pattern) ───────

_PROMOTION_REQUIRED_KEYS = (
    "schema_version", "operation", "venue", "asset_id", "instrument",
    "timeframe", "incumbent_model_id", "incumbent_artifact_sha256",
    "candidate_model_id", "candidate_artifact_sha256",
    "candidate_config_sha256", "compatibility_report_sha256",
    "shadow_report_sha256", "issued_at", "expires_at", "nonce",
)


@dataclass(frozen=True)
class PromotionBinding:
    """Everything a promotion capability must bind to, exactly."""

    seat: SeatContract
    candidate: CandidateContract
    incumbent_model_id: str
    incumbent_artifact_sha256: str
    compatibility_report_sha256: str
    shadow_report_sha256: str


def validate_promotion_capability(
    payload: Mapping[str, Any],
    *,
    binding: PromotionBinding,
    now: Optional[datetime] = None,
) -> CapabilityRecord:
    """Strict v1 validation. Every failure refuses; expiry is typed."""
    now = now or _utc_now()
    if not isinstance(payload, Mapping):
        raise SuccessionError("promotion capability must be a JSON object")
    missing = [key for key in _PROMOTION_REQUIRED_KEYS if key not in payload]
    if missing:
        raise SuccessionError(
            f"promotion capability missing keys: {missing}")
    unknown = sorted(set(payload) - set(_PROMOTION_REQUIRED_KEYS))
    if unknown:
        raise SuccessionError(
            f"promotion capability has unknown keys: {unknown}")
    if payload["schema_version"] != PROMOTION_SCHEMA_VERSION:
        raise SuccessionError(
            f"promotion capability schema must be"
            f" {PROMOTION_SCHEMA_VERSION!r}")
    if payload["operation"] != PROMOTION_OPERATION:
        raise SuccessionError(
            f"promotion capability operation must be"
            f" {PROMOTION_OPERATION!r}")
    seat = binding.seat
    if payload["venue"] not in PROMOTION_VENUES:
        raise SuccessionError(
            f"promotion capability venue {payload['venue']!r} is not a"
            " Paper/Demo venue — Live promotion authority does not exist"
            " here")
    if payload["venue"] != seat.venue:
        raise SuccessionError(
            "promotion capability venue does not match the seat")
    if payload["asset_id"] != seat.asset_id:
        raise SuccessionError(
            "promotion capability asset does not match the seat")
    if payload["instrument"] != seat.instrument:
        raise SuccessionError(
            "promotion capability instrument does not match the seat")
    if payload["timeframe"] != seat.timeframe:
        raise SuccessionError(
            "promotion capability timeframe does not match the seat")
    if payload["incumbent_model_id"] != binding.incumbent_model_id:
        raise SuccessionError(
            "promotion capability names a different incumbent model")
    if (payload["incumbent_artifact_sha256"]
            != binding.incumbent_artifact_sha256):
        raise SuccessionError(
            "promotion capability incumbent artifact hash mismatch")
    candidate = binding.candidate
    if payload["candidate_model_id"] != candidate.model_id:
        raise SuccessionError(
            "promotion capability names a different candidate model")
    if payload["candidate_artifact_sha256"] != candidate.artifact_sha256:
        raise SuccessionError(
            "promotion capability candidate artifact hash mismatch")
    if payload["candidate_config_sha256"] != candidate.config_sha256:
        raise SuccessionError(
            "promotion capability candidate config hash mismatch")
    if (payload["compatibility_report_sha256"]
            != binding.compatibility_report_sha256):
        raise SuccessionError(
            "promotion capability does not bind the presented"
            " compatibility report")
    if payload["shadow_report_sha256"] != binding.shadow_report_sha256:
        raise SuccessionError(
            "promotion capability does not bind the presented shadow"
            " report")
    issued_at = _parse_utc(payload["issued_at"], "issued_at")
    expires_at = _parse_utc(payload["expires_at"], "expires_at")
    if (issued_at - now).total_seconds() > _ISSUED_AT_SKEW_SECONDS:
        raise SuccessionError(
            "promotion capability issued_at is in the future")
    if expires_at <= issued_at:
        raise SuccessionError(
            "promotion capability expires before it is issued")
    if ((expires_at - issued_at).total_seconds()
            > MAX_PROMOTION_VALIDITY_SECONDS):
        raise SuccessionError(
            f"promotion capability validity exceeds"
            f" {MAX_PROMOTION_VALIDITY_SECONDS}s")
    if now >= expires_at:
        raise PromotionCapabilityExpired(
            "promotion capability is expired")
    nonce = str(payload["nonce"])
    if len(nonce) < 32 or any(c not in "0123456789abcdef" for c in nonce):
        raise SuccessionError(
            "promotion capability nonce must be at least 32 lowercase hex"
            " chars")
    return CapabilityRecord(
        capability_sha256=capability_digest(dict(payload)),
        nonce_sha256=hashlib.sha256(nonce.encode()).hexdigest(),
        metadata={
            "schema_version": PROMOTION_SCHEMA_VERSION,
            "operation": PROMOTION_OPERATION,
            "venue": seat.venue,
            "asset_id": seat.asset_id,
            "instrument": seat.instrument,
            "timeframe": seat.timeframe,
            "incumbent_model_id": binding.incumbent_model_id,
            "incumbent_artifact_sha256": binding.incumbent_artifact_sha256,
            "candidate_model_id": candidate.model_id,
            "candidate_artifact_sha256": candidate.artifact_sha256,
            "compatibility_report_sha256":
                binding.compatibility_report_sha256,
            "shadow_report_sha256": binding.shadow_report_sha256,
            "issued_at": issued_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
    )


def _require_promotion_signer_pin(
    allowed_signers: Path, require_root_pin: bool
) -> None:
    """The root-pinned allowed-signers file is a store-wide precondition:
    while it is absent or weak, promotion is structurally disabled."""
    if not allowed_signers.is_file():
        raise SuccessionError(
            f"owner-signer pin {allowed_signers} does not exist —"
            " promotion is disabled until the owner completes the signer"
            " setup (mirror of docs/security/OWNER_RESUME_SIGNER_SETUP)")
    pin_stat = os.stat(allowed_signers)
    if require_root_pin and pin_stat.st_uid != 0:
        raise SuccessionError(
            f"owner-signer pin {allowed_signers} is not root-owned — an"
            " agent-writable pin is no pin; refusing")
    if pin_stat.st_mode & 0o022:
        raise SuccessionError(
            "owner-signer pin must not be group/other-writable")


def verify_promotion_owner_signature(
    capability_path: Path,
    signature_path: Path,
    *,
    allowed_signers: Path = PROMOTION_ALLOWED_SIGNERS,
    require_root_pin: bool = True,
    capability_bytes: Optional[bytes] = None,
) -> dict[str, Any]:
    """Findings 094/227: the passphrase-protected owner SIGNATURE over the
    exact capability bytes is the human-authentication boundary. The
    signature is verified against the caller's immutable byte snapshot
    (TOCTOU protection); any byte change refuses."""
    _require_promotion_signer_pin(allowed_signers, require_root_pin)
    if not signature_path.is_file():
        raise SuccessionError(
            f"detached owner signature {signature_path} is missing")
    signed_bytes = (
        capability_path.read_bytes()
        if capability_bytes is None
        else bytes(capability_bytes)
    )
    result = subprocess.run(
        ["ssh-keygen", "-Y", "verify",
         "-f", str(allowed_signers),
         "-I", OWNER_PRINCIPAL,
         "-n", PROMOTION_SIGNATURE_NAMESPACE,
         "-s", str(signature_path)],
        input=signed_bytes, capture_output=True, timeout=30,
    )
    if result.returncode != 0:
        error = result.stderr or result.stdout or b""
        if isinstance(error, bytes):
            error = error.decode("utf-8", errors="replace")
        raise SuccessionError(
            "owner signature verification FAILED: "
            + str(error).strip()[:200])
    return {"verified": True, "principal": OWNER_PRINCIPAL,
            "namespace": PROMOTION_SIGNATURE_NAMESPACE,
            "capability_sha256":
                hashlib.sha256(signed_bytes).hexdigest()}


def _check_store_dir(store_dir: Path) -> None:
    if not store_dir.is_dir():
        raise SuccessionError(
            f"promotion capability store {store_dir} does not exist; the"
            " owner must mint one with tools/mint_promotion_capability.py")
    mode = stat.S_IMODE(os.stat(store_dir).st_mode)
    if mode & 0o077:
        raise SuccessionError(
            f"promotion capability store {store_dir} has permissive mode"
            f" {oct(mode)}; required 0o700")


def classify_promotion_store(
    store_dir: Path,
    *,
    binding: PromotionBinding,
    olap: Optional[L1ExecutionOlap] = None,
    now: Optional[datetime] = None,
    allowed_signers: Path = PROMOTION_ALLOWED_SIGNERS,
    require_root_pin: bool = True,
) -> list[StoreEntry]:
    """Finding 227 verbatim: type EVERY top-level JSON BEFORE any
    ambiguity check. Each regular file is captured once as an immutable
    byte snapshot; signature, JSON syntax, schema/binding validity,
    expiry and consumption are all judged against that snapshot. Symlinks
    are ineligible. Nothing here writes, moves or deletes anything."""
    _check_store_dir(store_dir)
    _require_promotion_signer_pin(allowed_signers, require_root_pin)
    now = now or _utc_now()
    entries: list[StoreEntry] = []
    for path in sorted(store_dir.glob("*.json")):
        path_stat = os.lstat(path)
        if not stat.S_ISREG(path_stat.st_mode):
            entries.append(StoreEntry(
                path, ENTRY_MALFORMED,
                "capability entry must be a regular file (no links)"))
            continue
        mode = stat.S_IMODE(path_stat.st_mode)
        if mode & 0o077:
            entries.append(StoreEntry(
                path, ENTRY_MALFORMED,
                f"permissive mode {oct(mode)}; required 0o600"))
            continue
        try:
            signed_bytes = path.read_bytes()
        except OSError as error:
            entries.append(StoreEntry(
                path, ENTRY_MALFORMED, f"unreadable capability: {error}"))
            continue
        try:
            payload = json.loads(signed_bytes)
        except (ValueError, TypeError) as error:
            entries.append(StoreEntry(
                path, ENTRY_MALFORMED, f"unreadable JSON: {error}"))
            continue
        signature_path = Path(str(path) + ".sig")
        try:
            verify_promotion_owner_signature(
                path, signature_path,
                allowed_signers=allowed_signers,
                require_root_pin=require_root_pin,
                capability_bytes=signed_bytes)
        except SuccessionError as error:
            entries.append(StoreEntry(
                path, ENTRY_UNSIGNED, str(error)))
            continue
        try:
            record = validate_promotion_capability(
                payload, binding=binding, now=now)
        except PromotionCapabilityExpired as error:
            entries.append(StoreEntry(
                path, ENTRY_EXPIRED, str(error), payload=payload))
            continue
        except (SuccessionError, ValueError, TypeError) as error:
            entries.append(StoreEntry(
                path, ENTRY_MALFORMED, str(error), payload=payload))
            continue
        if olap is not None and (
            olap.capability_row(record.capability_sha256) is not None
            or olap.nonce_consumed(record.nonce_sha256)
        ):
            entries.append(StoreEntry(
                path, ENTRY_CONSUMED,
                "capability already consumed; a spent capability can"
                " never authorize a later promotion",
                payload=payload, record=record))
            continue
        entries.append(StoreEntry(
            path, ENTRY_VALID,
            "signed, current, seat/candidate/report-bound, unconsumed",
            payload=payload, record=record))
    return entries


def select_promotion_capability(
    store_dir: Path,
    *,
    binding: PromotionBinding,
    olap: Optional[L1ExecutionOlap] = None,
    explicit_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    allowed_signers: Path = PROMOTION_ALLOWED_SIGNERS,
    require_root_pin: bool = True,
) -> tuple[StoreEntry, list[StoreEntry]]:
    """Return ``(chosen, ignored)`` under the finding-227 rules: typed
    side files can never deny the one valid signed capability; two or
    more valid capabilities refuse (ambiguity) unless the owner names one
    with an explicit path INSIDE the protected store."""
    entries = classify_promotion_store(
        store_dir, binding=binding, olap=olap, now=now,
        allowed_signers=allowed_signers, require_root_pin=require_root_pin)

    if explicit_path is not None:
        explicit = Path(os.path.expanduser(str(explicit_path))).resolve()
        if explicit.parent != store_dir.resolve():
            raise SuccessionError(
                f"--capability {explicit} is outside the protected store"
                f" {store_dir}; the owner selects a file inside the store")
        chosen = next(
            (e for e in entries if e.path.resolve() == explicit), None)
        if chosen is None:
            raise SuccessionError(
                f"--capability {explicit.name} does not exist in the"
                " store (only top-level *.json files are eligible)")
        if chosen.kind != ENTRY_VALID:
            raise SuccessionError(
                f"--capability {explicit.name} is {chosen.kind}:"
                f" {chosen.detail}")
        return chosen, [e for e in entries if e.path != chosen.path]

    valid = [e for e in entries if e.kind == ENTRY_VALID]
    ignored = [e for e in entries if e.kind != ENTRY_VALID]
    if not valid:
        reasons = "; ".join(
            f"{e.path.name}: {e.kind} ({e.detail})" for e in ignored)
        raise SuccessionError(
            "no valid signed promotion capability in the store; typed"
            " side files: " + (reasons or "store is empty"))
    if len(valid) > 1:
        names = ", ".join(e.path.name for e in valid)
        raise SuccessionError(
            f"{len(valid)} valid signed current capabilities present"
            f" ({names}); ambiguity is a refusal — the owner keeps"
            " exactly one, or names one explicitly")
    return valid[0], ignored


# ── stage 6: native SL and TP on every opening order ───────────────────


def assert_native_protection(
    *,
    strategy_config: Mapping[str, Any],
    instrument_capability: Mapping[str, Any],
) -> dict[str, Any]:
    """Refuse any successor whose opening orders would not carry native
    SL and TP. Checks BOTH the successor's strategy geometry and the
    venue's live-observed instrument capability. Returns a typed proof."""
    if not isinstance(strategy_config, Mapping):
        raise SuccessionError(
            f"{MISSING_NATIVE_PROTECTION}: successor strategy config is"
            " missing — unknown is a refusal")
    geometry: dict[str, Any]
    stop = strategy_config.get("stop_fraction")
    take = strategy_config.get("take_profit_fraction")
    fixed = strategy_config.get("risk_geometry")
    if isinstance(fixed, Mapping) and fixed.get("mode") == "fixed_price":
        stop_price = fixed.get("stop_price")
        take_price = fixed.get("take_profit_price")
        if not (isinstance(stop_price, (int, float)) and stop_price > 0
                and isinstance(take_price, (int, float)) and take_price > 0):
            raise SuccessionError(
                f"{MISSING_NATIVE_PROTECTION}: fixed-price geometry lacks"
                " positive stop_price/take_profit_price")
        geometry = {"mode": "fixed_price",
                    "stop_price": float(stop_price),
                    "take_profit_price": float(take_price)}
    else:
        try:
            stop = float(stop)
            take = float(take)
        except (TypeError, ValueError):
            raise SuccessionError(
                f"{MISSING_NATIVE_PROTECTION}: successor strategy config"
                " lacks numeric stop_fraction/take_profit_fraction") from None
        if stop <= 0 or take <= 0:
            raise SuccessionError(
                f"{MISSING_NATIVE_PROTECTION}: stop_fraction and"
                " take_profit_fraction must both be positive")
        geometry = {"mode": "fraction_of_reference",
                    "stop_fraction": stop, "take_profit_fraction": take}
    lacking = [name for name in
               ("native_stop_loss", "native_take_profit", "native_bracket")
               if instrument_capability.get(name) is not True]
    if lacking:
        raise SuccessionError(
            f"{MISSING_NATIVE_PROTECTION}: venue instrument capability"
            f" lacks {lacking}; emulated protection is not protection")
    return {
        "native_stop_loss": True,
        "native_take_profit": True,
        "native_bracket": True,
        "geometry": geometry,
    }


# ── stage 4: drain and real-balance session carry ──────────────────────


def _session_id(venue: str, account: str, symbol: str, model_id: str,
                artifact_sha256: str, config_sha256: str) -> str:
    return "model-session-" + hashlib.sha256(
        (
            f"{venue}|{account}|{symbol}|{model_id}|"
            f"{artifact_sha256}|{config_sha256}"
        ).encode()
    ).hexdigest()[:24]


def _active_session(con: sqlite3.Connection, venue: str, account: str,
                    symbol: str) -> Optional[dict[str, Any]]:
    row = con.execute(
        "SELECT session_id,model_id,artifact_sha256,config_sha256,"
        "starting_balance,starting_equity FROM live_model_sessions "
        "WHERE venue=? AND account_fingerprint=? AND symbol=? "
        "AND state='active'",
        (venue, account, symbol),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(
        ("session_id", "model_id", "artifact_sha256", "config_sha256",
         "starting_balance", "starting_equity"), row))


def _carry_sql(
    con: sqlite3.Connection,
    *,
    venue: str,
    account: str,
    symbol: str,
    incumbent_session_id: str,
    successor: Mapping[str, str],
    balance: float,
    equity: float,
    now: datetime,
) -> str:
    """End the incumbent session and open the successor session at the
    SAME actual balance/equity — statement-level only; the caller owns
    the transaction boundary (autocommit standalone, or one atomic
    unit inside ``promote_paper_champion``)."""
    con.execute(
        "UPDATE live_model_sessions SET ended_at=?,ending_balance=?,"
        "ending_equity=?,state='closed' WHERE session_id=? AND"
        " state='active'",
        (now.isoformat(), balance, equity, incumbent_session_id),
    )
    session_id = _session_id(
        venue, account, symbol, successor["model_id"],
        successor["artifact_sha256"], successor["config_sha256"])
    con.execute(
        "INSERT INTO live_model_sessions VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, venue, account, symbol, successor["model_id"],
         successor["artifact_sha256"], successor["config_sha256"],
         now.isoformat(), balance, equity, None, None, None, "active"),
    )
    return session_id


def drain_and_carry_session(
    *,
    store: L1ExecutionOlap,
    executor: Any,
    seat: SeatContract,
    successor: Mapping[str, str],
    account_fingerprint: str,
    open_orders: Sequence[Any],
    positions: Sequence[Any],
    account: Mapping[str, Any],
    reason: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Stage 4 standalone: drain through the journaled lifecycle, then
    carry the ACTUAL post-close balance/equity into the successor session
    (Alpaca 2026-08-03 precedent; both model hashes recorded).

    ``open_orders``/``positions``/``account`` are DIRECT broker facts
    gathered THIS tick by the caller; this function never opens a socket.
    While the seat is not flat (and the contract says ``close_all``) the
    result is a typed ``draining_for_succession`` — no carry happens and
    nothing is consumed."""
    now = now or _utc_now()
    for key in ("model_id", "artifact_sha256", "config_sha256"):
        if not successor.get(key):
            raise SuccessionError(f"successor is missing {key!r}")
    con = store._con
    incumbent = _active_session(
        con, seat.venue, account_fingerprint, seat.instrument)
    if incumbent is None:
        raise SuccessionError(
            "no active incumbent session for this seat — unknown"
            " incumbency is a refusal, not an empty carry")
    drained = executor.drain_for_succession(reason)
    if open_orders or positions:
        if (positions and not open_orders
                and seat.execution.transfer_policy == "transfer_permitted"):
            transferred = [dict(p) if isinstance(p, Mapping) else
                           {"position": str(p)} for p in positions]
        else:
            return {
                "schema": CARRY_SCHEMA,
                "state": "draining_for_succession",
                "carried": False,
                "drained": list(drained),
                "open_orders": len(open_orders),
                "positions": len(positions),
                "transfer_policy": seat.execution.transfer_policy,
            }
    else:
        transferred = []
    try:
        balance = float(account["cash"])
        equity = float(account["equity"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SuccessionError(
            "broker account snapshot lacks cash/equity — the successor"
            " starting state must be an ACTUAL broker fact, never a"
            " default") from exc
    session_id = _carry_sql(
        con, venue=seat.venue, account=account_fingerprint,
        symbol=seat.instrument,
        incumbent_session_id=incumbent["session_id"],
        successor=successor, balance=balance, equity=equity, now=now)
    doc = {
        "schema": CARRY_SCHEMA,
        "state": "carried",
        "carried": True,
        "venue": seat.venue,
        "instrument": seat.instrument,
        "outgoing": {
            "session_id": incumbent["session_id"],
            "model_id": incumbent["model_id"],
            "artifact_sha256": incumbent["artifact_sha256"],
            "ending_balance": balance,
            "ending_equity": equity,
        },
        "incoming": {
            "session_id": session_id,
            "model_id": successor["model_id"],
            "artifact_sha256": successor["artifact_sha256"],
            "config_sha256": successor["config_sha256"],
            "starting_balance": balance,
            "starting_equity": equity,
        },
        "transferred_positions": transferred,
        "drained": list(drained),
        "reason": reason,
        "at": now.isoformat(),
    }
    store.set_state(
        f"last_succession_carry:{seat.venue}:{seat.instrument}",
        json.dumps(doc, sort_keys=True, default=str))
    return doc


# ── stage 5: atomic manifest switch with typed rollback ────────────────


def _fsync_dir(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(target: Path, data: bytes) -> None:
    tmp = target.parent / f".{target.name}.tmp.{os.getpid()}"
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    finally:
        if tmp.exists():
            tmp.unlink()


def switch_manifest_atomically(
    manifest_file: str | Path,
    new_manifest: Mapping[str, Any] | bytes,
    *,
    expected_previous_sha256: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Flip the seat's manifest pointer atomically (tmp+fsync+rename).

    The previous manifest is preserved beside the pointer BEFORE the
    flip, and the returned record is everything ``rollback_manifest``
    needs. ``expected_previous_sha256`` is a compare-and-swap guard: when
    provided, a concurrent change of the pointer refuses instead of
    being clobbered."""
    now = now or _utc_now()
    manifest_path = Path(os.path.expandvars(str(manifest_file))).expanduser()
    if not manifest_path.is_file():
        raise SuccessionError(
            f"seat manifest {manifest_path} does not exist — a promotion"
            " switches an existing pointer, it never creates a seat")
    previous_bytes = manifest_path.read_bytes()
    previous_sha256 = hashlib.sha256(previous_bytes).hexdigest()
    if (expected_previous_sha256 is not None
            and previous_sha256 != expected_previous_sha256):
        raise SuccessionError(
            "seat manifest changed concurrently"
            f" (found {previous_sha256[:16]}…, expected"
            f" {expected_previous_sha256[:16]}…); refusing the switch")
    if isinstance(new_manifest, (bytes, bytearray)):
        new_bytes = bytes(new_manifest)
        json.loads(new_bytes)          # must at least be valid JSON
    else:
        new_bytes = json.dumps(
            dict(new_manifest), indent=1, sort_keys=True).encode() + b"\n"
    new_sha256 = hashlib.sha256(new_bytes).hexdigest()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    preserved = manifest_path.parent / (
        f"{manifest_path.name}.prev.{previous_sha256[:8]}.{stamp}")
    _atomic_write(preserved, previous_bytes)
    _atomic_write(manifest_path, new_bytes)
    return {
        "schema": SWITCH_SCHEMA,
        "manifest_file": str(manifest_path),
        "previous_sha256": previous_sha256,
        "new_sha256": new_sha256,
        "preserved_path": str(preserved),
        "switched_at": now.isoformat(),
    }


def rollback_manifest(switch_record: Mapping[str, Any],
                      *, now: Optional[datetime] = None) -> dict[str, Any]:
    """ONE typed rollback operation: restore the preserved previous
    manifest atomically. Refuses when the preserved bytes do not hash to
    the recorded previous manifest — a tampered backup never rolls in."""
    now = now or _utc_now()
    if switch_record.get("schema") != SWITCH_SCHEMA:
        raise SuccessionError("rollback requires a manifest switch record")
    preserved = Path(str(switch_record["preserved_path"]))
    manifest_path = Path(str(switch_record["manifest_file"]))
    if not preserved.is_file():
        raise SuccessionError(
            f"preserved previous manifest {preserved} is missing —"
            " rollback refused")
    previous_bytes = preserved.read_bytes()
    actual = hashlib.sha256(previous_bytes).hexdigest()
    if actual != switch_record.get("previous_sha256"):
        raise SuccessionError(
            "preserved previous manifest does not hash to the recorded"
            f" previous_sha256 (found {actual[:16]}…); rollback refused")
    _atomic_write(manifest_path, previous_bytes)
    return {
        "schema": ROLLBACK_SCHEMA,
        "manifest_file": str(manifest_path),
        "restored_sha256": actual,
        "rolled_back_from_sha256": switch_record.get("new_sha256"),
        "rolled_back_at": now.isoformat(),
    }


# ── stage 7: outgoing champion shadow window ───────────────────────────


def _outgoing_key(seat: SeatContract) -> str:
    return f"outgoing_shadow:{seat.venue}:{seat.instrument}"


def register_outgoing_shadow(
    store: L1ExecutionOlap,
    *,
    seat: SeatContract,
    outgoing_model_id: str,
    outgoing_artifact_sha256: str,
    outgoing_config_sha256: str,
    window_days: float = OUTGOING_SHADOW_DEFAULT_DAYS,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Register the displaced incumbent as a zero-order shadow for a
    configured window (doc 32 S3: at least seven days)."""
    now = now or _utc_now()
    if window_days < OUTGOING_SHADOW_DEFAULT_DAYS:
        raise SuccessionError(
            f"outgoing shadow window must be at least"
            f" {OUTGOING_SHADOW_DEFAULT_DAYS} days (doc 32 S3)")
    doc = {
        "schema": OUTGOING_SHADOW_SCHEMA,
        "venue": seat.venue,
        "instrument": seat.instrument,
        "timeframe": seat.timeframe,
        "model_id": outgoing_model_id,
        "artifact_sha256": outgoing_artifact_sha256,
        "config_sha256": outgoing_config_sha256,
        "window_days": float(window_days),
        "registered_at": now.isoformat(),
        "expires_at": (now + timedelta(days=window_days)).isoformat(),
    }
    store.set_state(_outgoing_key(seat),
                    json.dumps(doc, sort_keys=True))
    return doc


def outgoing_shadow_status(
    store: L1ExecutionOlap,
    *,
    seat: SeatContract,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = now or _utc_now()
    raw = store.get_state(_outgoing_key(seat), "")
    if not raw:
        return {"state": "none"}
    doc = json.loads(raw)
    expires_at = _parse_utc(doc["expires_at"], "expires_at")
    remaining = (expires_at - now).total_seconds()
    return {
        "state": "active" if remaining > 0 else "expired",
        "registration": doc,
        "remaining_seconds": max(0.0, remaining),
    }


def record_outgoing_shadow_decision(
    store: L1ExecutionOlap,
    *,
    seat: SeatContract,
    account_fingerprint: str,
    inference: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One zero-order due-bar shadow row for the DISPLACED incumbent on
    the same due bar the successor decides. Refuses outside the window
    or without a registration — silence is never coverage."""
    now = now or _utc_now()
    status = outgoing_shadow_status(store, seat=seat, now=now)
    if status["state"] == "none":
        raise SuccessionError(
            "no outgoing shadow registration for this seat")
    if status["state"] != "active":
        raise SuccessionError(
            "outgoing shadow window has elapsed; the registration is"
            f" expired since {status['registration']['expires_at']}")
    registration = status["registration"]
    for key in ("last_closed_bar", "action", "input_sha256"):
        if not inference.get(key):
            raise SuccessionError(
                f"outgoing shadow inference is missing {key!r}")
    bar_close = str(inference["last_closed_bar"])
    decision_id = (
        f"outgoing-shadow:{registration['model_id']}:{bar_close}")
    recorded = store.record_due_bar_decision({
        "venue": seat.venue,
        "account_fingerprint": account_fingerprint,
        "asset_id": seat.asset_id,
        "instrument": seat.instrument,
        "timeframe": seat.timeframe,
        "bar_close": bar_close,
        "decided_at": now.isoformat(),
        "feature_cutoff": inference.get("feature_cutoff", bar_close),
        "input_sha256": str(inference["input_sha256"]),
        "config_sha256": registration["config_sha256"],
        "model_id": registration["model_id"],
        "artifact_sha256": registration["artifact_sha256"],
        "action": str(inference["action"]),
        "score": inference.get("score"),
        "outcome": "outgoing_shadow",
        "reason": "displaced_incumbent_shadow_window",
        "decision_id": decision_id,
    })
    return {
        "recorded": bool(recorded),
        "decision_id": decision_id,
        "bar_close": bar_close,
        "model_id": registration["model_id"],
        "artifact_sha256": registration["artifact_sha256"],
    }


# ── orchestrator ───────────────────────────────────────────────────────


def promote_paper_champion(
    *,
    store: L1ExecutionOlap,
    executor: Any,
    seat: SeatContract,
    candidate: CandidateContract,
    compatibility_report: Mapping[str, Any],
    shadow_report: Mapping[str, Any],
    strategy_config: Mapping[str, Any],
    instrument_capability: Mapping[str, Any],
    capability_store_dir: Path,
    account_fingerprint: str,
    open_orders: Sequence[Any],
    positions: Sequence[Any],
    account: Mapping[str, Any],
    new_manifest: Mapping[str, Any] | bytes,
    allowed_signers: Path = PROMOTION_ALLOWED_SIGNERS,
    require_root_pin: bool = True,
    explicit_capability: Optional[Path] = None,
    outgoing_shadow_days: float = OUTGOING_SHADOW_DEFAULT_DAYS,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Execute one owner-approved Paper succession, fail-closed.

    Order of operations (each refusal leaves the capability unconsumed
    and the seat untouched):

    1. re-verify the compatibility proof (verdict + recomputed digest);
    2. re-verify the shadow evidence (binding + recomputed digest);
    3. assert native SL/TP on the successor's opening orders;
    4. establish the ACTUAL incumbent session and require it to match
       the capability's incumbent binding;
    5. drain; while the seat is not flat this returns the typed
       ``draining_for_succession`` result — nothing is consumed;
    6. select + validate the ONE owner-signed capability;
    7. ONE ``BEGIN IMMEDIATE`` ledger unit: re-check the nonce, burn the
       capability (UNIQUE digest/nonce), carry the actual post-close
       balance/equity into the successor session, register the outgoing
       shadow window and persist the full audit document;
    8. atomically switch the manifest pointer (previous preserved,
       ``rollback_manifest`` is the one typed undo).

    A crash between 7 and 8 leaves a burned capability, a committed
    ledger promotion and the incumbent manifest — fail-closed: the
    runner keeps refusing the successor until the switch is completed
    (re-run with the committed audit doc) or rolled back; the ledger
    audit row names both states.
    """
    now = now or _utc_now()

    compatibility_sha = require_compatible(compatibility_report, candidate)
    shadow_sha = require_shadow_evidence(shadow_report, candidate)
    protection = assert_native_protection(
        strategy_config=strategy_config,
        instrument_capability=instrument_capability)

    con = store._con
    incumbent = _active_session(
        con, seat.venue, account_fingerprint, seat.instrument)
    if incumbent is None:
        raise SuccessionError(
            "no active incumbent session for this seat — promotion"
            " succeeds an incumbent, it never seats into the unknown")

    binding = PromotionBinding(
        seat=seat, candidate=candidate,
        incumbent_model_id=incumbent["model_id"],
        incumbent_artifact_sha256=incumbent["artifact_sha256"],
        compatibility_report_sha256=compatibility_sha,
        shadow_report_sha256=shadow_sha,
    )
    chosen, ignored = select_promotion_capability(
        capability_store_dir, binding=binding, olap=store,
        explicit_path=explicit_capability, now=now,
        allowed_signers=allowed_signers,
        require_root_pin=require_root_pin)
    record = chosen.record
    assert record is not None  # ENTRY_VALID always carries the record

    drained = executor.drain_for_succession(
        reason=f"owner_promotion:{candidate.model_id}")
    if open_orders or positions:
        if not (positions and not open_orders
                and seat.execution.transfer_policy == "transfer_permitted"):
            return {
                "schema": PROMOTION_RESULT_SCHEMA,
                "state": "draining_for_succession",
                "capability_consumed": False,
                "drained": list(drained),
                "open_orders": len(open_orders),
                "positions": len(positions),
            }

    try:
        balance = float(account["cash"])
        equity = float(account["equity"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SuccessionError(
            "broker account snapshot lacks cash/equity — the successor"
            " starting state must be an ACTUAL broker fact") from exc

    successor = {
        "model_id": candidate.model_id,
        "artifact_sha256": candidate.artifact_sha256,
        "config_sha256": candidate.config_sha256,
    }
    shadow_registration = {
        "schema": OUTGOING_SHADOW_SCHEMA,
        "venue": seat.venue,
        "instrument": seat.instrument,
        "timeframe": seat.timeframe,
        "model_id": incumbent["model_id"],
        "artifact_sha256": incumbent["artifact_sha256"],
        "config_sha256": incumbent["config_sha256"],
        "window_days": float(outgoing_shadow_days),
        "registered_at": now.isoformat(),
        "expires_at": (
            now + timedelta(days=float(outgoing_shadow_days))
        ).isoformat(),
    }
    if outgoing_shadow_days < OUTGOING_SHADOW_DEFAULT_DAYS:
        raise SuccessionError(
            f"outgoing shadow window must be at least"
            f" {OUTGOING_SHADOW_DEFAULT_DAYS} days (doc 32 S3)")

    effect_id = f"succession-{record.nonce_sha256[:16]}"
    audit = {
        "schema": PROMOTION_RESULT_SCHEMA,
        "state": "promoted_ledger_committed",
        "venue": seat.venue,
        "instrument": seat.instrument,
        "timeframe": seat.timeframe,
        "capability_sha256": record.capability_sha256,
        "compatibility_report_sha256": compatibility_sha,
        "shadow_report_sha256": shadow_sha,
        "outgoing": {
            "session_id": incumbent["session_id"],
            "model_id": incumbent["model_id"],
            "artifact_sha256": incumbent["artifact_sha256"],
            "ending_balance": balance,
            "ending_equity": equity,
        },
        "incoming": {
            "model_id": successor["model_id"],
            "artifact_sha256": successor["artifact_sha256"],
            "config_sha256": successor["config_sha256"],
            "starting_balance": balance,
            "starting_equity": equity,
        },
        "protection": protection,
        "at": now.isoformat(),
    }
    try:
        with store.atomic_unit():
            if store.nonce_consumed(record.nonce_sha256):
                raise SuccessionError(
                    "this capability was already consumed; promotion"
                    " needs a freshly minted capability")
            still_incumbent = _active_session(
                con, seat.venue, account_fingerprint, seat.instrument)
            if (still_incumbent is None
                    or still_incumbent["session_id"]
                    != incumbent["session_id"]):
                raise SuccessionError(
                    "the incumbent session changed while promoting —"
                    " re-run against current seat truth")
            store.consume_capability(
                record.capability_sha256, record.nonce_sha256,
                {**record.metadata, "consumed_for": "paper_promotion"},
                effect_id)
            session_id = _carry_sql(
                con, venue=seat.venue, account=account_fingerprint,
                symbol=seat.instrument,
                incumbent_session_id=incumbent["session_id"],
                successor=successor, balance=balance, equity=equity,
                now=now)
            audit["incoming"]["session_id"] = session_id
            store.set_state(
                _outgoing_key(seat),
                json.dumps(shadow_registration, sort_keys=True))
            store.set_state(
                f"last_promotion:{seat.venue}:{seat.instrument}",
                json.dumps(audit, sort_keys=True, default=str))
    except sqlite3.IntegrityError as exc:
        raise SuccessionError(
            "capability burn conflicted with a concurrent consumer;"
            " treat this capability as spent") from exc

    switch = switch_manifest_atomically(
        seat.manifest_file, new_manifest, now=now)
    result = {
        **audit,
        "state": "promoted",
        "capability_consumed": True,
        "drained": list(drained),
        "manifest_switch": switch,
        "outgoing_shadow": shadow_registration,
        "ignored_capability_files": [
            {"file": entry.path.name, "kind": entry.kind}
            for entry in ignored
        ],
    }
    store.set_state(
        f"last_promotion:{seat.venue}:{seat.instrument}",
        json.dumps(result, sort_keys=True, default=str))
    return result
