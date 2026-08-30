"""Authenticated MT5 demo bridge and restart-safe observation store."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = "lts.mt5.bridge_olap.v1"
BRIDGE_VERSION = "lts.mt5.bridge.readonly.v1"
HEARTBEAT_SCHEMA = "lts.mt5.heartbeat.v1"
SNAPSHOT_SCHEMA = "lts.mt5.snapshot.v1"
EVENT_SCHEMA = "lts.mt5.trade_event.v1"
STATUS_SCHEMA = "lts.mt5.operational_status.v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class Mt5BridgeError(RuntimeError):
    """Raised when the MT5 bridge contract fails closed."""


@dataclass(frozen=True)
class Mt5BridgeConfig:
    database_path: Path
    secret_env: str
    environment: str
    read_only: bool
    bind_host: str
    port: int
    max_clock_skew_seconds: int
    nonce_retention_seconds: int
    stale_heartbeat_seconds: int
    allowed_account_fingerprints: tuple[str, ...]

    @classmethod
    def load(cls, path: Path | str) -> "Mt5BridgeConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema") != "lts.mt5.bridge_config.v1":
            raise Mt5BridgeError("Unsupported or missing MT5 bridge config schema")
        if data.get("environment") != "demo":
            raise Mt5BridgeError("The initial MT5 bridge is demo-only")
        if data.get("read_only") is not True:
            raise Mt5BridgeError("The initial MT5 bridge must remain read-only")
        secret_env = str(data.get("secret_env", "")).strip()
        if not secret_env:
            raise Mt5BridgeError("secret_env is required")
        forbidden = {"secret", "token", "password", "account_id", "login"}
        if forbidden.intersection(data):
            raise Mt5BridgeError("Credentials or account identifiers cannot be tracked")
        port = int(data.get("port", 8766))
        if not 1024 <= port <= 65535:
            raise Mt5BridgeError("MT5 bridge port must be between 1024 and 65535")
        fingerprints = tuple(
            sorted(
                {
                    str(value).lower()
                    for value in data.get("allowed_account_fingerprints", [])
                    if str(value).strip()
                }
            )
        )
        for value in fingerprints:
            if len(value) < 12 or any(char not in "0123456789abcdef" for char in value):
                raise Mt5BridgeError("Invalid allowed account fingerprint")
        return cls(
            database_path=_expand_path(
                str(data.get("database_path", "~/.local/state/lts/mt5-bridge.sqlite"))
            ),
            secret_env=secret_env,
            environment="demo",
            read_only=True,
            bind_host=str(data.get("bind_host", "0.0.0.0")),
            port=port,
            max_clock_skew_seconds=max(
                5, int(data.get("max_clock_skew_seconds", 90))
            ),
            nonce_retention_seconds=max(
                120, int(data.get("nonce_retention_seconds", 900))
            ),
            stale_heartbeat_seconds=max(
                30, int(data.get("stale_heartbeat_seconds", 180))
            ),
            allowed_account_fingerprints=fingerprints,
        )

    def secret(self, environment: Optional[Mapping[str, str]] = None) -> bytes:
        source = environment if environment is not None else os.environ
        value = source.get(self.secret_env, "").strip()
        if len(value) < 32:
            raise Mt5BridgeError(
                f"{self.secret_env} must contain at least 32 characters"
            )
        return value.encode("utf-8")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HeartbeatPayload(StrictModel):
    schema_name: str = Field(alias="schema")
    adapter_version: str
    account_fingerprint: str = Field(min_length=12, max_length=64)
    server_fingerprint: str = Field(min_length=12, max_length=64)
    environment: str
    connected: bool
    trade_allowed: bool
    terminal_build: int = Field(ge=0)
    terminal_ping_ms: float = Field(ge=0)
    observed_at: datetime


class PositionSnapshot(StrictModel):
    ticket: str
    symbol: str
    side: str
    volume: float = Field(gt=0)
    price_open: float
    # WP3 C6: the EA has always emitted POSITION_TIME as
    # ``time_open_unix`` and app/mt5_model_runner.py has always read
    # it, but this strict model did not declare it. With
    # extra="forbid" the endpoint answered 422 and the WHOLE snapshot
    # was rejected, so no MT5 snapshot carrying a position was ever
    # stored and the runner's holding-bars feature could never
    # resolve. Declared here with the range POSITION_TIME actually
    # has: a positive epoch second.
    time_open_unix: int = Field(gt=0, strict=True)
    stop_loss: float
    take_profit: float
    profit: float


class OrderSnapshot(StrictModel):
    ticket: str
    symbol: str
    order_type: str
    volume: float = Field(gt=0)
    price_open: float
    stop_loss: float
    take_profit: float
    state: str


class SymbolSnapshot(StrictModel):
    symbol: str
    bid: float
    ask: float
    point: float = Field(gt=0)
    volume_min: float = Field(gt=0)
    volume_max: float = Field(gt=0)
    volume_step: float = Field(gt=0)
    trade_mode: int
    observed_at: datetime


class BarSnapshot(StrictModel):
    symbol: str
    timeframe: str
    time: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)


class SnapshotPayload(StrictModel):
    schema_name: str = Field(alias="schema")
    account_fingerprint: str = Field(min_length=12, max_length=64)
    observed_at: datetime
    currency: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    positions: list[PositionSnapshot] = Field(default_factory=list)
    orders: list[OrderSnapshot] = Field(default_factory=list)
    symbols: list[SymbolSnapshot] = Field(default_factory=list)
    bars: list[BarSnapshot] = Field(default_factory=list)


class TradeEventPayload(StrictModel):
    schema_name: str = Field(alias="schema")
    event_id: str = Field(min_length=8, max_length=128)
    account_fingerprint: str = Field(min_length=12, max_length=64)
    event_type: str
    order_ticket: str = ""
    deal_ticket: str = ""
    symbol: str = ""
    volume: float = 0.0
    price: float = 0.0
    result_code: int = 0
    observed_at: datetime


class Mt5BridgeStore:
    """SQLite OLAP facts for MT5 observations and replay protection."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self.connection:
            self.connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_nonces (
                    nonce TEXT PRIMARY KEY,
                    request_timestamp INTEGER NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS heartbeats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_fingerprint TEXT NOT NULL,
                    server_fingerprint TEXT NOT NULL,
                    adapter_version TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    connected INTEGER NOT NULL,
                    trade_allowed INTEGER NOT NULL,
                    terminal_build INTEGER NOT NULL,
                    terminal_ping_ms REAL NOT NULL,
                    terminal_observed_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mt5_heartbeat_account_time
                    ON heartbeats(account_fingerprint, received_at);
                CREATE TABLE IF NOT EXISTS account_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_fingerprint TEXT NOT NULL,
                    terminal_observed_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    balance REAL NOT NULL,
                    equity REAL NOT NULL,
                    margin REAL NOT NULL,
                    free_margin REAL NOT NULL,
                    positions_total INTEGER NOT NULL,
                    orders_total INTEGER NOT NULL,
                    symbols_total INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS position_snapshots (
                    snapshot_id INTEGER NOT NULL,
                    ticket TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    volume REAL NOT NULL,
                    price_open REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    profit REAL NOT NULL,
                    FOREIGN KEY(snapshot_id) REFERENCES account_snapshots(id)
                );
                CREATE TABLE IF NOT EXISTS order_snapshots (
                    snapshot_id INTEGER NOT NULL,
                    ticket TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    volume REAL NOT NULL,
                    price_open REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    state TEXT NOT NULL,
                    FOREIGN KEY(snapshot_id) REFERENCES account_snapshots(id)
                );
                CREATE TABLE IF NOT EXISTS symbol_snapshots (
                    snapshot_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    bid REAL NOT NULL,
                    ask REAL NOT NULL,
                    spread REAL NOT NULL,
                    spread_points REAL NOT NULL,
                    point REAL NOT NULL,
                    volume_min REAL NOT NULL,
                    volume_max REAL NOT NULL,
                    volume_step REAL NOT NULL,
                    trade_mode INTEGER NOT NULL,
                    terminal_observed_at TEXT NOT NULL,
                    FOREIGN KEY(snapshot_id) REFERENCES account_snapshots(id)
                );
                CREATE INDEX IF NOT EXISTS idx_mt5_symbol_time
                    ON symbol_snapshots(symbol, terminal_observed_at);
                CREATE TABLE IF NOT EXISTS bar_snapshots (
                    snapshot_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    bar_time TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    PRIMARY KEY(snapshot_id,symbol,timeframe,bar_time),
                    FOREIGN KEY(snapshot_id) REFERENCES account_snapshots(id)
                );
                CREATE INDEX IF NOT EXISTS idx_mt5_bar_time
                    ON bar_snapshots(symbol,timeframe,bar_time);
                CREATE TABLE IF NOT EXISTS trade_events (
                    event_id TEXT PRIMARY KEY,
                    account_fingerprint TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    order_ticket TEXT NOT NULL,
                    deal_ticket TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    volume REAL NOT NULL,
                    price REAL NOT NULL,
                    result_code INTEGER NOT NULL,
                    terminal_observed_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",
                ("schema_version", SCHEMA_VERSION),
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",
                ("bridge_version", BRIDGE_VERSION),
            )

    def accept_nonce(
        self,
        nonce: str,
        request_timestamp: int,
        *,
        now: Optional[int] = None,
        retention_seconds: int = 900,
    ) -> bool:
        observed = int(time.time()) if now is None else int(now)
        cutoff = observed - retention_seconds
        with self._lock, self.connection:
            self.connection.execute(
                "DELETE FROM auth_nonces WHERE request_timestamp < ?", (cutoff,)
            )
            try:
                self.connection.execute(
                    """
                    INSERT INTO auth_nonces(nonce,request_timestamp,observed_at)
                    VALUES (?,?,?)
                    """,
                    (nonce, request_timestamp, _utc_now()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def record_heartbeat(self, payload: HeartbeatPayload) -> None:
        value = payload.model_dump(by_alias=True, mode="json")
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO heartbeats(
                    account_fingerprint,server_fingerprint,adapter_version,
                    environment,connected,trade_allowed,terminal_build,
                    terminal_ping_ms,terminal_observed_at,received_at,payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    payload.account_fingerprint.lower(),
                    payload.server_fingerprint.lower(),
                    payload.adapter_version,
                    payload.environment,
                    int(payload.connected),
                    int(payload.trade_allowed),
                    payload.terminal_build,
                    payload.terminal_ping_ms,
                    payload.observed_at.isoformat(),
                    _utc_now(),
                    _canonical_json(value),
                ),
            )

    def record_snapshot(self, payload: SnapshotPayload) -> int:
        value = payload.model_dump(by_alias=True, mode="json")
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO account_snapshots(
                    account_fingerprint,terminal_observed_at,received_at,currency,
                    balance,equity,margin,free_margin,positions_total,orders_total,
                    symbols_total,payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    payload.account_fingerprint.lower(),
                    payload.observed_at.isoformat(),
                    _utc_now(),
                    payload.currency,
                    payload.balance,
                    payload.equity,
                    payload.margin,
                    payload.free_margin,
                    len(payload.positions),
                    len(payload.orders),
                    len(payload.symbols),
                    _canonical_json(value),
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            self.connection.executemany(
                """
                INSERT INTO position_snapshots(
                    snapshot_id,ticket,symbol,side,volume,price_open,stop_loss,
                    take_profit,profit
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        snapshot_id,
                        item.ticket,
                        item.symbol,
                        item.side,
                        item.volume,
                        item.price_open,
                        item.stop_loss,
                        item.take_profit,
                        item.profit,
                    )
                    for item in payload.positions
                ],
            )
            self.connection.executemany(
                """
                INSERT INTO order_snapshots(
                    snapshot_id,ticket,symbol,order_type,volume,price_open,
                    stop_loss,take_profit,state
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        snapshot_id,
                        item.ticket,
                        item.symbol,
                        item.order_type,
                        item.volume,
                        item.price_open,
                        item.stop_loss,
                        item.take_profit,
                        item.state,
                    )
                    for item in payload.orders
                ],
            )
            self.connection.executemany(
                """
                INSERT INTO symbol_snapshots(
                    snapshot_id,symbol,bid,ask,spread,spread_points,point,
                    volume_min,volume_max,volume_step,trade_mode,
                    terminal_observed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        snapshot_id,
                        item.symbol,
                        item.bid,
                        item.ask,
                        item.ask - item.bid,
                        (item.ask - item.bid) / item.point,
                        item.point,
                        item.volume_min,
                        item.volume_max,
                        item.volume_step,
                        item.trade_mode,
                        item.observed_at.isoformat(),
                    )
                    for item in payload.symbols
                ],
            )
            self.connection.executemany(
                """
                INSERT INTO bar_snapshots(
                    snapshot_id,symbol,timeframe,bar_time,open,high,low,close,volume
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        snapshot_id, item.symbol, item.timeframe,
                        item.time.isoformat(), item.open, item.high, item.low,
                        item.close, item.volume,
                    )
                    for item in payload.bars
                ],
            )
        return snapshot_id

    def record_event(self, payload: TradeEventPayload) -> bool:
        value = payload.model_dump(by_alias=True, mode="json")
        with self._lock, self.connection:
            try:
                self.connection.execute(
                    """
                    INSERT INTO trade_events(
                        event_id,account_fingerprint,event_type,order_ticket,
                        deal_ticket,symbol,volume,price,result_code,
                        terminal_observed_at,received_at,payload_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        payload.event_id,
                        payload.account_fingerprint.lower(),
                        payload.event_type,
                        payload.order_ticket,
                        payload.deal_ticket,
                        payload.symbol,
                        payload.volume,
                        payload.price,
                        payload.result_code,
                        payload.observed_at.isoformat(),
                        _utc_now(),
                        _canonical_json(value),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def report(self, stale_seconds: int = 180) -> dict[str, Any]:
        with self._lock:
            heartbeat = self.connection.execute(
                "SELECT * FROM heartbeats ORDER BY id DESC LIMIT 1"
            ).fetchone()
            snapshot = self.connection.execute(
                "SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            counts = {
                "heartbeats": self.connection.execute(
                    "SELECT COUNT(*) FROM heartbeats"
                ).fetchone()[0],
                "snapshots": self.connection.execute(
                    "SELECT COUNT(*) FROM account_snapshots"
                ).fetchone()[0],
                "events": self.connection.execute(
                    "SELECT COUNT(*) FROM trade_events"
                ).fetchone()[0],
            }
        latest_snapshot = (
            {
                "received_at": snapshot["received_at"],
                "currency": snapshot["currency"],
                "balance": snapshot["balance"],
                "equity": snapshot["equity"],
                "margin": snapshot["margin"],
                "free_margin": snapshot["free_margin"],
                "positions_total": snapshot["positions_total"],
                "orders_total": snapshot["orders_total"],
                "symbols_total": snapshot["symbols_total"],
            }
            if snapshot is not None
            else None
        )
        if heartbeat is None:
            return {
                "available": False,
                "reason": "no_heartbeat",
                "bridge_version": BRIDGE_VERSION,
                "latest_snapshot": latest_snapshot,
                "counts": counts,
            }
        received = datetime.fromisoformat(heartbeat["received_at"]).timestamp()
        age = max(0.0, time.time() - received)
        return {
            "available": True,
            "bridge_version": BRIDGE_VERSION,
            "account_fingerprint": heartbeat["account_fingerprint"],
            "server_fingerprint": heartbeat["server_fingerprint"],
            "environment": heartbeat["environment"],
            "connected": bool(heartbeat["connected"]),
            "trade_allowed": bool(heartbeat["trade_allowed"]),
            "terminal_build": heartbeat["terminal_build"],
            "terminal_ping_ms": heartbeat["terminal_ping_ms"],
            "last_heartbeat_at": heartbeat["received_at"],
            "heartbeat_age_seconds": age,
            "stale": age > stale_seconds,
            "latest_snapshot": latest_snapshot,
            "counts": counts,
        }

    def operational_status(self, stale_seconds: int = 180) -> dict[str, Any]:
        """Return fleet-safe freshness evidence without account or capital data."""
        report = self.report(stale_seconds)
        snapshot = report.get("latest_snapshot") or {}
        public_snapshot = (
            {
                "received_at": snapshot.get("received_at"),
                "positions_total": int(snapshot.get("positions_total") or 0),
                "orders_total": int(snapshot.get("orders_total") or 0),
                "symbols_total": int(snapshot.get("symbols_total") or 0),
            }
            if snapshot
            else None
        )
        status: dict[str, Any] = {
            "schema": STATUS_SCHEMA,
            "available": bool(report.get("available")),
            "reason": report.get("reason"),
            "bridge_version": BRIDGE_VERSION,
            "read_only": True,
            "heartbeat": None,
            "latest_snapshot": public_snapshot,
            "counts": dict(report.get("counts") or {}),
        }
        if report.get("available"):
            status["heartbeat"] = {
                "environment": report.get("environment"),
                "connected": bool(report.get("connected")),
                "trade_allowed": bool(report.get("trade_allowed")),
                "terminal_build": report.get("terminal_build"),
                "terminal_ping_ms": report.get("terminal_ping_ms"),
                "received_at": report.get("last_heartbeat_at"),
                "age_seconds": report.get("heartbeat_age_seconds"),
                "stale": bool(report.get("stale")),
            }
        return status

    def close(self) -> None:
        self.connection.close()


class Mt5RequestAuthenticator:
    """HMAC-SHA256 request authentication with persistent nonce replay defense."""

    def __init__(
        self,
        secret: bytes,
        store: Mt5BridgeStore,
        *,
        max_clock_skew_seconds: int,
        nonce_retention_seconds: int,
    ):
        self.secret = secret
        self.store = store
        self.max_clock_skew_seconds = max_clock_skew_seconds
        self.nonce_retention_seconds = nonce_retention_seconds

    @staticmethod
    def canonical_request(
        method: str,
        path: str,
        timestamp: str,
        nonce: str,
        body: bytes,
        route_identity: str = "",
    ) -> bytes:
        """AUD-F2-20260823-301: an authenticated route identity
        (account, symbol, EA magic, protocol version) binds into the
        signature as a sixth canonical line when present. The empty
        default preserves the legacy five-line framing byte-for-byte
        for single-symbol deployments until their coordinated update."""
        body_hash = hashlib.sha256(body).hexdigest()
        parts = [method.upper(), path, timestamp, nonce, body_hash]
        if route_identity:
            parts.append(route_identity)
        return "\n".join(parts).encode("utf-8")

    def signature(
        self,
        method: str,
        path: str,
        timestamp: str,
        nonce: str,
        body: bytes,
        route_identity: str = "",
    ) -> str:
        canonical = self.canonical_request(
            method, path, timestamp, nonce, body, route_identity)
        return hmac.new(self.secret, canonical, hashlib.sha256).hexdigest()

    def verify(
        self,
        method: str,
        path: str,
        timestamp: str,
        nonce: str,
        signature: str,
        body: bytes,
        *,
        route_identity: str = "",
        now: Optional[int] = None,
    ) -> None:
        if not timestamp or not nonce or not signature:
            raise Mt5BridgeError("Missing bridge authentication headers")
        if len(nonce) < 12 or len(nonce) > 128:
            raise Mt5BridgeError("Invalid bridge nonce")
        try:
            request_time = int(timestamp)
        except ValueError as exc:
            raise Mt5BridgeError("Invalid bridge timestamp") from exc
        current = int(time.time()) if now is None else int(now)
        if abs(current - request_time) > self.max_clock_skew_seconds:
            raise Mt5BridgeError("Bridge request timestamp is outside the allowed skew")
        expected = self.signature(method, path, timestamp, nonce, body,
                                  route_identity)
        if not hmac.compare_digest(expected, signature.lower()):
            raise Mt5BridgeError("Invalid bridge request signature")
        if not self.store.accept_nonce(
            nonce,
            request_time,
            now=current,
            retention_seconds=self.nonce_retention_seconds,
        ):
            raise Mt5BridgeError("Bridge request nonce was already used")


def create_mt5_bridge_app(
    config: Mt5BridgeConfig,
    store: Mt5BridgeStore,
    secret: bytes,
) -> FastAPI:
    app = FastAPI(
        title="LTS MT5 Demo Bridge",
        version=BRIDGE_VERSION,
        docs_url=None,
        redoc_url=None,
    )
    authenticator = Mt5RequestAuthenticator(
        secret,
        store,
        max_clock_skew_seconds=config.max_clock_skew_seconds,
        nonce_retention_seconds=config.nonce_retention_seconds,
    )

    async def authenticate(request: Request) -> None:
        body = await request.body()
        try:
            authenticator.verify(
                request.method,
                request.url.path,
                request.headers.get("X-LTS-Timestamp", ""),
                request.headers.get("X-LTS-Nonce", ""),
                request.headers.get("X-LTS-Signature", ""),
                body,
            )
        except Mt5BridgeError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def validate_common(
        schema_name: str,
        expected_schema: str,
        account_fingerprint: str,
        environment: Optional[str] = None,
    ) -> None:
        if schema_name != expected_schema:
            raise HTTPException(status_code=422, detail="Unsupported payload schema")
        if environment is not None and environment != "demo":
            raise HTTPException(status_code=403, detail="Only MT5 demo is accepted")
        fingerprint = account_fingerprint.lower()
        if (
            config.allowed_account_fingerprints
            and fingerprint not in config.allowed_account_fingerprints
        ):
            raise HTTPException(status_code=403, detail="Unapproved account fingerprint")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "bridge_version": BRIDGE_VERSION,
            "environment": config.environment,
            "read_only": config.read_only,
        }

    @app.get("/v1/status")
    def operational_status() -> dict[str, Any]:
        return store.operational_status(config.stale_heartbeat_seconds)

    @app.post("/v1/heartbeat")
    async def heartbeat(payload: HeartbeatPayload, request: Request) -> dict[str, Any]:
        await authenticate(request)
        validate_common(
            payload.schema_name,
            HEARTBEAT_SCHEMA,
            payload.account_fingerprint,
            payload.environment,
        )
        store.record_heartbeat(payload)
        return {
            "accepted": True,
            "read_only": True,
            "server_time": _utc_now(),
        }

    @app.post("/v1/snapshot")
    async def snapshot(payload: SnapshotPayload, request: Request) -> dict[str, Any]:
        await authenticate(request)
        validate_common(
            payload.schema_name,
            SNAPSHOT_SCHEMA,
            payload.account_fingerprint,
        )
        snapshot_id = store.record_snapshot(payload)
        return {
            "accepted": True,
            "snapshot_id": snapshot_id,
            "read_only": True,
        }

    @app.post("/v1/events")
    async def event(payload: TradeEventPayload, request: Request) -> dict[str, Any]:
        await authenticate(request)
        validate_common(
            payload.schema_name,
            EVENT_SCHEMA,
            payload.account_fingerprint,
        )
        inserted = store.record_event(payload)
        return {"accepted": True, "duplicate": not inserted, "read_only": True}

    @app.get("/v1/commands/next")
    async def next_command(
        request: Request,
        account_fingerprint: str,
    ) -> JSONResponse:
        await authenticate(request)
        validate_common("", "", account_fingerprint)
        return JSONResponse(
            {
                "command": None,
                "read_only": True,
                "reason": "protected canaries are not enabled",
            }
        )

    @app.get("/v1/report")
    async def report(request: Request) -> dict[str, Any]:
        await authenticate(request)
        return store.report(config.stale_heartbeat_seconds)

    return app


def create_signed_headers(
    secret: bytes,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    timestamp: Optional[int] = None,
    nonce: Optional[str] = None,
    route_identity: str = "",
) -> dict[str, str]:
    """Create test/client headers using the bridge's canonical signature."""
    stamp = str(int(time.time()) if timestamp is None else int(timestamp))
    request_nonce = nonce or secrets.token_hex(16)
    body_hash = hashlib.sha256(body).hexdigest()
    parts = [method.upper(), path, stamp, request_nonce, body_hash]
    if route_identity:
        parts.append(route_identity)
    canonical = "\n".join(parts).encode("utf-8")
    signature = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    headers = {
        "X-LTS-Timestamp": stamp,
        "X-LTS-Nonce": request_nonce,
        "X-LTS-Signature": signature,
    }
    if route_identity:
        headers["X-LTS-Route-Identity"] = route_identity
    return headers
