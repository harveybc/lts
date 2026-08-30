"""WP3.4 — calendar-aware watchdog classification.

Six situations that a single "stale" alert used to blur together, kept
apart on purpose:

* ``expected_market_closed`` — the venue calendar says the market is
  shut, so a bar that has not advanced is the expected state;
* ``stale_feed_during_open_window`` — the market is open and the feed
  has stopped, which is an incident;
* ``terminal_or_account_disconnected`` — the venue session itself is
  gone;
* ``flatten_failed`` — a close was requested and direct evidence never
  showed zero positions and zero orders;
* ``unexpected_exposure_during_closure`` — exposure exists while the
  market is closed;
* ``recovery_active`` — a durable obligation from an earlier run has
  not been discharged.

**An expected closure suppresses the STALE-BAR alert and nothing else.**
Terminal, account, protection, order and exposure incidents are all
independent of the calendar: a market being shut is not a reason to
stop noticing that the terminal is disconnected or that exposure is
open. That precedence is enforced here, not left to the caller.

The severity and category vocabulary is the one the existing paper
execution watchdog already uses, so these events join the same ledger
rather than starting a second one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from app.venue_direct_evidence import (
    VenueEvidenceError, require_bool, require_enum, require_real,
    require_text, require_venue_direct)

SEVERITIES = ("critical", "warning", "info")
CATEGORIES = ("operations", "reconciliation", "broker", "portfolio",
              "market_data", "research")

CLASSIFICATIONS = (
    "healthy",
    "expected_market_closed",
    "stale_feed_during_open_window",
    "terminal_or_account_disconnected",
    "flatten_failed",
    "unexpected_exposure_during_closure",
    "recovery_active",
)

# Incidents the calendar may NEVER suppress.
CALENDAR_SUPPRESSES_ONLY = ("stale_feed_during_open_window",)

_EVENT = {
    "expected_market_closed": (
        "session_expected_market_closed", "info", "market_data",
        "the venue calendar declares this interval closed; a bar that "
        "has not advanced is the expected state"),
    "stale_feed_during_open_window": (
        "session_stale_feed_open_window", "critical", "market_data",
        "the market is open and the feed has stopped advancing"),
    "terminal_or_account_disconnected": (
        "session_terminal_disconnected", "critical", "broker",
        "the venue session is not connected or trading is not "
        "permitted"),
    "flatten_failed": (
        "session_flatten_failed", "critical", "reconciliation",
        "a close was requested and direct evidence never showed zero "
        "positions and zero orders"),
    "unexpected_exposure_during_closure": (
        "session_unexpected_exposure_during_closure", "critical",
        "reconciliation",
        "exposure is open while the venue calendar declares the "
        "market closed"),
    "recovery_active": (
        "session_recovery_active", "warning", "operations",
        "a durable flatten obligation from an earlier run has not "
        "been discharged"),
}


@dataclass(frozen=True)
class WatchdogVerdict:
    classification: str
    event_key: str
    severity: str
    category: str
    detail: str
    suppressed: tuple
    session_state: str

    def as_event(self, *, venue: str, symbol: str) -> dict:
        return {
            "key": self.event_key,
            "title": f"{venue} {symbol}: {self.classification}",
            "detail": self.detail,
            "severity": self.severity,
            "category": self.category,
            "discussion": False,
            "session_state": self.session_state,
            "suppressed": list(self.suppressed),
        }


def classify(*, session_state: str,
             session_connected: Any,
             trading_enabled: Any,
             bar_age_seconds: Any,
             max_bar_age_seconds: Any,
             positions_total: Any,
             orders_total: Any,
             flatten_requested: Any,
             flatten_confirmed: Any,
             recovery_active: Any,
             provenance: Mapping[str, Any]) -> WatchdogVerdict:
    """One verdict, from DIRECT venue facts only.

    Every input is validated: an unavailable fact refuses rather than
    reading as healthy, because "we could not tell" and "nothing is
    wrong" are different statements."""
    require_venue_direct(provenance)
    state = require_enum("session_state", session_state,
                         ("NORMAL_TRADING", "WIND_DOWN",
                          "FORCED_FLATTEN", "EXPECTED_MARKET_CLOSED",
                          "REOPEN_BLACKOUT", "RECOVERY"))
    connected = require_bool("session_connected", session_connected)
    trading = require_bool("trading_enabled", trading_enabled)
    bar_age = require_real("bar_age_seconds", bar_age_seconds,
                           nonnegative=True)
    max_age = require_real("max_bar_age_seconds", max_bar_age_seconds,
                           positive=True)
    positions = require_real("positions_total", positions_total,
                             nonnegative=True)
    orders = require_real("orders_total", orders_total,
                          nonnegative=True)
    requested = require_bool("flatten_requested", flatten_requested)
    confirmed = require_bool("flatten_confirmed", flatten_confirmed)
    recovering = require_bool("recovery_active", recovery_active)

    market_closed = state == "EXPECTED_MARKET_CLOSED"
    suppressed = (CALENDAR_SUPPRESSES_ONLY if market_closed else ())

    # Precedence. The calendar appears only in the LAST branch, and
    # only against the stale-bar alert.
    if not connected or not trading:
        return _verdict("terminal_or_account_disconnected", state,
                        suppressed,
                        f"connected={connected} trading={trading}")
    if requested and not confirmed:
        return _verdict("flatten_failed", state, suppressed,
                        f"positions={positions:g} orders={orders:g} "
                        "and no direct zero/zero confirmation")
    if market_closed and (positions > 0 or orders > 0):
        return _verdict("unexpected_exposure_during_closure", state,
                        suppressed,
                        f"positions={positions:g} orders={orders:g} "
                        "while the market is closed")
    if recovering:
        return _verdict("recovery_active", state, suppressed,
                        "an outstanding obligation blocks new risk")
    if bar_age > max_age:
        if market_closed:
            return _verdict("expected_market_closed", state,
                            suppressed,
                            f"bar age {bar_age:g}s exceeds "
                            f"{max_age:g}s, which is expected while "
                            "the market is closed")
        return _verdict("stale_feed_during_open_window", state,
                        suppressed,
                        f"bar age {bar_age:g}s exceeds {max_age:g}s "
                        "during an OPEN window")
    if market_closed:
        return _verdict("expected_market_closed", state, suppressed,
                        "market closed and nothing else is wrong")
    return WatchdogVerdict(classification="healthy",
                           event_key="session_healthy",
                           severity="info", category="operations",
                           detail="no incident",
                           suppressed=suppressed,
                           session_state=state)


def _verdict(classification: str, state: str, suppressed: tuple,
             detail: str) -> WatchdogVerdict:
    key, severity, category, description = _EVENT[classification]
    return WatchdogVerdict(classification=classification,
                           event_key=key, severity=severity,
                           category=category,
                           detail=f"{description}: {detail}",
                           suppressed=tuple(suppressed),
                           session_state=state)
