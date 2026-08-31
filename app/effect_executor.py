"""WP3 effect executor — ONE shared orchestration layer.

Both venue runner adapters consume this module; neither reimplements
it, and it reimplements no weekly-flat policy of its own. It takes a
validated :class:`VenueDirective` and drives its effects in the only
legal order, journaling durably at every boundary so that a crash at
any point is fail-closed and replay-safe.

The sequence it enforces, and will not reorder:

1. the directive identity and the effect plan are PERSISTED before
   any effect. A plan that was never durably recorded never touches a
   venue;
2. ``cancel_pending_entries`` runs first, restricted to the exact
   identities the directive names, and each identity is re-verified
   as an ENTRY in fresh direct order evidence immediately before the
   cancellation is issued. A protective identity is therefore
   structurally impossible to submit: it is refused twice, once by
   the directive contract and once against the live order book;
3. terminal venue outcomes are obtained for every requested
   cancellation and ``permits_dependent_effects()`` decides. A
   rejection, a fill before the cancel landed, a still-open order, an
   unknown disappearance, stale evidence or a missing verdict each
   STOP the plan;
4. only after a permitted verdict does the dependent model effect
   run, and the directive, evidence-policy and authority code
   identities are re-checked immediately before it;
5. a forced flatten opens the accepted live custody BEFORE the close
   is requested, and confirmation requires fresh direct zero-position
   zero-order evidence. A restart resumes the unresolved obligation
   -- the obligation identity is derived from the plan identity, so a
   resumed plan can never mint a sibling;
6. every transition and every venue acknowledgement is journaled
   idempotently, in a digest-chained journal whose records are
   written with the O_EXCL / fsync / rename discipline. Replay reads
   the chain, refuses a broken one, and never re-issues an effect the
   venue already acknowledged.

This module holds no venue client and no credential. Effects travel
through an injected PORT whose construction is the caller's
responsibility; the tests drive fakes, and the only port this
repository ships is the refusing dry-run interface.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from app.live_flatten_custody import (
    LiveCustodyError, LiveFlattenCustody)
from app.session_authority_adapter import (
    AuthorityUnavailable, VenueDirective, load_authority)
from app.venue_direct_evidence import (
    VenueDirectEvidence, VenueEvidenceError, VenueEvidencePolicy)

FILE_MODE = 0o600
DIR_MODE = 0o700

PLAN_STATES = ("planned", "cancelling", "gated", "executing",
               "completed", "stopped", "unresolved")


class ExecutorError(RuntimeError):
    """The effect executor refuses — typed, never a default."""


class PlanAlreadyClaimed(ExecutorError):
    """A second invocation of the same plan: resume, do not re-run."""


class PlanStopped(ExecutorError):
    """The plan stopped at a gate; nothing dependent was executed."""


# ---------------------------------------------------------------- #
# durable, digest-chained journal                                   #
# ---------------------------------------------------------------- #

def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True,
                      separators=(",", ":"), default=str).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _refuse_symlink(path: Path, what: str) -> None:
    if path.is_symlink():
        raise ExecutorError(f"{what} {path.name}: symlinked path "
                            "refused")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    """O_EXCL temp, write, fchmod, fsync, exclusive final create,
    rename, parent fsync. A failure leaves nothing acknowledged."""
    _refuse_symlink(path.parent, "journal root")
    _refuse_symlink(path, "journal record")
    # the temp name carries pid AND thread id so that two concurrent
    # invocations contend at the FINAL exclusive create — the actual
    # election point — instead of colliding on the scratch file
    tmp = path.with_suffix(
        path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                     FILE_MODE)
    except FileExistsError as exc:
        raise ExecutorError(
            f"{tmp.name}: concurrent write in progress") from exc
    try:
        os.write(fd, json.dumps(payload, indent=1,
                                default=str).encode())
        os.fchmod(fd, FILE_MODE)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        final = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        FILE_MODE)
    except FileExistsError as exc:
        os.unlink(tmp)
        raise PlanAlreadyClaimed(
            f"{path.name}: already claimed") from exc
    os.close(final)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


class EffectJournal:
    """Sequential, digest-chained, idempotent by (kind, key)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        _refuse_symlink(self.root, "journal root")
        self.root.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        os.chmod(self.root, DIR_MODE)

    def records(self) -> tuple:
        """The verified chain. A record that fails its digest, or a
        gap in the sequence, refuses the WHOLE journal: a journal
        that cannot be trusted end to end proves nothing."""
        entries = []
        for path in sorted(self.root.glob("[0-9]*.json")):
            _refuse_symlink(path, "journal record")
            try:
                record = json.loads(path.read_text())
            except Exception as exc:
                raise ExecutorError(
                    f"{path.name}: unreadable journal record: {exc}"
                ) from exc
            entries.append(record)
        prev = ""
        for index, record in enumerate(entries):
            if record.get("seq") != index:
                raise ExecutorError(
                    f"journal sequence broken at {index}: record "
                    f"claims seq {record.get('seq')}")
            body = {k: v for k, v in record.items() if k != "digest"}
            expected = _sha(_canonical(body) + prev.encode())
            if record.get("digest") != expected:
                raise ExecutorError(
                    f"journal digest broken at seq {index} — the "
                    "chain was altered and the plan is unresolved")
            prev = record["digest"]
        return tuple(entries)

    def find(self, kind: str, key: Optional[str] = None
             ) -> Optional[dict]:
        for record in self.records():
            if record["kind"] == kind and (
                    key is None or record.get("key") == key):
                return record
        return None

    def append_once(self, kind: str, *, key: str = "",
                    payload: Optional[Mapping[str, Any]] = None
                    ) -> dict:
        """Idempotent append: an existing (kind, key) is returned
        as-is and nothing is written twice."""
        existing = self.find(kind, key or None)
        if existing is not None:
            return existing
        chain = self.records()
        prev = chain[-1]["digest"] if chain else ""
        record = {"seq": len(chain), "kind": kind, "key": key,
                  "payload": dict(payload or {}),
                  "at": datetime.now(timezone.utc).isoformat()}
        record["digest"] = _sha(_canonical(record) + prev.encode())
        _durable_exclusive(self.root / f"{record['seq']:04d}.json",
                           record)
        return record


# ---------------------------------------------------------------- #
# the executor                                                      #
# ---------------------------------------------------------------- #

def directive_digest(directive: VenueDirective) -> str:
    return _sha(_canonical(directive.as_dict()))


class EffectExecutor:
    """Drives ONE plan for one validated directive.

    ``port`` is the venue interface: ``cancel_order(identity)``,
    ``submit_decision(command)`` and ``request_close()``, each
    returning an acknowledgement mapping. This module never
    constructs a real port. ``fresh_orders`` and ``fresh_positions``
    return CURRENT :class:`VenueDirectEvidence`; ``outcomes`` returns
    the venue's terminal verdict per cancelled identity, derived from
    direct evidence and never assumed.
    """

    def __init__(self, *, journal_root: Path, plan_id: str,
                 directive: VenueDirective,
                 policy: VenueEvidencePolicy,
                 authority_root: Path,
                 expected_code_identity: str,
                 port: Any,
                 fresh_orders: Callable[[], VenueDirectEvidence],
                 fresh_positions: Callable[[], VenueDirectEvidence],
                 outcomes: Callable[[], Mapping[str, str]],
                 custody: Optional[LiveFlattenCustody] = None,
                 clock: Callable[[], datetime] = lambda:
                 datetime.now(timezone.utc)):
        if not isinstance(directive, VenueDirective):
            raise ExecutorError(
                "a validated VenueDirective is required")
        if not isinstance(plan_id, str) or not plan_id.strip() or \
                "/" in plan_id or plan_id.startswith("."):
            raise ExecutorError(f"unsafe plan id {plan_id!r}")
        for name in ("cancel_order", "submit_decision",
                     "request_close"):
            if not callable(getattr(port, name, None)):
                raise ExecutorError(
                    f"the port does not implement {name}")
        self.plan_id = plan_id
        self.directive = directive
        self.policy = policy
        self.authority_root = Path(authority_root)
        self.expected_code_identity = expected_code_identity
        self.port = port
        self.fresh_orders = fresh_orders
        self.fresh_positions = fresh_positions
        self.outcomes = outcomes
        self.custody = custody
        self.clock = clock
        self.journal = EffectJournal(Path(journal_root) / plan_id)

    # -- plan persistence ------------------------------------------
    def _persist_plan(self) -> dict:
        plan_path = self.journal.root / "plan.json"
        payload = {
            "plan_id": self.plan_id,
            "directive_digest": directive_digest(self.directive),
            "directive": self.directive.as_dict(),
            "evidence_policy_digest": self.policy.policy_digest,
            "authority_code_identity": self.expected_code_identity,
            "effects": list(self.directive.effects),
        }
        try:
            _durable_exclusive(plan_path, payload)
        except PlanAlreadyClaimed:
            raise
        return payload

    def _read_plan(self) -> dict:
        plan_path = self.journal.root / "plan.json"
        _refuse_symlink(plan_path, "plan record")
        if not plan_path.is_file():
            raise ExecutorError(
                f"plan {self.plan_id} was never persisted")
        return json.loads(plan_path.read_text())

    # -- gates ------------------------------------------------------
    def _stop(self, reason: str) -> None:
        self.journal.append_once("plan_stopped",
                                 payload={"reason": reason})
        raise PlanStopped(reason)

    def _recheck_identities(self) -> None:
        """Immediately before the dependent effect: the directive,
        the policy and the authority code must still be the ones the
        plan was persisted under."""
        plan = self._read_plan()
        if plan["directive_digest"] != directive_digest(
                self.directive):
            self._stop("directive identity changed since the plan "
                       "was persisted")
        if plan["evidence_policy_digest"] != self.policy.policy_digest:
            self._stop("evidence policy changed since the plan was "
                       "persisted")
        try:
            load_authority(
                self.authority_root,
                expected_code_identity=self.expected_code_identity)
        except AuthorityUnavailable as exc:
            self._stop(f"authority identity recheck failed: {exc}")
        if plan["authority_code_identity"] != \
                self.expected_code_identity:
            self._stop("authority identity changed since the plan "
                       "was persisted")

    def _verified_entry_identities(self) -> tuple:
        """The directive's cancel identities, each re-verified as an
        ENTRY in fresh direct order evidence. A protective identity
        cannot pass here, so it cannot reach the port."""
        wanted = tuple(self.directive.cancel_order_identities)
        if not wanted:
            return ()
        try:
            evidence = self.fresh_orders()
            evidence.verify(self.policy, now=self.clock())
        except VenueEvidenceError as exc:
            self._stop(f"stale or invalid order evidence before "
                       f"cancellation: {exc}")
        by_identity = {row["order_identity"]: row
                       for row in evidence.facts.get("orders", ())}
        for identity in wanted:
            row = by_identity.get(identity)
            if row is not None and row.get("role") != "entry":
                self._stop(
                    f"identity {identity!r} is {row.get('role')!r} in "
                    "the live order book — a protective identity is "
                    "never submitted for cancellation")
        return wanted

    # -- execution --------------------------------------------------
    def execute(self) -> dict:
        """Run the plan from the beginning. A second call for the
        same plan id refuses with PlanAlreadyClaimed: use resume()."""
        self._persist_plan()
        self.journal.append_once("planned", payload={
            "effects": list(self.directive.effects)})
        return self._run()

    def resume(self) -> dict:
        """Continue after a crash. Reads the verified chain, never
        re-issues an acknowledged venue call, and never mints a
        sibling custody obligation."""
        self._read_plan()
        chain = self.journal.records()
        if not chain:
            self.journal.append_once("planned", payload={
                "effects": list(self.directive.effects)})
        if self.journal.find("plan_completed") is not None:
            return {"state": "completed", "resumed": True}
        stopped = self.journal.find("plan_stopped")
        if stopped is not None:
            raise PlanStopped(stopped["payload"]["reason"])
        return self._run()

    def _run(self) -> dict:
        effects = list(self.directive.effects)

        if "cancel_pending_entries" in effects:
            identities = self._verified_entry_identities()
            for identity in identities:
                self.journal.append_once("cancel_requested",
                                         key=identity)
                if self.journal.find("cancel_acknowledged",
                                     identity) is None:
                    ack = self.port.cancel_order(identity)
                    self.journal.append_once(
                        "cancel_acknowledged", key=identity,
                        payload={"ack": dict(ack or {})})

            outcomes = dict(self.outcomes())
            self.journal.append_once("cancellation_outcomes",
                                     payload={"outcomes": outcomes})
            verdict = self.directive.permits_dependent_effects(
                outcomes)
            self.journal.append_once("gate_verdict", payload=verdict)
            if not verdict["permitted"]:
                self._stop(verdict["reason"])

        if "submit_decision" in effects:
            self._recheck_identities()
            requested = self.journal.find("effect_requested",
                                          "submit_decision")
            acked = self.journal.find("effect_acknowledged",
                                      "submit_decision")
            if requested is not None and acked is None:
                # The decision was requested and no acknowledgement
                # was ever journaled: whether the venue received it is
                # UNKNOWN. Submitting a decision adds risk, so it is
                # strictly at-most-once — re-issuing here could double
                # an order. Fail closed and require disposition.
                self.journal.append_once(
                    "decision_unresolved",
                    payload={"reason": "the decision was requested "
                             "and never acknowledged; whether the "
                             "venue received it is unknown"})
                return {"state": "unresolved",
                        "incident": "decision requested, never "
                        "acknowledged — not re-issued"}
            if acked is None:
                self.journal.append_once("effect_requested",
                                         key="submit_decision")
                ack = self.port.submit_decision(
                    self.directive.final_command)
                self.journal.append_once(
                    "effect_acknowledged", key="submit_decision",
                    payload={"ack": dict(ack or {})})
            self.journal.append_once("plan_completed")
            return {"state": "completed"}

        if "request_close" in effects:
            return self._run_forced_flatten()

        self.journal.append_once("plan_completed")
        return {"state": "completed"}

    # -- forced flatten ---------------------------------------------
    def _obligation_id(self) -> str:
        return f"flatten-{self.plan_id}"

    def _run_forced_flatten(self) -> dict:
        if self.custody is None:
            self._stop("a forced flatten requires the accepted live "
                       "custody and none was provided")
        self._recheck_identities()
        obligation_id = self._obligation_id()

        # the obligation is opened BEFORE the close is requested, and
        # its identity derives from the plan, so a resumed plan finds
        # its own obligation instead of minting a sibling
        if self.journal.find("custody_opened") is None:
            try:
                signed = sum(
                    float(row["signed_quantity"]) for row in
                    self.fresh_positions().verify(
                        self.policy, now=self.clock()
                    ).facts.get("positions", ()))
            except VenueEvidenceError as exc:
                self._stop(f"stale or invalid position evidence "
                           f"before the close: {exc}")
            try:
                self.custody.open(obligation_id,
                                  signed_exposure=signed,
                                  requested_at_bar=0)
            except Exception as exc:
                # already claimed by a previous run of THIS plan is
                # idempotent; anything else refuses
                if "already claimed" not in str(exc):
                    raise
            self.journal.append_once(
                "custody_opened", key=obligation_id)

        # A close, unlike a decision, is reduce-only and its success
        # is judged solely by direct zero/zero evidence below, so an
        # unacknowledged request may be re-issued safely.
        self.journal.append_once("close_requested",
                                 key=obligation_id)
        if self.journal.find("close_acknowledged",
                             obligation_id) is None:
            ack = self.port.request_close()
            self.journal.append_once(
                "close_acknowledged", key=obligation_id,
                payload={"ack": dict(ack or {})})
        try:
            self.custody.mark_in_flight(obligation_id, bar_index=0)
        except Exception as exc:
            if "already terminal" in str(exc):
                pass
            elif "cannot go in flight" not in str(exc) and \
                    "flatten_in_flight" not in str(exc):
                raise

        # confirmation: ONLY fresh direct zero/zero evidence
        try:
            positions = self.fresh_positions()
            orders = self.fresh_orders()
            record = self.custody.confirm_with_direct_evidence(
                obligation_id, positions=positions, orders=orders,
                policy=self.policy, now=self.clock(), bar_index=0)
        except (LiveCustodyError, VenueEvidenceError) as exc:
            self.journal.append_once(
                "flatten_unresolved",
                payload={"incident": f"{type(exc).__name__}: {exc}"})
            return {"state": "unresolved",
                    "obligation_id": obligation_id,
                    "incident": str(exc)}
        self.journal.append_once("flatten_confirmed",
                                 key=obligation_id,
                                 payload={"reconciliation":
                                          record["reconciliation"]})
        self.journal.append_once("plan_completed")
        return {"state": "completed",
                "obligation_id": obligation_id}
