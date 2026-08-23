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
_ACTIONS = frozenset({"open_long", "open_short", "close"})


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
            unresolved = self.connection.execute(
                "SELECT command_id FROM execution_commands WHERE "
                "account_fingerprint=? AND symbol=? AND state IN ('pending','delivered')",
                (config.account_fingerprint, symbol),
            ).fetchone()
            if unresolved is not None:
                raise Mt5BridgeError("An unresolved MT5 route command already exists")
            if action in _OPEN_ACTIONS:
                count = self.connection.execute(
                    "SELECT COUNT(*) FROM execution_commands WHERE action LIKE 'open_%' "
                    "AND created_at>=? AND state!='failed'",
                    (f"{now.date().isoformat()}T00:00:00+00:00",),
                ).fetchone()[0]
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
            if row[0] in {"succeeded", "failed"}:
                if row[1] != serialized:
                    raise Mt5BridgeError("MT5 command result identity collision")
                return {"duplicate": True, "state": row[0]}
            state = "succeeded" if payload.success else "failed"
            self.connection.execute(
                "UPDATE execution_commands SET state=?,completed_at=?,result_json=? "
                "WHERE command_id=?",
                (state, _utc_now(), serialized, payload.command_id),
            )
            return {"duplicate": False, "state": state}

    def command_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT state,COUNT(*) FROM execution_commands GROUP BY state"
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
                "SELECT * FROM execution_commands WHERE"
                " account_fingerprint=? AND idempotency_key=?",
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
                "SELECT action,symbol,volume,stop_loss,take_profit,result_json "
                "FROM execution_commands WHERE state='succeeded' "
                "ORDER BY completed_at,created_at"
            ).fetchall()
        if snapshot is None:
            return {"available": False, "reason": "snapshot_missing"}

        payload = json.loads(str(snapshot[0]))
        positions = list(payload.get("positions") or [])
        orders = list(payload.get("orders") or [])
        authorized_by_ticket: dict[str, dict[str, Any]] = {}
        for row in commands:
            action, symbol = str(row[0]), str(row[1]).upper()
            if action == "close_position":
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
