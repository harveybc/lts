"""Typed refusal and DECLARED recovery for the MT5 Demo lane.

RP149 (MT5 demo and Alpaca paper). RP157 lane 3 built ``app/broker_refusal.py``
for the Alpaca, OANDA, Capital and IBKR adapters and said so explicitly:
MT5 was NOT covered, because ``mt5_execution_bridge`` and ``mt5_bridge_lab``
refuse INBOUND EA traffic by HTTP status and are not outbound adapters. This
module is the missing half, and it reuses that taxonomy's kinds, recoveries and
read-versus-mutating axis rather than inventing a second vocabulary.

Offline by construction: it imports nothing that can open a socket, reads no
credential, contacts no terminal and places no order. Every verdict is derived
from a machine code and an operation, never from prose. The EA's or the
terminal's own words are CARRIED verbatim and never interpreted.

The gap it closes
-----------------
The MT5 Demo lane already transports a result. ``ExecutionResultPayload``
(``app/mt5_execution_bridge.py:167``) carries ``success: bool``,
``result_code: int`` and the EA's ``message``, and the EA fills ``result_code``
with the terminal's ``TRADE_RETCODE_*``. But ``Mt5ExecutionStore.complete``
reads only ``success``::

    state = "succeeded" if payload.success else "failed"

``result_code`` is serialised into ``result_json`` and never read by anything.
So today:

* a market that was closed (10018), an account not entitled to trade (10017),
  an invalid volume (10014) and a request that TIMED OUT (10012) all become the
  same ``failed`` row, and nothing downstream can tell them apart;
* ``failed`` is treated as "nothing happened" — the daily entry budget in
  ``enqueue`` counts ``state != 'failed'``, so a timed-out command frees a slot
  in the budget. A timeout is precisely the case where the terminal never told
  us what it did: the position may be open. Reading it as flat is the one error
  that can double a position;
* an entitlement refusal is indistinguishable from a transient one, so a runner
  that retries transients retries an entitlement failure for ever.

This module does not change that service. It provides the classification the
service is missing, as a pure function of the facts the payload already carries,
so the integration is a single call at the one place that decides the state.
``tests/unit/test_mt5_refusal_recovery.py`` pins BOTH: what the classifier says,
and what the bridge currently does instead.

Provenance of the code tables
-----------------------------
``MT5_RETCODE_KINDS`` and ``MT5_TERMINAL_ERROR_KINDS`` are transcribed from
MetaTrader 5 vendor documentation and are **not verified against a live
terminal by this lane** — the order forbids contacting one. They are handled
exactly as ``broker_refusal.OANDA_REJECT_KINDS`` is: machine enum values, so
keying on them is not message sniffing, and an unrecognised value falls through
to a fail-closed venue rejection rather than to a guess.

What this lane could NOT close, and why
--------------------------------------
RP149 also names canaries, and they need a confirmed MT5 Demo account. None of
the following is closable without one, and none of it is blocked on code:

* which retcodes this broker's server actually returns, and with what comment
  text. The tables above are the vendor's documented space, not an observation;
* whether 10011, 10012, 10028 and 10031 do leave an effect behind on THIS
  server, which is the difference between a conservative classification and a
  measured one;
* the entitlement codes: whether autotrading is enabled for the account and the
  symbol, which only the account can answer;
* an end-to-end canary — one order acknowledged and one fill reconciled — which
  is the only thing that turns this lane from a contract into evidence.

So the lane's state is: the refusal and recovery contract exists and is tested
offline; no broker call has been made, no acknowledgement and no fill exists, and
the record must keep saying so until a confirmed account and a risk mandate
arrive.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from app.broker_refusal import (
    KIND_AUTHENTICATION_FAILURE,
    KIND_DUPLICATE_CLIENT_ORDER_ID,
    KIND_INSTRUMENT_NOT_PERMITTED,
    KIND_INSUFFICIENT_ENTITLEMENT,
    KIND_MARKET_CLOSED,
    KIND_STALE_QUOTE,
    KIND_TRANSPORT_FAILURE,
    KIND_VENUE_REJECTION,
    OPERATION_MUTATING,
    OPERATION_READ,
    RECOVERY_REFUSE_TO_PROCEED,
    BrokerRefusal,
    build_refusal,
)

#: The venue name every refusal from this lane carries. There is deliberately
#: no live counterpart: this lane is Demo-only, as ``Mt5ExecutionConfig.load``
#: already enforces by refusing any config that is not ``environment=demo``.
VENUE = "mt5_demo"

# ------------------------------------------------------- TRADE_RETCODE_*
#: Codes that report a SUCCESS. Calling the classifier on one is a programming
#: error, not a refusal to be typed — the same rule that keeps IBKR's 1101/1102
#: restorations out of ``IBKR_ERROR_KINDS`` and makes ``market_closed_refusal``
#: raise on an open market.
MT5_SUCCESS_RETCODES = frozenset({
    10008,   # TRADE_RETCODE_PLACED
    10009,   # TRADE_RETCODE_DONE
    10010,   # TRADE_RETCODE_DONE_PARTIAL
})

#: Codes after which the terminal never told us what it did. On a MUTATING call
#: the effect is unproven and the recovery is reconciliation; on a READ the same
#: code is a blip and may be retried. The read/mutating axis does that work, so
#: these are all ``KIND_TRANSPORT_FAILURE`` — the set is named here because
#: "which codes leave an unknown position behind" is the question this lane
#: exists to answer, and a reader should not have to derive it from a table.
MT5_UNPROVEN_ON_A_MUTATING_CALL = frozenset({
    10011,   # TRADE_RETCODE_ERROR: request processing error
    10012,   # TRADE_RETCODE_TIMEOUT: canceled by timeout — or not canceled
    10024,   # TRADE_RETCODE_TOO_MANY_REQUESTS
    10028,   # TRADE_RETCODE_LOCKED: still being processed by the server
    10031,   # TRADE_RETCODE_CONNECTION: no connection with the trade server
})

#: Codes that prove the account may not do this at all. NONE of them may ever
#: be retried as transient, and 10032 in particular must never be "fixed" by
#: pointing this lane at a live account.
MT5_ENTITLEMENT_RETCODES = frozenset({
    10017,   # TRADE_RETCODE_TRADE_DISABLED
    10026,   # TRADE_RETCODE_SERVER_DISABLES_AT: autotrading disabled by server
    10027,   # TRADE_RETCODE_CLIENT_DISABLES_AT: autotrading disabled by client
    10032,   # TRADE_RETCODE_ONLY_REAL: operation allowed only for live accounts
})

MT5_RETCODE_KINDS: dict[int, str] = {
    10004: KIND_STALE_QUOTE,                 # REQUOTE
    10006: KIND_VENUE_REJECTION,             # REJECT
    10007: KIND_VENUE_REJECTION,             # CANCEL
    10011: KIND_TRANSPORT_FAILURE,           # ERROR — outcome unproven
    10012: KIND_TRANSPORT_FAILURE,           # TIMEOUT — outcome unproven
    10013: KIND_VENUE_REJECTION,             # INVALID
    10014: KIND_VENUE_REJECTION,             # INVALID_VOLUME
    10015: KIND_VENUE_REJECTION,             # INVALID_PRICE
    10016: KIND_VENUE_REJECTION,             # INVALID_STOPS
    10017: KIND_INSUFFICIENT_ENTITLEMENT,    # TRADE_DISABLED
    10018: KIND_MARKET_CLOSED,               # MARKET_CLOSED
    10019: KIND_VENUE_REJECTION,             # NO_MONEY
    10020: KIND_STALE_QUOTE,                 # PRICE_CHANGED
    10021: KIND_STALE_QUOTE,                 # PRICE_OFF: no quotes to process
    10022: KIND_VENUE_REJECTION,             # INVALID_EXPIRATION
    10023: KIND_VENUE_REJECTION,             # ORDER_CHANGED
    10024: KIND_TRANSPORT_FAILURE,           # TOO_MANY_REQUESTS
    10025: KIND_VENUE_REJECTION,             # NO_CHANGES
    10026: KIND_INSUFFICIENT_ENTITLEMENT,    # SERVER_DISABLES_AT
    10027: KIND_INSUFFICIENT_ENTITLEMENT,    # CLIENT_DISABLES_AT
    10028: KIND_TRANSPORT_FAILURE,           # LOCKED — still being processed
    10029: KIND_VENUE_REJECTION,             # FROZEN
    10030: KIND_VENUE_REJECTION,             # INVALID_FILL
    10031: KIND_TRANSPORT_FAILURE,           # CONNECTION
    10032: KIND_INSUFFICIENT_ENTITLEMENT,    # ONLY_REAL
    10033: KIND_VENUE_REJECTION,             # LIMIT_ORDERS
    10034: KIND_VENUE_REJECTION,             # LIMIT_VOLUME
    10035: KIND_VENUE_REJECTION,             # INVALID_ORDER
    10036: KIND_VENUE_REJECTION,             # POSITION_CLOSED
    10038: KIND_VENUE_REJECTION,             # INVALID_CLOSE_VOLUME
    # CLOSE_ORDER_EXIST: MT5 has no client order id, so a second close is
    # identified by the close order the position ALREADY carries. The recovery
    # is the same one a duplicate id declares — resume from what exists and
    # reconcile it — and placing a second close is not one of the options this
    # taxonomy can express.
    10039: KIND_DUPLICATE_CLIENT_ORDER_ID,   # CLOSE_ORDER_EXIST
    10040: KIND_VENUE_REJECTION,             # LIMIT_POSITIONS
    10041: KIND_VENUE_REJECTION,             # REJECT_CANCEL
    10042: KIND_INSTRUMENT_NOT_PERMITTED,    # LONG_ONLY
    10043: KIND_INSTRUMENT_NOT_PERMITTED,    # SHORT_ONLY
    10044: KIND_INSTRUMENT_NOT_PERMITTED,    # CLOSE_ONLY
    10045: KIND_INSTRUMENT_NOT_PERMITTED,    # FIFO_CLOSE
    10046: KIND_INSTRUMENT_NOT_PERMITTED,    # HEDGE_PROHIBITED
}

# ------------------------------------------- the python integration's errors
#: ``MetaTrader5.last_error()`` codes, for the case where ``order_send`` returns
#: nothing at all and the only fact available is this pair.
MT5_TERMINAL_ERROR_KINDS: dict[int, str] = {
    -1: KIND_TRANSPORT_FAILURE,              # RES_E_FAIL: generic failure
    -2: KIND_VENUE_REJECTION,                # RES_E_INVALID_PARAMS
    -3: KIND_VENUE_REJECTION,                # RES_E_NO_MEMORY
    -4: KIND_VENUE_REJECTION,                # RES_E_NOT_FOUND
    -5: KIND_VENUE_REJECTION,                # RES_E_INVALID_VERSION
    -6: KIND_AUTHENTICATION_FAILURE,         # RES_E_AUTH_FAILED
    -7: KIND_VENUE_REJECTION,                # RES_E_UNSUPPORTED
    -8: KIND_INSUFFICIENT_ENTITLEMENT,       # RES_E_AUTO_TRADING_DISABLED
    -10000: KIND_TRANSPORT_FAILURE,          # RES_E_INTERNAL_FAIL
    -10001: KIND_TRANSPORT_FAILURE,          # RES_E_INTERNAL_FAIL_SEND
    -10002: KIND_TRANSPORT_FAILURE,          # RES_E_INTERNAL_FAIL_RECEIVE
    -10003: KIND_TRANSPORT_FAILURE,          # RES_E_INTERNAL_FAIL_INIT
    -10004: KIND_TRANSPORT_FAILURE,          # RES_E_INTERNAL_FAIL_CONNECT
    -10005: KIND_TRANSPORT_FAILURE,          # RES_E_INTERNAL_FAIL_TIMEOUT
}

#: ``RES_S_OK``. Classifying it is a programming error, as with the success
#: retcodes above.
MT5_TERMINAL_OK = 1

#: What a refusal says when the terminal reported no code at all. It is never
#: read as "rejected", because a rejection PROVES nothing was placed and a
#: silent failure proves nothing whatsoever.
NO_CODE_REPORTED = (
    "the MT5 result reported no machine code, so what the terminal did is "
    "unknown; it is not read as a rejection, because a rejection proves "
    "nothing was placed and this proves nothing at all"
)


class Mt5SuccessIsNotARefusal(ValueError):
    """Raised when a success code reaches a refusal classifier."""


class Mt5ContradictoryResult(ValueError):
    """Raised when a result's ``success`` flag and its code disagree.

    A payload claiming failure while carrying ``TRADE_RETCODE_DONE`` — or the
    mirror — is not resolved in either direction here. Choosing one would be a
    guess about whether a position exists, and that guess is invisible in
    everything downstream of it.
    """


def _verbatim(text: Any, fallback: str) -> str:
    """The venue's own words, unedited. An absent message still names something,
    because ``BrokerRefusal`` refuses to exist without a reason."""
    if isinstance(text, str) and text:
        return text
    return fallback


def classify_mt5_retcode(
    *,
    retcode: int,
    venue_message: Any = None,
    operation: str = OPERATION_READ,
    client_order_id: Optional[str] = None,
    detail: Optional[Mapping[str, Any]] = None,
) -> BrokerRefusal:
    """Type one ``TRADE_RETCODE_*`` the terminal returned.

    ``venue_message`` is the EA's or terminal's own comment and is carried
    verbatim into ``venue_reason``. It is never read: the kind comes from the
    integer alone, and ``MT5_RETCODE_KINDS`` has no entry keyed on prose.
    """
    code = int(retcode)
    if code in MT5_SUCCESS_RETCODES:
        raise Mt5SuccessIsNotARefusal(
            f"TRADE_RETCODE {code} reports a placed or completed order; it is "
            f"not a refusal and must not be typed as one"
        )
    kind = MT5_RETCODE_KINDS.get(code, KIND_VENUE_REJECTION)
    reason = _verbatim(venue_message, f"TRADE_RETCODE {code}")
    if kind == KIND_STALE_QUOTE and operation == OPERATION_MUTATING:
        # A requote, a changed price or no quotes at all PROVES the order was
        # not executed at the price asked for, so nothing was placed and there
        # is nothing to reconcile -- but a submission is never "retried" either,
        # because the decision has to be re-made against the new price. This is
        # the same split ``broker_refusal.stale_quote_refusal`` makes; it is
        # written out here because that helper carries no venue code.
        return BrokerRefusal(
            kind=KIND_STALE_QUOTE, venue=VENUE, venue_reason=reason,
            recovery=RECOVERY_REFUSE_TO_PROCEED, operation=OPERATION_MUTATING,
            venue_code=code, client_order_id=client_order_id,
            blocks_new_orders=True, detail=dict(detail or {}),
        )
    return build_refusal(
        kind=kind,
        venue=VENUE,
        venue_reason=reason,
        operation=operation,
        venue_code=code,
        client_order_id=client_order_id,
        detail=dict(detail or {}),
    )


def classify_mt5_terminal_error(
    *,
    error_code: int,
    error_text: Any = None,
    operation: str = OPERATION_READ,
    client_order_id: Optional[str] = None,
) -> BrokerRefusal:
    """Type a ``last_error()`` pair, for the case where ``order_send`` returned
    nothing and there is no retcode to read.

    On a mutating call this is the worst case the lane has: the request may have
    reached the trade server and the answer may have been lost. The declared
    recovery is reconciliation, and it blocks new orders until it happens.
    """
    code = int(error_code)
    if code == MT5_TERMINAL_OK:
        raise Mt5SuccessIsNotARefusal(
            "RES_S_OK reports success; it is not a refusal"
        )
    kind = MT5_TERMINAL_ERROR_KINDS.get(code, KIND_VENUE_REJECTION)
    return build_refusal(
        kind=kind,
        venue=VENUE,
        venue_reason=_verbatim(error_text, f"MT5 last_error {code}"),
        operation=operation,
        venue_code=code,
        client_order_id=client_order_id,
        detail={"source": "MetaTrader5.last_error"},
    )


def classify_order_send_returned_nothing(
    *,
    error_code: Optional[int] = None,
    error_text: Any = None,
    client_order_id: Optional[str] = None,
) -> BrokerRefusal:
    """``order_send`` returned ``None``. Always a mutating call, always unknown.

    With a ``last_error`` pair the kind comes from the table; without one there
    is no fact at all, and the refusal says so rather than naming a cause.
    """
    if error_code is not None:
        return classify_mt5_terminal_error(
            error_code=error_code, error_text=error_text,
            operation=OPERATION_MUTATING, client_order_id=client_order_id,
        )
    return build_refusal(
        kind=KIND_TRANSPORT_FAILURE,
        venue=VENUE,
        venue_reason=NO_CODE_REPORTED,
        operation=OPERATION_MUTATING,
        client_order_id=client_order_id,
        detail={"source": "MetaTrader5.order_send returned None"},
    )


def refusal_for_execution_result(
    result: Mapping[str, Any],
    *,
    operation: str = OPERATION_MUTATING,
    client_order_id: Optional[str] = None,
) -> BrokerRefusal:
    """Type one ``ExecutionResultPayload``-shaped mapping from the bridge.

    This is the call ``Mt5ExecutionStore.complete`` does not yet make. It reads
    ``success`` and ``result_code`` and nothing else — never ``message``.

    * a successful result is not a refusal and raises;
    * a failed result whose code reports success is a CONTRADICTION and raises,
      because deciding which half to believe decides whether a position exists;
    * a failed result with no code at all is UNKNOWN, never a rejection.
    """
    success = result.get("success")
    if not isinstance(success, bool):
        raise Mt5ContradictoryResult(
            "an MT5 execution result must state success as a boolean; a missing "
            "flag is not read as either outcome"
        )
    raw = result.get("result_code")
    code = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else None
    if success:
        raise Mt5SuccessIsNotARefusal(
            "this MT5 execution result reports success; it is not a refusal"
        )
    if code is not None and code in MT5_SUCCESS_RETCODES:
        raise Mt5ContradictoryResult(
            f"the result says success=False and carries TRADE_RETCODE {code}, "
            f"which reports a placed or completed order; the contradiction is "
            f"reported rather than resolved, because resolving it would decide "
            f"whether a position exists"
        )
    if code is None or code == 0:
        return build_refusal(
            kind=KIND_TRANSPORT_FAILURE,
            venue=VENUE,
            venue_reason=_verbatim(result.get("message"), NO_CODE_REPORTED),
            operation=operation,
            client_order_id=client_order_id,
            detail={"source": "lts.mt5.execution_result.v1",
                    "result_code": "absent or zero"},
        )
    return classify_mt5_retcode(
        retcode=code, venue_message=result.get("message"),
        operation=operation, client_order_id=client_order_id,
        detail={"source": "lts.mt5.execution_result.v1"},
    )


def command_state_for(refusal: BrokerRefusal) -> str:
    """The durable state a typed refusal maps to, which is THREE states and not
    two.

    ``Mt5ExecutionStore.complete`` writes ``succeeded`` or ``failed``. A
    refusal whose outcome is unknown is neither: recording it as ``failed``
    asserts nothing was placed, and the daily budget in ``enqueue`` then frees
    the slot it occupied. This function names the third state the lane needs;
    introducing it into the schema is a migration of a live service and is
    deliberately NOT done here.
    """
    if refusal.state_is_unknown:
        return "effect_unknown"
    return "failed"
