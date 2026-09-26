"""Authenticated MT5 Demo execution bridge with a durable command outbox.

The existing read-only bridge remains unchanged. This v2 service accepts the
same heartbeat/snapshot/event contracts and adds a narrow, signed command
channel for an MT5 Demo EA. Only model-bound protected entries and exact route
closures can be queued; MT5 Live accounts are never accepted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import Field

from app.mt5_unknown_outcome import (
    BUDGET_RELEASING_STATES,
    CLOSED_BY_END_OF_RECORD,
    CLOSED_BY_NEXT_COMMAND,
    OPEN_STATES,
    EVENT_MIGRATION_CORRECTION,
    OUTCOME_SCHEMA,
    STATE_DELIVERED,
    STATE_EFFECT_UNKNOWN,
    STATE_FAILED,
    STATE_PENDING,
    STATE_SUCCEEDED,
    TERMINAL_STATES,
    UNRESOLVED_STATES,
    Outcome,
    OrderObservation,
    RECONCILABLE_STATES,
    RetainedRecordWindow,
    outcome_for_execution_result,
    outcome_from_observation,
)
from app.mt5_bridge_lab import (
    EVENT_SCHEMA,
    HEARTBEAT_SCHEMA,
    SNAPSHOT_SCHEMA,
    HeartbeatPayload,
    Mt5BridgeError,
    Mt5BridgeStore,
    Mt5RequestAuthenticator,
    SnapshotPayload,
    StrictModel,
    TradeEventPayload,
    _canonical_json,
    _expand_path,
    _utc_now,
)


EXECUTION_BRIDGE_VERSION = "lts.mt5.bridge.execution.v2"

# AUD-F2-20260823-306: DECLARED concurrent-position semantics for
# dual-symbol Demo operation. This is intentional per-route
# concurrency, not account-wide serialization:
# - per symbol: at most ONE unresolved queue command and at most one
#   open model position (each runner enforces max_concurrent_positions
#   on its own route);
# - account-wide: at most len(allowed_symbols) concurrent positions
#   (one per route), bounded Demo volume each;
# - the daily open-command budget is ACCOUNT-WIDE at this bridge and
#   is shared by both symbols by design (a busy ETH day reduces the
#   USDCAD entry budget — conservative and intentional);
# - one route's failures never block the other's queue.
DECLARED_CONCURRENCY = {
    "per_symbol_unresolved_commands": 1,
    "per_symbol_open_positions": 1,
    "account_wide_positions": "one_per_allowed_symbol",
    "daily_open_budget_scope": "account_wide_shared",
    "failure_isolation": "per_symbol",
}
COMMAND_SCHEMA = "lts.mt5.execution_command.v1"
RESULT_SCHEMA = "lts.mt5.execution_result.v1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9._-]{2,32}$")
_OPEN_ACTIONS = frozenset({"open_long", "open_short"})
#: The route-closing verb, spelled ONCE. Correction of 2026-09-26: a reader of
#: `exposure_reconciliation` branched on `"close_position"`, a verb this
#: vocabulary has never contained, so that branch had never executed and a
#: closed position's ticket was never retired from the authorized set. Naming
#: the close verb here, and pinning the partition below, is what stops the same
#: mismatch from being reintroduced silently.
_CLOSE_ACTIONS = frozenset({"close"})
_ACTIONS = _OPEN_ACTIONS | _CLOSE_ACTIONS
#: Every action is either an open or a close. If a third kind is ever added, this
#: assertion fails at import instead of leaving a branch quietly unreachable.
assert _ACTIONS == _OPEN_ACTIONS | _CLOSE_ACTIONS
assert not _OPEN_ACTIONS & _CLOSE_ACTIONS


@dataclass(frozen=True)
class Mt5ExecutionConfig:
    database_path: Path
    secret_env: str
    bind_host: str
    port: int
    max_clock_skew_seconds: int
    nonce_retention_seconds: int
    stale_heartbeat_seconds: int
    account_fingerprint: str
    allowed_symbols: tuple[str, ...]
    symbol_magics: dict[str, int]
    require_route_identity: bool
    max_volume: float
    max_open_commands_per_day: int
    delivery_retry_seconds: int

    @classmethod
    def load(cls, path: Path | str) -> "Mt5ExecutionConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema") != "lts.mt5.execution_bridge_config.v2":
            raise Mt5BridgeError("Unsupported MT5 execution bridge config")
        if data.get("environment") != "demo" or data.get("execution_enabled") is not True:
            raise Mt5BridgeError("MT5 execution v2 is Demo-only and explicitly enabled")
        forbidden = {"secret", "token", "password", "account_id", "login"}
        if forbidden.intersection(data):
            raise Mt5BridgeError("Credentials or raw account identifiers cannot be tracked")
        fingerprint = str(data.get("account_fingerprint", "")).lower()
        if len(fingerprint) < 12 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise Mt5BridgeError("A valid Demo account fingerprint is required")
        symbols = tuple(sorted({str(v).upper() for v in data.get("allowed_symbols", [])}))
        if not symbols or any(not _SYMBOL_RE.fullmatch(value) for value in symbols):
            raise Mt5BridgeError("allowed_symbols must contain strict MT5 symbols")
        # AUD-F2-20260823-301/304: a multi-symbol mandate DECLARES each
        # chart EA's magic; missing or duplicate values refuse — magic
        # is never guessed or defaulted in validation.
        raw_magics = data.get("symbol_magics") or {}
        magics = {}
        for key, value in raw_magics.items():
            symbol = str(key).upper()
            if symbol not in symbols:
                raise Mt5BridgeError(
                    f"symbol_magics declares unknown symbol {symbol}")
            if isinstance(value, bool) or not isinstance(value, int)                     or value <= 0:
                raise Mt5BridgeError(
                    f"symbol_magics[{symbol}] must be a positive int")
            magics[symbol] = value
        if len(symbols) > 1:
            missing = [s_ for s_ in symbols if s_ not in magics]
            if missing:
                raise Mt5BridgeError(
                    f"multi-symbol mandate requires symbol_magics for "
                    f"{missing}")
            if len(set(magics.values())) != len(magics):
                raise Mt5BridgeError(
                    "symbol_magics values must be unique per chart")
        max_volume = float(data.get("max_volume", 0))
        budget = int(data.get("max_open_commands_per_day", 0))
        if not 0 < max_volume <= 1.0 or not 1 <= budget <= 24:
            raise Mt5BridgeError("MT5 Demo volume or daily command budget is invalid")
        secret_env = str(data.get("secret_env", "")).strip()
        if not secret_env:
            raise Mt5BridgeError("secret_env is required")
        port = int(data.get("port", 8766))
        if not 1024 <= port <= 65535:
            raise Mt5BridgeError("MT5 bridge port must be between 1024 and 65535")
        require_route = bool(data.get("require_route_identity", False))
        return cls(
            database_path=_expand_path(str(data.get(
                "database_path", "~/.local/state/lts/mt5-bridge.sqlite"
            ))),
            secret_env=secret_env,
            bind_host=str(data.get("bind_host", "0.0.0.0")),
            port=port,
            max_clock_skew_seconds=max(5, int(data.get("max_clock_skew_seconds", 90))),
            nonce_retention_seconds=max(120, int(data.get("nonce_retention_seconds", 900))),
            stale_heartbeat_seconds=max(30, int(data.get("stale_heartbeat_seconds", 180))),
            account_fingerprint=fingerprint,
            allowed_symbols=symbols,
            symbol_magics=magics,
            require_route_identity=require_route,
            max_volume=max_volume,
            max_open_commands_per_day=budget,
            delivery_retry_seconds=max(5, int(data.get("delivery_retry_seconds", 30))),
        )

    def secret(self, environment: Optional[Mapping[str, str]] = None) -> bytes:
        source = environment if environment is not None else os.environ
        value = source.get(self.secret_env, "").strip()
        if len(value) < 32:
            raise Mt5BridgeError(f"{self.secret_env} must contain at least 32 characters")
        return value.encode()


class ExecutionResultPayload(StrictModel):
    schema_name: str = Field(alias="schema")
    command_id: str = Field(min_length=16, max_length=96)
    account_fingerprint: str = Field(min_length=12, max_length=64)
    success: bool
    result_code: int
    order_ticket: str = ""
    deal_ticket: str = ""
    message: str = Field(default="", max_length=256)
    observed_at: datetime


class Mt5ExecutionStore(Mt5BridgeStore):
    """Observation store plus one durable, idempotent command lifecycle."""

    def __init__(self, path: Path | str):
        super().__init__(path)
        with self._lock, self.connection:
            self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS execution_commands (
                command_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                account_fingerprint TEXT NOT NULL,
                action TEXT NOT NULL,
                symbol TEXT NOT NULL,
                volume REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                model_id TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL,
                config_sha256 TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                completed_at TEXT,
                result_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mt5_commands_route_state
                ON execution_commands(account_fingerprint,symbol,state,created_at);
            CREATE TABLE IF NOT EXISTS bars_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                received_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                digest TEXT NOT NULL
            );
            -- Owner grant 2026-09-26: the append-only outcome ledger. A
            -- command's terminal state is the LATEST record here, and a
            -- record is never edited: an EA result, a read-side
            -- reconciliation and a migration's correction of a historical
            -- mislabelling are three separate appended events, each with its
            -- own timestamps. execution_commands.state keeps the first
            -- terminal write for compatibility and is only ever a fallback
            -- for commands that predate this ledger.
            CREATE TABLE IF NOT EXISTS execution_command_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                evidence TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                consumes_budget_slot INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                observed_at TEXT,
                supersedes_state TEXT,
                outcome_json TEXT NOT NULL,
                source_schema TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mt5_outcomes_command
                ON execution_command_outcomes(command_id,id);
            """)

    def record_bars_evidence(self, *, symbol: str, payload: str,
                             digest: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO bars_evidence(symbol,received_at,"
                "payload_json,digest) VALUES (?,?,?,?)",
                (symbol.upper(), _utc_now(), payload, digest))

    def latest_bars_evidence(self, symbol: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.connection.execute(
                "SELECT received_at,payload_json,digest FROM "
                "bars_evidence WHERE symbol=? ORDER BY id DESC LIMIT 1",
                (symbol.upper(),),
            ).fetchone()
        if row is None:
            return None
        return {"received_at": row[0], "payload_json": row[1],
                "digest": row[2]}

    @staticmethod
    def _command_id(idempotency_key: str) -> str:
        return "mt5-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:40]

    # ------------------------------------------------- the outcome ledger
    #: The read path for a command's state: the LATEST appended outcome, and the
    #: legacy ``state`` column only for commands that never got one. Nothing is
    #: rewritten in place, so every reader must join through this.
    _EFFECTIVE_FROM = (
        " FROM execution_commands c"
        " LEFT JOIN execution_command_outcomes o"
        " ON o.id = (SELECT MAX(x.id) FROM execution_command_outcomes x"
        " WHERE x.command_id = c.command_id)"
    )
    _EFFECTIVE_STATE = "COALESCE(o.outcome,c.state)"

    def policy_inputs(
        self, config: "Mt5ExecutionConfig", *, symbol: str,
        now: Optional[datetime] = None,
    ) -> dict[str, int]:
        """The three counts ``mt5_policy_risk`` refuses an order on, measured
        from this store rather than passed in by a caller.

        ``entries_today`` is the number of budget slots HELD, so an unknown
        effect counts. Handing the policy interface a count derived any other
        way is how the defect reached the budget boundary in the first place.
        """
        moment = now or datetime.now(timezone.utc)
        day_start = f"{moment.date().isoformat()}T00:00:00+00:00"
        unknown = self.unreconciled_unknown_effects(config.account_fingerprint)
        placeholders = ",".join("?" for _ in sorted(OPEN_STATES))
        with self._lock:
            unresolved = int(self.connection.execute(
                f"SELECT COUNT(*){self._EFFECTIVE_FROM}"
                " WHERE c.account_fingerprint=? AND c.symbol=?"
                f" AND {self._EFFECTIVE_STATE} IN ({placeholders})",
                (config.account_fingerprint, symbol.upper(),
                 *sorted(OPEN_STATES)),
            ).fetchone()[0])
        return {
            "entries_today": self.daily_entry_slots_consumed(
                day_start=day_start,
                account_fingerprint=config.account_fingerprint),
            "unknown_effects": len(unknown),
            "unresolved_commands_on_route": unresolved,
        }

    def _append_outcome(
        self, command_id: str, outcome: Outcome, previous_state: Optional[str],
    ) -> int:
        """Append one outcome record. Called with the lock and inside a
        transaction by the writer; it never UPDATEs an existing record."""
        cursor = self.connection.execute(
            "INSERT INTO execution_command_outcomes"
            "(command_id,outcome,evidence,event_kind,consumes_budget_slot,"
            "recorded_at,observed_at,supersedes_state,outcome_json,"
            "source_schema) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                command_id, outcome.state, outcome.evidence, outcome.event_kind,
                1 if outcome.consumes_budget_slot else 0, _utc_now(),
                None if outcome.observed_at is None
                else outcome.observed_at.isoformat(),
                previous_state, _canonical_json(outcome.as_fact()),
                OUTCOME_SCHEMA,
            ),
        )
        return int(cursor.lastrowid)

    def effective_state(self, command_id: str) -> Optional[str]:
        """The command's state as the read path sees it, or ``None`` if the
        command does not exist."""
        with self._lock:
            row = self.connection.execute(
                f"SELECT {self._EFFECTIVE_STATE}{self._EFFECTIVE_FROM}"
                " WHERE c.command_id=?", (command_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def outcome_history(self, command_id: str) -> list[dict[str, Any]]:
        """Every appended outcome for one command, oldest first. The chain is
        the audit trail: a reconciliation never erases the unknown it exits."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM execution_command_outcomes WHERE command_id=?"
                " ORDER BY id", (command_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def unreconciled_unknown_effects(
        self, account_fingerprint: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Commands whose effect nobody has observed. Each one holds a daily
        budget slot and blocks new risk on its route until a read-side query
        says what the broker actually did."""
        query = (
            "SELECT c.command_id,c.symbol,c.action,c.created_at,c.completed_at,"
            f"o.evidence,o.recorded_at{self._EFFECTIVE_FROM}"
            f" WHERE {self._EFFECTIVE_STATE}=?"
        )
        params: list[Any] = [STATE_EFFECT_UNKNOWN]
        if account_fingerprint is not None:
            query += " AND c.account_fingerprint=?"
            params.append(account_fingerprint)
        with self._lock:
            rows = self.connection.execute(
                query + " ORDER BY c.created_at", tuple(params)
            ).fetchall()
        return [dict(row) for row in rows]

    def retained_record_window(
        self,
        *,
        command_id: str,
        account_fingerprint: str,
        max_gap_seconds: float,
    ) -> RetainedRecordWindow:
        """Build the retained evidence about one command's whole window.

        READ-ONLY, and it reads only this store's own streams. The window runs
        from the moment the command completed to the creation of the route's
        next command; where no next command exists it runs to the end of the
        record and says so, which is what makes the staleness condition in
        ``observation_from_retained_records`` bite.

        Observations are ordered by primary key, so the returned row ids are the
        evidence a reader can go back to. Nothing here decides anything: every
        admission condition lives in the outcome module.
        """
        with self._lock:
            return read_retained_record_window(
                self.connection, command_id=command_id,
                account_fingerprint=account_fingerprint,
                max_gap_seconds=max_gap_seconds)

    def venue_break_command_rows(self) -> list[dict[str, Any]]:
        """Every command as the venue-break derivation reads it.

        Three fields only: when it was created, its effective state and the
        venue's own machine code. No message is read, and no window is derived
        here — ``app/mt5_venue_break.py`` owns that and can refuse.
        """
        with self._lock:
            rows = self.connection.execute(
                f"SELECT c.created_at,{self._EFFECTIVE_STATE} AS state,"
                f"c.result_json{self._EFFECTIVE_FROM} ORDER BY c.created_at"
            ).fetchall()
        return [_venue_break_row(row) for row in rows]

    def daily_entry_slots_consumed(
        self, *, day_start: str, account_fingerprint: Optional[str] = None
    ) -> int:
        """How many of the day's entry slots are held.

        The inversion that fixes the defect: a slot is counted as consumed
        unless the effective state is one that PROVES no order exists. The old
        query asked ``state != 'failed'`` over a column where every unproven
        outcome had already been written as ``failed``, so a timed-out send
        freed the slot it may still have been holding.
        """
        placeholders = ",".join("?" for _ in sorted(BUDGET_RELEASING_STATES))
        query = (
            f"SELECT COUNT(*){self._EFFECTIVE_FROM}"
            " WHERE c.action LIKE 'open_%' AND c.created_at>=?"
            f" AND {self._EFFECTIVE_STATE} NOT IN ({placeholders})"
        )
        params: list[Any] = [day_start, *sorted(BUDGET_RELEASING_STATES)]
        if account_fingerprint is not None:
            query += " AND c.account_fingerprint=?"
            params.append(account_fingerprint)
        with self._lock:
            return int(self.connection.execute(
                query, tuple(params)).fetchone()[0])

    def enqueue(
        self,
        *,
        config: Mt5ExecutionConfig,
        idempotency_key: str,
        action: str,
        symbol: str,
        volume: float,
        stop_loss: float,
        take_profit: float,
        model_id: str,
        artifact_sha256: str,
        config_sha256: str,
        input_sha256: str,
    ) -> dict[str, Any]:
        action = action.lower()
        symbol = symbol.upper()
        if action not in _ACTIONS or symbol not in config.allowed_symbols:
            raise Mt5BridgeError("MT5 command action or symbol is outside the mandate")
        if not _MODEL_RE.fullmatch(model_id):
            raise Mt5BridgeError("Invalid source model id")
        for value in (artifact_sha256, config_sha256, input_sha256):
            if not _HASH_RE.fullmatch(value):
                raise Mt5BridgeError("MT5 commands require exact SHA-256 model evidence")
        if action in _OPEN_ACTIONS:
            if not 0 < volume <= config.max_volume:
                raise Mt5BridgeError("MT5 entry volume exceeds the Demo mandate")
            if action == "open_long" and not 0 < stop_loss < take_profit:
                raise Mt5BridgeError("MT5 long command lacks valid SL/TP geometry")
            if action == "open_short" and not 0 < take_profit < stop_loss:
                raise Mt5BridgeError("MT5 short command lacks valid SL/TP geometry")
        elif volume != 0 or stop_loss != 0 or take_profit != 0:
            raise Mt5BridgeError("MT5 close command must not carry entry geometry")
        command_id = self._command_id(idempotency_key)
        now = datetime.now(timezone.utc)
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT * FROM execution_commands WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return {**dict(existing), "replayed": True}
            # An unknown effect is UNRESOLVED on its route exactly as a pending
            # or delivered command is: the position may exist, so no new order
            # is placed on that route until a read-side query says otherwise.
            placeholders = ",".join("?" for _ in sorted(UNRESOLVED_STATES))
            unresolved = self.connection.execute(
                f"SELECT c.command_id,{self._EFFECTIVE_STATE}"
                f"{self._EFFECTIVE_FROM}"
                " WHERE c.account_fingerprint=? AND c.symbol=?"
                f" AND {self._EFFECTIVE_STATE} IN ({placeholders})",
                (config.account_fingerprint, symbol,
                 *sorted(UNRESOLVED_STATES)),
            ).fetchone()
            if unresolved is not None:
                if str(unresolved[1]) == STATE_EFFECT_UNKNOWN:
                    raise Mt5BridgeError(
                        "An unresolved MT5 effect on this route has never been "
                        "observed; it is reconciled by a read-side broker query "
                        "or by the retained record under its own conditions "
                        "before any new order exists")
                raise Mt5BridgeError("An unresolved MT5 route command already exists")
            if action in _OPEN_ACTIONS:
                # The daily budget counts every slot that is HELD. An unknown
                # effect holds its slot, because the position may exist;
                # releasing one requires a positive observation that no order
                # exists, never the absence of a confirmation.
                count = self.daily_entry_slots_consumed(
                    day_start=f"{now.date().isoformat()}T00:00:00+00:00")
                if count >= config.max_open_commands_per_day:
                    raise Mt5BridgeError("MT5 daily Demo entry budget is exhausted")
            self.connection.execute(
                "INSERT INTO execution_commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,"
                "'pending',?,NULL,NULL,NULL)",
                (
                    command_id, idempotency_key, config.account_fingerprint,
                    action, symbol, volume, stop_loss, take_profit, model_id,
                    artifact_sha256, config_sha256, input_sha256, now.isoformat(),
                ),
            )
            return dict(self.connection.execute(
                "SELECT * FROM execution_commands WHERE command_id=?", (command_id,)
            ).fetchone())

    def next_command(
        self, account_fingerprint: str, *, retry_seconds: int,
        symbol: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Deliver the oldest deliverable command for the route.

        Dual-symbol order 2026-08-23: with two EA instances polling the
        same account, delivery MUST be symbol-scoped — an EA may only
        ever receive commands for its own chart symbol, or a USDCAD
        command would be "stolen" by the ETHUSD chart EA and executed
        there. ``symbol=None`` remains valid only for single-symbol
        mandates (resolved by the endpoint, never guessed here).
        """
        cutoff = time.time() - retry_seconds
        with self._lock, self.connection:
            if symbol is not None:
                rows = self.connection.execute(
                    "SELECT * FROM execution_commands WHERE "
                    "account_fingerprint=? AND symbol=? "
                    "AND state IN ('pending','delivered') "
                    "ORDER BY created_at",
                    (account_fingerprint, symbol.upper()),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT * FROM execution_commands WHERE "
                    "account_fingerprint=? "
                    "AND state IN ('pending','delivered') "
                    "ORDER BY created_at",
                    (account_fingerprint,),
                ).fetchall()
            selected = None
            for row in rows:
                delivered = row[14]
                if row[12] == "pending" or (
                    delivered and datetime.fromisoformat(delivered).timestamp() <= cutoff
                ):
                    selected = row
                    break
            if selected is None:
                return None
            now = _utc_now()
            self.connection.execute(
                "UPDATE execution_commands SET state='delivered',delivered_at=? "
                "WHERE command_id=?", (now, selected[0]),
            )
            return dict(self.connection.execute(
                "SELECT * FROM execution_commands WHERE command_id=?", (selected[0],)
            ).fetchone())

    def complete(self, payload: ExecutionResultPayload) -> dict[str, Any]:
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT state,result_json FROM execution_commands WHERE command_id=? "
                "AND account_fingerprint=?",
                (payload.command_id, payload.account_fingerprint.lower()),
            ).fetchone()
            if row is None:
                raise Mt5BridgeError("Unknown MT5 command result")
            value = payload.model_dump(by_alias=True, mode="json")
            serialized = _canonical_json(value)
            if row[0] in TERMINAL_STATES:
                if row[1] != serialized:
                    raise Mt5BridgeError("MT5 command result identity collision")
                return {"duplicate": True,
                        "state": self.effective_state(payload.command_id),
                        "recorded_state": row[0]}
            # THREE outcomes, not two. ``failed`` from here on means the
            # venue's own machine code proves no order exists; a send whose
            # effect was never observed is ``effect_unknown`` and stays that
            # way until a read-side query reconciles it.
            outcome = outcome_for_execution_result(value)
            self.connection.execute(
                "UPDATE execution_commands SET state=?,completed_at=?,result_json=? "
                "WHERE command_id=?",
                (outcome.state, _utc_now(), serialized, payload.command_id),
            )
            self._append_outcome(payload.command_id, outcome, row[0])
            return {"duplicate": False, "state": outcome.state,
                    "evidence": outcome.evidence,
                    "consumes_budget_slot": outcome.consumes_budget_slot}

    def reconcile_unknown_effect(
        self,
        *,
        command_id: str,
        account_fingerprint: str,
        observation: OrderObservation,
    ) -> dict[str, Any]:
        """The ONLY exit from ``effect_unknown``.

        A retry cannot take it, a timeout cannot take it and an operator's
        assumption cannot take it: the only argument accepted is an
        ``OrderObservation``, which refuses to exist unless it names a declared
        READ-side query and carries the time the state was observed. An
        unanswered query raises ``ReconciliationInconclusive`` and the command
        keeps both its unknown state and its budget slot.

        Two sources are admissible, per the correction of 2026-09-26: a live
        read-side broker query, or the lane's retained account-snapshot and
        trade-event streams built by ``retained_record_window`` and admitted by
        ``observation_from_retained_records`` — which refuses whenever its
        window is uncovered, holds exposure, holds a transaction, is stale or is
        sampled across a gap.

        The exit is APPENDED as its own event. The original unknown record and
        the ``state`` column are left exactly as they were written.
        """
        with self._lock, self.connection:
            row = self.connection.execute(
                f"SELECT c.command_id,{self._EFFECTIVE_STATE}"
                f"{self._EFFECTIVE_FROM}"
                " WHERE c.command_id=? AND c.account_fingerprint=?",
                (command_id, account_fingerprint.lower()),
            ).fetchone()
            if row is None:
                raise Mt5BridgeError("Unknown MT5 command reconciliation")
            current = str(row[1])
            if current not in RECONCILABLE_STATES:
                raise Mt5BridgeError(
                    f"only an MT5 command whose effect is unknown is "
                    f"reconciled; this one is {current}")
            outcome = outcome_from_observation(observation)
            record_id = self._append_outcome(command_id, outcome, current)
            return {
                "command_id": command_id,
                "previous_state": current,
                "state": outcome.state,
                "evidence": outcome.evidence,
                "consumes_budget_slot": outcome.consumes_budget_slot,
                "outcome_record_id": record_id,
                "observed_at": observation.observed_at.isoformat(),
                "query": observation.query,
            }

    def record_migration_correction(
        self, *, command_id: str, outcome: Outcome, previous_state: str,
    ) -> int:
        """Append one migration correction. Used only by
        ``tools/mt5_unknown_outcome_migration.py``; refuses any other event
        kind so a live path cannot relabel a command through this door."""
        if outcome.event_kind != EVENT_MIGRATION_CORRECTION:
            raise Mt5BridgeError(
                "only a migration correction is appended through this path")
        with self._lock, self.connection:
            return self._append_outcome(command_id, outcome, previous_state)

    def command_counts(self) -> dict[str, int]:
        """Counts by EFFECTIVE state, so a reconciled or corrected command is
        counted as what it is now rather than as what was first written."""
        with self._lock:
            rows = self.connection.execute(
                f"SELECT {self._EFFECTIVE_STATE} AS s,COUNT(*)"
                f"{self._EFFECTIVE_FROM} GROUP BY s"
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def command_for_idempotency(
        self, account_fingerprint: str, idempotency_key: str
    ) -> Optional[dict[str, Any]]:
        """Read the one durable command bound to an account and decision.

        The account predicate is deliberate: an idempotency collision or a
        caller using the wrong account must look absent instead of exposing
        or adopting another account's command state.
        """
        with self._lock:
            row = self.connection.execute(
                f"SELECT c.*,{self._EFFECTIVE_STATE} AS effective_state,"
                f"o.evidence AS outcome_evidence{self._EFFECTIVE_FROM}"
                " WHERE c.account_fingerprint=? AND c.idempotency_key=?",
                (account_fingerprint.lower(), idempotency_key),
            ).fetchone()
        return dict(row) if row is not None else None

    def exposure_reconciliation(self) -> dict[str, Any]:
        """Match current MT5 exposure to completed, model-bound commands."""
        with self._lock:
            snapshot = self.connection.execute(
                "SELECT payload_json FROM account_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            commands = self.connection.execute(
                "SELECT c.action,c.symbol,c.volume,c.stop_loss,c.take_profit,"
                f"c.result_json{self._EFFECTIVE_FROM}"
                f" WHERE {self._EFFECTIVE_STATE}='{STATE_SUCCEEDED}'"
                " ORDER BY c.completed_at,c.created_at"
            ).fetchall()
        if snapshot is None:
            return {"available": False, "reason": "snapshot_missing"}

        payload = json.loads(str(snapshot[0]))
        positions = list(payload.get("positions") or [])
        orders = list(payload.get("orders") or [])
        authorized_by_ticket: dict[str, dict[str, Any]] = {}
        for row in commands:
            action, symbol = str(row[0]), str(row[1]).upper()
            if action in _CLOSE_ACTIONS:
                authorized_by_ticket = {
                    ticket: command
                    for ticket, command in authorized_by_ticket.items()
                    if command["symbol"] != symbol
                }
                continue
            if action not in _OPEN_ACTIONS or not row[5]:
                continue
            result = json.loads(str(row[5]))
            ticket = str(result.get("order_ticket") or "")
            if not ticket:
                continue
            authorized_by_ticket[ticket] = {
                "symbol": symbol,
                "side": "long" if action == "open_long" else "short",
                "volume": float(row[2]),
                "stop_loss": float(row[3]),
                "take_profit": float(row[4]),
            }

        authorized = 0
        unexpected: list[str] = []
        for position in positions:
            ticket = str(position.get("ticket") or "")
            command = authorized_by_ticket.get(ticket)
            matches = command is not None and all(
                (
                    str(position.get("symbol") or "").upper() == command["symbol"],
                    str(position.get("side") or "").lower() == command["side"],
                    float(position.get("volume") or 0) == command["volume"],
                    float(position.get("stop_loss") or 0) == command["stop_loss"],
                    float(position.get("take_profit") or 0) == command["take_profit"],
                    command["stop_loss"] > 0,
                    command["take_profit"] > 0,
                )
            )
            if matches:
                authorized += 1
            else:
                unexpected.append(ticket or "missing_ticket")
        return {
            "available": True,
            "positions_total": len(positions),
            "orders_total": len(orders),
            "authorized_positions": authorized,
            "unexpected_positions": len(unexpected),
            "unexpected_orders": len(orders),
            "all_authorized": not unexpected and not orders,
        }


# ============================================ the retained-record read path
# Correction of 2026-09-26. These are module-level and take a CONNECTION, not a
# store, for one reason: the second admissible reconciliation source has to be
# readable from a `file:<path>?mode=ro` connection that cannot write, so a report
# against a live database never risks creating a table or a journal. The store
# method delegates here under its own lock.


def _venue_break_row(row: Any) -> dict[str, Any]:
    """One command as the venue-break derivation reads it: when, what state, and
    the venue's machine code. The message is never read."""
    code: Optional[int] = None
    raw = row["result_json"]
    if raw:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, Mapping):
            candidate = value.get("result_code")
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                code = candidate
    return {"created_at": row["created_at"], "state": str(row["state"]),
            "result_code": code}


def read_venue_break_command_rows(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Every command, read for the venue-break derivation. Read-only.

    A database written by pre-fix code has no outcome ledger at all, and a
    read-only connection must not create one. Where the ledger is absent the
    legacy ``state`` column is read directly — which is exactly what the
    effective-state join would fall back to anyway.
    """
    connection.row_factory = sqlite3.Row
    ledger = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND"
        " name='execution_command_outcomes'"
    ).fetchone()
    if ledger is None:
        rows = connection.execute(
            "SELECT created_at,state,result_json FROM execution_commands"
            " ORDER BY created_at"
        ).fetchall()
    else:
        rows = connection.execute(
            f"SELECT c.created_at,{Mt5ExecutionStore._EFFECTIVE_STATE} AS state,"
            f"c.result_json{Mt5ExecutionStore._EFFECTIVE_FROM}"
            f" ORDER BY c.created_at"
        ).fetchall()
    return [_venue_break_row(row) for row in rows]


def _bracket(row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {"row_id": row["id"], "observed_at": row["received_at"],
            "positions_total": row["positions_total"],
            "orders_total": row["orders_total"]}


def read_retained_record_window(
    connection: sqlite3.Connection,
    *,
    command_id: str,
    account_fingerprint: str,
    max_gap_seconds: float,
) -> RetainedRecordWindow:
    """Collect the retained evidence about one command's whole window.

    The window runs from the moment the command completed to the creation of the
    route's next command; with no next command it runs to the end of the record
    and SAYS SO, which is what makes the staleness condition in
    ``observation_from_retained_records`` bite on a silent bridge.

    This function decides nothing. It gathers rows and names them by primary key
    so a reader can go back to each one; every admission condition lives in
    ``app/mt5_unknown_outcome.py`` and every one of them can refuse.
    """
    connection.row_factory = sqlite3.Row
    fingerprint = account_fingerprint.lower()
    command = connection.execute(
        "SELECT command_id,symbol,completed_at,created_at FROM "
        "execution_commands WHERE command_id=? AND account_fingerprint=?",
        (command_id, fingerprint),
    ).fetchone()
    if command is None:
        raise Mt5BridgeError("Unknown MT5 command window")
    start = command["completed_at"] or command["created_at"]
    if start is None:
        raise Mt5BridgeError(
            "an MT5 command with no completion has no settled window")
    following = connection.execute(
        "SELECT created_at FROM execution_commands WHERE "
        "account_fingerprint=? AND symbol=? AND created_at>? "
        "ORDER BY created_at LIMIT 1",
        (fingerprint, command["symbol"], start),
    ).fetchone()
    if following is None:
        closed_by = CLOSED_BY_END_OF_RECORD
        end_row = connection.execute(
            "SELECT MAX(received_at) FROM account_snapshots WHERE "
            "account_fingerprint=?", (fingerprint,),
        ).fetchone()
        end = None if end_row is None else end_row[0]
        if end is None or end <= start:
            raise Mt5BridgeError(
                "the retained record holds no observation after this command "
                "completed")
    else:
        closed_by = CLOSED_BY_NEXT_COMMAND
        end = following["created_at"]
    observations = [
        {"row_id": row["id"], "observed_at": row["received_at"],
         "positions_total": row["positions_total"],
         "orders_total": row["orders_total"]}
        for row in connection.execute(
            "SELECT id,received_at,positions_total,orders_total FROM "
            "account_snapshots WHERE account_fingerprint=? AND received_at>? "
            "AND received_at<? ORDER BY id", (fingerprint, start, end),
        ).fetchall()
    ]
    before = connection.execute(
        "SELECT id,received_at,positions_total,orders_total FROM "
        "account_snapshots WHERE account_fingerprint=? AND received_at<=? "
        "ORDER BY id DESC LIMIT 1", (fingerprint, start),
    ).fetchone()
    after = connection.execute(
        "SELECT id,received_at,positions_total,orders_total FROM "
        "account_snapshots WHERE account_fingerprint=? AND received_at>=? "
        "ORDER BY id LIMIT 1", (fingerprint, end),
    ).fetchone()
    events = [
        {"event_id": row["event_id"], "event_type": row["event_type"],
         "observed_at": row["received_at"]}
        for row in connection.execute(
            "SELECT event_id,event_type,received_at FROM trade_events WHERE "
            "account_fingerprint=? AND received_at>? AND received_at<? "
            "ORDER BY received_at", (fingerprint, start, end),
        ).fetchall()
    ]
    return RetainedRecordWindow(
        command_id=command_id,
        window_start=start,
        window_end=end,
        closed_by=closed_by,
        observations=observations,
        trade_events=events,
        max_gap_seconds=max_gap_seconds,
        boundary_before=_bracket(before),
        boundary_after=_bracket(after),
        detail={"symbol": command["symbol"]},
    )


def _command_line(command: Mapping[str, Any]) -> str:
    values = (
        "v1", command["command_id"], command["action"], command["symbol"],
        format(float(command["volume"]), ".8f"),
        format(float(command["stop_loss"]), ".10f"),
        format(float(command["take_profit"]), ".10f"), command["model_id"],
        command["artifact_sha256"], command["config_sha256"],
        command["input_sha256"],
    )
    return "|".join(str(value) for value in values)


def _response_signature(secret: bytes, request_nonce: str, body: bytes) -> str:
    digest = hashlib.sha256(body).hexdigest()
    return hmac.new(secret, f"{request_nonce}\n{digest}".encode(), hashlib.sha256).hexdigest()


def create_mt5_execution_app(
    config: Mt5ExecutionConfig, store: Mt5ExecutionStore, secret: bytes
) -> FastAPI:
    app = FastAPI(title="LTS MT5 Demo Execution Bridge", docs_url=None, redoc_url=None)
    authenticator = Mt5RequestAuthenticator(
        secret, store,
        max_clock_skew_seconds=config.max_clock_skew_seconds,
        nonce_retention_seconds=config.nonce_retention_seconds,
    )

    async def authenticate(request: Request) -> tuple[str, str]:
        body = await request.body()
        nonce = request.headers.get("X-LTS-Nonce", "")
        route_identity = request.headers.get("X-LTS-Route-Identity", "")
        # Practical order item 5 (2026-08-23): the EXPLICIT retirement
        # switch for the legacy five-line HMAC. Once both chart EAs
        # sign the route identity, the operator sets
        # require_route_identity=true and the legacy framing is dead
        # on EVERY signed endpoint — permanently; a later release
        # deletes the optional path outright.
        if config.require_route_identity and not route_identity:
            raise HTTPException(
                status_code=401,
                detail="legacy unbound-route signatures are retired "
                       "on this bridge; a signed route identity is "
                       "required")
        try:
            authenticator.verify(
                request.method, request.url.path,
                request.headers.get("X-LTS-Timestamp", ""), nonce,
                request.headers.get("X-LTS-Signature", ""), body,
                route_identity=route_identity,
            )
        except Mt5BridgeError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return nonce, route_identity

    def parse_route_identity(route_identity: str) -> dict[str, Any]:
        """AUD-F2-20260823-301: 'v2|<account>|<symbol>|<magic>'. The
        signature already covers this string; parsing binds it to the
        actual route. A multi-symbol mandate REQUIRES it."""
        if not route_identity:
            if len(config.allowed_symbols) > 1:
                raise HTTPException(
                    status_code=401,
                    detail="multi-symbol mandate requires a signed "
                           "route identity")
            return {}
        parts = route_identity.split("|")
        if len(parts) != 4 or parts[0] != "v2":
            raise HTTPException(status_code=401,
                                detail="malformed route identity")
        account, symbol, magic_raw = (parts[1].lower(),
                                      parts[2].upper(), parts[3])
        if account != config.account_fingerprint:
            raise HTTPException(status_code=403,
                                detail="route identity account mismatch")
        if symbol not in config.allowed_symbols:
            raise HTTPException(status_code=403,
                                detail="route identity symbol outside "
                                       "the mandate")
        try:
            magic = int(magic_raw)
        except ValueError:
            raise HTTPException(status_code=401,
                                detail="malformed route identity magic")
        expected = config.symbol_magics.get(symbol)
        if expected is not None and magic != expected:
            raise HTTPException(
                status_code=403,
                detail="route identity magic does not match the "
                       "declared chart magic")
        return {"account": account, "symbol": symbol, "magic": magic}

    def refuse_duplicate_query_keys(request: Request) -> None:
        raw = request.url.query or ""
        keys = [pair.split("=", 1)[0] for pair in raw.split("&") if pair]
        if len(keys) != len(set(keys)):
            raise HTTPException(status_code=400,
                                detail="duplicate query keys refused")

    def account_allowed(fingerprint: str) -> None:
        if fingerprint.lower() != config.account_fingerprint:
            raise HTTPException(status_code=403, detail="Unapproved MT5 Demo account")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok", "bridge_version": EXECUTION_BRIDGE_VERSION,
            "environment": "demo", "execution_enabled": True,
            "command_counts": store.command_counts(),
        }

    @app.get("/v1/status")
    def status() -> dict[str, Any]:
        result = store.operational_status(config.stale_heartbeat_seconds)
        result["bridge_version"] = EXECUTION_BRIDGE_VERSION
        result["read_only"] = False
        result["execution_enabled"] = True
        result["command_counts"] = store.command_counts()
        result["exposure_reconciliation"] = store.exposure_reconciliation()
        result["declared_concurrency"] = DECLARED_CONCURRENCY
        # Owner grant 2026-09-26: an effect nobody observed is visible in the
        # status, holds its daily budget slot and blocks its route until a
        # read-side query reconciles it.
        unknown = store.unreconciled_unknown_effects()
        result["unreconciled_unknown_effects"] = {
            "count": len(unknown),
            "commands": unknown,
            "exit": "read_side_broker_query_only",
        }
        return result

    @app.post("/v1/heartbeat")
    async def heartbeat(payload: HeartbeatPayload, request: Request) -> dict[str, Any]:
        await authenticate(request)
        account_allowed(payload.account_fingerprint)
        if payload.schema_name != HEARTBEAT_SCHEMA or payload.environment != "demo":
            raise HTTPException(status_code=422, detail="Invalid Demo heartbeat")
        store.record_heartbeat(payload)
        return {"accepted": True, "read_only": False, "server_time": _utc_now()}

    @app.post("/v1/snapshot")
    async def snapshot(payload: SnapshotPayload, request: Request) -> dict[str, Any]:
        await authenticate(request)
        account_allowed(payload.account_fingerprint)
        if payload.schema_name != SNAPSHOT_SCHEMA:
            raise HTTPException(status_code=422, detail="Invalid snapshot")
        return {"accepted": True, "snapshot_id": store.record_snapshot(payload),
                "read_only": False}

    @app.post("/v1/events")
    async def event(payload: TradeEventPayload, request: Request) -> dict[str, Any]:
        await authenticate(request)
        account_allowed(payload.account_fingerprint)
        if payload.schema_name != EVENT_SCHEMA:
            raise HTTPException(status_code=422, detail="Invalid event")
        inserted = store.record_event(payload)
        return {"accepted": True, "duplicate": not inserted, "read_only": False}

    @app.get("/v2/commands/next")
    async def next_command(
        request: Request, account_fingerprint: str, symbol: str = ""
    ):
        nonce, route_identity = await authenticate(request)
        refuse_duplicate_query_keys(request)
        identity = parse_route_identity(route_identity)
        account_allowed(account_fingerprint)
        if identity:
            if identity["account"] != account_fingerprint.lower():
                raise HTTPException(
                    status_code=403,
                    detail="signed route identity does not match the "
                           "query account (post-signing mutation)")
            if symbol and identity["symbol"] != symbol.strip().upper():
                raise HTTPException(
                    status_code=403,
                    detail="signed route identity does not match the "
                           "query symbol (post-signing mutation)")
        # Dual-symbol order 2026-08-23: delivery is symbol-scoped. A
        # multi-symbol mandate REQUIRES the polling EA to declare its
        # chart symbol; a single-symbol mandate resolves an absent
        # declaration to that one symbol (deterministic, not a guess).
        requested = symbol.strip().upper()
        if requested:
            if requested not in config.allowed_symbols:
                raise HTTPException(
                    status_code=403,
                    detail="symbol outside the mandate")
        elif len(config.allowed_symbols) == 1:
            requested = config.allowed_symbols[0]
        else:
            raise HTTPException(
                status_code=400,
                detail="multi-symbol mandate requires the polling "
                       "EA to declare its chart symbol")
        command = store.next_command(
            config.account_fingerprint,
            retry_seconds=config.delivery_retry_seconds,
            symbol=requested,
        )
        body = b"" if command is None else _command_line(command).encode()
        headers = {
            "X-LTS-Response-Signature": _response_signature(secret, nonce, body),
            "Cache-Control": "no-store",
        }
        return PlainTextResponse(
            body, status_code=204 if command is None else 200, headers=headers
        )

    @app.post("/v2/evidence/bars")
    async def bars_evidence(request: Request):
        """AUD-F2-20260823-302: signed CopyRates evidence envelope.

        The request HMAC (with the route identity bound into the
        canonical) is the attestation that this capture came from the
        EA holding the secret, on the declared account, chart symbol
        and magic. The envelope is stored verbatim; the preflight
        consumes ONLY stored envelopes."""
        _nonce, route_identity = await authenticate(request)
        identity = parse_route_identity(route_identity)
        if not identity:
            raise HTTPException(
                status_code=401,
                detail="bars evidence requires a signed route identity")
        body = await request.body()
        try:
            doc = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=422,
                                detail="malformed evidence body")
        if doc.get("schema") != "lts.mt5.bars_evidence.v1":
            raise HTTPException(status_code=422,
                                detail="unsupported evidence schema")
        if str(doc.get("account_fingerprint", "")).lower() != (
                config.account_fingerprint):
            raise HTTPException(status_code=403,
                                detail="evidence account mismatch")
        if str(doc.get("symbol", "")).upper() != identity["symbol"]:
            raise HTTPException(
                status_code=403,
                detail="evidence symbol does not match the signed "
                       "route identity")
        digest = hashlib.sha256(body).hexdigest()
        store.record_bars_evidence(
            symbol=identity["symbol"],
            payload=body.decode("utf-8"), digest=digest)
        return {"stored": True, "digest": digest}

    @app.post("/v2/commands/result")
    async def command_result(payload: ExecutionResultPayload, request: Request):
        _nonce, route_identity = await authenticate(request)
        identity = parse_route_identity(route_identity)
        if identity:
            with store._lock:
                row = store.connection.execute(
                    "SELECT symbol FROM execution_commands WHERE "
                    "command_id=?", (payload.command_id,),
                ).fetchone()
            if row is not None and str(row[0]).upper() != (
                    identity["symbol"]):
                raise HTTPException(
                    status_code=403,
                    detail="an EA may only acknowledge or fail "
                           "commands for its own signed route symbol")
        account_allowed(payload.account_fingerprint)
        if payload.schema_name != RESULT_SCHEMA:
            raise HTTPException(status_code=422, detail="Invalid result schema")
        try:
            result = store.complete(payload)
        except Mt5BridgeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"accepted": True, **result}

    @app.get("/v1/report")
    async def report(request: Request) -> dict[str, Any]:
        await authenticate(request)
        result = store.report(config.stale_heartbeat_seconds)
        result["command_counts"] = store.command_counts()
        return result

    return app
