"""LTS demo execution service — L0 dry-run vertical (doc 29 §7).

LTS is the sole order authority. This service consumes ``AssetIntent``,
applies account risk and capability checks, emits protected
``OrderIntentV2`` through a zero-network sink and persists every decision,
would-be order, reservation, exposure and lifecycle fact in an append-only
hash-chained OLAP.

Correction lineage (auditor findings, all regression-tested):

- 039-042: protected-contract era (see ``trading_contracts.execution_v2``);
- 043: protection is anchored to the persisted decision reference price —
  long requires SL < reference < TP, short the mirror — before any
  reservation or serialization;
- 044: filled exposure lives in a persisted exposure lifecycle and stays in
  every risk total until the position is closed; order terminal states
  never imply exposure closure;
- 045: budget check and reservation write happen inside one
  ``BEGIN IMMEDIATE`` transaction re-reading totals, so concurrent service
  instances serialize instead of double-spending a cap;
- 046: everything that can fail validation is constructed before any
  reservation; every rejection is a persisted, replayable outcome;
- 047: hold/kill/flatten_all/cancel_pending and the uncovered-exposure
  emergency emit deterministic zero-network risk-reducing intents with
  lifecycle facts, not just state flags.

No credential, venue endpoint or network write path exists in this module.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

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
            # Ruling R2: bounds above 100% are rejected, never clipped.
            if not (0.0 < float(fraction) <= 1.0):
                raise DemoExecutionError(f"{name} must be in (0, 1]")
        if float(value["max_overshoot_ratio"]) < 0.0:
            raise DemoExecutionError("max_overshoot_ratio must be >= 0")
        verbs = {"hold", "kill", "flatten_all", "cancel_pending"}
        phrases = dict(value["command_phrases"])
        unknown = set(phrases) - verbs
        if unknown:
            raise DemoExecutionError(
                f"command_phrases has non-risk-reducing verbs: {sorted(unknown)}"
            )
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
            "reduce_action": intent.reduce_action,
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
    reference_price REAL,
    quote_time TEXT,
    capability_evidence TEXT,
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
CREATE TABLE IF NOT EXISTS exposures (
    exposure_id TEXT PRIMARY KEY,
    order_intent_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    units_open REAL NOT NULL,
    risk_fraction REAL NOT NULL,
    gross_fraction REAL NOT NULL,
    margin_fraction REAL NOT NULL,
    capability_evidence TEXT NOT NULL,
    state TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT
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
    """Append-only, hash-chained order/decision facts (social-lab idiom).

    Connections run in autocommit (``isolation_level=None``); the
    ``atomic_unit`` context takes ``BEGIN IMMEDIATE`` so a check-and-write
    sequence is one serialized transaction across processes (finding 045).
    """

    def __init__(self, path: str | Path) -> None:
        self._con = sqlite3.connect(str(path), isolation_level=None, timeout=10.0)
        self._con.execute("PRAGMA busy_timeout=10000")
        self._con.executescript(_SCHEMA)
        self._in_unit = False

    def close(self) -> None:
        self._con.close()

    @contextmanager
    def atomic_unit(self) -> Iterator[None]:
        if self._in_unit:
            yield
            return
        self._con.execute("BEGIN IMMEDIATE")
        self._in_unit = True
        try:
            yield
        except BaseException:
            self._con.execute("ROLLBACK")
            raise
        else:
            self._con.execute("COMMIT")
        finally:
            self._in_unit = False

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
        reference_price: Optional[float] = None,
        quote_time: Optional[str] = None,
        capability_evidence: Optional[str] = None,
    ) -> Optional[str]:
        payload_json = None if payload is None else _canonical(payload)
        digest = (
            None
            if payload_json is None
            else hashlib.sha256(payload_json.encode()).hexdigest()
        )
        self._con.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                idempotency_key,
                _utc_now().isoformat(),
                outcome,
                reason,
                reference_price,
                quote_time,
                capability_evidence,
                intent_json,
                payload_json,
                digest,
            ),
        )
        return digest

    # -- reservations + exposures (findings 040, 044) -----------------------
    def active_totals(self, day: str) -> dict[str, float]:
        risk_active, gross_r, margin_r, count_r = self._con.execute(
            "SELECT COALESCE(SUM(risk_fraction),0), COALESCE(SUM(gross_fraction),0),"
            " COALESCE(SUM(margin_fraction),0), COUNT(*) FROM reservations "
            "WHERE state='active'"
        ).fetchone()
        risk_e, gross_e, margin_e, count_e = self._con.execute(
            "SELECT COALESCE(SUM(risk_fraction),0), COALESCE(SUM(gross_fraction),0),"
            " COALESCE(SUM(margin_fraction),0), COUNT(*) FROM exposures "
            "WHERE state='open'"
        ).fetchone()
        day_risk = self._con.execute(
            "SELECT COALESCE(SUM(risk_fraction),0) FROM reservations "
            "WHERE day=? AND state IN ('active','consumed')",
            (day,),
        ).fetchone()[0]
        return {
            "risk_active": risk_active + risk_e,
            "gross": gross_r + gross_e,
            "margin": margin_r + margin_e,
            "positions": float(count_r + count_e),
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

    def release(self, reservation_id: str, terminal_state: str) -> None:
        if terminal_state not in ("released", "consumed"):
            raise DemoExecutionError("reservation terminal state invalid")
        cur = self._con.execute(
            "UPDATE reservations SET state=? WHERE reservation_id=? AND state='active'",
            (terminal_state, reservation_id),
        )
        if cur.rowcount != 1:
            raise DemoExecutionError(
                f"reservation {reservation_id} not active; release is not "
                "idempotent by design"
            )

    def scale_reservation(self, reservation_id: str, remaining_ratio: float) -> None:
        if not (0.0 <= remaining_ratio <= 1.0):
            raise DemoExecutionError("remaining_ratio must be in [0, 1]")
        self._con.execute(
            "UPDATE reservations SET risk_fraction=risk_fraction*?, "
            "gross_fraction=gross_fraction*?, margin_fraction=margin_fraction*? "
            "WHERE reservation_id=? AND state='active'",
            (remaining_ratio, remaining_ratio, remaining_ratio, reservation_id),
        )

    def reservation_row(self, reservation_id: str) -> Optional[dict[str, Any]]:
        row = self._con.execute(
            "SELECT risk_fraction, gross_fraction, margin_fraction, state "
            "FROM reservations WHERE reservation_id=?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "risk_fraction": row[0], "gross_fraction": row[1],
            "margin_fraction": row[2], "state": row[3],
        }

    def open_exposure(
        self,
        exposure_id: str,
        order_intent_id: str,
        instrument: str,
        units_open: float,
        risk_fraction: float,
        gross_fraction: float,
        margin_fraction: float,
        capability_evidence: str,
    ) -> None:
        self._con.execute(
            "INSERT INTO exposures VALUES (?,?,?,?,?,?,?,?,'open',?,NULL) "
            "ON CONFLICT(exposure_id) DO UPDATE SET "
            "units_open=excluded.units_open, risk_fraction=excluded.risk_fraction,"
            "gross_fraction=excluded.gross_fraction,"
            "margin_fraction=excluded.margin_fraction",
            (
                exposure_id, order_intent_id, instrument, units_open,
                risk_fraction, gross_fraction, margin_fraction,
                capability_evidence, _utc_now().isoformat(),
            ),
        )

    def close_exposure(self, exposure_id: str) -> None:
        cur = self._con.execute(
            "UPDATE exposures SET state='closed', closed_at=? "
            "WHERE exposure_id=? AND state='open'",
            (_utc_now().isoformat(), exposure_id),
        )
        if cur.rowcount != 1:
            raise DemoExecutionError(f"exposure {exposure_id} not open")

    def open_exposures(self) -> list[dict[str, Any]]:
        rows = self._con.execute(
            "SELECT exposure_id, order_intent_id, instrument, units_open,"
            " capability_evidence FROM exposures WHERE state='open'"
        ).fetchall()
        return [
            {"exposure_id": r[0], "order_intent_id": r[1], "instrument": r[2],
             "units_open": r[3], "capability_evidence": r[4]}
            for r in rows
        ]

    def active_reservation_intents(self) -> list[dict[str, Any]]:
        rows = self._con.execute(
            "SELECT reservation_id, idempotency_key FROM reservations "
            "WHERE state='active'"
        ).fetchall()
        return [{"reservation_id": r[0], "idempotency_key": r[1]} for r in rows]

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

    # -- deterministic service state ---------------------------------------
    def set_state(self, key: str, value: str) -> None:
        self._con.execute(
            "INSERT INTO service_state VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

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

    _PRODUCER = {"name": "lts.demo_execution_service", "version": "0.2.0"}

    def __init__(
        self,
        config: DemoExecutionConfig,
        olap: DemoExecutionOlap,
        sink: ZeroNetworkSink,
    ) -> None:
        self.config = config
        self.olap = olap
        self.sink = sink

    # -- owner command path: zero LLM dependency (findings 042, 047) --------
    def apply_owner_command(
        self, command: OwnerCommand, now: Optional[datetime] = None
    ) -> dict[str, Any]:
        now = now or _utc_now()
        if command.issuer_id not in self.config.owner_issuer_allowlist:
            self.olap.record_command(command, False, "issuer_not_allowlisted")
            return {"accepted": False, "reason": "issuer_not_allowlisted"}
        if self.olap.nonce_seen(command.nonce):
            return {"accepted": False, "reason": "nonce_replay"}
        if command.expires_at <= now:
            self.olap.record_command(command, False, "expired")
            return {"accepted": False, "reason": "expired"}
        expected = self.config.command_phrases.get(command.command)
        if expected is None or command.exact_phrase != expected:
            self.olap.record_command(command, False, "phrase_mismatch")
            return {"accepted": False, "reason": "phrase_mismatch"}
        self.olap.record_command(command, True, None)
        result: dict[str, Any] = {"accepted": True}
        if command.command in ("hold", "kill"):
            self.olap.set_state("halt", command.command)
        emitted: list[dict[str, Any]] = []
        if command.command in ("flatten_all", "kill"):
            emitted.extend(self._emit_flatten_all(command.trace_id, now))
        if command.command in ("cancel_pending", "kill"):
            emitted.extend(self._emit_cancel_pending(command.trace_id, now))
        result["state"] = self.olap.get_state("halt", "none")
        result["emitted"] = emitted
        return result

    def _emit_flatten_all(self, trace_id: str, now: datetime) -> list[dict[str, Any]]:
        """Finding 047: flatten is an emitted zero-network intent, not a flag."""
        emitted = []
        for exposure in self.olap.open_exposures():
            intent = OrderIntentV2(
                object_id=f"oi2-flatten-{exposure['exposure_id']}",
                as_of=now,
                producer=self._PRODUCER,
                trace_id=trace_id,
                account_ref=self.config.account_fingerprint,
                asset_id=exposure["instrument"],
                venue=self.config.venue,
                instrument=exposure["instrument"],
                intent_class="risk_reducing",
                reduce_action="flatten",
                order_type="market",
                delta_units=-exposure["units_open"],
                idempotency_key=f"flatten:{exposure['exposure_id']}:{now.isoformat()}",
            )
            payload = self.sink.serialize(intent)
            with self.olap.atomic_unit():
                self.olap.record_decision(
                    intent.idempotency_key, "would_be_flatten", None,
                    intent.model_dump_json(), payload,
                    capability_evidence=exposure["capability_evidence"],
                )
                self.olap.append_lifecycle(ExecutionReportV2(
                    object_id=f"er-{intent.object_id}", as_of=now,
                    producer=self._PRODUCER, trace_id=trace_id,
                    order_intent_id=intent.object_id,
                    attempt_id=f"attempt-{intent.object_id}",
                    bracket_role="parent", state="requested",
                    requested_units=intent.delta_units,
                ))
            emitted.append({"kind": "flatten", "order_intent_id": intent.object_id,
                            "units": intent.delta_units})
        return emitted

    def _emit_cancel_pending(self, trace_id: str, now: datetime) -> list[dict[str, Any]]:
        """Finding 047: cancel is an emitted zero-network intent per pending entry."""
        emitted = []
        for pending in self.olap.active_reservation_intents():
            intent = OrderIntentV2(
                object_id=f"oi2-cancel-{pending['reservation_id']}",
                as_of=now,
                producer=self._PRODUCER,
                trace_id=trace_id,
                account_ref=self.config.account_fingerprint,
                asset_id="pending-entry",
                venue=self.config.venue,
                instrument="pending-entry",
                intent_class="risk_reducing",
                reduce_action="cancel",
                order_type="market",
                delta_units=0.0,
                idempotency_key=f"cancel:{pending['reservation_id']}:{now.isoformat()}",
            )
            payload = self.sink.serialize(intent)
            with self.olap.atomic_unit():
                self.olap.record_decision(
                    intent.idempotency_key, "would_be_cancel", None,
                    intent.model_dump_json(), payload,
                )
                self.olap.append_lifecycle(ExecutionReportV2(
                    object_id=f"er-{intent.object_id}", as_of=now,
                    producer=self._PRODUCER, trace_id=trace_id,
                    order_intent_id=intent.object_id,
                    attempt_id=f"attempt-{intent.object_id}",
                    bracket_role="parent", state="requested",
                    requested_units=0.0,
                ))
            emitted.append({"kind": "cancel",
                            "reservation_id": pending["reservation_id"]})
        return emitted

    # -- the decision path --------------------------------------------------
    def process_intent(
        self,
        intent: AssetIntent,
        capability: BrokerCapabilitySnapshot,
        *,
        equity: float,
        reference_price: float,
        quote_time: Optional[datetime] = None,
        instrument: str,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        now = now or _utc_now()
        quote_time = quote_time or now
        idem = f"{intent.object_id}:{intent.as_of.isoformat()}"

        replay = self.olap.recorded_decision(idem)
        if replay is not None:
            return replay

        def reject(reason: str) -> dict[str, Any]:
            with self.olap.atomic_unit():
                self.olap.record_decision(
                    idem, "rejected", reason, intent.model_dump_json(), None,
                    reference_price=reference_price,
                    quote_time=quote_time.isoformat(),
                    capability_evidence=capability.capability_evidence,
                )
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
        geometry = intent.risk_geometry
        if geometry is None or geometry.stop_price is None \
                or geometry.take_profit_price is None:
            return reject("missing_protection_geometry")

        # Finding 043: protection must be anchored to the decision reference.
        side_long = intent.target_exposure > 0
        sl, tp = geometry.stop_price, geometry.take_profit_price
        if side_long and not (sl < reference_price < tp):
            return reject(
                "protection_not_anchored: long requires "
                "stop < reference < take_profit"
            )
        if not side_long and not (sl > reference_price > tp):
            return reject(
                "protection_not_anchored: short requires "
                "stop > reference > take_profit"
            )

        entry = {cap.instrument: cap for cap in capability.instruments}.get(
            instrument
        )
        if not entry or not entry.tradeable:
            return reject("instrument_not_tradeable")
        if not side_long and not entry.shortable:
            return reject("short_not_supported")
        if not (entry.native_stop_loss and entry.native_take_profit
                and entry.native_bracket):
            return reject("native_protection_unavailable")

        # Finding 046: everything that can fail validation is constructed
        # BEFORE any reservation. The trial intent proves bracket geometry,
        # hashes and schema; only the unit size changes afterwards.
        stop_distance = abs(reference_price - sl)
        capability_hash = content_hash(capability.model_dump(mode="json"))
        reservation_id = f"rsv-{hashlib.sha256(idem.encode()).hexdigest()[:16]}"

        def build_order(units: float, risk: RiskEnvelope) -> OrderIntentV2:
            return OrderIntentV2(
                object_id=f"oi2-{reservation_id}",
                as_of=now,
                producer=self._PRODUCER,
                trace_id=intent.trace_id,
                account_ref=self.config.account_fingerprint,
                asset_id=intent.asset_id,
                venue=self.config.venue,
                instrument=instrument,
                intent_class="risk_increasing",
                order_type="market",
                delta_units=units if side_long else -units,
                protection=ProtectiveBracket(
                    stop_loss_price=sl, take_profit_price=tp
                ),
                risk=risk,
                capability_snapshot_hash=capability_hash,
                idempotency_key=idem,
                preflight={
                    "reference_price": reference_price,
                    "quote_time": quote_time.isoformat(),
                    "capability_evidence": capability.capability_evidence,
                },
            )

        trial_risk = RiskEnvelope(
            risk_fraction_at_stop=min(self.config.risk_fraction_at_stop, 1.0),
            gross_notional_fraction=1.0,
            margin_fraction=1.0,
            daily_loss_budget_fraction=self.config.daily_loss_budget_fraction,
            reservation_id=reservation_id,
        )
        try:
            build_order(entry.min_units, trial_risk)
        except Exception as error:  # noqa: BLE001 — every failure persists
            return reject(f"contract_validation_failed: {error}")

        # Finding 045: one BEGIN IMMEDIATE unit re-reads totals and writes
        # reservation + decision + lifecycle atomically; concurrent
        # instances serialize on the database lock.
        day = now.date().isoformat()
        try:
            with self.olap.atomic_unit():
                totals = self.olap.active_totals(day)
                if totals["positions"] + 1 > self.config.max_concurrent_positions:
                    raise DemoExecutionError("max_concurrent_positions")
                epsilon = 1e-12
                available_day = (
                    self.config.daily_loss_budget_fraction - totals["day_risk"]
                )
                available_gross = (
                    self.config.gross_notional_fraction_max - totals["gross"]
                )
                available_margin = (
                    self.config.margin_fraction_max - totals["margin"]
                )
                if available_day <= epsilon:
                    raise DemoExecutionError("daily_loss_budget_exhausted")
                if available_gross <= epsilon:
                    raise DemoExecutionError("gross_notional_cap")
                if available_margin <= epsilon:
                    raise DemoExecutionError("margin_cap")
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
                if units <= 0:
                    raise DemoExecutionError("no_viable_size")
                risk_fraction = (units * stop_distance) / equity
                notional_fraction = (units * reference_price) / equity
                margin_fraction = notional_fraction * float(
                    entry.margin_rate or 1.0
                )
                order = build_order(units, RiskEnvelope(
                    risk_fraction_at_stop=risk_fraction,
                    gross_notional_fraction=notional_fraction,
                    margin_fraction=min(margin_fraction, 1.0),
                    daily_loss_budget_fraction=(
                        self.config.daily_loss_budget_fraction
                    ),
                    reservation_id=reservation_id,
                ))
                payload = self.sink.serialize(order)
                self.olap.reserve(reservation_id, idem, day, risk_fraction,
                                  notional_fraction, margin_fraction)
                digest = self.olap.record_decision(
                    idem, "would_be_order", None, order.model_dump_json(),
                    payload, reference_price=reference_price,
                    quote_time=quote_time.isoformat(),
                    capability_evidence=capability.capability_evidence,
                )
                self.olap.append_lifecycle(ExecutionReportV2(
                    object_id=f"er-{reservation_id}", as_of=now,
                    producer=self._PRODUCER, trace_id=intent.trace_id,
                    order_intent_id=order.object_id,
                    attempt_id=f"attempt-{reservation_id}",
                    bracket_role="parent", state="requested",
                    requested_units=order.delta_units,
                ))
        except DemoExecutionError as error:
            return reject(str(error))
        except Exception as error:  # noqa: BLE001 — finding 046: the atomic
            # unit rolled back; the failure becomes a persisted rejection and
            # can never leak an active reservation.
            return reject(f"atomic_unit_failed: {type(error).__name__}: {error}")

        return {
            "outcome": "would_be_order",
            "reason": None,
            "payload": payload,
            "payload_sha256": digest,
            "order_intent_id": order.object_id,
            "reservation_id": reservation_id,
            "delta_units": order.delta_units,
            "capability_evidence": capability.capability_evidence,
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
        result: dict[str, Any] = {"state": report.state}
        reservation_id = (
            report.attempt_id[len("attempt-"):]
            if report.attempt_id.startswith("attempt-rsv-")
            else None
        )
        row = self.olap.reservation_row(reservation_id) if reservation_id else None
        with self.olap.atomic_unit():
            result["chain_hash"] = self.olap.append_lifecycle(report)
            if row is not None and row["state"] == "active":
                magnitude = abs(report.requested_units)
                if report.state in ("rejected", "cancelled", "expired"):
                    self.olap.release(reservation_id, "released")
                    result["reservation"] = "released"
                elif report.state == "partially_filled" and magnitude > 0:
                    # Finding 044: filled part becomes open exposure; the
                    # remaining entry keeps a scaled reservation. No double
                    # counting, no vanished risk.
                    filled_ratio = report.filled_units / magnitude
                    self.olap.open_exposure(
                        f"exp-{report.order_intent_id}",
                        report.order_intent_id,
                        "USD.CAD",
                        report.filled_units,
                        row["risk_fraction"] * filled_ratio,
                        row["gross_fraction"] * filled_ratio,
                        row["margin_fraction"] * filled_ratio,
                        "unknown",
                    )
                    self.olap.scale_reservation(
                        reservation_id, 1.0 - filled_ratio
                    )
                    result["reservation"] = "scaled"
                    result["exposure"] = "opened_partial"
                elif report.state == "filled":
                    self.olap.open_exposure(
                        f"exp-{report.order_intent_id}",
                        report.order_intent_id,
                        "USD.CAD",
                        report.filled_units,
                        row["risk_fraction"],
                        row["gross_fraction"],
                        row["margin_fraction"],
                        "unknown",
                    )
                    self.olap.release(reservation_id, "consumed")
                    result["reservation"] = "consumed"
                    result["exposure"] = "opened"
        if report.state in ("filled", "partially_filled") and \
                not protection_covers_filled(report):
            self.olap.set_state("halt", "hold")
            emitted = self._emit_flatten_all(report.trace_id, _utc_now())
            result["emergency"] = "unprotected_exposure_hold_and_flatten"
            result["emitted"] = emitted
        return result

    def apply_position_close(self, order_intent_id: str) -> dict[str, Any]:
        """Close the persisted exposure lifecycle (R4: order state never
        implies exposure state; this is the explicit position-close path)."""
        with self.olap.atomic_unit():
            self.olap.close_exposure(f"exp-{order_intent_id}")
        return {"exposure": "closed"}
