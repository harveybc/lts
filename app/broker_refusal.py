"""Typed refusal and DECLARED recovery for the demo/paper venue adapters.

RP157 lane 3 (demo/paper broker refusal/recovery interfaces). Offline by
construction: this module imports nothing that can open a socket, reads no
credential, and places no order. It classifies failures that the adapters
already produce.

Why this module exists
----------------------
``app/runner_retry_taxonomy.py`` answers ONE question — may the runner try
again, or must it page? That is the right question for a polling loop and
the wrong question for an order. A submission that failed with an unproven
outcome must not be "retried"; it must be reconciled. A venue that refused
an order for lack of entitlement must never be retried at all. And a
refusal must never be downgraded into a silent no-op, nor an unknown state
read as flat.

So this module adds the second axis — WHAT the venue refused and WHICH
recovery is therefore declared — and reuses ``transient_cause`` for the
first. It is a taxonomy, like its neighbour: no service, no registry, no
state of its own.

Message sniffing is refused here exactly as it is refused next door.
Classification reads only structured facts — exception TYPE and errno,
HTTP status, the venue's own machine code. The venue's words are CARRIED,
byte for byte, and never interpreted. ``test_broker_refusal_recovery.py``
pins that by classifying the same structured facts under adversarial prose
and requiring an identical verdict.

The implicit contract each adapter already had (surveyed 2026-09-25)
--------------------------------------------------------------------
``app/alpaca_paper_lab.py`` (read path, ``AlpacaPaperClient._get``)
    Raises ``AlpacaPaperError`` for every failure. A transport failure is
    re-raised ``from exc`` — R4 made that chain the whole reason a blip is
    still classified transient. An HTTP refusal is a NEW exception with no
    cause, carrying the status and the venue's ``message`` inside the
    message STRING; the probe list records ``http_status`` and an
    ``error_kind``. Leaves behind: one probe row per attempt. Retries:
    none of its own; the runner retries.

``app/alpaca_l1.py`` (write path, ``AlpacaPaperTradingClient._write_request``
and ``AlpacaL1Executor.submit``)
    Same exception type for transport and refusal. The executor journals a
    ``call_attempt`` fact and advances the effect to ``effect_unknown``
    BEFORE the venue is called, so a crash or a transport failure mid-flight
    is provably ambiguous; any exception sets ``halt=hold`` and re-raises. A
    repeated idempotency key replays the journal and never submits twice.
    What it did NOT do before this lane: read the hold back. ``submit`` set
    ``halt=hold`` and never checked it, so a fresh idempotency key could
    place new risk while an earlier effect was still ``effect_unknown``.

``app/ibkr_l1_executor.py`` + ``app/ibkr_l1_broker.py``
    The reference implementation, and unchanged by this lane. Capability
    burn and first durable effect are one transaction; every broker call is
    bracketed by ``call_attempt``/``call_result`` facts; a failure raises
    ``L1EffectUnknown`` after journalling ``effect_unknown``; ``halt`` is
    read before new risk; ``resume_report`` demotes, never promotes.

``app/ibkr_l1_outbox.py``
    Durable ``terminal_rejected`` effects for fail-closed refusals,
    deferrals for recoverable ones, ``effect_unknown`` plus a hold for
    anything unproven. "A missing fact is never read as zero."

``app/capital_demo_lab.py``
    GET-only observer; no order path exists. Transport failures are wrapped
    ``from exc``; HTTP refusals carry only the status in the message string
    and were indistinguishable from one another (401 vs 403 vs 422).

``app/oanda_practice_lab.py``
    Wraps every HTTP >= 400 into ``OandaPracticeError``, interpolating the
    venue's ``errorMessage``/``errorCode`` into the message string. The
    machine code was reachable only by parsing that prose.

``app/ibkr_l1_tws.py``
    Records IBKR connectivity codes 1100/1101/1102 as session events. The
    rest of the IBKR error-code space reached no classifier.

What this lane changed: the adapters now ATTACH the venue's structured
facts to the exception they already raised (``attach_venue_facts``), the
message strings are untouched, and ``AlpacaL1Executor`` reads the hold and
the unknown effects back before placing new risk.
"""
from __future__ import annotations

import json

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from app.runner_retry_taxonomy import transient_cause

# --------------------------------------------------------------- kinds
# One name per way a demo/paper venue can refuse. Every kind is a REFUSAL:
# there is deliberately no "ignored", "warning" or "no-op" member.
KIND_VENUE_REJECTION = "venue_rejection"
KIND_INSUFFICIENT_ENTITLEMENT = "insufficient_entitlement"
KIND_MARKET_CLOSED = "market_closed"
KIND_INSTRUMENT_NOT_PERMITTED = "instrument_not_permitted"
KIND_DUPLICATE_CLIENT_ORDER_ID = "duplicate_client_order_id"
KIND_STALE_QUOTE = "stale_quote"
KIND_TRANSPORT_FAILURE = "transport_failure"
KIND_AUTHENTICATION_FAILURE = "authentication_failure"

KINDS = frozenset({
    KIND_VENUE_REJECTION, KIND_INSUFFICIENT_ENTITLEMENT, KIND_MARKET_CLOSED,
    KIND_INSTRUMENT_NOT_PERMITTED, KIND_DUPLICATE_CLIENT_ORDER_ID,
    KIND_STALE_QUOTE, KIND_TRANSPORT_FAILURE, KIND_AUTHENTICATION_FAILURE,
})

# ----------------------------------------------------------- recoveries
RECOVERY_RETRY_WITH_BOUND = "retry_with_bound"
RECOVERY_RESUME_FROM_PERSISTED_CLIENT_ORDER_ID = (
    "resume_from_persisted_client_order_id"
)
RECOVERY_RECONCILE_UNKNOWN_FILL = "reconcile_unknown_fill"
RECOVERY_REFUSE_TO_PROCEED = "refuse_to_proceed"

RECOVERIES = frozenset({
    RECOVERY_RETRY_WITH_BOUND,
    RECOVERY_RESUME_FROM_PERSISTED_CLIENT_ORDER_ID,
    RECOVERY_RECONCILE_UNKNOWN_FILL,
    RECOVERY_REFUSE_TO_PROCEED,
})

#: Which recoveries each kind may ever declare. An entitlement or
#: authentication failure can NEVER be retried as if it were transient:
#: that is the single most expensive mislabelling this taxonomy prevents.
_ALLOWED_RECOVERIES: dict[str, frozenset[str]] = {
    KIND_TRANSPORT_FAILURE: frozenset({
        RECOVERY_RETRY_WITH_BOUND, RECOVERY_RECONCILE_UNKNOWN_FILL,
    }),
    KIND_STALE_QUOTE: frozenset({
        RECOVERY_RETRY_WITH_BOUND, RECOVERY_REFUSE_TO_PROCEED,
    }),
    KIND_DUPLICATE_CLIENT_ORDER_ID: frozenset({
        RECOVERY_RESUME_FROM_PERSISTED_CLIENT_ORDER_ID,
    }),
    KIND_VENUE_REJECTION: frozenset({
        RECOVERY_REFUSE_TO_PROCEED, RECOVERY_RECONCILE_UNKNOWN_FILL,
    }),
    KIND_INSUFFICIENT_ENTITLEMENT: frozenset({RECOVERY_REFUSE_TO_PROCEED}),
    KIND_AUTHENTICATION_FAILURE: frozenset({RECOVERY_REFUSE_TO_PROCEED}),
    KIND_MARKET_CLOSED: frozenset({RECOVERY_REFUSE_TO_PROCEED}),
    KIND_INSTRUMENT_NOT_PERMITTED: frozenset({RECOVERY_REFUSE_TO_PROCEED}),
}

#: A call that can change venue state. The distinction is not decoration:
#: a transport failure on a READ is a blip to retry, and the SAME failure
#: on a MUTATING call is an unproven outcome to reconcile.
OPERATION_READ = "read"
OPERATION_MUTATING = "mutating"
OPERATIONS = frozenset({OPERATION_READ, OPERATION_MUTATING})


class BrokerRefusalError(RuntimeError):
    """Raised by an adapter that refuses, carrying its typed refusal."""

    def __init__(self, refusal: "BrokerRefusal") -> None:
        super().__init__(f"{refusal.kind}: {refusal.venue_reason}")
        self.refusal = refusal


@dataclass(frozen=True)
class RetryBound:
    """The bound of a declared retry. ``max_attempts=None`` means bounded
    in CADENCE and unbounded in count — a polling runner that stops asking
    cannot notice that the venue came back (see ``backoff_seconds``)."""

    base_seconds: float
    cap_seconds: float
    max_attempts: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.base_seconds > 0 or not self.cap_seconds > 0:
            raise ValueError("retry bound seconds must be positive")
        if self.cap_seconds < self.base_seconds:
            raise ValueError("retry cap must not be below the base")
        if self.max_attempts is not None and self.max_attempts < 1:
            raise ValueError("retry attempts start at 1")

    def as_fact(self) -> dict[str, Any]:
        return {
            "base_seconds": float(self.base_seconds),
            "cap_seconds": float(self.cap_seconds),
            "max_attempts": self.max_attempts,
        }


#: The polling cadence the model runners already use (15s doubling to 300s).
POLL_RETRY_BOUND = RetryBound(base_seconds=15.0, cap_seconds=300.0)
#: A deferral that re-evaluates on the next due bar rather than spinning.
QUOTE_RETRY_BOUND = RetryBound(base_seconds=15.0, cap_seconds=60.0,
                               max_attempts=3)


@dataclass(frozen=True)
class BrokerRefusal:
    """One typed refusal with its declared recovery.

    ``venue_reason`` is the venue's OWN reason, verbatim. It is never
    reworded, truncated, normalised or parsed. A refusal that cannot state
    the venue's reason refuses to be constructed.
    """

    kind: str
    venue: str
    venue_reason: str
    recovery: str
    operation: str = OPERATION_READ
    retry_bound: Optional[RetryBound] = None
    venue_status: Optional[int] = None
    venue_code: Optional[Any] = None
    transient_cause_type: Optional[str] = None
    client_order_id: Optional[str] = None
    state_is_unknown: bool = False
    blocks_new_orders: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown refusal kind: {self.kind!r}")
        if self.recovery not in RECOVERIES:
            raise ValueError(f"unknown recovery: {self.recovery!r}")
        if self.operation not in OPERATIONS:
            raise ValueError(f"unknown operation: {self.operation!r}")
        if not isinstance(self.venue, str) or not self.venue:
            raise ValueError("a refusal must name its venue")
        if not isinstance(self.venue_reason, str) or not self.venue_reason:
            raise ValueError(
                "a refusal must carry the venue's own reason; an empty "
                "reason would be a silent no-op"
            )
        if self.recovery not in _ALLOWED_RECOVERIES[self.kind]:
            raise ValueError(
                f"{self.kind} may not declare recovery {self.recovery}"
            )
        if (self.recovery == RECOVERY_RETRY_WITH_BOUND) != (
            self.retry_bound is not None
        ):
            raise ValueError(
                "a retry declares its bound and nothing else carries one"
            )
        if (
            self.operation == OPERATION_MUTATING
            and self.recovery == RECOVERY_RETRY_WITH_BOUND
        ):
            raise ValueError(
                "a mutating call is never retried: its outcome is unproven "
                "and the recovery is reconciliation"
            )
        if self.state_is_unknown:
            if not self.blocks_new_orders:
                raise ValueError(
                    "an unknown state always blocks new orders; it is never "
                    "assumed flat"
                )
            if self.recovery == RECOVERY_RETRY_WITH_BOUND:
                raise ValueError("an unknown state is reconciled, not retried")

    @property
    def is_transient(self) -> bool:
        """True only where the runner-level taxonomy would also retry."""
        return self.recovery == RECOVERY_RETRY_WITH_BOUND

    def as_fact(self) -> dict[str, Any]:
        """A journalable fact. The venue's reason survives verbatim."""
        return {
            "schema": REFUSAL_FACT_SCHEMA,
            "kind": self.kind,
            "venue": self.venue,
            "venue_reason": self.venue_reason,
            "venue_status": self.venue_status,
            "venue_code": self.venue_code,
            "operation": self.operation,
            "recovery": self.recovery,
            "retry_bound": None if self.retry_bound is None
            else self.retry_bound.as_fact(),
            "transient_cause_type": self.transient_cause_type,
            "client_order_id": self.client_order_id,
            "state_is_unknown": bool(self.state_is_unknown),
            "blocks_new_orders": bool(self.blocks_new_orders),
            "detail": dict(self.detail),
        }


REFUSAL_FACT_SCHEMA = "lts.broker_refusal.v1"


# ------------------------------------------- structured facts on errors
_FACT_ATTRIBUTES = ("venue_status", "venue_code", "venue_reason")


def attach_venue_facts(
    error: BaseException,
    *,
    status: Optional[int] = None,
    code: Optional[Any] = None,
    reason: Optional[str] = None,
) -> BaseException:
    """Attach the venue's structured facts to an adapter exception.

    The exception TYPE and its message are untouched: existing callers and
    tests keep working, and the classifier stops needing the message. The
    reason is stored exactly as the venue sent it.
    """
    if status is not None:
        error.venue_status = int(status)
    if code is not None:
        error.venue_code = code
    if reason is not None:
        error.venue_reason = reason
    return error


def venue_facts(error: BaseException) -> dict[str, Any]:
    """Read back whatever structured facts an exception carries."""
    return {
        name: getattr(error, name, None) for name in _FACT_ATTRIBUTES
    }


# ------------------------------------------------------- venue code maps
#: IBKR's documented error codes. 1101/1102 are RESTORATION events and are
#: deliberately absent: ``ibkr_l1_tws.connectivity_events`` records those,
#: and a restoration is not a refusal.
IBKR_ERROR_KINDS: dict[int, str] = {
    103: KIND_DUPLICATE_CLIENT_ORDER_ID,   # duplicate order id
    200: KIND_INSTRUMENT_NOT_PERMITTED,    # no security definition found
    201: KIND_VENUE_REJECTION,             # order rejected - reason follows
    202: KIND_VENUE_REJECTION,             # order cancelled
    354: KIND_INSUFFICIENT_ENTITLEMENT,    # market data not subscribed
    502: KIND_TRANSPORT_FAILURE,           # could not connect to TWS
    504: KIND_TRANSPORT_FAILURE,           # not connected
    1100: KIND_TRANSPORT_FAILURE,          # connectivity to IB lost
}

#: OANDA v20 reject reasons are machine enum values, not prose, so keying
#: on them is not message sniffing. Transcribed from venue documentation
#: and NOT verified against the live venue by this lane; an unrecognised
#: value falls through to a fail-closed venue rejection.
OANDA_REJECT_KINDS: dict[str, str] = {
    "INSUFFICIENT_AUTHORIZATION": KIND_INSUFFICIENT_ENTITLEMENT,
    "MARKET_HALTED": KIND_MARKET_CLOSED,
    "INSTRUMENT_NOT_TRADEABLE": KIND_INSTRUMENT_NOT_PERMITTED,
    "INVALID_INSTRUMENT": KIND_INSTRUMENT_NOT_PERMITTED,
    "CLIENT_ORDER_ID_ALREADY_EXISTS": KIND_DUPLICATE_CLIENT_ORDER_ID,
}

#: Alpaca answers errors with a numeric ``code`` beside its ``message``.
#: That code space was not observable from here — reading it would require
#: contacting the venue, which this lane may not do — so it is left empty
#: on purpose rather than guessed. HTTP status governs, and an unknown
#: code stays a fail-closed venue rejection. Populating this table is an
#: entitlement-mandate item, not a code change.
ALPACA_ERROR_KINDS: dict[Any, str] = {}

VENUE_CODE_KINDS: dict[str, Mapping[Any, str]] = {
    "ibkr_paper": IBKR_ERROR_KINDS,
    "oanda_practice": OANDA_REJECT_KINDS,
    "alpaca_paper": ALPACA_ERROR_KINDS,
}

#: HTTP status -> kind, for the venues that answer over HTTP. 5xx and 429
#: are deliberately split by operation below.
_STATUS_KINDS: dict[int, str] = {
    401: KIND_AUTHENTICATION_FAILURE,
    403: KIND_INSUFFICIENT_ENTITLEMENT,
}


def _kind_for_status(status: int) -> str:
    if status in _STATUS_KINDS:
        return _STATUS_KINDS[status]
    if status == 429 or status >= 500:
        # A read may try again; a mutating call that was answered 429/5xx
        # has an unproven outcome and is handled as a transport failure
        # whose recovery is reconciliation.
        return KIND_TRANSPORT_FAILURE
    return KIND_VENUE_REJECTION


def _default_recovery(kind: str, operation: str) -> tuple[str, Optional[RetryBound]]:
    if kind == KIND_TRANSPORT_FAILURE:
        if operation == OPERATION_MUTATING:
            return RECOVERY_RECONCILE_UNKNOWN_FILL, None
        return RECOVERY_RETRY_WITH_BOUND, POLL_RETRY_BOUND
    if kind == KIND_DUPLICATE_CLIENT_ORDER_ID:
        return RECOVERY_RESUME_FROM_PERSISTED_CLIENT_ORDER_ID, None
    if kind == KIND_STALE_QUOTE:
        return RECOVERY_RETRY_WITH_BOUND, QUOTE_RETRY_BOUND
    return RECOVERY_REFUSE_TO_PROCEED, None


def _unknown_state(kind: str, recovery: str) -> bool:
    """An outcome is unknown exactly when the venue never told us what it
    did. A rejection PROVES nothing was placed; an interrupted mutating
    call proves nothing at all."""
    return recovery == RECOVERY_RECONCILE_UNKNOWN_FILL or (
        kind == KIND_DUPLICATE_CLIENT_ORDER_ID
    )


def build_refusal(
    *,
    kind: str,
    venue: str,
    venue_reason: str,
    operation: str = OPERATION_READ,
    venue_status: Optional[int] = None,
    venue_code: Optional[Any] = None,
    transient_cause_type: Optional[str] = None,
    client_order_id: Optional[str] = None,
    detail: Optional[Mapping[str, Any]] = None,
) -> BrokerRefusal:
    """Build a refusal with this taxonomy's declared recovery for its kind."""
    recovery, bound = _default_recovery(kind, operation)
    unknown = _unknown_state(kind, recovery)
    blocks = unknown or (
        operation == OPERATION_MUTATING
        and recovery == RECOVERY_REFUSE_TO_PROCEED
    )
    return BrokerRefusal(
        kind=kind,
        venue=venue,
        venue_reason=venue_reason,
        recovery=recovery,
        operation=operation,
        retry_bound=bound,
        venue_status=venue_status,
        venue_code=venue_code,
        transient_cause_type=transient_cause_type,
        client_order_id=client_order_id,
        state_is_unknown=unknown,
        blocks_new_orders=blocks,
        detail=dict(detail or {}),
    )


# ------------------------------------------------------------ entry points
def classify_http_refusal(
    *,
    venue: str,
    status: int,
    venue_reason: str,
    venue_code: Optional[Any] = None,
    operation: str = OPERATION_READ,
    client_order_id: Optional[str] = None,
) -> BrokerRefusal:
    """Classify an HTTP answer the venue actually returned.

    The venue's machine code wins over the status when this repository has
    a declared mapping for it; otherwise the status governs. Neither path
    looks at ``venue_reason``.
    """
    table = VENUE_CODE_KINDS.get(venue, {})
    kind = table.get(venue_code) if venue_code is not None else None
    if kind is None:
        kind = _kind_for_status(int(status))
    return build_refusal(
        kind=kind, venue=venue, venue_reason=venue_reason,
        operation=operation, venue_status=int(status), venue_code=venue_code,
        client_order_id=client_order_id,
    )


def classify_exception(
    error: BaseException,
    *,
    venue: str,
    operation: str = OPERATION_READ,
    client_order_id: Optional[str] = None,
) -> BrokerRefusal:
    """Classify any exception an adapter raised. Never returns ``None``.

    Order of authority, all structural:

    1. the explicit ``raise ... from`` chain (reused from
       ``runner_retry_taxonomy``) — a transport failure;
    2. the venue's structured facts attached by the adapter;
    3. neither — which on a mutating call means the outcome is unproven.
    """
    cause = transient_cause(error)
    if cause is not None:
        return build_refusal(
            kind=KIND_TRANSPORT_FAILURE, venue=venue,
            venue_reason=_verbatim(cause), operation=operation,
            transient_cause_type=type(cause).__name__,
            client_order_id=client_order_id,
        )
    facts = venue_facts(error)
    if facts["venue_status"] is not None or facts["venue_code"] is not None:
        return classify_http_refusal(
            venue=venue,
            status=int(facts["venue_status"] or 0),
            venue_reason=facts["venue_reason"] or _verbatim(error),
            venue_code=facts["venue_code"],
            operation=operation,
            client_order_id=client_order_id,
        )
    if operation == OPERATION_READ:
        return build_refusal(
            kind=KIND_VENUE_REJECTION, venue=venue,
            venue_reason=_verbatim(error), operation=operation,
            client_order_id=client_order_id,
        )
    # A mutating call that failed for a reason the venue never explained
    # left an unproven effect behind: reconcile, never assume flat.
    return BrokerRefusal(
        kind=KIND_VENUE_REJECTION, venue=venue,
        venue_reason=_verbatim(error), recovery=RECOVERY_RECONCILE_UNKNOWN_FILL,
        operation=operation, client_order_id=client_order_id,
        state_is_unknown=True, blocks_new_orders=True,
    )


def classify_ibkr_error(
    *,
    error_code: int,
    error_string: str,
    venue: str = "ibkr_paper",
    operation: str = OPERATION_READ,
) -> BrokerRefusal:
    """Classify one TWS ``errorEvent`` by its documented numeric code."""
    kind = IBKR_ERROR_KINDS.get(int(error_code), KIND_VENUE_REJECTION)
    return build_refusal(
        kind=kind, venue=venue, venue_reason=error_string,
        operation=operation, venue_code=int(error_code),
    )


def duplicate_client_order_id_refusal(
    *,
    venue: str,
    client_order_id: str,
    venue_reason: str,
    venue_status: Optional[int] = None,
    venue_code: Optional[Any] = None,
) -> BrokerRefusal:
    """The id is already live at the venue, or already in our journal.

    The declared recovery is to resume from the persisted id and reconcile
    it. Placing a second order is not one of the options this taxonomy can
    express.
    """
    return build_refusal(
        kind=KIND_DUPLICATE_CLIENT_ORDER_ID, venue=venue,
        venue_reason=venue_reason, operation=OPERATION_MUTATING,
        venue_status=venue_status, venue_code=venue_code,
        client_order_id=client_order_id,
    )


def market_closed_refusal(
    *, venue: str, clock_fact: Mapping[str, Any]
) -> BrokerRefusal:
    """Built from the venue's own clock payload, not from any message.

    A clock without an ``is_open`` boolean proves nothing, so it is refused
    as a venue rejection: an absent fact is never read as "open".
    """
    is_open = clock_fact.get("is_open")
    reason = _canonical(clock_fact)
    if not isinstance(is_open, bool):
        return build_refusal(
            kind=KIND_VENUE_REJECTION, venue=venue, venue_reason=reason,
            operation=OPERATION_MUTATING,
            detail={"clock": "is_open absent or not a boolean"},
        )
    if is_open:
        raise ValueError("market_closed_refusal called on an open market")
    return build_refusal(
        kind=KIND_MARKET_CLOSED, venue=venue, venue_reason=reason,
        operation=OPERATION_MUTATING,
    )


#: The quote reason codes ``ibkr_l1_outbox`` already emits. They are this
#: repository's own namespaced tokens, not venue prose.
QUOTE_REFUSAL_PREFIXES = ("quote_missing", "quote_invalid", "quote_stale",
                          "quote_wide")


def stale_quote_refusal(
    *, venue: str, reason_code: str, operation: str = OPERATION_READ
) -> BrokerRefusal:
    """A deferral: the decision stays pending and the next tick re-reads."""
    if operation == OPERATION_MUTATING:
        return BrokerRefusal(
            kind=KIND_STALE_QUOTE, venue=venue, venue_reason=reason_code,
            recovery=RECOVERY_REFUSE_TO_PROCEED, operation=OPERATION_MUTATING,
        )
    return build_refusal(
        kind=KIND_STALE_QUOTE, venue=venue, venue_reason=reason_code,
        operation=operation,
    )


def _verbatim(error: BaseException) -> str:
    """The exception's own words, unedited, with its type so an empty
    message still names something."""
    text = str(error)
    return text if text else type(error).__name__


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      default=str)
