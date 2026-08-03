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
            """)

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
        self, account_fingerprint: str, *, retry_seconds: int
    ) -> Optional[dict[str, Any]]:
        cutoff = time.time() - retry_seconds
        with self._lock, self.connection:
            rows = self.connection.execute(
                "SELECT * FROM execution_commands WHERE account_fingerprint=? "
                "AND state IN ('pending','delivered') ORDER BY created_at",
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

    async def authenticate(request: Request) -> str:
        body = await request.body()
        nonce = request.headers.get("X-LTS-Nonce", "")
        try:
            authenticator.verify(
                request.method, request.url.path,
                request.headers.get("X-LTS-Timestamp", ""), nonce,
                request.headers.get("X-LTS-Signature", ""), body,
            )
        except Mt5BridgeError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return nonce

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
        result["execution_enabled"] = True
        result["command_counts"] = store.command_counts()
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
    async def next_command(request: Request, account_fingerprint: str):
        nonce = await authenticate(request)
        account_allowed(account_fingerprint)
        command = store.next_command(
            config.account_fingerprint, retry_seconds=config.delivery_retry_seconds
        )
        body = b"" if command is None else _command_line(command).encode()
        headers = {
            "X-LTS-Response-Signature": _response_signature(secret, nonce, body),
            "Cache-Control": "no-store",
        }
        return PlainTextResponse(
            body, status_code=204 if command is None else 200, headers=headers
        )

    @app.post("/v2/commands/result")
    async def command_result(payload: ExecutionResultPayload, request: Request):
        await authenticate(request)
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
