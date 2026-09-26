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
3. **Reconciliation is the only exit, from one of TWO admissible sources.** An
   ``effect_unknown`` becomes ``succeeded`` or ``failed`` only through a
   READ-side observation of the actual order state, recorded as a separate event
   carrying its own observation timestamp. Not a retry, not a resend, not a
   timeout, not an operator's assumption. The read-versus-mutating axis of
   ``app/broker_refusal.py`` does that work here: an ``OrderObservation``
   refuses to exist unless it names a declared read-side query, and
   ``order_send`` is not one.

   **Correction of 2026-09-26 (owner's own).** The first ruling admitted only a
   LIVE query against the broker. That is stricter than the evidence requires
   and, whenever the terminal has gone silent, unsatisfiable — so an unknown
   effect would stay unknown forever and its route blocked forever. The second
   admissible source is therefore **the retained record**: the account-snapshot
   and trade-event streams this lane already stores. It is admitted only under
   conditions strict enough that the absence of data can never be read as the
   absence of an order — see ``RetainedRecordWindow`` and
   ``observation_from_retained_records``, where every one of those conditions
   can refuse. Neither source may be satisfied by a retry, a guess, a timeout or
   an operator's assumption, and every refusal keeps the command's state and its
   budget slot.
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
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

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
#: The second admissible source, kept distinct AT THE EVIDENCE LEVEL so a reader
#: never has to open the detail to learn that an exit came from the retained
#: streams rather than from a live broker query.
EVIDENCE_RETAINED_RECORDS_OBSERVED_NO_ORDER = (
    "retained_records_observed_no_order"
)

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
    EVIDENCE_RETAINED_RECORDS_OBSERVED_NO_ORDER,
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
#: The second admissible source, named as its own query: a read of the lane's
#: OWN retained streams. It is a read-side query in exactly the sense that
#: matters — it observes what happened and cannot cause anything — and it is
#: admitted only through ``observation_from_retained_records``, which is where
#: all of its conditions live.
QUERY_RETAINED_RECORDS = "lts.mt5.retained_records"
READ_SIDE_QUERIES = frozenset({
    "MetaTrader5.positions_get",
    "MetaTrader5.orders_get",
    "MetaTrader5.history_orders_get",
    "MetaTrader5.history_deals_get",
    "lts.mt5.account_snapshot",
    QUERY_RETAINED_RECORDS,
})

# ------------------------------------------------ the reconciliation sources
#: A live read-side query against the broker: the first ruling's only source.
SOURCE_LIVE_BROKER_QUERY = "live_broker_read_query"
#: The retained account-snapshot and trade-event streams: the source added by
#: the correction of 2026-09-26.
SOURCE_RETAINED_RECORDS = "retained_account_snapshots_and_trade_events"
RECONCILIATION_SOURCES = frozenset({
    SOURCE_LIVE_BROKER_QUERY, SOURCE_RETAINED_RECORDS,
})
#: The tables a retained-record observation rests on. Named in the appended
#: event, because an exit whose provenance is not readable is not provenance.
RETAINED_RECORD_TABLES = ("account_snapshots", "trade_events")


def reconciliation_source_for(query: Any) -> str:
    """Which admissible source a declared read query belongs to."""
    if query == QUERY_RETAINED_RECORDS:
        return SOURCE_RETAINED_RECORDS
    return SOURCE_LIVE_BROKER_QUERY
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
    source = reconciliation_source_for(observation.query)
    detail = {"query": observation.query,
              "reconciliation_source": source,
              "broker_reference": observation.broker_reference,
              **dict(observation.detail)}
    if observation.order_exists:
        if source == SOURCE_RETAINED_RECORDS:
            # The retained streams can witness that nothing is there. They
            # cannot witness that a position they DO see belongs to this
            # command rather than to another command or to a hand trade, and a
            # reconciliation that guessed the attribution would be the same
            # class of error this module exists to forbid.
            raise ReconciliationInconclusive(
                "the retained record shows exposure it cannot attribute to "
                "this command; an order that exists is confirmed by a live "
                "read-side broker query, never by attribution"
            )
        return Outcome(
            state=STATE_SUCCEEDED,
            evidence=EVIDENCE_READ_SIDE_OBSERVED_ORDER,
            event_kind=EVENT_RECONCILIATION,
            observed_at=observation.observed_at,
            detail=detail,
        )
    return Outcome(
        state=STATE_FAILED,
        evidence=(EVIDENCE_RETAINED_RECORDS_OBSERVED_NO_ORDER
                  if source == SOURCE_RETAINED_RECORDS
                  else EVIDENCE_READ_SIDE_OBSERVED_NO_ORDER),
        event_kind=EVENT_RECONCILIATION,
        observed_at=observation.observed_at,
        detail=detail,
    )


# ================================================ the retained-record source
#: How a window's far end is closed.
CLOSED_BY_NEXT_COMMAND = "next_command"
CLOSED_BY_END_OF_RECORD = "end_of_record"
WINDOW_CLOSURES = frozenset({CLOSED_BY_NEXT_COMMAND, CLOSED_BY_END_OF_RECORD})


def _aware(value: Any, what: str) -> datetime:
    """One instant, or a refusal. A naive timestamp is not a time."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{what} is not readable as a time") from error
    else:
        raise ValueError(f"{what} is missing")
    if moment.tzinfo is None:
        raise ValueError(f"{what} carries no timezone and so is not a time")
    return moment.astimezone(timezone.utc)


def _count(row: Mapping[str, Any], key: str) -> int:
    """A stored count, or a refusal. A missing count is NEVER read as zero —
    that inference is the whole defect this module exists to forbid."""
    value = row.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ReconciliationInconclusive(
            f"a retained observation is missing {key}; a missing count is not "
            f"a count of zero"
        )
    return value


@dataclass(frozen=True)
class RetainedRecordWindow:
    """The retained evidence about one unknown command's whole window.

    The window runs from the moment the unknown command COMPLETED to the moment
    the route's next command was created — the only interval in which an order
    from that command could have appeared and still be attributable to it. The
    admission conditions live in ``observation_from_retained_records`` and every
    one of them can refuse; this type only carries the rows.

    ``observations`` are the account snapshots inside the window, each a mapping
    with ``row_id``, ``observed_at``, ``positions_total`` and ``orders_total``.
    ``boundary_before`` and ``boundary_after`` are the observations that bracket
    it, so coverage is proved at both ends rather than assumed. ``trade_events``
    are the venue transactions received for the window. ``max_gap_seconds`` is
    the DECLARED continuity budget and comes from configuration — the lane's own
    ``stale_heartbeat_seconds`` — never from a number invented here.
    """

    command_id: str
    window_start: datetime
    window_end: datetime
    closed_by: str
    observations: Sequence[Mapping[str, Any]]
    trade_events: Sequence[Mapping[str, Any]]
    max_gap_seconds: float
    boundary_before: Optional[Mapping[str, Any]] = None
    boundary_after: Optional[Mapping[str, Any]] = None
    source_tables: Sequence[str] = RETAINED_RECORD_TABLES
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.closed_by not in WINDOW_CLOSURES:
            raise ValueError(f"undeclared window closure: {self.closed_by!r}")
        if self.max_gap_seconds <= 0:
            raise ValueError(
                "the continuity budget is a positive number of seconds, "
                "declared in configuration"
            )
        start = _aware(self.window_start, "window_start")
        end = _aware(self.window_end, "window_end")
        if end <= start:
            raise ValueError("a window ends after it starts")


def observation_from_retained_records(
    window: RetainedRecordWindow,
    *,
    now: Optional[datetime] = None,
) -> OrderObservation:
    """Admit the retained record as a positive no-order observation, or refuse.

    Six conditions. Each one exists because without it the absence of data could
    be read as the absence of an order, and every failure raises
    ``ReconciliationInconclusive`` naming what was missing — the command then
    keeps its ``effect_unknown`` state AND its budget slot.

    1. **The window is covered at its start.** There must be an observation at
       or before the moment the command completed, and it must read flat, so the
       account's state is known when the window opens.
    2. **The window is covered at its end.** Either the route's next command
       closes it and an observation exists at or after that moment — proving the
       stream was still alive when the window ended — or the window runs to the
       end of the record, and then the record must still be FRESH within the
       same declared budget. A stale store cannot settle an unknown whose window
       reaches the present; the bridge going silent is precisely the case that
       must refuse. The closing observation's own counts are deliberately NOT
       required to be flat: at that moment the route's next command exists, and
       its exposure is not this command's.
    3. **Every observation inside reads flat**, with both counts present. A
       missing count is not zero.
    4. **No transaction was received for the window.** ``trade_events`` is a
       pushed event stream, not a sample: one row in it is a transaction and
       refuses the reconciliation outright.
    5. **No interval of the window is unobserved for longer than the declared
       budget.** The continuity sequence runs from the opening observation
       through every observation inside to the window's own end, so the tail
       between the last flat observation and the end of the window is bounded by
       the same budget as every interior gap. This is the condition the one real
       ``effect_unknown`` in this fleet FAILS: its window holds a 424.7-second
       hole, on the terminal's own clock, in which the account was not observed
       at all.
    6. **Nothing is inferred from silence alone.** The observation returned can
       only ever say ``order_exists=False``; presence is never attributed from
       the retained streams (see ``outcome_from_observation``).

    Returns an ``OrderObservation`` naming ``QUERY_RETAINED_RECORDS``, its own
    observation timestamp and the rows it rests on, so the appended
    reconciliation event carries its whole provenance.
    """
    start = _aware(window.window_start, "window_start")
    end = _aware(window.window_end, "window_end")
    budget = float(window.max_gap_seconds)

    if window.boundary_before is None:
        raise ReconciliationInconclusive(
            "the retained record does not observe the account at the moment "
            "the unknown command completed; the window is not covered at its "
            "start and absence of an observation is not absence of an order"
        )
    before_at = _aware(window.boundary_before.get("observed_at"),
                       "boundary_before.observed_at")
    if before_at > start:
        raise ReconciliationInconclusive(
            "the first retained observation falls after the command completed; "
            "the window's opening moment is unobserved"
        )

    if window.closed_by == CLOSED_BY_NEXT_COMMAND:
        if window.boundary_after is None:
            raise ReconciliationInconclusive(
                "the retained record does not observe the account at the "
                "moment the route's next command was created; the window is "
                "not covered at its end"
            )
        after_at = _aware(window.boundary_after.get("observed_at"),
                          "boundary_after.observed_at")
        if after_at < end:
            raise ReconciliationInconclusive(
                "the closing retained observation falls before the window "
                "ends; the window's closing moment is unobserved"
            )
    else:
        if now is None:
            raise ReconciliationInconclusive(
                "a window closed by the end of the record is only admissible "
                "against a reference time, so its staleness can be measured"
            )
        reference = _aware(now, "now")
        newest = before_at
        for row in window.observations:
            observed = _aware(row.get("observed_at"), "observation.observed_at")
            if observed > newest:
                newest = observed
        age = (reference - newest).total_seconds()
        if age > budget:
            raise ReconciliationInconclusive(
                f"the retained record's newest observation is {age:.1f}s old "
                f"against a {budget:.1f}s budget; a stale record cannot settle "
                f"a window that reaches the present"
            )
        after_at = newest

    rows = [window.boundary_before, *window.observations]
    flat = 0
    for row in rows:
        positions = _count(row, "positions_total")
        orders = _count(row, "orders_total")
        if positions or orders:
            raise ReconciliationInconclusive(
                "the retained record shows exposure inside the window; what it "
                "belongs to is not settled by these streams and a live "
                "read-side broker query is required"
            )
        flat += 1

    if window.trade_events:
        raise ReconciliationInconclusive(
            f"{len(window.trade_events)} venue transaction(s) were received "
            f"for this window; a transaction is a positive event and refuses "
            f"the reconciliation rather than being explained away"
        )

    # The sequence ends at the WINDOW's end, not at the closing observation:
    # what has to be bounded is the interval between the last flat observation
    # and the moment the window closes.
    times = sorted(
        [_aware(row.get("observed_at"), "observation.observed_at")
         for row in rows] + [end]
    )
    worst = 0.0
    worst_from = None
    worst_to = None
    for index in range(len(times) - 1):
        gap = (times[index + 1] - times[index]).total_seconds()
        if gap > worst:
            worst = gap
            worst_from = times[index]
            worst_to = times[index + 1]
    if worst > budget:
        raise ReconciliationInconclusive(
            f"the retained observation is sampled across a {worst:.1f}s gap "
            f"({None if worst_from is None else worst_from.isoformat()} -> "
            f"{None if worst_to is None else worst_to.isoformat()}) against a "
            f"{budget:.1f}s declared budget; in that gap the account was not "
            f"observed at all, and absence of data is not absence of an order"
        )

    return OrderObservation(
        query=QUERY_RETAINED_RECORDS,
        observed_at=after_at,
        order_exists=False,
        detail={
            "reconciliation_source": SOURCE_RETAINED_RECORDS,
            "source_tables": list(window.source_tables),
            "command_id": window.command_id,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "window_closed_by": window.closed_by,
            "flat_observations": flat,
            "observation_row_ids": [row.get("row_id") for row in rows],
            "closing_observation_row_id": (
                None if window.boundary_after is None
                else window.boundary_after.get("row_id")),
            "trade_events": 0,
            "max_observed_gap_seconds": worst,
            "max_gap_budget_seconds": budget,
            **dict(window.detail),
        },
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
