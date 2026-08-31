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
    SecureFileError, VenueDirectEvidence, VenueEvidenceError,
    VenueEvidencePolicy, require_utc, secure_create_bytes,
    secure_read_bytes, secure_rewrite_bytes)

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


class PlanLockHeld(ExecutorError):
    """Another claimant holds the run lock for this plan. The lock
    covers the whole reconcile-through-acknowledgement transaction,
    because per-record exclusivity alone lets two resumes both
    observe an unacknowledged effect and both issue it."""


PLAN_SCHEMA = "lts.effect_executor.plan.v1"


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
            # E13-3: descriptor-bound verified read — no path-time
            # check followed by a second raceable path resolution
            try:
                record = json.loads(secure_read_bytes(
                    path, what="journal record").decode("utf-8"))
            except SecureFileError as exc:
                raise ExecutorError(
                    f"{path.name}: journal record refused: {exc}"
                ) from exc
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
                 terminal_orders: Callable[[], VenueDirectEvidence],
                 custody: Optional[LiveFlattenCustody] = None,
                 receipt_ledger: Optional[Any] = None,
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
        self.terminal_orders = terminal_orders
        self.custody = custody
        self.receipt_ledger = receipt_ledger
        self.clock = clock
        self.journal = EffectJournal(Path(journal_root) / plan_id)

    # -- the per-plan run lock (E4/E6/E9) --------------------------
    # The lock file is NEVER unlinked. Release was unlink-then-fsync,
    # so an unlink that succeeded before a failing directory fsync
    # left the lock ABSENT from the live namespace and a second
    # claimant entered immediately. The lock is now MONOTONE content:
    # held:<pid> -> releasing -> released, every transition an
    # in-place write plus file fsync — no namespace change, so no
    # directory fsync can strand it. A claimant may enter only on the
    # exact content "released"; held, releasing, garbage, or an
    # unreadable file all refuse, so no failure can leave an absent,
    # unacknowledged lock.

    def _lock_path(self) -> Path:
        return self.journal.root / "run.lock"

    @staticmethod
    def _write_lock_state(lock: Path, state: str, *,
                          fsync: bool = True) -> None:
        # E13-3: descriptor-verified in-place write — no O_TRUNC at
        # open, no path re-resolution after the object is verified
        secure_rewrite_bytes(Path(lock), state.encode(),
                             what="run lock", fsync=fsync)

    def _release_intent_path(self, epoch: str) -> Path:
        return self.journal.root / f"run.lock.rel.{epoch}"

    def _release_completion_path(self, epoch: str) -> Path:
        return self.journal.root / f"run.lock.reldone.{epoch}"

    def _acquire_run_lock(self):
        from app.venue_direct_evidence import new_generation
        lock = self._lock_path()
        epoch = new_generation()
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE)
        except FileExistsError:
            return self._reclaim_run_lock(lock)
        except OSError as exc:
            raise PlanLockHeld(
                f"plan {self.plan_id}: the run lock could not be "
                f"created ({exc}) — an operator disposes") from exc
        try:
            try:
                os.write(fd, f"held:{os.getpid()}:{epoch}".encode())
                os.fchmod(fd, FILE_MODE)
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_dir(lock.parent)
        except Exception as exc:
            raise PlanLockHeld(
                f"plan {self.plan_id}: the lock acquisition could "
                f"not be made durable ({exc}) — an uncertain acquire "
                "blocks execution, the lock stays in place, and an "
                "operator disposes") from exc
        return lock, epoch

    def _read_lock_content(self, lock: Path) -> str:
        try:
            return secure_read_bytes(lock, what="run lock").decode(
                "utf-8", errors="replace")
        except FileNotFoundError:
            return "<absent>"
        except SecureFileError as exc:
            return f"<unverifiable: {exc}>"

    def _completed_release_epoch(self, lock: Path) -> str:
        """E14: entering requires released:<epoch> plus an epoch-
        bound, self-integral completion record. Recovery evaluates
        the physical state: a released write that reached storage
        while its fsync failed admits a claimant exactly when its
        completion record is durable too — anything less refuses."""
        import re as re_module
        from app.venue_direct_evidence import (
            LOCK_RELEASE_COMPLETION_SCHEMA, load_sealed_json)
        content = self._read_lock_content(lock)
        if not re_module.fullmatch(r"released:[0-9a-f]{32}",
                                   content):
            raise PlanLockHeld(
                f"plan {self.plan_id}: the run lock reads "
                f"{content[:80]!r}, not a completed epoch release — "
                "another claimant holds it or its release is "
                "uncertain; an operator disposes, never an "
                "automatic takeover")
        epoch = content.split(":", 1)[1]
        try:
            raw = secure_read_bytes(
                self._release_completion_path(epoch),
                what="run lock release completion")
        except FileNotFoundError as exc:
            raise PlanLockHeld(
                f"plan {self.plan_id}: the lock reads "
                f"released:{epoch} but no durable release completion "
                "record exists — the release never completed and no "
                "claimant may enter; an operator disposes") from exc
        completion = load_sealed_json(
            raw, schema=LOCK_RELEASE_COMPLETION_SCHEMA,
            fields=("schema", "scope", "epoch"))
        if completion is None or \
                completion["scope"] != str(self.plan_id) or \
                completion["epoch"] != epoch:
            raise PlanLockHeld(
                f"plan {self.plan_id}: the release completion "
                "record is malformed, mismatched or from another "
                "generation — a stale completion never releases a "
                "new holder; an operator disposes")
        return epoch

    def _reclaim_run_lock(self, lock: Path):
        """Enter ONLY over a completed, epoch-witnessed release. The
        reclaim transition is elected by its own O_EXCL lock so two
        claimants reading the same released state cannot both
        rewrite it."""
        from app.venue_direct_evidence import new_generation
        self._completed_release_epoch(lock)
        reclaim = lock.with_suffix(".lock.reclaim")
        try:
            fd = os.open(reclaim, os.O_WRONLY | os.O_CREAT |
                         os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         FILE_MODE)
        except FileExistsError as exc:
            # a persisting marker BLOCKS, never authorizes
            raise PlanLockHeld(
                f"plan {self.plan_id}: a competing reclaim is in "
                "progress") from exc
        os.close(fd)
        epoch = new_generation()
        try:
            # revalidate under the election, descriptor-first,
            # before authority passes
            self._completed_release_epoch(lock)
            try:
                self._write_lock_state(
                    lock, f"held:{os.getpid()}:{epoch}")
            except Exception as exc:
                raise PlanLockHeld(
                    f"plan {self.plan_id}: the reclaim could not be "
                    f"made durable ({exc}) — refused; whatever "
                    "content remains is not a completed release, "
                    "and refuses") from exc
        finally:
            try:
                os.unlink(reclaim)
            except FileNotFoundError:
                pass
        return lock, epoch

    def _release_run_lock(self, handle) -> None:
        """E14: append-only witnessed release. An immutable intent
        record is durable BEFORE the lock moves; the lock walks
        releasing:<epoch> -> released:<epoch> in place; success is a
        SEPARATE exclusive completion record persisted only after
        released is durable. Nothing is overwritten or deleted to
        establish or undo the release, and NO restoration write is
        trusted: recovery admits a claimant exactly when the epoch-
        bound completion physically persisted, and refuses
        otherwise."""
        from app.venue_direct_evidence import (
            LOCK_RELEASE_COMPLETION_SCHEMA,
            LOCK_RELEASE_INTENT_SCHEMA, sealed_json_bytes)
        lock, epoch = handle
        lock = Path(lock)
        try:
            secure_create_bytes(
                self._release_intent_path(epoch),
                sealed_json_bytes({
                    "schema": LOCK_RELEASE_INTENT_SCHEMA,
                    "scope": str(self.plan_id), "epoch": epoch,
                    "holder_pid": os.getpid()}),
                what="run lock release intent")
            _fsync_dir(lock.parent)
            self._write_lock_state(lock, f"releasing:{epoch}")
            self._write_lock_state(lock, f"released:{epoch}")
            secure_create_bytes(
                self._release_completion_path(epoch),
                sealed_json_bytes({
                    "schema": LOCK_RELEASE_COMPLETION_SCHEMA,
                    "scope": str(self.plan_id), "epoch": epoch}),
                what="run lock release completion")
            _fsync_dir(lock.parent)
        except Exception as exc:
            raise ExecutorError(
                f"plan {self.plan_id}: the lock release could not be "
                f"made durable ({exc}) — no claimant may enter "
                "unless recovery finds a durable completion record; "
                "an operator must dispose") from exc

    # -- plan persistence ------------------------------------------
    def _persist_plan(self) -> dict:
        plan_path = self.journal.root / "plan.json"
        payload = {
            "schema": PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "directive_digest": directive_digest(self.directive),
            "directive": self.directive.as_dict(),
            "evidence_policy_digest": self.policy.policy_digest,
            "authority_code_identity": self.expected_code_identity,
            "effects": list(self.directive.effects),
        }
        payload["digest"] = _sha(_canonical(
            {k: v for k, v in payload.items() if k != "digest"}))
        _durable_exclusive(plan_path, payload)
        return payload

    def _read_plan(self) -> dict:
        plan_path = self.journal.root / "plan.json"
        try:
            raw = secure_read_bytes(plan_path, what="plan record")
        except FileNotFoundError as exc:
            raise ExecutorError(
                f"plan {self.plan_id} was never persisted") from exc
        except SecureFileError as exc:
            raise ExecutorError(
                f"plan {self.plan_id}: plan record refused: "
                f"{exc}") from exc
        plan = json.loads(raw.decode("utf-8"))
        if plan.get("schema") != PLAN_SCHEMA:
            raise ExecutorError(
                f"plan {self.plan_id}: schema "
                f"{plan.get('schema')!r} is not {PLAN_SCHEMA!r}")
        body = {k: v for k, v in plan.items() if k != "digest"}
        if plan.get("digest") != _sha(_canonical(body)):
            raise ExecutorError(
                f"plan {self.plan_id}: plan digest mismatch — the "
                "persisted plan was altered and nothing may run "
                "against it")
        return plan

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
        if len(set(wanted)) != len(wanted):
            self._stop("duplicate cancellation identities in the "
                       "directive")
        # presence is a PRE-CANCELLATION check: an identity whose
        # cancellation the venue already acknowledged is absent from
        # the book precisely BECAUSE it was cancelled, and the typed
        # terminal evidence — not this check — judges its outcome
        pending = tuple(
            identity for identity in wanted
            if self.journal.find("cancel_acknowledged",
                                 identity) is None)
        if not pending:
            return wanted
        evidence = self._fresh(self.fresh_orders, "open_orders",
                               what="order evidence before "
                               "cancellation")
        by_identity = {row["order_identity"]: row
                       for row in evidence.facts.get("orders", ())}
        for identity in pending:
            row = by_identity.get(identity)
            if row is None:
                self._stop(
                    f"identity {identity!r} is ABSENT from the fresh "
                    "order book — cancelling an object the venue does "
                    "not show is refused, and absence is never a "
                    "verdict")
            if row.get("role") != "entry":
                self._stop(
                    f"identity {identity!r} is {row.get('role')!r} in "
                    "the live order book — a protective identity is "
                    "never submitted for cancellation")
        return wanted

    def _fresh(self, provider, expected_type: str, *, what: str):
        """Typed, policy-verified, RIGHT-KIND evidence or a stop."""
        try:
            evidence = provider()
        except VenueEvidenceError as exc:
            self._stop(f"invalid {what}: {exc}")
        if not isinstance(evidence, VenueDirectEvidence):
            raise ExecutorError(
                f"{what} must be VenueDirectEvidence, got "
                f"{type(evidence).__name__} — a bare mapping is an "
                "assertion, not evidence")
        if evidence.evidence_type != expected_type:
            self._stop(
                f"{what} carries evidence type "
                f"{evidence.evidence_type!r}, not "
                f"{expected_type!r} — the wrong kind of evidence "
                "proves nothing here")
        try:
            evidence.verify(self.policy, now=self.clock())
        except VenueEvidenceError as exc:
            self._stop(f"stale or invalid {what}: {exc}")
        return evidence

    # -- execution --------------------------------------------------
    def execute(self) -> dict:
        """Run the plan from the beginning. A second call for the
        same plan id refuses with PlanAlreadyClaimed: use resume().
        The run lock covers the whole transaction."""
        lock = self._acquire_run_lock()
        try:
            self._persist_plan()
            self.journal.append_once("planned", payload={
                "effects": list(self.directive.effects)})
            return self._run()
        finally:
            self._release_run_lock(lock)

    def resume(self) -> dict:
        """Continue after a crash. Reads the verified chain, never
        re-issues an acknowledged venue call, and never mints a
        sibling custody obligation."""
        lock = self._acquire_run_lock()
        try:
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
        finally:
            self._release_run_lock(lock)

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

            evidence = self._fresh(self.terminal_orders,
                                   "terminal_orders",
                                   what="terminal-order evidence")
            # E11: the receipt must be REGISTERED in the durable
            # per-route ledger before this evidence authorizes
            # anything — monotone sequence and body uniqueness are
            # facts of the ledger, not declarations of the receipt.
            if self.receipt_ledger is None:
                self._stop("no receipt ledger was provided; an "
                           "unregistered receipt authorizes nothing")
            route = "|".join((self.policy.venue,
                              self.policy.account_fingerprint,
                              self.policy.symbol))
            try:
                registered = self.receipt_ledger.register(
                    evidence.receipt, route=route)
            except VenueEvidenceError as exc:
                self._stop(f"receipt registration refused: {exc}")
            # E10: freshness is judged PER REQUESTED IDENTITY from
            # each verdict's own venue event time. No aggregate
            # maximum may authorize another row.
            now = self.clock()
            verdicts = dict(evidence.facts.get("verdicts", ()))
            outcomes = {}
            dropped_stale = {}
            for identity in identities:
                entry = verdicts.get(identity)
                if entry is None:
                    continue
                event_at = require_utc("verdict.event_at",
                                       entry["event_at"])
                age = (now - event_at).total_seconds()
                if age > self.policy.max_age_seconds:
                    dropped_stale[identity] = round(age, 3)
                    continue
                outcomes[identity] = entry["verdict"]
            self.journal.append_once(
                "cancellation_outcomes",
                payload={"outcomes": outcomes,
                         "dropped_stale": dropped_stale,
                         "receipt_registered": dict(registered),
                         "provenance": evidence.provenance()})
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

    def _ordinal(self) -> int:
        """A durable, MONOTONE executor event ordinal: the length of
        the verified journal chain at the moment of the transition.
        The journal only appends, so the ordinal only grows; a
        constant zero is not live provenance and this is."""
        return len(self.journal.records())

    def _close_contract(self) -> dict:
        """E3: the close binds THE position — identity, side, units,
        reduce-only, and a durable idempotency key derived from the
        plan. A generic 'close whatever is there' is never issued."""
        evidence = self._fresh(self.fresh_positions, "positions",
                               what="position evidence before the "
                               "close")
        rows = tuple(evidence.facts.get("positions", ()))
        if len(rows) != 1:
            self._stop(
                f"the close requires exactly ONE open position for "
                f"{self.policy.symbol!r}; the venue shows {len(rows)} "
                "— ambiguous exposure is never closed blind")
        row = rows[0]
        return {
            "position_identity": row["position_identity"],
            # E7: the KIND decides whether replay equality can ever
            # be trusted. Alpaca's asset_id names the ASSET, not the
            # instance; only a venue position-instance identity (the
            # MT5 ticket) supports a same-position claim.
            "identity_kind": row.get("identity_kind",
                                     "asset_identity_only"),
            "side": row["side"],
            "units": abs(float(row["signed_quantity"])),
            "signed_quantity": float(row["signed_quantity"]),
            "entry_price": float(row["entry_price"]),
            "reduce_only": True,
            "idempotency_key": f"close-{self.plan_id}",
        }

    def _unresolved(self, kind: str, reason: str,
                    obligation_id: str) -> dict:
        self.journal.append_once(kind, key=obligation_id,
                                 payload={"incident": reason})
        return {"state": "unresolved",
                "obligation_id": obligation_id, "incident": reason}

    def _run_forced_flatten(self) -> dict:
        if self.custody is None:
            self._stop("a forced flatten requires the accepted live "
                       "custody and none was provided")
        self._recheck_identities()
        obligation_id = self._obligation_id()

        # E5: TYPED custody idempotency — the record is read, never
        # an exception message matched. The obligation is opened
        # BEFORE the close is requested, and its identity derives
        # from the plan, so a resumed plan finds its own obligation
        # instead of minting a sibling.
        existing = self.custody.exists(obligation_id)
        if existing is None:
            contract = self._close_contract()
            self.custody.open(
                obligation_id,
                signed_exposure=contract["signed_quantity"],
                requested_at_bar=self._ordinal())
            self.journal.append_once(
                "custody_opened", key=obligation_id,
                payload={"close_contract": contract})
        else:
            differing = self.custody.binding.matches(existing)
            if differing:
                self._stop(
                    f"the recovered obligation disagrees on "
                    f"{list(differing)} — this plan may not act on "
                    "someone else's obligation")
            self.journal.append_once("custody_opened",
                                     key=obligation_id)

        requested = self.journal.find("close_requested",
                                      obligation_id)
        acked = self.journal.find("close_acknowledged",
                                  obligation_id)

        if acked is None and requested is not None:
            # E3: an unacknowledged close is RECONCILED first, never
            # blindly re-issued.
            positions = self._fresh(
                self.fresh_positions, "positions",
                what="position evidence for close reconciliation")
            rows = tuple(positions.facts.get("positions", ()))
            if not rows:
                # already flat: confirmation below decides, and no
                # second close is ever sent
                self.journal.append_once("close_reconciled_flat",
                                         key=obligation_id)
            else:
                contract = (requested.get("payload") or {}).get(
                    "close_contract")
                if not contract:
                    return self._unresolved(
                        "close_unresolved",
                        "an unacknowledged close has no persisted "
                        "contract; sameness cannot be verified and it "
                        "is not re-issued", obligation_id)
                # E7: without a venue position-INSTANCE identity,
                # equality of asset, side, quantity and even price
                # proves nothing: a coincidentally identical REOPENED
                # position must never inherit an old close.
                if contract.get("identity_kind") != \
                        "venue_position_instance":
                    return self._unresolved(
                        "close_unresolved",
                        "the venue supplies no position-instance "
                        "identity for this position; an "
                        "unacknowledged close cannot be proven to "
                        "target the same instance and is not "
                        "re-issued — operator disposition required",
                        obligation_id)
                same = (
                    len(rows) == 1
                    and rows[0].get("identity_kind") ==
                    "venue_position_instance"
                    and rows[0]["position_identity"] ==
                    contract["position_identity"]
                    and rows[0]["side"] == contract["side"]
                    and abs(abs(float(rows[0]["signed_quantity"]))
                            - float(contract["units"])) <= 1e-9
                    and abs(float(rows[0]["entry_price"])
                            - float(contract["entry_price"]))
                    <= 1e-9)
                if not same:
                    return self._unresolved(
                        "close_unresolved",
                        "the position changed, multiplied or was "
                        "reopened since the close was requested — a "
                        "changed state is never closed by replay",
                        obligation_id)
                if getattr(self.port, "close_contract", None) != \
                        "same_key_idempotent_reduce_only":
                    return self._unresolved(
                        "close_unresolved",
                        "the port does not prove same-key idempotent "
                        "reduce-only close semantics — an "
                        "unacknowledged close is not re-issued on "
                        "hope", obligation_id)
                ack = self.port.request_close(**contract)
                self.journal.append_once(
                    "close_acknowledged", key=obligation_id,
                    payload={"ack": dict(ack or {}),
                             "reissued_with_same_key": True})
        elif acked is None:
            opened = self.journal.find("custody_opened",
                                       obligation_id)
            contract = (opened.get("payload") or {}).get(
                "close_contract") if opened else None
            if not contract:
                contract = self._close_contract()
            self.journal.append_once(
                "close_requested", key=obligation_id,
                payload={"close_contract": contract})
            ack = self.port.request_close(**contract)
            self.journal.append_once(
                "close_acknowledged", key=obligation_id,
                payload={"ack": dict(ack or {})})

        # typed in-flight transition (E5)
        record = self.custody.exists(obligation_id)
        if record is None:
            self._stop(f"obligation {obligation_id} vanished from "
                       "custody")
        state = record["state"]
        if state == "flatten_requested":
            self.custody.mark_in_flight(obligation_id,
                                        bar_index=self._ordinal())
        elif state == "flatten_in_flight":
            pass
        elif state == "flatten_confirmed":
            self.journal.append_once("plan_completed")
            return {"state": "completed",
                    "obligation_id": obligation_id}
        else:
            self._stop(f"obligation {obligation_id} is terminal in "
                       f"state {state!r} and claims no closure")

        # confirmation: ONLY fresh direct zero/zero evidence
        try:
            positions = self.fresh_positions()
            orders = self.fresh_orders()
            record = self.custody.confirm_with_direct_evidence(
                obligation_id, positions=positions, orders=orders,
                policy=self.policy, now=self.clock(),
                bar_index=self._ordinal())
        except (LiveCustodyError, VenueEvidenceError) as exc:
            return self._unresolved(
                "flatten_unresolved",
                f"{type(exc).__name__}: {exc}", obligation_id)
        self.journal.append_once("flatten_confirmed",
                                 key=obligation_id,
                                 payload={"reconciliation":
                                          record["reconciliation"]})
        self.journal.append_once("plan_completed")
        return {"state": "completed",
                "obligation_id": obligation_id}
