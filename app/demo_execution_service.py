"""LTS demo execution service — L0 dry-run vertical (doc 29 §7, findings 039-042).

LTS is the sole order authority. This service consumes ``AssetIntent``,
applies account risk and capability checks, emits protected
``OrderIntentV2`` through a zero-network sink and persists every decision,
would-be order and lifecycle fact in an append-only hash-chained OLAP.

L0 construction guarantees, by design and proven in tests:

- the sink performs no network operation of any kind; the venue submission
  count is structurally zero;
- every risk-increasing intent carries broker-side SL and TP or is
  rejected (the v2 contract makes the naked form inexpressible);
- risk is reserved atomically before serialization and released
  deterministically on reject/cancel/close (finding 040);
- a venue minimum size never rounds up through a risk cap;
- unknown acknowledgements block retries until reconciliation
  (finding 041);
- the owner hold/kill path is deterministic and works with every LLM
  process dead (finding 042).

No credential, venue endpoint or write path exists in this module.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_contracts import (
    AssetIntent,
    BrokerCapabilitySnapshot,
    ExecutionReportV2,
    OrderIntentV2,
    OwnerCommand,
    ProtectiveBracket,
    RiskEnvelope,
    content_hash,
    protection_covers_filled,
)


class DemoExecutionError(RuntimeError):
    """Fail-closed rejection; the reason is persisted as a decision fact."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class DemoExecutionConfig:
    """Resolved from JSON configuration; no hidden defaults in code paths."""

    venue: str
    account_fingerprint: str
    database_path: str
    risk_fraction_at_stop: float
    max_overshoot_ratio: float
    gross_notional_fraction_max: float
    margin_fraction_max: float
    daily_loss_budget_fraction: float
    max_concurrent_positions: int
    signal_max_age_seconds: float
    owner_issuer_allowlist: tuple[str, ...]
    command_phrases: Mapping[str, str]  # command verb -> exact phrase

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DemoExecutionConfig":
        required = [
            "venue", "account_fingerprint", "database_path",
            "risk_fraction_at_stop", "max_overshoot_ratio",
            "gross_notional_fraction_max", "margin_fraction_max",
            "daily_loss_budget_fraction", "max_concurrent_positions",
            "signal_max_age_seconds", "owner_issuer_allowlist",
            "command_phrases",
        ]
        missing = [key for key in required if key not in value]
        if missing:
            raise DemoExecutionError(f"config missing keys: {missing}")
        fractions = {
            "risk_fraction_at_stop": value["risk_fraction_at_stop"],
            "gross_notional_fraction_max": value["gross_notional_fraction_max"],
            "margin_fraction_max": value["margin_fraction_max"],
            "daily_loss_budget_fraction": value["daily_loss_budget_fraction"],
        }
        for name, fraction in fractions.items():
            if not (0.0 < float(fraction) <= 1.0):
                raise DemoExecutionError(f"{name} must be in (0, 1]")
        if float(value["max_overshoot_ratio"]) < 0.0:
            raise DemoExecutionError("max_overshoot_ratio must be >= 0")
        verbs = {"hold", "kill", "flatten_all", "cancel_pending"}
        phrases = dict(value["command_phrases"])
        unknown = set(phrases) - verbs
        if unknown:
            raise DemoExecutionError(f"command_phrases has non-risk-reducing verbs: {sorted(unknown)}")
        return cls(
            venue=str(value["venue"]),
            account_fingerprint=str(value["account_fingerprint"]),
            database_path=str(value["database_path"]),
            risk_fraction_at_stop=float(value["risk_fraction_at_stop"]),
            max_overshoot_ratio=float(value["max_overshoot_ratio"]),
            gross_notional_fraction_max=float(value["gross_notional_fraction_max"]),
            margin_fraction_max=float(value["margin_fraction_max"]),
            daily_loss_budget_fraction=float(value["daily_loss_budget_fraction"]),
            max_concurrent_positions=int(value["max_concurrent_positions"]),
            signal_max_age_seconds=float(value["signal_max_age_seconds"]),
            owner_issuer_allowlist=tuple(value["owner_issuer_allowlist"]),
            command_phrases=phrases,
        )


class ZeroNetworkSink:
    """The adapter serialization boundary with structurally zero submissions.

    Serializes the exact IBKR-bracket-shaped payload the L1 adapter will
    send, hashes and stores it, and never imports or touches a socket. The
    L0->L1 switch replaces only this class behind the same interface.
    """

    def __init__(self) -> None:
        self.would_be_orders: int = 0
        self.network_submissions: int = 0  # structurally constant

    def serialize(self, intent: OrderIntentV2) -> dict[str, Any]:
        if intent.intent_class == "risk_increasing" and intent.protection is None:
            raise DemoExecutionError("sink refuses unprotected payload")
        side = "BUY" if intent.delta_units > 0 else "SELL"
        payload = {
            "adapter": "ibkr_paper.bracket.v1",
            "venue": intent.venue,
            "instrument": intent.instrument,
            "side": side,
            "total_quantity": abs(intent.delta_units),
            "order_type": intent.order_type.upper(),
            "limit_price": intent.limit_price,
            "entry_trigger_price": intent.entry_trigger_price,
            "bracket": None
            if intent.protection is None
            else {
                "stop_loss_price": intent.protection.stop_loss_price,
                "take_profit_price": intent.protection.take_profit_price,
                "transmit_rule": "parent_and_children_atomic",
            },
            "idempotency_key": intent.idempotency_key,
            "intent_class": intent.intent_class,
        }
        self.would_be_orders += 1
        return payload


_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    idempotency_key TEXT PRIMARY KEY,
    decided_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT,
    intent_json TEXT,
    payload_json TEXT,
    payload_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    day TEXT NOT NULL,
    risk_fraction REAL NOT NULL,
    gross_fraction REAL NOT NULL,
    margin_fraction REAL NOT NULL,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lifecycle_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    order_intent_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    state TEXT NOT NULL,
    report_json TEXT NOT NULL,
    prev_chain_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    nonce TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    issuer_id TEXT NOT NULL,
    command TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS service_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DemoExecutionOlap:
    """Append-only, hash-chained order/decision facts (social-lab idiom)."""

    def __init__(self, path: str | Path) -> None:
        self._con = sqlite3.connect(str(path))
        self._con.executescript(_SCHEMA)
        self._con.commit()

    def close(self) -> None:
        self._con.close()

    # -- decisions / idempotency ------------------------------------------
    def recorded_decision(self, idempotency_key: str) -> Optional[dict[str, Any]]:
        row = self._con.execute(
            "SELECT outcome, reason, payload_json, payload_sha256 FROM decisions "
            "WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "outcome": row[0],
            "reason": row[1],
            "payload": None if row[2] is None else json.loads(row[2]),
            "payload_sha256": row[3],
            "replayed": True,
        }

    def record_decision(
        self,
        idempotency_key: str,
        outcome: str,
        reason: Optional[str],
        intent_json: Optional[str],
        payload: Optional[dict[str, Any]],
    ) -> Optional[str]:
        payload_json = None if payload is None else _canonical(payload)
        digest = (
            None
            if payload_json is None
            else hashlib.sha256(payload_json.encode()).hexdigest()
        )
        self._con.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?)",
            (
                idempotency_key,
                _utc_now().isoformat(),
                outcome,
                reason,
                intent_json,
                payload_json,
                digest,
            ),
        )
        self._con.commit()
        return digest

    # -- reservations (finding 040) ---------------------------------------
    def active_totals(self, day: str) -> dict[str, float]:
        risk_active, gross, margin, count = self._con.execute(
            "SELECT COALESCE(SUM(risk_fraction),0), COALESCE(SUM(gross_fraction),0),"
            " COALESCE(SUM(margin_fraction),0), COUNT(*) FROM reservations "
            "WHERE state='active'"
        ).fetchone()
        day_risk = self._con.execute(
            "SELECT COALESCE(SUM(risk_fraction),0) FROM reservations "
            "WHERE day=? AND state IN ('active','consumed')",
            (day,),
        ).fetchone()[0]
        return {
            "risk_active": risk_active,
            "gross": gross,
            "margin": margin,
            "positions": float(count),
            "day_risk": day_risk,
        }

    def reserve(
        self,
        reservation_id: str,
        idempotency_key: str,
        day: str,
        risk_fraction: float,
        gross_fraction: float,
        margin_fraction: float,
    ) -> None:
        self._con.execute(
            "INSERT INTO reservations VALUES (?,?,?,?,?,?,?,'active')",
            (
                reservation_id,
                idempotency_key,
                _utc_now().isoformat(),
                day,
                risk_fraction,
                gross_fraction,
                margin_fraction,
            ),
        )
        self._con.commit()

    def release(self, reservation_id: str, terminal_state: str) -> None:
        if terminal_state not in ("released", "consumed"):
            raise DemoExecutionError("reservation terminal state invalid")
        cur = self._con.execute(
            "UPDATE reservations SET state=? WHERE reservation_id=? AND state='active'",
            (terminal_state, reservation_id),
        )
        if cur.rowcount != 1:
            raise DemoExecutionError(
                f"reservation {reservation_id} not active; release is not idempotent by design"
            )
        self._con.commit()

    # -- lifecycle events (finding 041) -----------------------------------
    def last_chain_hash(self) -> str:
        row = self._con.execute(
            "SELECT chain_hash FROM lifecycle_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else "genesis"

    def append_lifecycle(self, report: ExecutionReportV2) -> str:
        report_json = report.model_dump_json()
        prev = self.last_chain_hash()
        chain = hashlib.sha256((prev + report_json).encode()).hexdigest()
        self._con.execute(
            "INSERT INTO lifecycle_events "
            "(recorded_at, order_intent_id, attempt_id, state, report_json,"
            " prev_chain_hash, chain_hash) VALUES (?,?,?,?,?,?,?)",
            (
                _utc_now().isoformat(),
                report.order_intent_id,
                report.attempt_id,
                report.state,
                report_json,
                prev,
                chain,
            ),
        )
        self._con.commit()
        return chain

    def last_state(self, order_intent_id: str) -> Optional[str]:
        row = self._con.execute(
            "SELECT state FROM lifecycle_events WHERE order_intent_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (order_intent_id,),
        ).fetchone()
        return row[0] if row else None

    def unreconciled(self) -> list[str]:
        rows = self._con.execute(
            "SELECT DISTINCT order_intent_id FROM lifecycle_events "
            "WHERE order_intent_id IN ("
            " SELECT order_intent_id FROM lifecycle_events GROUP BY order_intent_id"
            " HAVING MAX(seq) IN (SELECT seq FROM lifecycle_events "
            " WHERE state='unknown_requires_reconciliation'))"
        ).fetchall()
        return [row[0] for row in rows]

    # -- owner commands (finding 042) --------------------------------------
    def nonce_seen(self, nonce: str) -> bool:
        return (
            self._con.execute(
                "SELECT 1 FROM commands WHERE nonce=?", (nonce,)
            ).fetchone()
            is not None
        )

    def record_command(
        self, command: OwnerCommand, accepted: bool, reason: Optional[str]
    ) -> None:
        self._con.execute(
            "INSERT INTO commands VALUES (?,?,?,?,?,?)",
            (
                command.nonce,
                _utc_now().isoformat(),
                command.issuer_id,
                command.command,
                1 if accepted else 0,
                reason,
            ),
        )
        self._con.commit()

    # -- deterministic service state ---------------------------------------
    def set_state(self, key: str, value: str) -> None:
        self._con.execute(
            "INSERT INTO service_state VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._con.commit()

    def get_state(self, key: str, default: str = "") -> str:
        row = self._con.execute(
            "SELECT value FROM service_state WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default


def plan_units(
    *,
    equity: float,
    risk_fraction_at_stop: float,
    stop_distance: float,
    reference_price: float,
    margin_rate: float,
    unit_step: float,
    min_units: float,
    max_overshoot_ratio: float,
    available_day_risk_fraction: float,
    available_gross_fraction: float,
    available_margin_fraction: float,
) -> float:
    """Size to the most binding of four separate dimensions (finding 040).

    The risk-at-stop target may be exceeded by a venue minimum only within
    ``max_overshoot_ratio``; the day-budget, gross-notional and margin caps
    are hard — a venue minimum that breaches any of them skips the order.
    A floor never silently overrides a cap.
    """
    if (
        equity <= 0 or stop_distance <= 0 or unit_step <= 0
        or min_units <= 0 or reference_price <= 0
    ):
        raise DemoExecutionError("sizing inputs must be positive")
    risk_target_units = (equity * risk_fraction_at_stop) / stop_distance
    day_max_units = (equity * available_day_risk_fraction) / stop_distance
    gross_max_units = (equity * available_gross_fraction) / reference_price
    margin_max_units = (
        (equity * available_margin_fraction) / (reference_price * margin_rate)
        if margin_rate > 0
        else gross_max_units
    )
    hard_max = min(day_max_units, gross_max_units, margin_max_units)
    units = min(risk_target_units, hard_max)
    units = int(units / unit_step) * unit_step
    if units >= min_units:
        return units
    if min_units > hard_max:
        binding = min(
            (day_max_units, "day_risk"),
            (gross_max_units, "gross_notional"),
            (margin_max_units, "margin"),
        )[1]
        raise DemoExecutionError(
            f"venue_minimum_breaches_hard_caps:{binding} — minimum size "
            "cannot fit under the remaining budget"
        )
    implied_risk = (min_units * stop_distance) / equity
    if implied_risk > risk_fraction_at_stop * (1.0 + max_overshoot_ratio):
        raise DemoExecutionError(
            "minimum_size_overshoot: venue minimum implies risk "
            f"{implied_risk:.6f} beyond configured cap"
        )
    return min_units


class DemoExecutionService:
    """Deterministic intent-to-protected-order planner, dry-run only."""

    def __init__(
        self,
        config: DemoExecutionConfig,
        olap: DemoExecutionOlap,
        sink: ZeroNetworkSink,
    ) -> None:
        self.config = config
        self.olap = olap
        self.sink = sink

    # -- owner command path: zero LLM dependency (042) ---------------------
    def apply_owner_command(self, command: OwnerCommand, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or _utc_now()
        if command.issuer_id not in self.config.owner_issuer_allowlist:
            self.olap.record_command(command, False, "issuer_not_allowlisted")
            return {"accepted": False, "reason": "issuer_not_allowlisted"}
        if self.olap.nonce_seen(command.nonce):
            # replay: record refusal under a derived key; original stands
            return {"accepted": False, "reason": "nonce_replay"}
        if command.expires_at <= now:
            self.olap.record_command(command, False, "expired")
            return {"accepted": False, "reason": "expired"}
        expected = self.config.command_phrases.get(command.command)
        if expected is None or command.exact_phrase != expected:
            self.olap.record_command(command, False, "phrase_mismatch")
            return {"accepted": False, "reason": "phrase_mismatch"}
        self.olap.record_command(command, True, None)
        if command.command in ("hold", "kill"):
            self.olap.set_state("halt", command.command)
        return {"accepted": True, "state": self.olap.get_state("halt", "none")}

    # -- the decision path --------------------------------------------------
    def process_intent(
        self,
        intent: AssetIntent,
        capability: BrokerCapabilitySnapshot,
        *,
        equity: float,
        reference_price: float,
        instrument: str,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        now = now or _utc_now()
        idem = f"{intent.object_id}:{intent.as_of.isoformat()}"

        replay = self.olap.recorded_decision(idem)
        if replay is not None:
            return replay

        def reject(reason: str) -> dict[str, Any]:
            self.olap.record_decision(idem, "rejected", reason,
                                      intent.model_dump_json(), None)
            return {"outcome": "rejected", "reason": reason, "replayed": False}

        halt = self.olap.get_state("halt", "none")
        if halt != "none":
            return reject(f"halted:{halt}")
        if self.olap.unreconciled():
            return reject("reconciliation_required_before_new_risk")

        age = (now - intent.as_of).total_seconds()
        if age > self.config.signal_max_age_seconds:
            return reject(f"stale_signal:{age:.1f}s")
        if intent.valid_until is not None and now > intent.valid_until:
            return reject("signal_expired")

        if intent.action.value in ("hold", "no_trade"):
            return reject(f"non_actionable:{intent.action.value}")
        if intent.target_exposure in (None, 0.0):
            return reject("no_target_exposure")
        if intent.risk_geometry is None or intent.risk_geometry.stop_price is None \
                or intent.risk_geometry.take_profit_price is None:
            return reject("missing_protection_geometry")

        entry = capability.instruments and {
            cap.instrument: cap for cap in capability.instruments
        }.get(instrument)
        if not entry or not entry.tradeable:
            return reject("instrument_not_tradeable")
        side_long = intent.target_exposure > 0
        if not side_long and not entry.shortable:
            return reject("short_not_supported")
        if not (entry.native_stop_loss and entry.native_take_profit
                and entry.native_bracket):
            return reject("native_protection_unavailable")

        stop_distance = abs(reference_price - intent.risk_geometry.stop_price)
        day = now.date().isoformat()
        totals = self.olap.active_totals(day)
        if totals["positions"] + 1 > self.config.max_concurrent_positions:
            return reject("max_concurrent_positions")
        epsilon = 1e-12
        available_day = self.config.daily_loss_budget_fraction - totals["day_risk"]
        available_gross = self.config.gross_notional_fraction_max - totals["gross"]
        available_margin = self.config.margin_fraction_max - totals["margin"]
        if available_day <= epsilon:
            return reject("daily_loss_budget_exhausted")
        if available_gross <= epsilon:
            return reject("gross_notional_cap")
        if available_margin <= epsilon:
            return reject("margin_cap")
        try:
            units = plan_units(
                equity=equity,
                risk_fraction_at_stop=self.config.risk_fraction_at_stop,
                stop_distance=stop_distance,
                reference_price=reference_price,
                margin_rate=float(entry.margin_rate or 1.0),
                unit_step=entry.unit_step,
                min_units=entry.min_units,
                max_overshoot_ratio=self.config.max_overshoot_ratio,
                available_day_risk_fraction=available_day,
                available_gross_fraction=available_gross,
                available_margin_fraction=available_margin,
            )
        except DemoExecutionError as error:
            return reject(str(error))
        if units <= 0:
            return reject("no_viable_size")

        risk_fraction = (units * stop_distance) / equity
        notional_fraction = (units * reference_price) / equity
        margin_fraction = notional_fraction * float(entry.margin_rate or 1.0)

        reservation_id = f"rsv-{hashlib.sha256(idem.encode()).hexdigest()[:16]}"
        self.olap.reserve(reservation_id, idem, day, risk_fraction,
                          notional_fraction, margin_fraction)

        order = OrderIntentV2(
            object_id=f"oi2-{reservation_id}",
            as_of=now,
            producer={"name": "lts.demo_execution_service", "version": "0.1.0"},
            trace_id=intent.trace_id,
            account_ref=self.config.account_fingerprint,
            asset_id=intent.asset_id,
            venue=self.config.venue,
            instrument=instrument,
            intent_class="risk_increasing",
            order_type="market",
            delta_units=units if side_long else -units,
            protection=ProtectiveBracket(
                stop_loss_price=intent.risk_geometry.stop_price,
                take_profit_price=intent.risk_geometry.take_profit_price,
            ),
            risk=RiskEnvelope(
                risk_fraction_at_stop=risk_fraction,
                gross_notional_fraction=notional_fraction,
                margin_fraction=min(margin_fraction, 1.0),
                daily_loss_budget_fraction=self.config.daily_loss_budget_fraction,
                reservation_id=reservation_id,
            ),
            capability_snapshot_hash=content_hash(
                capability.model_dump(mode="json")
            ),
            idempotency_key=idem,
        )
        payload = self.sink.serialize(order)
        digest = self.olap.record_decision(
            idem, "would_be_order", None, order.model_dump_json(), payload
        )
        report = ExecutionReportV2(
            object_id=f"er-{reservation_id}",
            as_of=now,
            producer={"name": "lts.demo_execution_service", "version": "0.1.0"},
            trace_id=intent.trace_id,
            order_intent_id=order.object_id,
            attempt_id=f"attempt-{reservation_id}",
            bracket_role="parent",
            state="requested",
            requested_units=order.delta_units,
        )
        self.olap.append_lifecycle(report)
        return {
            "outcome": "would_be_order",
            "reason": None,
            "payload": payload,
            "payload_sha256": digest,
            "order_intent_id": order.object_id,
            "reservation_id": reservation_id,
            "delta_units": order.delta_units,
            "replayed": False,
        }

    # -- lifecycle ingestion (L0: synthetic/replayed events only) -----------
    def apply_execution_event(self, report: ExecutionReportV2) -> dict[str, Any]:
        previous = self.olap.last_state(report.order_intent_id)
        if previous is not None and report.previous_state != previous:
            raise DemoExecutionError(
                f"event previous_state {report.previous_state!r} does not match "
                f"ledger state {previous!r}; reconcile from the ledger, not memory"
            )
        chain = self.olap.append_lifecycle(report)
        result: dict[str, Any] = {"chain_hash": chain, "state": report.state}
        reservation_id = f"rsv-{report.attempt_id.split('attempt-', 1)[-1]}" \
            if report.attempt_id.startswith("attempt-") else None
        if report.state in ("rejected", "cancelled", "expired") and reservation_id:
            self.olap.release(reservation_id, "released")
            result["reservation"] = "released"
        if report.state in ("filled", "partially_filled") and reservation_id:
            if report.state == "filled":
                self.olap.release(reservation_id, "consumed")
                result["reservation"] = "consumed"
            if not protection_covers_filled(report):
                self.olap.set_state("halt", "hold")
                result["emergency"] = "unprotected_exposure_hold_and_flatten"
        return result
