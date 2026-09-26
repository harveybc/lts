"""Venue breaks: a scheduling defect, separate from the store defect.

Correction of 2026-09-26. The forensics of the five MT5 `failed` commands found
a pattern that the store fix cannot reach and that is not bad luck:

    zero of 47 commands created outside 21:00-22:59 UTC ever failed, and five
    of the six created inside it did. Four of those five carry
    `TRADE_RETCODE_MARKET_CLOSED` at the venue's daily rollover break, into
    which the runner's four-hour bar boundary lands deterministically, every
    day, for as long as the lane runs.

Sending an order into a market the venue has already told us is closed is a
**scheduling** error. This module is the scheduling repair and nothing more: it
decides *whether this is a moment at which a command may be issued at all*. It
never decides what the command would be, never changes a size, a stop, a target
or a direction, and never touches the store's outcome vocabulary.

Two rules govern it.

1. **A break is declared or derived, never guessed.** Either the operator
   declares it in configuration, or it is derived from the venue's own refusals
   in the retained record. There is no default window and no hardcoded hour: a
   lane with neither source gets no break and behaves exactly as before, with
   the absence recorded by name.
2. **A derived break must be falsifiable.** The derivation reads only the
   venue's structured machine codes (the committed `market_closed` retcode
   kinds, never a message), requires the refusal to have been observed on
   several distinct UTC dates, refuses if any command SUCCEEDED inside the
   window it would declare — which would prove the window is not a break — and
   refuses unless the record itself shows a minute at which the venue was
   observed accepting again. A record that cannot show when the venue reopened
   does not get to invent one.

When a boundary falls inside a break the command is **deferred by name**, with
the break, its source, the rows it rests on and the moment trading resumes all
carried in the deferral. Deferring is not a retry and not a refusal of the
decision: the closed bar that produced it is still the closed bar after the
break, so the same decision is issued into an open market instead of into a
closed one.

Offline by construction: nothing imported here can open a socket, read a
credential, contact a terminal or place an order.
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from app.broker_refusal import KIND_MARKET_CLOSED
from app.mt5_refusal import MT5_RETCODE_KINDS

#: Schema of one declared or derived break.
VENUE_BREAK_SCHEMA = "lts.mt5.venue_break.v1"

# ------------------------------------------------------------- the sources
#: The operator declared it in the runner configuration.
SOURCE_CONFIGURED = "configuration"
#: It was derived from the venue's own refusals in the retained record.
SOURCE_OBSERVED_RECORD = "observed_record"
SOURCES = frozenset({SOURCE_CONFIGURED, SOURCE_OBSERVED_RECORD})

#: What a deferred command is deferred BY. A deferral is named, so a reader of
#: the runner's heartbeat never has to infer why nothing was issued.
REFUSAL_VENUE_BREAK = "venue_break"
#: The runner state a deferral reports.
STATE_VENUE_BREAK_DEFERRED = "venue_break_deferred"
#: The runner state when neither source establishes a break. Recorded rather
#: than left silent: "no break is known" is a fact about our evidence.
VENUE_BREAK_UNDECLARED = "venue_break_undeclared"

MINUTES_PER_DAY = 24 * 60
#: A break longer than this is refused as a misdeclaration. A daily rollover
#: break is minutes long; a two-hour declaration would blind the lane for a
#: twelfth of every day, and that is a decision for the owner, not a config
#: typo this module silently honours.
MAX_BREAK_MINUTES = 120
#: How many distinct UTC dates the venue must have been observed refusing on
#: before a break is derived from the record. One refusal is an incident; a
#: recurring daily break shows up on several days.
MINIMUM_OBSERVED_DATES = 2

#: The retcodes that mean "the venue is closed", DERIVED from the committed
#: retcode-to-kind table rather than transcribed again here. If RP149's table
#: is corrected, this set follows it.
MARKET_CLOSED_RETCODES = frozenset(
    code for code, kind in MT5_RETCODE_KINDS.items()
    if kind == KIND_MARKET_CLOSED
)

#: ``HH:MM`` on a 24-hour UTC clock, and nothing else.
_CLOCK = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")

#: The configuration key a declared break list lives under.
CONFIG_KEY = "venue_breaks"


class VenueBreakMisdeclared(ValueError):
    """A configured break is not readable as a break.

    Raised, never defaulted around: a lane whose operator meant to declare a
    break and mistyped it must fail loudly rather than run with no break.
    """


class VenueBreakUndeclarable(ValueError):
    """The retained record does not establish a break.

    This is a normal outcome, not a fault. It means the evidence is not there
    yet, and the caller's correct response is to carry no break and say so —
    never to fall back on a guessed window.
    """


def minute_of_day(moment: datetime) -> int:
    """Minutes since UTC midnight for an aware instant.

    A naive datetime is refused. A break is a statement about the venue's UTC
    clock, and a timestamp without a zone is not a time.
    """
    if not isinstance(moment, datetime):
        raise ValueError("a venue-break decision needs an instant")
    if moment.tzinfo is None:
        raise ValueError(
            "a naive timestamp cannot be placed on the venue's UTC clock"
        )
    utc = moment.astimezone(timezone.utc)
    return utc.hour * 60 + utc.minute


def _clock_minute(value: Any, key: str) -> int:
    if not isinstance(value, str):
        raise VenueBreakMisdeclared(
            f"{key} must be a UTC clock time written HH:MM"
        )
    found = _CLOCK.fullmatch(value)
    if found is None:
        raise VenueBreakMisdeclared(
            f"{key}={value!r} is not a UTC clock time written HH:MM"
        )
    return int(found.group(1)) * 60 + int(found.group(2))


def _clock_text(minute: int) -> str:
    return "%02d:%02d" % (minute // 60, minute % 60)


@dataclass(frozen=True)
class VenueBreak:
    """One daily window in which this lane does not issue commands.

    ``start_minute`` is inclusive and ``resume_minute`` exclusive, both counted
    from UTC midnight. A window that would wrap midnight is refused rather than
    silently split: a wrapping break is two breaks and the declarer says so.
    """

    start_minute: int
    resume_minute: int
    source: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("start_minute", "resume_minute"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise VenueBreakMisdeclared(f"{name} is a minute of the day")
        if not 0 <= self.start_minute < MINUTES_PER_DAY:
            raise VenueBreakMisdeclared("start_minute is outside the day")
        if not 0 < self.resume_minute <= MINUTES_PER_DAY:
            raise VenueBreakMisdeclared("resume_minute is outside the day")
        if self.resume_minute <= self.start_minute:
            raise VenueBreakMisdeclared(
                "a venue break resumes after it starts; a window that wraps "
                "midnight is declared as two breaks, never inferred as one"
            )
        if self.resume_minute - self.start_minute > MAX_BREAK_MINUTES:
            raise VenueBreakMisdeclared(
                f"a {self.resume_minute - self.start_minute}-minute break "
                f"exceeds the {MAX_BREAK_MINUTES}-minute ceiling; a window "
                f"that long is an owner decision, not a schedule detail"
            )
        if self.source not in SOURCES:
            raise VenueBreakMisdeclared(f"undeclared source: {self.source!r}")

    @property
    def duration_minutes(self) -> int:
        return self.resume_minute - self.start_minute

    def contains(self, moment: datetime) -> bool:
        """Is this instant inside the break?"""
        return self.start_minute <= minute_of_day(moment) < self.resume_minute

    def resumes_at(self, moment: datetime) -> datetime:
        """The first instant at or after ``moment`` outside this break."""
        utc = moment.astimezone(timezone.utc)
        midnight = utc.replace(hour=0, minute=0, second=0, microsecond=0)
        resume = midnight + timedelta(minutes=self.resume_minute)
        if resume <= utc:
            resume = resume + timedelta(days=1)
        return resume

    def as_fact(self) -> dict[str, Any]:
        return {
            "schema": VENUE_BREAK_SCHEMA,
            "start_utc": _clock_text(self.start_minute),
            "resume_utc": _clock_text(self.resume_minute % MINUTES_PER_DAY),
            "start_minute": self.start_minute,
            "resume_minute": self.resume_minute,
            "duration_minutes": self.duration_minutes,
            "source": self.source,
            "evidence": dict(self.evidence),
        }


def venue_breaks_from_config(
    config: Mapping[str, Any]
) -> tuple[VenueBreak, ...]:
    """Read the declared breaks out of a runner configuration.

    An absent key is not an error and not a default window: it means nothing was
    declared, and the caller must then either derive a break from the record or
    carry none. A key that is present and malformed IS an error.
    """
    declared = config.get(CONFIG_KEY)
    if declared is None:
        return ()
    if not isinstance(declared, (list, tuple)):
        raise VenueBreakMisdeclared(f"{CONFIG_KEY} is a list of breaks")
    breaks = []
    for item in declared:
        if not isinstance(item, Mapping):
            raise VenueBreakMisdeclared(
                f"each {CONFIG_KEY} entry is an object with start_utc and "
                f"resume_utc"
            )
        unknown = set(item) - {"start_utc", "resume_utc", "label"}
        if unknown:
            raise VenueBreakMisdeclared(
                f"unknown {CONFIG_KEY} keys: {sorted(unknown)}"
            )
        start = _clock_minute(item.get("start_utc"), "start_utc")
        resume = _clock_minute(item.get("resume_utc"), "resume_utc")
        evidence: dict[str, Any] = {"declared_in": CONFIG_KEY}
        label = item.get("label")
        if label is not None:
            evidence["label"] = str(label)
        breaks.append(VenueBreak(start_minute=start, resume_minute=resume,
                                 source=SOURCE_CONFIGURED, evidence=evidence))
    return tuple(sorted(breaks, key=lambda item: item.start_minute))


def _utc_date_and_minute(value: Any) -> tuple[str, int]:
    if not isinstance(value, str):
        raise VenueBreakUndeclarable("a command row carries an ISO created_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise VenueBreakUndeclarable(
            "a command row's created_at is not readable as a time"
        ) from error
    if parsed.tzinfo is None:
        raise VenueBreakUndeclarable(
            "a command row's created_at carries no timezone"
        )
    utc = parsed.astimezone(timezone.utc)
    return utc.date().isoformat(), utc.hour * 60 + utc.minute


def venue_break_from_observed_record(
    rows: Sequence[Mapping[str, Any]],
    *,
    minimum_dates: int = MINIMUM_OBSERVED_DATES,
) -> VenueBreak:
    """Derive the break from the venue's own refusals in the retained record.

    ``rows`` are historical commands, each carrying ``created_at``, the
    effective ``state`` and the venue's ``result_code``. Only the machine code
    is read; no message is consulted, exactly as in the outcome vocabulary.

    Four conditions, and every one of them can fail:

    * the ``market_closed`` refusal must have been observed on at least
      ``minimum_dates`` distinct UTC dates — one refusal is an incident, not a
      schedule;
    * **no command may have SUCCEEDED inside the window** the refusals span. A
      success inside it proves the window is not a break, and the derivation
      refuses rather than declaring one anyway;
    * the record must itself show a minute AFTER the window at which a command
      succeeded — that observed minute, and not a guess or a pad, is where the
      break resumes;
    * the resulting window must be shorter than the ceiling.

    Raises ``VenueBreakUndeclarable`` when any of that is missing. The caller's
    correct response is to carry no break, never to invent one.
    """
    if minimum_dates < 1:
        raise ValueError("a derivation observes at least one date")
    refused_minutes: set[int] = set()
    refused_dates: set[str] = set()
    refused_rows = 0
    succeeded_minutes: set[int] = set()
    for row in rows:
        day, minute = _utc_date_and_minute(row.get("created_at"))
        code = row.get("result_code")
        if isinstance(code, int) and not isinstance(code, bool) and (
            code in MARKET_CLOSED_RETCODES
        ):
            refused_minutes.add(minute)
            refused_dates.add(day)
            refused_rows += 1
        if row.get("state") == "succeeded":
            succeeded_minutes.add(minute)
    if not refused_minutes:
        raise VenueBreakUndeclarable(
            "the record holds no market_closed refusal; no break is derivable "
            "and none is guessed"
        )
    if len(refused_dates) < minimum_dates:
        raise VenueBreakUndeclarable(
            f"the venue was observed closed on {len(refused_dates)} date(s); "
            f"{minimum_dates} distinct dates are required before a recurring "
            f"break is derived from the record"
        )
    start = min(refused_minutes)
    last_refused = max(refused_minutes)
    inside = sorted(m for m in succeeded_minutes if start <= m <= last_refused)
    if inside:
        raise VenueBreakUndeclarable(
            f"a command succeeded at minute(s) {inside} inside the window the "
            f"refusals span; that window is therefore not a venue break"
        )
    after = sorted(m for m in succeeded_minutes if m > last_refused)
    if not after:
        raise VenueBreakUndeclarable(
            "the record never shows a command succeeding after the refusals, "
            "so it does not establish when the venue reopened"
        )
    resume = after[0]
    if resume - start > MAX_BREAK_MINUTES:
        raise VenueBreakUndeclarable(
            f"the record's own bounds span {resume - start} minutes, beyond "
            f"the {MAX_BREAK_MINUTES}-minute ceiling; the evidence does not "
            f"narrow the break enough to schedule against"
        )
    return VenueBreak(
        start_minute=start,
        resume_minute=resume,
        source=SOURCE_OBSERVED_RECORD,
        evidence={
            "market_closed_retcodes": sorted(MARKET_CLOSED_RETCODES),
            "refusal_rows": refused_rows,
            "refusal_dates_utc": sorted(refused_dates),
            "refused_minutes": sorted(refused_minutes),
            "resume_minute_observed_succeeding": resume,
            "commands_read": len(rows),
        },
    )


def break_containing(
    breaks: Sequence[VenueBreak], moment: datetime
) -> Optional[VenueBreak]:
    """The break this instant falls in, or ``None``."""
    for window in breaks:
        if window.contains(moment):
            return window
    return None


def deferral_for(
    breaks: Sequence[VenueBreak],
    moment: datetime,
    *,
    boundary: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """The named deferral for this instant, or ``None`` to proceed.

    The returned mapping is the runner's own state record: it names the refusal,
    the break, the break's source and the rows it rests on, and the instant at
    which the lane resumes. Nothing is retried and nothing is dropped — the
    closed bar that produced the decision is still the closed bar afterwards.
    """
    window = break_containing(breaks, moment)
    if window is None:
        return None
    return {
        "state": STATE_VENUE_BREAK_DEFERRED,
        "refusal": REFUSAL_VENUE_BREAK,
        "venue_break": window.as_fact(),
        "deferred_at": moment.astimezone(timezone.utc).isoformat(),
        "resumes_at": window.resumes_at(moment).isoformat(),
        "boundary": boundary,
        "commands_queued": 0,
        "reason": (
            "the venue has refused orders in this window on the dates the "
            "record holds; a command is not issued into a known venue break"
        ),
    }
