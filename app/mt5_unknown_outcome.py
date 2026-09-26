"""The third outcome: an MT5 command whose effect was never observed.

Owner grant of 2026-09-26. This module closes the live defect RP149 recorded
and deliberately did not fix, because fixing it is a migration of a service:

    ``Mt5ExecutionStore.complete`` wrote ``state = "succeeded" if
    payload.success else "failed"``, and ``enqueue`` counted the daily entry
    budget with ``state != 'failed'``.

Composed, those two lines say: *a send that timed out did not happen, and the
budget slot it was holding is free again*. TRADE_RETCODE_TIMEOUT (10012) is
precisely the case where the terminal cancelled the REQUEST and never learned
what the trade server did with it. The position may exist. Recording it as
``failed`` is a store-level assertion that it does not, and at the budget
boundary that assertion lets a second entry through — the one error in this
lane that can double a position.

The ruling this module implements
--------------------------------
1. **An unknown effect is a third durable state**, ``effect_unknown``. It is a
   distinct stored value in its own right, not a flag hung on ``failed``.
   ``failed`` keeps one meaning only, and it is now a strong one: the venue's
   own structured facts PROVE that no order exists.
2. **An unknown outcome CONSUMES its daily budget slot.** A slot is released by
   a POSITIVE observation that no order exists, never by the absence of a
   confirmation. ``consumes_budget_slot`` is written as a deny-by-default
   predicate for that reason: a state this module has never heard of holds the
   slot rather than freeing it.
3. **Reconciliation is the only exit.** An ``effect_unknown`` becomes
   ``succeeded`` or ``failed`` only through a READ-side query against the broker
   that observes the actual order state, recorded as a separate event carrying
   its own observation timestamp. Not a retry, not a resend, not a timeout, not
   an operator's assumption. The read-versus-mutating axis of
   ``app/broker_refusal.py`` does that work here: an ``OrderObservation``
   refuses to exist unless it names a declared read-side broker query, and
   ``order_send`` is not one.
4. **Nothing is rewritten in place.** Outcomes are APPENDED to
   ``execution_command_outcomes``; the read path prefers the latest record per
   command. A reconciliation, and a migration's correction of a historical
   mislabelling, are both new records with their own timestamps. The corpus
   forbids editing a record that was already published, so neither does this.

No message is ever read. Every verdict here comes from a machine code, a
boolean the venue set, the presence or absence of a structured field, or the
call site a fact came from — the same discipline ``app/broker_refusal.py`` and
``app/mt5_refusal.py`` keep, and pinned by the same AST tests.

Offline by construction: nothing imported here can open a socket, read a
credential, contact a terminal or place an order.
"""
from __future__ import annotations

import json

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from app.broker_refusal import (
    OPERATION_MUTATING,
    OPERATION_READ,
    BrokerRefusal,
)
from app.mt5_refusal import (
    Mt5ContradictoryResult,
    Mt5SuccessIsNotARefusal,
    command_state_for,
    refusal_for_execution_result,
)

#: Schema of one appended outcome record.
OUTCOME_SCHEMA = "lts.mt5.command_outcome.v1"

# ------------------------------------------------------------ the states
STATE_PENDING = "pending"
STATE_DELIVERED = "delivered"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
#: The third outcome. Neither success nor failure: the effect was never
#: observed, so the only honest thing the store can say is that it does not
#: know. It is a stored value like the other four.
STATE_EFFECT_UNKNOWN = "effect_unknown"

#: States a command can be in before anything terminal is known.
OPEN_STATES = frozenset({STATE_PENDING, STATE_DELIVERED})
#: States ``complete`` and a reconciliation may write.
TERMINAL_STATES = frozenset({STATE_SUCCEEDED, STATE_FAILED,
                             STATE_EFFECT_UNKNOWN})
STATES = OPEN_STATES | TERMINAL_STATES

#: A command in this state is not resolved: it either has not finished, or it
#: finished with an effect nobody has observed. Both block new risk on the
#: route, and the second one is the addition this module makes.
UNRESOLVED_STATES = OPEN_STATES | {STATE_EFFECT_UNKNOWN}

#: The ONLY state that releases a daily entry budget slot. ``failed`` now means
#: exactly one thing — the venue's structured facts prove no order exists —
#: and it is the only reading under which the slot was never consumed.
#: ``succeeded`` does not release a slot either: it spent it.
BUDGET_RELEASING_STATES = frozenset({STATE_FAILED})

#: An ``effect_unknown`` is the only state a reconciliation may be applied to.
RECONCILABLE_STATES = frozenset({STATE_EFFECT_UNKNOWN})


def consumes_budget_slot(state: Any) -> bool:
    """Does a command in this state hold its daily entry slot?

    Deny by default, and that direction is the whole fix. The question is NOT
    "do we know this order failed?" but "can we prove no order exists?" — so
    anything that is not a proven non-effect consumes the slot, including a
    state this module has never heard of.
    """
    return state not in BUDGET_RELEASING_STATES


def is_unresolved(state: Any) -> bool:
    """Does this state leave risk unaccounted for on its route?"""
    return state in UNRESOLVED_STATES


# ---------------------------------------------------------- event kinds
#: How an outcome record came to be. Each is a separate appended event.
EVENT_EA_RESULT = "ea_result"                     # the EA posted a result
EVENT_RECONCILIATION = "reconciliation"           # a read-side observation
EVENT_MIGRATION_CORRECTION = "migration_correction"  # a historical relabel
EVENT_KINDS = frozenset({EVENT_EA_RESULT, EVENT_RECONCILIATION,
                         EVENT_MIGRATION_CORRECTION})

# ------------------------------------------------------------- evidence
# WHY a state was written. Every outcome record carries one, so a reader never
# has to infer from a state what was actually observed.
EVIDENCE_VENUE_REPORTED_SUCCESS = "venue_reported_success"
EVIDENCE_RETCODE_PROVES_NO_ORDER = "retcode_proves_no_order_exists"
EVIDENCE_RETCODE_LEAVES_OUTCOME_UNPROVEN = "retcode_leaves_outcome_unproven"
EVIDENCE_NO_MACHINE_CODE_REPORTED = "no_machine_code_reported"
EVIDENCE_RESULT_CONTRADICTS_ITSELF = "result_contradicts_itself"
EVIDENCE_DELIVERED_WITHOUT_A_RESULT = "delivered_without_a_result"
EVIDENCE_NEVER_DELIVERED_TO_THE_EA = "never_delivered_to_the_ea"
#: The migration's honest answer where the stored record supports neither
#: reading. It is recorded AS unknown and the row says why, because a record
#: that cannot distinguish "refused before sending" from "sent, never
#: confirmed" is not evidence that nothing was sent.
EVIDENCE_RECORD_CANNOT_SUPPORT_EITHER_READING = (
    "record_cannot_support_either_reading"
)
EVIDENCE_READ_SIDE_OBSERVED_ORDER = "read_side_observed_the_order"
EVIDENCE_READ_SIDE_OBSERVED_NO_ORDER = "read_side_observed_no_order"

#: Evidence that can only ever justify ``effect_unknown``.
UNKNOWN_EVIDENCE = frozenset({
    EVIDENCE_RETCODE_LEAVES_OUTCOME_UNPROVEN,
    EVIDENCE_NO_MACHINE_CODE_REPORTED,
    EVIDENCE_RESULT_CONTRADICTS_ITSELF,
    EVIDENCE_DELIVERED_WITHOUT_A_RESULT,
    EVIDENCE_RECORD_CANNOT_SUPPORT_EITHER_READING,
})
#: Evidence that can only come from a read-side reconciliation.
RECONCILIATION_EVIDENCE = frozenset({
    EVIDENCE_READ_SIDE_OBSERVED_ORDER,
    EVIDENCE_READ_SIDE_OBSERVED_NO_ORDER,
})
EVIDENCE = UNKNOWN_EVIDENCE | RECONCILIATION_EVIDENCE | frozenset({
    EVIDENCE_VENUE_REPORTED_SUCCESS,
    EVIDENCE_RETCODE_PROVES_NO_ORDER,
    EVIDENCE_NEVER_DELIVERED_TO_THE_EA,
})


# ------------------------------------------------------- the call sites
#: MetaTrader5 calls that only OBSERVE. A reconciliation must come from one of
#: these; the axis is the same one ``broker_refusal`` draws, applied to the call
#: site rather than to the failure. ``account_snapshot`` is this repository's
#: own read path (the bridge's signed snapshot), named here so a reconciliation
#: can cite it.
READ_SIDE_QUERIES = frozenset({
    "MetaTrader5.positions_get",
    "MetaTrader5.orders_get",
    "MetaTrader5.history_orders_get",
    "MetaTrader5.history_deals_get",
    "lts.mt5.account_snapshot",
})
#: Calls that can CHANGE venue state. Naming them is not decoration: a
#: reconciliation citing one of these is not an observation of the effect, it is
#: a second attempt at causing it, and it is refused by name.
MUTATING_CALL_SITES = frozenset({
    "MetaTrader5.order_send",
    "MetaTrader5.order_check",
    "lts.mt5.execution_command",
})


class ReconciliationInconclusive(ValueError):
    """The broker was asked and did not answer whether the order exists.

    Raised instead of returning an outcome, because the caller must be unable
    to mistake "we could not tell" for "there is nothing there". The command
    stays ``effect_unknown`` and keeps its budget slot.
    """


class ReconciliationIsNotARead(ValueError):
    """A reconciliation was offered a mutating call site as its evidence."""


class HistoricalRowNotAFailure(ValueError):
    """The migration was handed a row it has no ruling about."""


@dataclass(frozen=True)
class Outcome:
    """One terminal verdict about one command, with the reason it was reached.

    ``state`` is what the store will report; ``evidence`` is what was actually
    observed. The pair is validated against itself, so an ``effect_unknown``
    can never be recorded with evidence that proves an order's absence, and a
    ``failed`` can never be recorded with evidence that proves nothing.
    """

    state: str
    evidence: str
    event_kind: str
    refusal: Optional[BrokerRefusal] = None
    observed_at: Optional[datetime] = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in TERMINAL_STATES:
            raise ValueError(f"not a terminal outcome state: {self.state!r}")
        if self.evidence not in EVIDENCE:
            raise ValueError(f"undeclared evidence: {self.evidence!r}")
        if self.event_kind not in EVENT_KINDS:
            raise ValueError(f"undeclared event kind: {self.event_kind!r}")
        if (self.state == STATE_EFFECT_UNKNOWN) != (
            self.evidence in UNKNOWN_EVIDENCE
        ):
            raise ValueError(
                "an unknown effect and its evidence must agree: evidence that "
                "proves an outcome cannot record it as unknown, and evidence "
                "that proves nothing cannot record it as known"
            )
        if (self.evidence in RECONCILIATION_EVIDENCE) != (
            self.event_kind == EVENT_RECONCILIATION
        ):
            raise ValueError(
                "a read-side observation is a reconciliation event and nothing "
                "else may claim one"
            )
        if self.event_kind == EVENT_RECONCILIATION and self.observed_at is None:
            raise ValueError(
                "a reconciliation carries the timestamp the broker state was "
                "observed at; without it the exit has no provenance"
            )

    @property
    def consumes_budget_slot(self) -> bool:
        return consumes_budget_slot(self.state)

    def as_fact(self) -> dict[str, Any]:
        return {
            "schema": OUTCOME_SCHEMA,
            "state": self.state,
            "evidence": self.evidence,
            "event_kind": self.event_kind,
            "consumes_budget_slot": self.consumes_budget_slot,
            "observed_at": None if self.observed_at is None
            else self.observed_at.isoformat(),
            "refusal": None if self.refusal is None else self.refusal.as_fact(),
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class OrderObservation:
    """What a READ-side broker query saw about one command's order.

    ``order_exists`` is a tri-state on purpose. ``None`` is not "no": it is
    "the question was asked and not answered", and it cannot be turned into an
    outcome (see ``ReconciliationInconclusive``). A missing fact is never read
    as zero here, exactly as in ``mt5_policy_risk.PositionState``.
    """

    query: str
    observed_at: datetime
    order_exists: Optional[bool]
    operation: str = OPERATION_READ
    broker_reference: Optional[str] = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.operation != OPERATION_READ:
            raise ReconciliationIsNotARead(
                "an unknown effect is observed, never re-attempted: a "
                f"reconciliation is a {OPERATION_READ} and "
                f"{OPERATION_MUTATING} was offered"
            )
        if self.query in MUTATING_CALL_SITES:
            raise ReconciliationIsNotARead(
                f"{self.query} can change venue state; it cannot witness what "
                f"an earlier call did"
            )
        if self.query not in READ_SIDE_QUERIES:
            raise ReconciliationIsNotARead(
                f"{self.query!r} is not a declared read-side broker query; an "
                f"exit from an unknown effect names the query that observed it"
            )
        if not isinstance(self.observed_at, datetime):
            raise ValueError("an observation carries when it was observed")
        if self.observed_at.tzinfo is None:
            raise ValueError(
                "an observation timestamp without a timezone is not a time"
            )
        if self.order_exists is not None and not isinstance(
            self.order_exists, bool
        ):
            raise ValueError(
                "order_exists is a boolean the broker's own fields decided, or "
                "None for an unanswered question"
            )


def outcome_from_observation(observation: OrderObservation) -> Outcome:
    """Turn one read-side observation into the exit from ``effect_unknown``.

    Both exits are POSITIVE findings. ``order_exists=False`` means the broker
    was asked and answered that no such order is there — that is what releases
    a budget slot. An unanswered query raises instead of returning, because
    "we did not see it" is the very inference this module exists to forbid.
    """
    if observation.order_exists is None:
        raise ReconciliationInconclusive(
            "the broker did not answer whether the order exists; the command "
            "stays unknown and keeps its budget slot, because the absence of a "
            "confirmation is not an observation of absence"
        )
    detail = {"query": observation.query,
              "broker_reference": observation.broker_reference,
              **dict(observation.detail)}
    if observation.order_exists:
        return Outcome(
            state=STATE_SUCCEEDED,
            evidence=EVIDENCE_READ_SIDE_OBSERVED_ORDER,
            event_kind=EVENT_RECONCILIATION,
            observed_at=observation.observed_at,
            detail=detail,
        )
    return Outcome(
        state=STATE_FAILED,
        evidence=EVIDENCE_READ_SIDE_OBSERVED_NO_ORDER,
        event_kind=EVENT_RECONCILIATION,
        observed_at=observation.observed_at,
        detail=detail,
    )


def outcome_for_execution_result(
    result: Mapping[str, Any],
    *,
    client_order_id: Optional[str] = None,
) -> Outcome:
    """The outcome of one ``ExecutionResultPayload``-shaped mapping.

    This is the call ``Mt5ExecutionStore.complete`` was missing. It reads
    ``success`` and ``result_code`` through ``mt5_refusal`` and never the
    message. Three answers, not two:

    * ``success`` true -> ``succeeded``;
    * a retcode that PROVES no order exists -> ``failed``;
    * a retcode that leaves the outcome unproven, no code at all, or a result
      that contradicts itself -> ``effect_unknown``.

    A self-contradicting payload (``success=False`` carrying
    TRADE_RETCODE_DONE) is not resolved in either direction: resolving it would
    be a guess about whether a position exists, so it is recorded as unknown
    and held for reconciliation.
    """
    success = result.get("success")
    if success is True:
        return Outcome(
            state=STATE_SUCCEEDED,
            evidence=EVIDENCE_VENUE_REPORTED_SUCCESS,
            event_kind=EVENT_EA_RESULT,
            detail={"result_code": result.get("result_code")},
        )
    try:
        refusal = refusal_for_execution_result(
            result, operation=OPERATION_MUTATING,
            client_order_id=client_order_id,
        )
    except Mt5SuccessIsNotARefusal:
        # ``success`` was neither True nor False-with-a-refusal: the payload
        # claims success by a route this function did not take. Unknown.
        return Outcome(
            state=STATE_EFFECT_UNKNOWN,
            evidence=EVIDENCE_RESULT_CONTRADICTS_ITSELF,
            event_kind=EVENT_EA_RESULT,
            detail={"reason": "the result reports success inconsistently"},
        )
    except Mt5ContradictoryResult as error:
        return Outcome(
            state=STATE_EFFECT_UNKNOWN,
            evidence=EVIDENCE_RESULT_CONTRADICTS_ITSELF,
            event_kind=EVENT_EA_RESULT,
            detail={"contradiction": str(error)},
        )
    return _outcome_from_refusal(refusal, result.get("result_code"),
                                 EVENT_EA_RESULT)


def _outcome_from_refusal(
    refusal: BrokerRefusal, raw_code: Any, event_kind: str
) -> Outcome:
    """Map one typed refusal onto a durable state and its evidence.

    ``mt5_refusal.command_state_for`` already names the state; this adds the
    evidence, and the code below is the only place the two vocabularies meet.
    """
    state = command_state_for(refusal)
    if state == STATE_EFFECT_UNKNOWN:
        has_code = isinstance(raw_code, int) and not isinstance(raw_code, bool)
        evidence = (EVIDENCE_RETCODE_LEAVES_OUTCOME_UNPROVEN
                    if has_code and raw_code != 0
                    else EVIDENCE_NO_MACHINE_CODE_REPORTED)
        return Outcome(state=state, evidence=evidence, event_kind=event_kind,
                       refusal=refusal, detail={"result_code": raw_code})
    return Outcome(state=STATE_FAILED,
                   evidence=EVIDENCE_RETCODE_PROVES_NO_ORDER,
                   event_kind=event_kind, refusal=refusal,
                   detail={"result_code": raw_code})


def outcome_for_historical_row(row: Mapping[str, Any]) -> Outcome:
    """Re-read one historical ``failed`` row from the evidence it carries.

    Every historical ``failed`` row was written by the collapsing line, so the
    label proves nothing on its own. What the row DOES carry is structured:

    * a stored result payload -> classified exactly as a live one is, so a
      timeout becomes ``effect_unknown`` and an invalid volume stays ``failed``;
    * no result payload and no delivery -> the command never reached the EA, so
      nothing was sent: ``failed``, and that is a positive finding from our own
      store rather than an absence of news;
    * no result payload but a delivery -> sent, never confirmed:
      ``effect_unknown``;
    * a result payload that is not readable as a result -> the record supports
      NEITHER reading, so it is recorded as unknown and says so.

    Only ``failed`` rows are in scope: a ``succeeded`` row carries the venue's
    own success and an open row is not terminal at all.
    """
    state = row.get("state")
    if state != STATE_FAILED:
        raise HistoricalRowNotAFailure(
            f"only a historical {STATE_FAILED!r} row is re-read; this row is "
            f"{state!r} and is left exactly as it is"
        )
    raw = row.get("result_json")
    if raw is None or raw == "":
        if row.get("delivered_at") is None:
            return Outcome(
                state=STATE_FAILED,
                evidence=EVIDENCE_NEVER_DELIVERED_TO_THE_EA,
                event_kind=EVENT_MIGRATION_CORRECTION,
                detail={"delivered_at": None, "result_json": None,
                        "reading": "the command was never handed to the EA, so "
                                   "no order could have been sent"},
            )
        return Outcome(
            state=STATE_EFFECT_UNKNOWN,
            evidence=EVIDENCE_DELIVERED_WITHOUT_A_RESULT,
            event_kind=EVENT_MIGRATION_CORRECTION,
            detail={"delivered_at": row.get("delivered_at"),
                    "result_json": None,
                    "reading": "the command was delivered and no result was "
                               "ever recorded: sent, never confirmed"},
        )
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        return _indeterminate("the stored result is not readable as JSON")
    if not isinstance(result, Mapping):
        return _indeterminate("the stored result is not an object")
    success = result.get("success")
    if success is True:
        return Outcome(
            state=STATE_EFFECT_UNKNOWN,
            evidence=EVIDENCE_RESULT_CONTRADICTS_ITSELF,
            event_kind=EVENT_MIGRATION_CORRECTION,
            detail={"reading": "the row is recorded failed and its stored "
                               "result reports success; the contradiction is "
                               "not resolved in either direction"},
        )
    if not isinstance(success, bool):
        return _indeterminate(
            "the stored result does not state success as a boolean"
        )
    try:
        refusal = refusal_for_execution_result(
            result, operation=OPERATION_MUTATING,
        )
    except (Mt5ContradictoryResult, Mt5SuccessIsNotARefusal) as error:
        return Outcome(
            state=STATE_EFFECT_UNKNOWN,
            evidence=EVIDENCE_RESULT_CONTRADICTS_ITSELF,
            event_kind=EVENT_MIGRATION_CORRECTION,
            detail={"contradiction": str(error)},
        )
    return _outcome_from_refusal(refusal, result.get("result_code"),
                                 EVENT_MIGRATION_CORRECTION)


def _indeterminate(reading: str) -> Outcome:
    """The record cannot support either reading, and the row says so."""
    return Outcome(
        state=STATE_EFFECT_UNKNOWN,
        evidence=EVIDENCE_RECORD_CANNOT_SUPPORT_EITHER_READING,
        event_kind=EVENT_MIGRATION_CORRECTION,
        detail={"reading": reading,
                "ruling": "a record that cannot distinguish a refusal before "
                          "sending from a send that was never confirmed is "
                          "not evidence that nothing was sent"},
    )
