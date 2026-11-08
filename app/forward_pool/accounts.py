"""Independent virtual accounts and the single-writer route book.

A candidate's trades never post into another candidate's ledger. The route
book is the only object that could reach a broker, and this module never
opens a socket. Protective exits keep running when the route is disconnected.
An unrelated position is not adopted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.forward_pool.core import (
    BusinessContract,
    PoolRefusal,
    command_checked,
    entry_allowed,
    order_units,
    protection_required,
)
from app.forward_pool.snapshot import HourlyBar, quote_for


@dataclass
class Position:
    direction: int
    units: int
    entry: float
    sl: float
    tp: float
    owner: str

    def side_name(self) -> str:
        return "long" if self.direction == 1 else "short"


@dataclass
class CloseRecord:
    reason: str
    price: float
    pnl: float
    owner: str


@dataclass
class VirtualAccount:
    account_id: str
    owner: str
    balance: float = 10_000.0
    position: Position | None = None
    closes: list[CloseRecord] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    entries: int = 0
    unrelated_locked: bool = False

    def equity(self, mark: float | None = None) -> float:
        if self.position is None or mark is None:
            return self.balance
        return self.balance + self.position.direction * (mark - self.position.entry) * self.position.units

    def apply(
        self,
        signal: int,
        bar: HourlyBar,
        contract: BusinessContract,
        *,
        manage: bool = True,
        entry_sl: float | None = None,
        entry_tp: float | None = None,
        fee: float = 0.0,
        financing: float = 0.0,
    ) -> dict[str, Any]:
        """Apply one signal. ``manage=False`` still runs protection and forced flat."""

        if self.unrelated_locked:
            self.refusals.append("UNRELATED_POSITION")
            return self._result(signal, 0, "refused", "UNRELATED_POSITION", None)
        contract.validate()
        position_side = 0 if self.position is None else self.position.direction
        command = command_checked(signal, position_side) if manage else 0
        close_reason = self._protections(bar, fee, financing)
        refusal = None
        if close_reason is None and self.position is not None and command == 3:
            self._close(bar, "AGENT_CLOSE", fee, financing)
            close_reason = None if self.position is not None else "AGENT_CLOSE"
        elif self.position is not None and command in (1, 2):
            self.refusals.append("POSITION_OCCUPIED")
            refusal = "POSITION_OCCUPIED"
        elif self.position is None and command in (1, 2):
            refusal = self._try_open(command, bar, contract, entry_sl, entry_tp, fee)
        if self.position is not None:
            intrabar = self._intrabar(bar, fee, financing)
            if intrabar is not None:
                close_reason = intrabar
        if self.position is not None and not bar.before_force_flat:
            self._close(bar, "FORCED_FLAT", fee, financing)
            if self.position is None:
                close_reason = "FORCED_FLAT"
        outcome = "refused" if refusal else "applied"
        return self._result(signal, command, outcome, refusal, close_reason)

    def _result(
        self,
        signal: int,
        command: int,
        outcome: str,
        refusal: str | None,
        close_reason: str | None,
    ) -> dict[str, Any]:
        intent = {0: "hold", 1: "open_long", 2: "open_short", 3: "close"}[command]
        if refusal and command in (1, 2):
            intent = "refuse"
        return {
            "signal": signal,
            "command": command,
            "intent": intent,
            "outcome": outcome,
            "refusal": refusal,
            "close_reason": close_reason,
            "balance": self.balance,
            "position": 0 if self.position is None else self.position.direction,
            "owner": self.owner,
            "entries": self.entries,
            "units": 0 if self.position is None else self.position.units,
        }

    def _protections(self, bar: HourlyBar, fee: float, financing: float) -> str | None:
        if self.position is None:
            return None
        price = quote_for(self.position.direction, bar, exit_fill=True)
        if price is None:
            return None
        if self.position.direction == 1 and price <= self.position.sl:
            self._close_at(price, "SL", fee, financing)
            return "SL"
        if self.position.direction == -1 and price >= self.position.sl:
            self._close_at(price, "SL", fee, financing)
            return "SL"
        return None

    def _intrabar(self, bar: HourlyBar, fee: float, financing: float) -> str | None:
        position = self.position
        if position is None or bar.high is None or bar.low is None:
            return None
        if position.direction == 1:
            stop_hit = bar.low <= position.sl
            take_hit = bar.high >= position.tp
        else:
            stop_hit = bar.high >= position.sl
            take_hit = bar.low <= position.tp
        if stop_hit:
            self._close_at(position.sl, "SL", fee, financing)
            return "SL"
        if take_hit:
            self._close_at(position.tp, "TP", fee, financing)
            return "TP"
        return None

    def _try_open(
        self,
        command: int,
        bar: HourlyBar,
        contract: BusinessContract,
        sl: float | None,
        tp: float | None,
        fee: float,
    ) -> str | None:
        if not bar.connected:
            self.refusals.append("BROKER_DISCONNECTED")
            return "BROKER_DISCONNECTED"
        if bar.bid is None or bar.ask is None:
            self.refusals.append("QUOTE_ABSENT")
            return "QUOTE_ABSENT"
        allowed, reason = entry_allowed(bar, self.balance, contract)
        if not allowed:
            self.refusals.append(reason)
            return reason
        direction = 1 if command == 1 else -1
        entry = quote_for(direction, bar, exit_fill=False)
        if entry is None:
            self.refusals.append("QUOTE_ABSENT")
            return "QUOTE_ABSENT"
        geometry = protection_required(sl, tp, direction, entry)
        if geometry is not None:
            self.refusals.append(geometry)
            return geometry
        units = order_units(
            self.balance,
            entry,
            contract.q,
            rel_volume=contract.rel_volume,
            leverage=contract.leverage,
            min_units=contract.min_units,
        )
        if units is None:
            self.refusals.append("UNITS")
            return "UNITS"
        assert sl is not None and tp is not None
        self.position = Position(direction, units, entry, float(sl), float(tp), self.owner)
        self.balance -= fee
        self.entries += 1
        return None

    def _close(self, bar: HourlyBar, reason: str, fee: float, financing: float) -> None:
        price = quote_for(self.position.direction, bar, exit_fill=True) if self.position else None
        if price is None or self.position is None:
            self.refusals.append("QUOTE_ABSENT")
            return
        self._close_at(price, reason, fee, financing)

    def _close_at(self, price: float, reason: str, fee: float, financing: float) -> None:
        position = self.position
        if position is None:
            return
        pnl = position.direction * (price - position.entry) * position.units - fee - financing
        self.balance += pnl
        self.closes.append(CloseRecord(reason, price, pnl, position.owner))
        self.position = None


@dataclass
class RouteEffect:
    effect_id: str
    state: str
    owner: str
    command: int
    snapshot_id: str
    transport_calls: int = 0


@dataclass
class RouteBook:
    """One writer, flat handover, no real-capital tier, no unrelated custody."""

    route_id: str
    asset: str = "EURUSD"
    tier: str = "shadow"
    authorized: bool = False
    writer: str | None = None
    queued: str | None = None
    cash: float = 10_000.0
    position: Position | None = None
    connected: bool = True
    authority_expires_utc_ns: int | None = None
    effects: dict[str, RouteEffect] = field(default_factory=dict)
    tenure: list[dict[str, Any]] = field(default_factory=list)
    halted_unknown: bool = False

    def __post_init__(self) -> None:
        if self.tier not in {"shadow", "paper", "demo"}:
            raise PoolRefusal("REAL_CAPITAL_DISABLED")
        if self.asset != "EURUSD":
            raise PoolRefusal("ASSET_MISMATCH")

    def request_switch(self, successor: str, *, now_ns: int, equity: float) -> str:
        self._authority(now_ns)
        if not self.authorized:
            raise PoolRefusal("UNAUTHORIZED_ROUTE")
        if self.writer is None:
            self._transfer(successor, now_ns, equity, opened=True)
            return "WRITER"
        if self.position is not None:
            self.queued = successor
            return "QUEUED_UNTIL_FLAT"
        self._transfer(successor, now_ns, equity, opened=False)
        return "HANDED_OVER"

    def _transfer(self, successor: str, now_ns: int, equity: float, *, opened: bool) -> None:
        self.tenure.append(
            {
                "writer": successor,
                "previous": self.writer,
                "at_utc_ns": now_ns,
                "cash": self.cash,
                "equity": equity,
                "opened": opened,
            }
        )
        self.writer = successor
        self.queued = None

    def submit(
        self,
        *,
        effect_id: str,
        owner: str,
        command: int,
        snapshot_id: str,
        now_ns: int,
        transport: Any = None,
    ) -> RouteEffect:
        self._authority(now_ns)
        if self.tier not in {"shadow", "paper", "demo"}:
            raise PoolRefusal("REAL_CAPITAL_DISABLED")
        if not self.authorized and self.tier != "shadow":
            raise PoolRefusal("UNAUTHORIZED_ROUTE")
        if self.writer is not None and owner != self.writer:
            raise PoolRefusal("CONCURRENT_WRITER")
        if self.position is not None and self.position.owner != owner:
            raise PoolRefusal("POSITION_OWNER")
        existing = self.effects.get(effect_id)
        if existing is not None:
            if existing.state == "effect_unknown":
                raise PoolRefusal("UNKNOWN_UNRECONCILED")
            return existing
        if self.halted_unknown:
            raise PoolRefusal("UNKNOWN_UNRECONCILED")
        effect = RouteEffect(effect_id, "journaled_pending", owner, command, snapshot_id)
        self.effects[effect_id] = effect
        if transport is None:
            effect.state = "acknowledged_shadow"
            return effect
        effect.transport_calls += 1
        try:
            result = transport(effect)
        except PoolRefusal as exc:
            if exc.code != "UNKNOWN_OUTCOME":
                raise
            effect.state = "effect_unknown"
            self.halted_unknown = True
            raise
        if result != "ack":
            effect.state = "effect_unknown"
            self.halted_unknown = True
            raise PoolRefusal("UNKNOWN_OUTCOME")
        effect.state = "acknowledged"
        return effect

    def reconcile(self, effect_id: str, outcome: str) -> None:
        effect = self.effects.get(effect_id)
        if effect is None or effect.state != "effect_unknown":
            raise PoolRefusal("RECONCILE_TARGET")
        if outcome not in {"filled", "rejected", "absent"}:
            raise PoolRefusal("RECONCILE_OUTCOME")
        effect.state = "terminal_" + outcome
        self.halted_unknown = any(item.state == "effect_unknown" for item in self.effects.values())

    def note_flat(self, now_ns: int, equity: float) -> str | None:
        if self.position is not None:
            return None
        if self.queued is None:
            return None
        successor = self.queued
        self._transfer(successor, now_ns, equity, opened=False)
        return successor

    def _authority(self, now_ns: int) -> None:
        expires = self.authority_expires_utc_ns
        if expires is not None and now_ns > expires:
            raise PoolRefusal("STALE_AUTHORITY")
