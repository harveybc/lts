"""Categorical selected70 contract used by the offline forward pool.

The integer mapping is the native ``command_for`` of the three-signal
environment: flat signals pass through, the opposite signal becomes command 3
(close), and the same direction or NOP holds. Command 3 does not reopen.

Two exported strings disagree with the engine that produced the epoch-0 tape.
They are resolved here and recorded for audit, not silently rewritten:

- recipe ``reverse = CLOSE_AND_OPEN`` describes the parent bar engine. The
  absolute interface and the epoch-0 tape have zero reversals, so a deployed
  step closes and waits for a later flat bar;
- recipe ``level_policy = clip`` is not applied. A level that fails the
  distance or gap check is refused;
- recipe activity text says ``orders_per_week >= 2``. The assignment requires
  strictly more than two. Exactly 2.0 is not eligible.

``Sizing.order_units`` is used with ``risk_accepts_minimum=False``. The broker
minimum never rounds a too-small allocation up. ``rel_volume`` is applied once,
inside that floor, and the margin is refused when it exceeds
``rel_volume * balance``. There is no daily entry cap.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

from app.model_position_control import decide_position_control


class PoolRefusal(Exception):
    """A named, fail-closed reason. Missing facts are not zeros or success."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def digest(value: Any) -> str:
    """Match ``trading_contracts.content_hash`` for JSON-safe values."""

    def norm(item: Any) -> Any:
        if isinstance(item, float):
            if not math.isfinite(item):
                raise PoolRefusal("NONFINITE")
            return 0.0 if item == 0.0 else item
        if isinstance(item, Mapping):
            return {str(key): norm(item[key]) for key in sorted(item, key=str)}
        if isinstance(item, (list, tuple)):
            return [norm(part) for part in item]
        if item is None or isinstance(item, (str, int, bool)):
            return item
        raise PoolRefusal("CANONICAL_TYPE", type(item).__name__)

    raw = json.dumps(
        norm(value),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def command_for(signal: int, position: int) -> int:
    """Native three-signal command. Position is -1, 0 or 1. Signal is 0, 1 or 2."""

    if type(signal) is not int or signal not in (0, 1, 2) or position not in (-1, 0, 1):
        raise PoolRefusal("SIGNAL_CONTRACT")
    if position == 0:
        return signal
    opposite = 2 if position == 1 else 1
    return 3 if signal == opposite else 0


def command_checked(signal: int, position: int) -> int:
    """Return ``command_for`` after it agrees with the existing position controller.

    ``decide_position_control`` already closes an opposite target without a
    one-step reversal. A disagreement stops the step instead of picking a side.
    """

    native = command_for(signal, position)
    names = {0: "hold", 1: "long", 2: "short"}
    exposure = {0: 0.0, 1: 1.0, -1: -1.0}[position]
    decision = decide_position_control(names[signal], current_exposure=exposure)
    expected = {
        (0, "hold"): 0,
        (0, "enter_long"): 1,
        (0, "enter_short"): 2,
        (1, "monitor"): 0,
        (1, "close"): 3,
        (-1, "monitor"): 0,
        (-1, "close"): 3,
    }.get((position, decision.disposition))
    if expected is None or expected != native:
        raise PoolRefusal(
            "CONTRACT_DISAGREEMENT",
            f"native={native} controller={decision.disposition}",
        )
    return native


def order_units(
    balance: float,
    price: float,
    percentual_order_size: float,
    *,
    rel_volume: float = 0.1,
    leverage: float = 50.0,
    min_units: int = 1,
) -> int | None:
    """Nested floor used by ``Sizing``, then the absolute margin cap.

    ``None`` means the order is refused. The minimum lot is not substituted.
    """

    if (
        not math.isfinite(balance)
        or not math.isfinite(price)
        or not math.isfinite(percentual_order_size)
        or balance <= 0
        or price <= 0
        or not 0 < percentual_order_size <= 1
        or rel_volume <= 0
        or leverage <= 0
        or min_units < 1
    ):
        raise PoolRefusal("SIZE_INPUT")
    units_max = max(0, int(math.floor(balance * rel_volume * leverage / price)))
    units = int(math.floor(percentual_order_size * units_max))
    if units < min_units:
        return None
    margin = units * price / leverage
    if margin > balance or margin > rel_volume * balance + 1e-9:
        return None
    return units


@dataclass(frozen=True)
class BusinessContract:
    """Checkpoint business contract for the EURUSD categorical route."""

    rel_volume: float = 0.1
    leverage: float = 50.0
    basis: str = "margin"
    min_units: int = 1
    max_entries_per_day: int | None = None
    entry_delay_after_open_hours: int = 12
    force_flat_before_close_hours: int = 12
    entry_cutoff_before_flat_hours: int = 0
    implicit_reversal: bool = False
    level_policy: str = "refuse"
    q: float = 1.0
    instrument: str = "EURUSD"

    def validate(self) -> None:
        if self.max_entries_per_day is not None:
            raise PoolRefusal("DAILY_CAP_REINTRODUCED")
        if self.implicit_reversal:
            raise PoolRefusal("IMPLICIT_REVERSAL")
        if self.level_policy != "refuse":
            raise PoolRefusal("LEVEL_CLIPPING")
        if self.leverage != 50 or self.rel_volume != 0.1 or self.basis != "margin":
            raise PoolRefusal("BUSINESS_CONTRACT")
        if self.entry_delay_after_open_hours != 12 or self.force_flat_before_close_hours != 12:
            raise PoolRefusal("CALENDAR_CONTRACT")
        if self.entry_cutoff_before_flat_hours != 0:
            raise PoolRefusal("CALENDAR_CONTRACT")
        if self.instrument != "EURUSD":
            raise PoolRefusal("INSTRUMENT")
        if not 0 < self.q <= 1:
            raise PoolRefusal("Q_RANGE")

    def digest(self) -> str:
        self.validate()
        return digest(
            {
                "basis": self.basis,
                "entry_cutoff_before_flat_hours": self.entry_cutoff_before_flat_hours,
                "entry_delay_after_open_hours": self.entry_delay_after_open_hours,
                "force_flat_before_close_hours": self.force_flat_before_close_hours,
                "implicit_reversal": self.implicit_reversal,
                "instrument": self.instrument,
                "leverage": self.leverage,
                "level_policy": self.level_policy,
                "max_entries_per_day": self.max_entries_per_day,
                "min_units": self.min_units,
                "q": self.q,
                "rel_volume": self.rel_volume,
            }
        )


def entry_allowed(snapshot: Any, balance: float, contract: BusinessContract) -> tuple[bool, str]:
    """Absolute entry gate. The daily cap is absent on purpose."""

    contract.validate()
    if not snapshot.same_session_next:
        return False, "SESSION_END"
    if not snapshot.operational:
        return False, "BEFORE_OPERATIONAL_START"
    if not snapshot.before_force_flat:
        return False, "ENTRY_CUTOFF"
    if balance <= 0 or not math.isfinite(balance):
        return False, "RUIN"
    return True, "OK"


def protection_required(sl: float | None, tp: float | None, direction: int, entry: float) -> str | None:
    if sl is None or tp is None or not math.isfinite(sl) or not math.isfinite(tp):
        return "PROTECTION_REQUIRED"
    if direction == 1 and not (sl < entry < tp):
        return "PROTECTION_GEOMETRY"
    if direction == -1 and not (tp < entry < sl):
        return "PROTECTION_GEOMETRY"
    return None
