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

Corrections of 2026-08-16 (Musashi order §3, findings 257 and 258):

- broker truth is no longer an argument. The orchestrator takes a real
  :class:`SuccessionVenue` (``app.succession_venue``) that OBSERVES the
  venue and OWNS the drain executor, and it re-observes orders,
  positions, balance and equity AFTER the drain — a pre-drain snapshot
  cannot authorize a switch. The production entry point is
  ``tools/promote_paper_champion.py``.
- the ledger commit and the manifest switch are no longer two
  independent steps. One durable ``promotion_saga`` row carries the
  exact target manifest BYTES and a ``manifest_pending`` state committed
  WITH the capability burn, so a crash anywhere is completable or
  explicitly reversible (``resume_promotion_saga``) without a second
  capability. While a saga is open, ``succession_pending`` makes every
  runner refuse new risk and report the split state.
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
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

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

# ── stage 1b: direct trading-activity evidence (finding 269) ───────────
#
# The defect: promotion DECLARED activity mandatory
# (`activity_required_for_promotion: true` in the runtime authority
# record) while no executable predicate existed anywhere in this
# repository — mechanical viability could reach a seat without one
# measured trade. The evidence demanded here is the campaign's own
# terminal cell record, not an operator assertion: the record is read
# from bytes, hashed, and its typed fields decide.
ACTIVITY_EVIDENCE_SCHEMA = "lts.succession.activity_evidence.v1"
VERDICT_ACTIVITY_EVIDENT = "ACTIVITY_EVIDENT"
VERDICT_ACTIVITY_NOT_EVIDENT = "ACTIVITY_NOT_EVIDENT"

#: terminal cell-record schemas this gate can read (agent-multi P1LR).
ACCEPTED_TERMINAL_RECORD_SCHEMAS = (
    "agent_multi.p1_difficulty_lr_cell_record.v1",
    "agent_multi.p1_difficulty_lr_cell_record.v2",
)
#: a mechanics screen verdict is NEVER activity evidence, even when it
#: says VIABLE everywhere (finding 263: viability is not activity).
MECHANICS_SCREEN_MARKERS = ("screen_verdict", "mechanics")

# Typed activity refusal codes. Every refusal is named.
NO_ACTIVITY_EVIDENCE = "NO_ACTIVITY_EVIDENCE"
ACTIVITY_RECORD_UNREADABLE = "ACTIVITY_RECORD_UNREADABLE"
ACTIVITY_RECORD_SCHEMA_UNSUPPORTED = "ACTIVITY_RECORD_SCHEMA_UNSUPPORTED"
ACTIVITY_EVIDENCE_FROM_MECHANICS_SCREEN = (
    "ACTIVITY_EVIDENCE_FROM_MECHANICS_SCREEN")
ACTIVITY_STATUS_NOT_ACTIVE = "ACTIVITY_STATUS_NOT_ACTIVE"
ACTIVITY_RECORD_NOT_PROMOTION_ELIGIBLE = (
    "ACTIVITY_RECORD_NOT_PROMOTION_ELIGIBLE")
ACTIVITY_ARTIFACT_MISMATCH = "ACTIVITY_ARTIFACT_MISMATCH"

# ── the promotion saga (finding 258) ───────────────────────────────────
#
# The defect: the ledger commit (capability burn + successor session) and
# the filesystem manifest switch were two independent steps. A crash in
# between burned the capability, moved the active session to the
# successor and left the manifest naming the incumbent — permanently,
# because a re-run then selected against the CHANGED active session and
# found its capability spent. Authority and pointer could disagree
# forever with no operation able to reconcile them.
#
# The correction: one durable, resumable saga row. The exact target
# manifest BYTES (not a recipe for them) and the exact previous bytes are
# persisted BEFORE anything is burned; the burn commits together with the
# `manifest_pending` state; and finalize/rollback are two idempotent
# transitions that need no second capability and never re-select against
# the already-changed active session.

SAGA_SCHEMA_VERSION = "lts.succession.saga.v1"

SAGA_PREPARED = "prepared"
SAGA_MANIFEST_PENDING = "manifest_pending"
SAGA_ROLLING_BACK = "rolling_back"
SAGA_COMPLETED = "completed"
SAGA_ROLLED_BACK = "rolled_back"
SAGA_ABORTED = "aborted"
#: while the saga is in ANY of these, the seat's authority and its
#: manifest may disagree: runners refuse new risk and status says so.
SAGA_OPEN_STATES = (SAGA_PREPARED, SAGA_MANIFEST_PENDING, SAGA_ROLLING_BACK)
SAGA_TERMINAL_STATES = (SAGA_COMPLETED, SAGA_ROLLED_BACK, SAGA_ABORTED)

#: Every point at which a crash must leave a completable-or-rollbackable
#: state. The orchestrator calls ``boundary(name)`` after each one; the
#: crash matrix injects a raise at every name in this tuple.
BOUNDARY_FACTS_OBSERVED = "facts_observed"
BOUNDARY_CAPABILITY_VALIDATED = "capability_validated"
BOUNDARY_DRAIN = "drain"
BOUNDARY_FACTS_REFRESHED = "facts_refreshed"
BOUNDARY_LEDGER_PREPARED = "ledger_prepared"
BOUNDARY_CAPABILITY_BURNED = "capability_burned"
BOUNDARY_MANIFEST_TEMP_WRITTEN = "manifest_temp_written"
BOUNDARY_MANIFEST_RENAMED = "manifest_renamed"
BOUNDARY_LEDGER_FINALIZED = "ledger_finalized"

PROMOTION_BOUNDARIES = (
    BOUNDARY_FACTS_OBSERVED,
    BOUNDARY_CAPABILITY_VALIDATED,
    BOUNDARY_DRAIN,
    BOUNDARY_FACTS_REFRESHED,
    BOUNDARY_LEDGER_PREPARED,
    BOUNDARY_CAPABILITY_BURNED,
    BOUNDARY_MANIFEST_TEMP_WRITTEN,
    BOUNDARY_MANIFEST_RENAMED,
    BOUNDARY_LEDGER_FINALIZED,
)


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
    #: the on-disk config the seat manifest must point at after a switch;
    #: empty means "this descriptor cannot build a successor manifest".
    config_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id, "model_kind": self.model_kind,
            "artifact_file": self.artifact_file,
            "artifact_sha256": self.artifact_sha256,
            "config_file": self.config_file,
            "config_sha256": self.config_sha256,
            "asset_id": self.asset_id, "timeframe": self.timeframe,
            "observation_dim": self.observation_dim,
            "feature_names": list(self.feature_names),
            "preprocessing_sha256": self.preprocessing_sha256,
            "action": self.action.to_dict(),
            "execution": self.execution.to_dict(),
        }


# ── direct venue facts (finding 257: broker truth is never an argument) ─


@dataclass(frozen=True)
class VenueFacts:
    """One instantaneous observation of DIRECT broker truth.

    Every field must come from the venue's OWN fact interface (REST
    account/orders/positions, the TWS session, the MT5 bridge snapshot the
    terminal itself posted). ``source`` names that interface verbatim so a
    reader can tell a direct observation from a derived one. Nothing in
    this module ever constructs a ``VenueFacts`` from operator-supplied
    JSON: an entry point that accepts fake account/order/position input as
    broker truth is exactly the defect this type exists to prevent.
    """

    venue: str
    account_fingerprint: str
    instrument: str
    observed_at: datetime
    cash: float
    equity: float
    open_orders: tuple[Any, ...]
    positions: tuple[Any, ...]
    instrument_capability: Mapping[str, Any]
    source: str

    @property
    def flat(self) -> bool:
        return not self.open_orders and not self.positions

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "account_fingerprint": self.account_fingerprint,
            "instrument": self.instrument,
            "observed_at": self.observed_at.isoformat(),
            "cash": self.cash,
            "equity": self.equity,
            "open_orders": len(self.open_orders),
            "positions": len(self.positions),
            "instrument_capability": dict(self.instrument_capability),
            "source": self.source,
        }

    def summary(self) -> dict[str, Any]:
        """Counts and typed availability only — no balances, no tickets."""
        data = self.to_dict()
        data.pop("cash")
        data.pop("equity")
        data["balance_available"] = True
        data["equity_available"] = True
        return data


class SuccessionVenue(Protocol):
    """The narrow interface a real seat must implement to be promotable.

    ``fetch_facts`` is a fresh DIRECT observation every time it is called
    — the orchestrator calls it again AFTER the drain because a pre-drain
    snapshot can never authorize a switch (finding 257/258 correction).
    """

    venue: str

    def fetch_facts(self) -> VenueFacts:
        ...

    def drain_for_succession(
        self,
        *,
        reason: str,
        incumbent_session_id: str,
        successor_artifact_sha256: str,
        now: datetime,
    ) -> list[dict[str, Any]]:
        ...


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


def candidate_activity_report(
    candidate: CandidateContract,
    terminal_record_file: Optional[Path | str],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Typed trading-activity evidence report (finding 269).

    Reads the candidate's terminal cell record — the campaign's own
    immutable measurement — and decides whether a DIRECT
    activity-eligible checkpoint exists whose bytes ARE the candidate
    artifact. Like ``preflight_candidate``, a negative outcome is data,
    not an exception; absence of the record is itself the typed
    ``NO_ACTIVITY_EVIDENCE`` outcome (fail-closed, never a default
    pass)."""
    now = now or _utc_now()
    problems: list[dict[str, Any]] = []
    record: dict[str, Any] = {}
    record_sha: Optional[str] = None
    record_path: Optional[str] = None

    if terminal_record_file is None:
        problems.append(_incompatibility(
            NO_ACTIVITY_EVIDENCE,
            "no terminal cell record was presented — promotion demands"
            " direct activity-eligible checkpoint evidence from the"
            " candidate's own campaign record; its absence refuses"
            " (finding 269)"))
    else:
        path = Path(os.path.expandvars(str(terminal_record_file))
                    ).expanduser()
        record_path = str(path)
        if not path.is_file():
            problems.append(_incompatibility(
                NO_ACTIVITY_EVIDENCE,
                f"terminal cell record {path} does not exist",
                terminal_record_file=str(path)))
        else:
            raw = path.read_bytes()
            record_sha = hashlib.sha256(raw).hexdigest()
            try:
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError("record is not an object")
            except ValueError as error:
                record = {}
                problems.append(_incompatibility(
                    ACTIVITY_RECORD_UNREADABLE,
                    f"terminal cell record is not a JSON object:"
                    f" {error}",
                    terminal_record_file=str(path)))

    if record:
        schema = record.get("schema")
        if schema not in ACCEPTED_TERMINAL_RECORD_SCHEMAS:
            code = (ACTIVITY_EVIDENCE_FROM_MECHANICS_SCREEN
                    if isinstance(schema, str)
                    and any(marker in schema
                            for marker in MECHANICS_SCREEN_MARKERS)
                    else ACTIVITY_RECORD_SCHEMA_UNSUPPORTED)
            problems.append(_incompatibility(
                code,
                f"presented evidence has schema {schema!r} — a"
                " mechanics screen verdict measures viability, never"
                " activity, and only a terminal cell record"
                f" {list(ACCEPTED_TERMINAL_RECORD_SCHEMAS)} carries"
                " the activity-eligible checkpoint fact",
                schema=schema))
        else:
            if record.get("activity_status") != "active":
                problems.append(_incompatibility(
                    ACTIVITY_STATUS_NOT_ACTIVE,
                    "the terminal record's activity_status is"
                    f" {record.get('activity_status')!r} — the policy"
                    " never produced an activity-eligible checkpoint"
                    " (train-tail and validation trade gates both"
                    " passing); a policy that never traded is never"
                    " promoted",
                    activity_status=record.get("activity_status"),
                    inactive_cause=record.get("inactive_cause"),
                    termination_cause=record.get("termination_cause")))
            if record.get("promotion_eligible") is not True:
                problems.append(_incompatibility(
                    ACTIVITY_RECORD_NOT_PROMOTION_ELIGIBLE,
                    "the terminal record does not carry"
                    " promotion_eligible=true — the campaign itself"
                    " typed this cell non-promotable",
                    promotion_eligible=record.get("promotion_eligible")))
            best_sha = record.get("best_model_sha256")
            if best_sha != candidate.artifact_sha256:
                problems.append(_incompatibility(
                    ACTIVITY_ARTIFACT_MISMATCH,
                    "the record's activity-eligible checkpoint"
                    " (best_model_sha256) is not the candidate"
                    " artifact — activity measured on OTHER bytes"
                    " proves nothing about these bytes",
                    record_best_model_sha256=best_sha,
                    candidate_artifact_sha256=candidate.artifact_sha256))

    report = {
        "schema": ACTIVITY_EVIDENCE_SCHEMA,
        "verdict": (VERDICT_ACTIVITY_EVIDENT if not problems
                    else VERDICT_ACTIVITY_NOT_EVIDENT),
        "problems": problems,
        "candidate_model_id": candidate.model_id,
        "candidate_artifact_sha256": candidate.artifact_sha256,
        "terminal_record_file": record_path,
        "terminal_record_sha256": record_sha,
        "activity_status": record.get("activity_status"),
        "checked_at": now.isoformat(),
    }
    report["report_sha256"] = _doc_sha256(report)
    return report


def require_activity_evidence(report: Any,
                              candidate: CandidateContract) -> str:
    """Re-verify activity evidence at consumption time (TOCTOU: the
    digest is recomputed from the presented document, never trusted).
    Absence of the report IS a refusal — there is no calling convention
    that promotes without it (finding 269). Returns the verified
    report digest."""
    if not isinstance(report, Mapping) or not report:
        raise SuccessionError(
            f"{NO_ACTIVITY_EVIDENCE}: promotion requires the typed"
            " activity-evidence report from candidate_activity_report;"
            " none was presented (finding 269)")
    if report.get("schema") != ACTIVITY_EVIDENCE_SCHEMA:
        raise SuccessionError(
            "activity evidence schema is unsupported")
    digest = _doc_sha256(report)
    if digest != report.get("report_sha256"):
        raise SuccessionError(
            "activity evidence digest mismatch — the document changed"
            " after it was produced")
    if report.get("candidate_artifact_sha256") != \
            candidate.artifact_sha256:
        raise SuccessionError(
            "activity evidence binds a different candidate artifact")
    if report.get("verdict") != VERDICT_ACTIVITY_EVIDENT:
        codes = sorted({item.get("code")
                        for item in report.get("problems", [])})
        raise SuccessionError(
            f"candidate is {report.get('verdict')}: {codes} —"
            " promotion requires DIRECT activity-eligible checkpoint"
            " evidence (finding 269)")
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
    if con.execute("SELECT 1 FROM live_model_sessions WHERE session_id=?",
                   (session_id,)).fetchone() is not None:
        # A rolled-back succession leaves the deterministic session id
        # occupied; a later, legitimately re-authorized promotion of the
        # SAME candidate must still be able to open a session.
        session_id = session_id + "-" + hashlib.sha256(
            now.isoformat().encode()).hexdigest()[:8]
    con.execute(
        "INSERT INTO live_model_sessions VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, venue, account, symbol, successor["model_id"],
         successor["artifact_sha256"], successor["config_sha256"],
         now.isoformat(), balance, equity, None, None, None, "active"),
    )
    return session_id


def _post_drain_facts(
    venue: Any, *, before: VenueFacts, seat: SeatContract,
) -> VenueFacts:
    """Re-observe the venue AFTER the drain and prove it is the same seat.

    Finding 257/258 correction: a pre-drain snapshot can never authorize a
    switch. The refreshed observation must name the same account and the
    same instrument, or the route moved under us and everything refuses.
    """
    after = venue.fetch_facts()
    if not isinstance(after, VenueFacts):
        raise SuccessionError(
            "venue.fetch_facts() must return direct VenueFacts; operator"
            " supplied broker truth is never accepted")
    if after.account_fingerprint != before.account_fingerprint:
        raise SuccessionError(
            "the venue account changed between the pre-drain and"
            " post-drain observations; refusing")
    if after.instrument != before.instrument or after.venue != seat.venue:
        raise SuccessionError(
            "the venue route changed between the pre-drain and post-drain"
            " observations; refusing")
    if after.observed_at < before.observed_at:
        raise SuccessionError(
            "the post-drain observation is older than the pre-drain"
            " observation — a stale snapshot cannot authorize a switch")
    return after


def _actual_balance(facts: VenueFacts) -> tuple[float, float]:
    try:
        balance = float(facts.cash)
        equity = float(facts.equity)
    except (TypeError, ValueError) as exc:
        raise SuccessionError(
            "broker account snapshot lacks cash/equity — the successor"
            " starting state must be an ACTUAL broker fact, never a"
            " default") from exc
    if balance != balance or equity != equity:      # NaN is unknown
        raise SuccessionError(
            "broker cash/equity is not a finite number — unknown is a"
            " refusal")
    return balance, equity


def drain_and_carry_session(
    *,
    store: L1ExecutionOlap,
    venue: Any,
    seat: SeatContract,
    successor: Mapping[str, str],
    account_fingerprint: str,
    reason: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Stage 4 standalone: drain through the journaled lifecycle, then
    carry the ACTUAL post-close balance/equity into the successor session
    (Alpaca 2026-08-03 precedent; both model hashes recorded).

    The orders/positions/balance/equity that decide and authorize the
    carry are DIRECT facts observed AFTER the drain, through
    ``venue.fetch_facts()``. Pre-drain facts only decide whether the drain
    is attempted. While the seat is not flat (and the contract says
    ``close_all``) the result is a typed ``draining_for_succession`` — no
    carry happens and nothing is consumed."""
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
    before = venue.fetch_facts()
    drained = venue.drain_for_succession(
        reason=reason, incumbent_session_id=incumbent["session_id"],
        successor_artifact_sha256=successor["artifact_sha256"], now=now)
    facts = _post_drain_facts(venue, before=before, seat=seat)
    open_orders, positions = facts.open_orders, facts.positions
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
                "facts_source": facts.source,
                "facts_observed_at": facts.observed_at.isoformat(),
            }
    else:
        transferred = []
    balance, equity = _actual_balance(facts)
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
        "facts_source": facts.source,
        "facts_observed_at": facts.observed_at.isoformat(),
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


def _atomic_write(
    target: Path,
    data: bytes,
    *,
    boundary: Optional[Callable[[str], None]] = None,
    temp_boundary: str = "",
    rename_boundary: str = "",
) -> None:
    """tmp + fsync + rename, with the two crash boundaries NAMED.

    ``boundary`` (default ``None``) is called after the temp file is
    durable and again after the rename+directory fsync. Production passes
    nothing; the crash-injection tests pass a callable that raises, which
    is how "crash after temp write" and "crash after rename" become
    reproducible instead of theoretical.
    """
    tmp = target.parent / f".{target.name}.tmp.{os.getpid()}"
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if boundary is not None and temp_boundary:
            boundary(temp_boundary)
        os.replace(tmp, target)
        _fsync_dir(target.parent)
        if boundary is not None and rename_boundary:
            boundary(rename_boundary)
    finally:
        if tmp.exists():
            tmp.unlink()


def switch_manifest_atomically(
    manifest_file: str | Path,
    new_manifest: Mapping[str, Any] | bytes,
    *,
    expected_previous_sha256: Optional[str] = None,
    boundary: Optional[Callable[[str], None]] = None,
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
    _atomic_write(manifest_path, new_bytes, boundary=boundary,
                  temp_boundary=BOUNDARY_MANIFEST_TEMP_WRITTEN,
                  rename_boundary=BOUNDARY_MANIFEST_RENAMED)
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


# ── the resumable promotion saga (finding 258) ─────────────────────────

_SAGA_TABLE = """
CREATE TABLE IF NOT EXISTS promotion_saga (
    saga_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    state TEXT NOT NULL,
    venue TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    instrument TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    capability_sha256 TEXT NOT NULL,
    nonce_sha256 TEXT NOT NULL,
    capability_metadata_json TEXT NOT NULL,
    incumbent_session_id TEXT NOT NULL,
    incumbent_model_id TEXT NOT NULL,
    incumbent_artifact_sha256 TEXT NOT NULL,
    incumbent_config_sha256 TEXT NOT NULL,
    successor_model_id TEXT NOT NULL,
    successor_artifact_sha256 TEXT NOT NULL,
    successor_config_sha256 TEXT NOT NULL,
    successor_session_id TEXT,
    carry_balance REAL NOT NULL,
    carry_equity REAL NOT NULL,
    manifest_file TEXT NOT NULL,
    manifest_previous_sha256 TEXT NOT NULL,
    manifest_previous_bytes BLOB NOT NULL,
    manifest_target_sha256 TEXT NOT NULL,
    manifest_target_bytes BLOB NOT NULL,
    preserved_path TEXT,
    outgoing_shadow_json TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    audit_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    outcome_reason TEXT
)
"""

_SAGA_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS one_open_promotion_saga_per_seat
ON promotion_saga(venue, account_fingerprint, instrument)
WHERE state IN ('prepared','manifest_pending','rolling_back')
"""

_SAGA_COLUMNS = (
    "saga_id", "schema_version", "state", "venue", "account_fingerprint",
    "instrument", "timeframe", "capability_sha256", "nonce_sha256",
    "capability_metadata_json", "incumbent_session_id",
    "incumbent_model_id", "incumbent_artifact_sha256",
    "incumbent_config_sha256", "successor_model_id",
    "successor_artifact_sha256", "successor_config_sha256",
    "successor_session_id", "carry_balance", "carry_equity",
    "manifest_file", "manifest_previous_sha256", "manifest_previous_bytes",
    "manifest_target_sha256", "manifest_target_bytes", "preserved_path",
    "outgoing_shadow_json", "facts_json", "audit_json", "created_at",
    "updated_at", "finished_at", "outcome_reason",
)


def ensure_saga_schema(store: L1ExecutionOlap) -> None:
    """Idempotent DDL, safe INSIDE an open ledger transaction.

    Deliberately two ``execute`` calls rather than ``executescript``:
    ``executescript`` COMMITs whatever transaction is open, which would
    silently break the atomicity of the very unit this saga exists to
    guarantee.
    """
    store._con.execute(_SAGA_TABLE)
    store._con.execute(_SAGA_INDEX)


def _saga_dict(row: Sequence[Any]) -> dict[str, Any]:
    saga = dict(zip(_SAGA_COLUMNS, row))
    saga["capability_metadata"] = json.loads(
        saga["capability_metadata_json"])
    saga["outgoing_shadow"] = json.loads(saga["outgoing_shadow_json"])
    saga["facts"] = json.loads(saga["facts_json"])
    saga["audit"] = json.loads(saga["audit_json"])
    for key in ("manifest_previous_bytes", "manifest_target_bytes"):
        saga[key] = bytes(saga[key])
    return saga


def _select_saga(store: L1ExecutionOlap, where: str,
                 params: tuple) -> Optional[dict[str, Any]]:
    ensure_saga_schema(store)
    row = store._con.execute(
        f"SELECT {','.join(_SAGA_COLUMNS)} FROM promotion_saga WHERE"
        f" {where}", params).fetchone()
    return None if row is None else _saga_dict(row)


def saga_row(store: L1ExecutionOlap, saga_id: str) -> Optional[dict[str, Any]]:
    return _select_saga(store, "saga_id=?", (saga_id,))


def open_promotion_saga(
    store: L1ExecutionOlap, *, venue: str, account_fingerprint: str,
    instrument: str,
) -> Optional[dict[str, Any]]:
    """The one open saga for this seat, or None. Open means the seat's
    authority and its manifest may disagree right now."""
    placeholders = ",".join("?" for _ in SAGA_OPEN_STATES)
    return _select_saga(
        store,
        "venue=? AND account_fingerprint=? AND instrument=? AND state IN"
        f" ({placeholders})",
        (venue, account_fingerprint, instrument, *SAGA_OPEN_STATES))


def succession_pending(
    store: L1ExecutionOlap, *, venue: str, instrument: str,
    account_fingerprint: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Runner-facing gate (order §3.4).

    Returns a typed, heartbeat-safe summary while a promotion saga is
    open for this seat, else None. While it returns a value the runner
    must refuse NEW risk: the ledger's active session and the manifest
    the runner would load are not provably the same model.
    """
    ensure_saga_schema(store)
    placeholders = ",".join("?" for _ in SAGA_OPEN_STATES)
    where = f"venue=? AND instrument=? AND state IN ({placeholders})"
    params: tuple = (venue, instrument, *SAGA_OPEN_STATES)
    if account_fingerprint:
        where = ("venue=? AND account_fingerprint=? AND instrument=? AND"
                 f" state IN ({placeholders})")
        params = (venue, account_fingerprint, instrument, *SAGA_OPEN_STATES)
    saga = _select_saga(store, where, params)
    if saga is None:
        return None
    manifest = Path(saga["manifest_file"])
    try:
        current_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    except OSError:
        current_sha256 = ""
    if current_sha256 == saga["manifest_target_sha256"]:
        manifest_points_at = "successor"
    elif current_sha256 == saga["manifest_previous_sha256"]:
        manifest_points_at = "incumbent"
    else:
        manifest_points_at = "unknown"
    return {
        "schema": SAGA_SCHEMA_VERSION,
        "saga_id": saga["saga_id"],
        "state": saga["state"],
        "venue": saga["venue"],
        "instrument": saga["instrument"],
        "ledger_authority": (
            "successor" if saga["state"] in (
                SAGA_MANIFEST_PENDING, SAGA_ROLLING_BACK) else "incumbent"),
        "manifest_points_at": manifest_points_at,
        "split_authority": (
            saga["state"] in (SAGA_MANIFEST_PENDING, SAGA_ROLLING_BACK)
            and manifest_points_at != "successor"),
        "incumbent_model_id": saga["incumbent_model_id"],
        "successor_model_id": saga["successor_model_id"],
        "manifest_file": saga["manifest_file"],
        "since": saga["created_at"],
        "detail": (
            "a promotion saga is open for this seat; new risk is refused"
            " until it is completed or explicitly rolled back"
            " (tools/promote_paper_champion.py --action resume-complete"
            " | resume-rollback)"),
    }


def _saga_id(record: CapabilityRecord) -> str:
    return "promotion-saga-" + record.nonce_sha256[:16]


def _mark_capability(con: sqlite3.Connection, capability_sha256: str,
                     state: str, reason: str) -> None:
    """Keep the burn (the nonce stays spent forever) and record WHY the
    spent capability did not seat a successor."""
    row = con.execute(
        "SELECT metadata_json FROM l1_capabilities WHERE capability_sha256=?",
        (capability_sha256,)).fetchone()
    metadata = json.loads(row[0]) if row else {}
    metadata["spent_outcome"] = state
    metadata["spent_reason"] = reason[:400]
    con.execute(
        "UPDATE l1_capabilities SET state=?, metadata_json=? "
        "WHERE capability_sha256=?",
        (state, json.dumps(metadata, sort_keys=True), capability_sha256))


def prepare_promotion_saga(
    store: L1ExecutionOlap,
    *,
    seat: SeatContract,
    candidate: CandidateContract,
    incumbent: Mapping[str, Any],
    record: CapabilityRecord,
    facts: VenueFacts,
    target_manifest: Mapping[str, Any] | bytes,
    outgoing_shadow: Mapping[str, Any],
    audit: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Persist the WHOLE intended switch before anything is burned.

    The exact target manifest bytes and the exact current bytes are stored
    in the ledger, so the operation is completable (or reversible) from
    the row alone — no recomputation, no second capability, no dependence
    on any file that a crash could have half-written.
    """
    now = now or _utc_now()
    ensure_saga_schema(store)
    manifest_path = Path(
        os.path.expandvars(str(seat.manifest_file))).expanduser()
    if not manifest_path.is_file():
        raise SuccessionError(
            f"seat manifest {manifest_path} does not exist — a promotion"
            " switches an existing pointer, it never creates a seat")
    previous_bytes = manifest_path.read_bytes()
    previous_sha256 = hashlib.sha256(previous_bytes).hexdigest()
    if isinstance(target_manifest, (bytes, bytearray)):
        target_bytes = bytes(target_manifest)
        json.loads(target_bytes)
    else:
        target_bytes = json.dumps(
            dict(target_manifest), indent=1, sort_keys=True).encode() + b"\n"
    target_sha256 = hashlib.sha256(target_bytes).hexdigest()
    if target_sha256 == previous_sha256:
        raise SuccessionError(
            "target manifest is byte-identical to the current manifest —"
            " there is nothing to switch")
    existing = open_promotion_saga(
        store, venue=seat.venue,
        account_fingerprint=facts.account_fingerprint,
        instrument=seat.instrument)
    if existing is not None:
        raise SuccessionError(
            f"seat already has an open promotion saga"
            f" {existing['saga_id']} in state {existing['state']}; finish"
            " or roll it back before starting another")
    saga_id = _saga_id(record)
    values = (
        saga_id, SAGA_SCHEMA_VERSION, SAGA_PREPARED, seat.venue,
        facts.account_fingerprint, seat.instrument, seat.timeframe,
        record.capability_sha256, record.nonce_sha256,
        json.dumps(dict(record.metadata), sort_keys=True),
        str(incumbent["session_id"]), str(incumbent["model_id"]),
        str(incumbent["artifact_sha256"]), str(incumbent["config_sha256"]),
        candidate.model_id, candidate.artifact_sha256,
        candidate.config_sha256, None,
        float(facts.cash), float(facts.equity),
        str(manifest_path), previous_sha256, previous_bytes,
        target_sha256, target_bytes, None,
        json.dumps(dict(outgoing_shadow), sort_keys=True),
        json.dumps(facts.to_dict(), sort_keys=True),
        json.dumps(dict(audit), sort_keys=True, default=str),
        now.isoformat(), now.isoformat(), None, None,
    )
    try:
        with store.atomic_unit():
            store._con.execute(
                "INSERT INTO promotion_saga VALUES"
                f" ({','.join('?' for _ in _SAGA_COLUMNS)})", values)
    except sqlite3.IntegrityError as exc:
        raise SuccessionError(
            "a promotion saga already exists for this capability or seat;"
            " a capability that entered a saga is spent — mint a fresh one"
        ) from exc
    return saga_row(store, saga_id) or {}


def commit_ledger_authority(
    store: L1ExecutionOlap,
    saga: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """``prepared`` → ``manifest_pending`` in ONE ledger transaction.

    The capability burn, the incumbent close, the successor open, the
    outgoing-shadow registration and the DECLARATION that the manifest is
    not yet switched all commit together. After this returns there is a
    window in which authority and pointer disagree — but the window is
    now a durable, named, resumable state instead of an invisible gap.
    """
    now = now or _utc_now()
    saga_id = str(saga["saga_id"])
    con = store._con
    try:
        with store.atomic_unit():
            current = saga_row(store, saga_id)
            if current is None:
                raise SuccessionError(f"promotion saga {saga_id} is gone")
            if current["state"] == SAGA_MANIFEST_PENDING:
                return current                      # idempotent replay
            if current["state"] != SAGA_PREPARED:
                raise SuccessionError(
                    f"promotion saga {saga_id} is {current['state']};"
                    " only a prepared saga can take ledger authority")
            if store.nonce_consumed(current["nonce_sha256"]):
                raise SuccessionError(
                    "this capability was already consumed; promotion needs"
                    " a freshly minted capability")
            still = _active_session(
                con, current["venue"], current["account_fingerprint"],
                current["instrument"])
            if (still is None
                    or still["session_id"] != current["incumbent_session_id"]):
                raise SuccessionError(
                    "the incumbent session changed while promoting —"
                    " re-run against current seat truth")
            store.consume_capability(
                current["capability_sha256"], current["nonce_sha256"],
                {**current["capability_metadata"],
                 "consumed_for": "paper_promotion",
                 "saga_id": saga_id},
                saga_id)
            session_id = _carry_sql(
                con, venue=current["venue"],
                account=current["account_fingerprint"],
                symbol=current["instrument"],
                incumbent_session_id=current["incumbent_session_id"],
                successor={
                    "model_id": current["successor_model_id"],
                    "artifact_sha256": current["successor_artifact_sha256"],
                    "config_sha256": current["successor_config_sha256"],
                },
                balance=float(current["carry_balance"]),
                equity=float(current["carry_equity"]), now=now)
            store.set_state(
                f"outgoing_shadow:{current['venue']}:{current['instrument']}",
                current["outgoing_shadow_json"])
            audit = dict(current["audit"])
            audit["state"] = "promoted_ledger_committed"
            audit["saga_id"] = saga_id
            audit.setdefault("incoming", {})["session_id"] = session_id
            con.execute(
                "UPDATE promotion_saga SET state=?, successor_session_id=?,"
                " audit_json=?, updated_at=? WHERE saga_id=?",
                (SAGA_MANIFEST_PENDING, session_id,
                 json.dumps(audit, sort_keys=True, default=str),
                 now.isoformat(), saga_id))
            store.set_state(
                f"last_promotion:{current['venue']}:{current['instrument']}",
                json.dumps(audit, sort_keys=True, default=str))
    except sqlite3.IntegrityError as exc:
        raise SuccessionError(
            "capability burn conflicted with a concurrent consumer;"
            " treat this capability as spent") from exc
    return saga_row(store, saga_id) or {}


def _switch_manifest_for_saga(
    saga: Mapping[str, Any],
    *,
    now: datetime,
    boundary: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Idempotent, byte-exact pointer flip driven ONLY by the saga row."""
    manifest_path = Path(str(saga["manifest_file"]))
    target_bytes = bytes(saga["manifest_target_bytes"])
    target_sha256 = str(saga["manifest_target_sha256"])
    previous_sha256 = str(saga["manifest_previous_sha256"])
    if not manifest_path.is_file():
        raise SuccessionError(
            f"seat manifest {manifest_path} disappeared while the"
            " promotion saga was pending; refusing to recreate a seat")
    current_bytes = manifest_path.read_bytes()
    current_sha256 = hashlib.sha256(current_bytes).hexdigest()
    if current_sha256 == target_sha256:
        return {
            "schema": SWITCH_SCHEMA,
            "manifest_file": str(manifest_path),
            "previous_sha256": previous_sha256,
            "new_sha256": target_sha256,
            "preserved_path": saga.get("preserved_path"),
            "already_switched": True,
            "switched_at": now.isoformat(),
        }
    if current_sha256 != previous_sha256:
        raise SuccessionError(
            "the seat manifest is neither the recorded previous nor the"
            f" recorded target manifest (found {current_sha256[:16]}…);"
            " a third party changed it — refusing to complete or roll"
            " back until the owner reconciles it")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    preserved = manifest_path.parent / (
        f"{manifest_path.name}.prev.{previous_sha256[:8]}.{stamp}")
    _atomic_write(preserved, current_bytes)
    _atomic_write(manifest_path, target_bytes, boundary=boundary,
                  temp_boundary=BOUNDARY_MANIFEST_TEMP_WRITTEN,
                  rename_boundary=BOUNDARY_MANIFEST_RENAMED)
    written = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if written != target_sha256:
        raise SuccessionError(
            "the switched manifest does not hash to the recorded target"
            " byte snapshot; refusing to declare the promotion complete")
    return {
        "schema": SWITCH_SCHEMA,
        "manifest_file": str(manifest_path),
        "previous_sha256": previous_sha256,
        "new_sha256": target_sha256,
        "preserved_path": str(preserved),
        "already_switched": False,
        "switched_at": now.isoformat(),
    }


def finalize_promotion_saga(
    store: L1ExecutionOlap,
    saga: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    boundary: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """``manifest_pending`` → ``completed``. Idempotent in both halves:
    the switch is a no-op when the manifest already carries the recorded
    target bytes, and a completed saga finalizes to itself."""
    now = now or _utc_now()
    saga_id = str(saga["saga_id"])
    current = saga_row(store, saga_id)
    if current is None:
        raise SuccessionError(f"promotion saga {saga_id} is gone")
    if current["state"] == SAGA_COMPLETED:
        return {**_saga_result(current), "replayed": True}
    if current["state"] != SAGA_MANIFEST_PENDING:
        raise SuccessionError(
            f"promotion saga {saga_id} is {current['state']}; only a"
            " manifest_pending saga can be completed")
    switch = _switch_manifest_for_saga(current, now=now, boundary=boundary)
    with store.atomic_unit():
        store._con.execute(
            "UPDATE promotion_saga SET state=?, preserved_path=?,"
            " updated_at=?, finished_at=?, outcome_reason=?"
            " WHERE saga_id=? AND state=?",
            (SAGA_COMPLETED, switch.get("preserved_path"), now.isoformat(),
             now.isoformat(), "manifest switched to the recorded target"
             " byte snapshot", saga_id, SAGA_MANIFEST_PENDING))
    if boundary is not None:
        boundary(BOUNDARY_LEDGER_FINALIZED)
    completed = saga_row(store, saga_id) or {}
    result = {**_saga_result(completed), "manifest_switch": switch}
    store.set_state(
        f"last_promotion:{completed['venue']}:{completed['instrument']}",
        json.dumps(result, sort_keys=True, default=str))
    return result


def _rollback_manifest_for_saga(
    saga: Mapping[str, Any], *, now: datetime,
) -> dict[str, Any]:
    manifest_path = Path(str(saga["manifest_file"]))
    previous_bytes = bytes(saga["manifest_previous_bytes"])
    previous_sha256 = str(saga["manifest_previous_sha256"])
    target_sha256 = str(saga["manifest_target_sha256"])
    if not manifest_path.is_file():
        raise SuccessionError(
            f"seat manifest {manifest_path} is missing; rollback refused")
    current_sha256 = hashlib.sha256(
        manifest_path.read_bytes()).hexdigest()
    if current_sha256 == previous_sha256:
        return {"schema": ROLLBACK_SCHEMA, "restored": False,
                "manifest_file": str(manifest_path),
                "restored_sha256": previous_sha256,
                "rolled_back_at": now.isoformat()}
    if current_sha256 != target_sha256:
        raise SuccessionError(
            "the seat manifest is neither the recorded previous nor the"
            f" recorded target manifest (found {current_sha256[:16]}…);"
            " rollback refused")
    _atomic_write(manifest_path, previous_bytes)
    written = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if written != previous_sha256:
        raise SuccessionError(
            "the restored manifest does not hash to the recorded previous"
            " byte snapshot; rollback refused")
    return {"schema": ROLLBACK_SCHEMA, "restored": True,
            "manifest_file": str(manifest_path),
            "restored_sha256": previous_sha256,
            "rolled_back_from_sha256": target_sha256,
            "rolled_back_at": now.isoformat()}


def rollback_promotion_saga(
    store: L1ExecutionOlap,
    saga: Mapping[str, Any],
    *,
    reason: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Explicitly undo the SAME operation, coherently.

    ``prepared``        → ``aborted``: nothing was burned by the ledger
      yet, so the burn happens HERE with the abort reason. A capability
      that entered a saga is spent whatever the outcome — the store shows
      it CONSUMED and the ledger row records why.
    ``manifest_pending``/``rolling_back`` → ``rolled_back``: the manifest
      is restored to the recorded previous bytes FIRST (so a crash mid
      rollback never leaves the successor manifest with the incumbent
      seated), then one ledger unit closes the successor session,
      reactivates the incumbent session unchanged, clears the outgoing
      shadow registration and marks the spent capability with the reason.
    """
    now = now or _utc_now()
    saga_id = str(saga["saga_id"])
    con = store._con
    current = saga_row(store, saga_id)
    if current is None:
        raise SuccessionError(f"promotion saga {saga_id} is gone")
    if current["state"] in (SAGA_ROLLED_BACK, SAGA_ABORTED):
        return {**_saga_result(current), "replayed": True}
    if current["state"] == SAGA_COMPLETED:
        raise SuccessionError(
            f"promotion saga {saga_id} already completed; a completed"
            " promotion is undone by a NEW owner-signed succession, never"
            " by a rollback")
    if current["state"] == SAGA_PREPARED:
        with store.atomic_unit():
            if not store.nonce_consumed(current["nonce_sha256"]):
                store.consume_capability(
                    current["capability_sha256"], current["nonce_sha256"],
                    {**current["capability_metadata"],
                     "consumed_for": "paper_promotion_aborted",
                     "saga_id": saga_id},
                    saga_id)
            _mark_capability(con, current["capability_sha256"],
                             "consumed_saga_aborted", reason)
            con.execute(
                "UPDATE promotion_saga SET state=?, updated_at=?,"
                " finished_at=?, outcome_reason=? WHERE saga_id=? AND"
                " state=?",
                (SAGA_ABORTED, now.isoformat(), now.isoformat(), reason,
                 saga_id, SAGA_PREPARED))
        return {**_saga_result(saga_row(store, saga_id) or {}),
                "manifest_rollback": {"restored": False,
                                      "detail": "manifest was never"
                                                " switched"}}
    # manifest_pending / rolling_back
    if current["state"] == SAGA_MANIFEST_PENDING:
        with store.atomic_unit():
            con.execute(
                "UPDATE promotion_saga SET state=?, updated_at=?,"
                " outcome_reason=? WHERE saga_id=? AND state=?",
                (SAGA_ROLLING_BACK, now.isoformat(), reason, saga_id,
                 SAGA_MANIFEST_PENDING))
        current = saga_row(store, saga_id) or current
    restored = _rollback_manifest_for_saga(current, now=now)
    with store.atomic_unit():
        successor_session = current.get("successor_session_id")
        if successor_session:
            con.execute(
                "UPDATE live_model_sessions SET state='rolled_back',"
                " ended_at=? WHERE session_id=? AND state='active'",
                (now.isoformat(), successor_session))
        con.execute(
            "UPDATE live_model_sessions SET state='active', ended_at=NULL,"
            " ending_balance=NULL, ending_equity=NULL WHERE session_id=?",
            (current["incumbent_session_id"],))
        store.set_state(
            f"outgoing_shadow:{current['venue']}:{current['instrument']}",
            "")
        _mark_capability(con, current["capability_sha256"],
                         "consumed_saga_rolled_back", reason)
        con.execute(
            "UPDATE promotion_saga SET state=?, updated_at=?,"
            " finished_at=?, outcome_reason=? WHERE saga_id=?",
            (SAGA_ROLLED_BACK, now.isoformat(), now.isoformat(), reason,
             saga_id))
    rolled = saga_row(store, saga_id) or {}
    result = {**_saga_result(rolled), "manifest_rollback": restored}
    store.set_state(
        f"last_promotion:{rolled['venue']}:{rolled['instrument']}",
        json.dumps(result, sort_keys=True, default=str))
    return result


def _saga_result(saga: Mapping[str, Any]) -> dict[str, Any]:
    """The typed, JSON-safe view of a saga row (never the raw bytes)."""
    if not saga:
        return {}
    state = str(saga["state"])
    return {
        "schema": PROMOTION_RESULT_SCHEMA,
        "saga_id": saga["saga_id"],
        "saga_state": state,
        "state": {
            SAGA_COMPLETED: "promoted",
            SAGA_ROLLED_BACK: "rolled_back",
            SAGA_ABORTED: "aborted",
            SAGA_PREPARED: "prepared",
            SAGA_MANIFEST_PENDING: "manifest_pending",
            SAGA_ROLLING_BACK: "rolling_back",
        }.get(state, state),
        "venue": saga["venue"],
        "instrument": saga["instrument"],
        "timeframe": saga["timeframe"],
        "capability_consumed": state != SAGA_PREPARED,
        "capability_sha256": saga["capability_sha256"],
        "incumbent": {
            "session_id": saga["incumbent_session_id"],
            "model_id": saga["incumbent_model_id"],
            "artifact_sha256": saga["incumbent_artifact_sha256"],
        },
        "successor": {
            "session_id": saga["successor_session_id"],
            "model_id": saga["successor_model_id"],
            "artifact_sha256": saga["successor_artifact_sha256"],
        },
        "manifest_file": saga["manifest_file"],
        "manifest_previous_sha256": saga["manifest_previous_sha256"],
        "manifest_target_sha256": saga["manifest_target_sha256"],
        "outgoing_shadow": saga["outgoing_shadow"],
        "audit": saga["audit"],
        "created_at": saga["created_at"],
        "finished_at": saga["finished_at"],
        "outcome_reason": saga["outcome_reason"],
    }


def resume_promotion_saga(
    store: L1ExecutionOlap,
    *,
    venue: str,
    account_fingerprint: str,
    instrument: str,
    action: str = "auto",
    reason: str = "",
    now: Optional[datetime] = None,
    boundary: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Finish the SAME interrupted operation after a restart.

    No second capability is minted or selected, and nothing is decided by
    re-reading the active session (which the interrupted operation may
    already have changed): every fact comes from the saga row.

    ``auto`` completes a ``manifest_pending``/``rolling_back`` saga in the
    direction it already committed to, and aborts a ``prepared`` one —
    a prepared saga's authorizing venue facts are stale after a crash.
    """
    now = now or _utc_now()
    if action not in ("auto", "complete", "rollback"):
        raise SuccessionError(
            f"unknown resume action {action!r}; use auto|complete|rollback")
    saga = open_promotion_saga(
        store, venue=venue, account_fingerprint=account_fingerprint,
        instrument=instrument)
    if saga is None:
        raise SuccessionError(
            "no open promotion saga for this seat; nothing to resume")
    state = saga["state"]
    if state == SAGA_ROLLING_BACK:
        if action == "complete":
            raise SuccessionError(
                "this saga already committed to a rollback; it can only be"
                " finished as a rollback")
        return rollback_promotion_saga(
            store, saga,
            reason=reason or saga.get("outcome_reason")
            or "resumed rollback", now=now)
    if state == SAGA_PREPARED:
        if action == "complete":
            committed = commit_ledger_authority(store, saga, now=now)
            if boundary is not None:
                boundary(BOUNDARY_CAPABILITY_BURNED)
            return finalize_promotion_saga(
                store, committed, now=now, boundary=boundary)
        return rollback_promotion_saga(
            store, saga,
            reason=reason or "resume: a prepared saga's post-drain venue"
                             " facts are stale after a restart",
            now=now)
    if action == "rollback":
        return rollback_promotion_saga(
            store, saga, reason=reason or "owner-requested rollback",
            now=now)
    return finalize_promotion_saga(
        store, saga, now=now, boundary=boundary)


# ── orchestrator ───────────────────────────────────────────────────────


def promote_paper_champion(
    *,
    store: L1ExecutionOlap,
    venue: Any,
    seat: SeatContract,
    candidate: CandidateContract,
    compatibility_report: Mapping[str, Any],
    activity_report: Mapping[str, Any],
    shadow_report: Mapping[str, Any],
    strategy_config: Mapping[str, Any],
    capability_store_dir: Path,
    new_manifest: Mapping[str, Any] | bytes,
    allowed_signers: Path = PROMOTION_ALLOWED_SIGNERS,
    require_root_pin: bool = True,
    explicit_capability: Optional[Path] = None,
    outgoing_shadow_days: float = OUTGOING_SHADOW_DEFAULT_DAYS,
    boundary: Optional[Callable[[str], None]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Execute one owner-approved Paper succession, fail-closed.

    ``venue`` is a real :class:`SuccessionVenue`: it OBSERVES direct
    broker facts and OWNS the drain executor. Broker truth is never a
    caller-supplied argument (finding 257), and the facts that authorize
    the switch are re-observed AFTER the drain (a pre-drain snapshot
    cannot authorize anything).

    Order of operations — each refusal before stage 7 leaves the
    capability unconsumed and the seat untouched:

    1. observe direct venue facts                  [facts_observed]
    2. re-verify the compatibility proof (verdict + recomputed digest);
    2b. re-verify the DIRECT trading-activity evidence (finding 269):
       a required argument with no default — a terminal candidate
       without an activity-eligible checkpoint bound to these exact
       artifact bytes refuses here, before anything is consumed;
    3. re-verify the shadow evidence (binding + recomputed digest);
    4. assert native SL/TP from the venue's OWN instrument capability;
    5. establish the ACTUAL incumbent session, refuse if a saga is open,
       select + validate the ONE owner-signed capability
                                                   [capability_validated]
    6. drain through the journaled lifecycle       [drain]
       and RE-OBSERVE the venue                    [facts_refreshed]
       — while the refreshed facts are not flat this returns the typed
       ``draining_for_succession`` result and nothing is consumed;
    7. persist the whole intended switch, target manifest bytes included
                                                   [ledger_prepared]
    8. ONE ledger unit: burn the capability, carry the ACTUAL post-drain
       balance/equity into the successor session, register the outgoing
       shadow window and DECLARE ``manifest_pending``
                                                   [capability_burned]
    9. flip the manifest pointer                   [manifest_temp_written]
                                                   [manifest_renamed]
       and mark the saga completed                 [ledger_finalized]

    A crash at ANY boundary leaves exactly one open saga row that
    ``resume_promotion_saga`` can complete or explicitly roll back
    without a second capability. Until it does, ``succession_pending``
    makes every runner refuse new risk for that seat.
    """
    now = now or _utc_now()

    def mark(name: str) -> None:
        if boundary is not None:
            boundary(name)

    facts = venue.fetch_facts()
    if not isinstance(facts, VenueFacts):
        raise SuccessionError(
            "venue.fetch_facts() must return direct VenueFacts; operator"
            " supplied broker truth is never accepted")
    if facts.venue != seat.venue or facts.instrument != seat.instrument:
        raise SuccessionError(
            "the venue facts describe a different seat than the contract")
    mark(BOUNDARY_FACTS_OBSERVED)

    compatibility_sha = require_compatible(compatibility_report, candidate)
    activity_sha = require_activity_evidence(activity_report, candidate)
    shadow_sha = require_shadow_evidence(shadow_report, candidate)
    protection = assert_native_protection(
        strategy_config=strategy_config,
        instrument_capability=facts.instrument_capability)
    if outgoing_shadow_days < OUTGOING_SHADOW_DEFAULT_DAYS:
        raise SuccessionError(
            f"outgoing shadow window must be at least"
            f" {OUTGOING_SHADOW_DEFAULT_DAYS} days (doc 32 S3)")

    account_fingerprint = facts.account_fingerprint
    con = store._con
    incumbent = _active_session(
        con, seat.venue, account_fingerprint, seat.instrument)
    if incumbent is None:
        raise SuccessionError(
            "no active incumbent session for this seat — promotion"
            " succeeds an incumbent, it never seats into the unknown")
    already = open_promotion_saga(
        store, venue=seat.venue, account_fingerprint=account_fingerprint,
        instrument=seat.instrument)
    if already is not None:
        raise SuccessionError(
            f"seat has an open promotion saga {already['saga_id']} in"
            f" state {already['state']}; resume or roll it back before"
            " starting another promotion")

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
    mark(BOUNDARY_CAPABILITY_VALIDATED)

    drained = venue.drain_for_succession(
        reason=f"owner_promotion:{candidate.model_id}",
        incumbent_session_id=incumbent["session_id"],
        successor_artifact_sha256=candidate.artifact_sha256,
        now=now)
    mark(BOUNDARY_DRAIN)
    facts = _post_drain_facts(venue, before=facts, seat=seat)
    mark(BOUNDARY_FACTS_REFRESHED)
    if not facts.flat:
        if not (facts.positions and not facts.open_orders
                and seat.execution.transfer_policy == "transfer_permitted"):
            return {
                "schema": PROMOTION_RESULT_SCHEMA,
                "state": "draining_for_succession",
                "capability_consumed": False,
                "drained": list(drained),
                "open_orders": len(facts.open_orders),
                "positions": len(facts.positions),
                "facts_source": facts.source,
                "facts_observed_at": facts.observed_at.isoformat(),
            }
    balance, equity = _actual_balance(facts)

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
    audit = {
        "schema": PROMOTION_RESULT_SCHEMA,
        "state": "promotion_prepared",
        "venue": seat.venue,
        "instrument": seat.instrument,
        "timeframe": seat.timeframe,
        "capability_sha256": record.capability_sha256,
        "compatibility_report_sha256": compatibility_sha,
        "activity_report_sha256": activity_sha,
        "shadow_report_sha256": shadow_sha,
        "outgoing": {
            "session_id": incumbent["session_id"],
            "model_id": incumbent["model_id"],
            "artifact_sha256": incumbent["artifact_sha256"],
            "ending_balance": balance,
            "ending_equity": equity,
        },
        "incoming": {
            "model_id": candidate.model_id,
            "artifact_sha256": candidate.artifact_sha256,
            "config_sha256": candidate.config_sha256,
            "starting_balance": balance,
            "starting_equity": equity,
        },
        "protection": protection,
        "drained": list(drained),
        "post_drain_facts": facts.to_dict(),
        "at": now.isoformat(),
    }
    saga = prepare_promotion_saga(
        store, seat=seat, candidate=candidate, incumbent=incumbent,
        record=record, facts=facts, target_manifest=new_manifest,
        outgoing_shadow=shadow_registration, audit=audit, now=now)
    mark(BOUNDARY_LEDGER_PREPARED)

    saga = commit_ledger_authority(store, saga, now=now)
    mark(BOUNDARY_CAPABILITY_BURNED)

    result = finalize_promotion_saga(
        store, saga, now=now, boundary=boundary)
    return {
        **result,
        "drained": list(drained),
        "protection": protection,
        "ignored_capability_files": [
            {"file": entry.path.name, "kind": entry.kind}
            for entry in ignored
        ],
    }
