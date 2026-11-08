"""Common hourly snapshot. One materialization serves every candidate of a recipe."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from app.forward_pool.core import PoolRefusal, digest


@dataclass(frozen=True)
class HourlyBar:
    """A completed-bar cut. ``civil_offset_minutes`` is recorded and not part of the decision key."""

    snapshot_id: str
    bar_close_utc_ns: int
    decision_time_utc_ns: int
    availability_watermark_utc_ns: int
    finalized: bool
    instrument: str
    columns: tuple[str, ...]
    values: tuple[float | None, ...]
    published_utc_ns: tuple[int, ...]
    bid: float | None
    ask: float | None
    high: float | None
    low: float | None
    session_id: str
    operational: bool
    before_force_flat: bool
    same_session_next: bool
    civil_offset_minutes: int
    quote_time_utc_ns: int | None = None
    quote_source: str = "snapshot"
    catch_up: bool = False
    connected: bool = True
    recorded_exit: float | None = None
    recorded_fee: float | None = None
    recorded_financing: float | None = None
    recorded_latency_ms: float | None = None
    recorded_rejection: str | None = None
    partial_fill: bool = False

    def __post_init__(self) -> None:
        if self.instrument != "EURUSD":
            raise PoolRefusal("INSTRUMENT")
        if len(self.columns) != len(self.values) or len(self.columns) != len(self.published_utc_ns):
            raise PoolRefusal("COLUMN_ORDER")
        if len(set(self.columns)) != len(self.columns):
            raise PoolRefusal("COLUMN_ORDER")
        if self.decision_time_utc_ns < self.bar_close_utc_ns:
            raise PoolRefusal("DECISION_TIME")
        if self.bid is not None and self.ask is not None and self.ask < self.bid:
            raise PoolRefusal("QUOTE")

    def decision_key(self) -> tuple[int, str]:
        return (self.bar_close_utc_ns, self.instrument)


@dataclass(frozen=True)
class Materialization:
    columns: tuple[str, ...]
    values: tuple[float, ...] | None
    feature_hash: str | None
    refusal: str | None


def materialize(bar: HourlyBar, columns: Sequence[str]) -> Materialization:
    """Consume only features published by the decision time.

    A missing column, a non-finite value, an incomplete bar or a watermark
    behind the bar suppresses the feature vector. Future publications are not
    read, so perturbing them cannot change the hash.
    """

    if not bar.finalized:
        return Materialization(tuple(columns), None, None, "INCOMPLETE_BAR")
    if bar.availability_watermark_utc_ns < bar.bar_close_utc_ns:
        return Materialization(tuple(columns), None, None, "STALE_INPUT")
    index = {name: position for position, name in enumerate(bar.columns)}
    consumed: list[tuple[str, float, int]] = []
    for name in columns:
        if name not in index:
            return Materialization(tuple(columns), None, None, "FEATURE_ABSENT")
        position = index[name]
        published = bar.published_utc_ns[position]
        if published > bar.decision_time_utc_ns:
            return Materialization(tuple(columns), None, None, "FUTURE_FEATURE")
        value = bar.values[position]
        if value is None or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return Materialization(tuple(columns), None, None, "FEATURE_ABSENT")
        consumed.append((name, float(value), int(published)))
    feature_hash = digest(
        {
            "bar_close_utc_ns": bar.bar_close_utc_ns,
            "columns": consumed,
            "instrument": bar.instrument,
        }
    )
    return Materialization(tuple(columns), tuple(item[1] for item in consumed), feature_hash, None)


def spread_of(bar: HourlyBar) -> float | None:
    if bar.bid is None or bar.ask is None:
        return None
    return float(bar.ask - bar.bid)


def quote_for(direction: int, bar: HourlyBar, *, exit_fill: bool) -> float | None:
    """Long pays the ask and sells the bid. Short sells the bid and buys the ask."""

    if bar.bid is None or bar.ask is None:
        return None
    if direction == 1:
        return float(bar.bid if exit_fill else bar.ask)
    if direction == -1:
        return float(bar.ask if exit_fill else bar.bid)
    raise PoolRefusal("SIGNAL_CONTRACT")
