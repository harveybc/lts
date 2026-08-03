"""Read-only Alpaca Paper capability and reconciliation laboratory."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import requests


PAPER_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"
SCHEMA_VERSION = "lts.alpaca.paper_olap.v1"
ADAPTER_VERSION = "lts.alpaca.paper.readonly.v1"
_ACCOUNT_SENSITIVE_KEYS = {
    "account_number",
    "api_key",
    "id",
    "key",
    "secret",
    "secret_key",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    return float(value)


def _redact_account(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: (
                "<redacted>"
                if key.lower() in _ACCOUNT_SENSITIVE_KEYS
                else _redact_account(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_account(item) for item in value]
    return value


class AlpacaPaperError(RuntimeError):
    """Raised when the Paper API contract or a read-only gate fails."""


@dataclass(frozen=True)
class InstrumentSelection:
    cell_id: str
    canonical_asset: str
    alpaca_symbol: str
    asset_class: str
    role: str
    timeframe: str
    priority: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InstrumentSelection":
        required = (
            "cell_id",
            "canonical_asset",
            "alpaca_symbol",
            "asset_class",
            "role",
            "timeframe",
            "priority",
        )
        missing = [key for key in required if value.get(key) in (None, "")]
        if missing:
            raise AlpacaPaperError(
                f"Instrument selection is missing required fields: {', '.join(missing)}"
            )
        asset_class = str(value["asset_class"]).lower()
        if asset_class not in {"crypto", "us_equity"}:
            raise AlpacaPaperError(f"Unsupported Alpaca asset class: {asset_class}")
        return cls(
            cell_id=str(value["cell_id"]),
            canonical_asset=str(value["canonical_asset"]).lower(),
            alpaca_symbol=str(value["alpaca_symbol"]).upper(),
            asset_class=asset_class,
            role=str(value["role"]),
            timeframe=str(value["timeframe"]),
            priority=int(value["priority"]),
        )


@dataclass(frozen=True)
class AlpacaPaperLabConfig:
    api_key_env: str
    api_secret_env: str
    database_path: Path
    instruments: tuple[InstrumentSelection, ...]
    data_location: str
    timeout_seconds: float

    @classmethod
    def load(cls, path: Path | str) -> "AlpacaPaperLabConfig":
        source = Path(path)
        data = json.loads(source.read_text(encoding="utf-8"))
        if data.get("schema") != "lts.alpaca.paper_lab_config.v1":
            raise AlpacaPaperError("Unsupported or missing Alpaca Paper config schema")
        if data.get("environment") != "paper":
            raise AlpacaPaperError("The Alpaca execution laboratory is paper-only")
        if data.get("trading_base_url", PAPER_BASE_URL).rstrip("/") != PAPER_BASE_URL:
            raise AlpacaPaperError("Only the Alpaca Paper trading endpoint is allowed")
        if data.get("data_base_url", DATA_BASE_URL).rstrip("/") != DATA_BASE_URL:
            raise AlpacaPaperError("Only the official Alpaca market-data endpoint is allowed")
        if data.get("orders", {}).get("enabled", False):
            raise AlpacaPaperError("The Alpaca capability laboratory is read-only")

        secrets = data.get("secrets", {})
        api_key_env = str(secrets.get("api_key_env", ""))
        api_secret_env = str(secrets.get("api_secret_env", ""))
        if not api_key_env or not api_secret_env:
            raise AlpacaPaperError("Secret environment-variable names are required")
        forbidden = {"api_key", "api_secret", "secret_key"}
        if forbidden.intersection(data) or forbidden.intersection(secrets):
            raise AlpacaPaperError("Credentials must not be embedded in tracked config")

        selections = tuple(
            sorted(
                (InstrumentSelection.from_dict(item) for item in data.get("instruments", [])),
                key=lambda item: item.priority,
            )
        )
        if not selections:
            raise AlpacaPaperError("At least one instrument selection is required")
        cell_ids = [item.cell_id for item in selections]
        if len(cell_ids) != len(set(cell_ids)):
            raise AlpacaPaperError("Alpaca selection cell_id values must be unique")

        data_location = str(data.get("data_location", "us"))
        if data_location not in {"us", "us-1", "eu-1"}:
            raise AlpacaPaperError(f"Unsupported Alpaca crypto data location: {data_location}")
        timeout_seconds = float(data.get("timeout_seconds", 20.0))
        if timeout_seconds <= 0:
            raise AlpacaPaperError("timeout_seconds must be positive")

        database_path = Path(
            os.path.expandvars(
                os.path.expanduser(
                    str(
                        data.get(
                            "database_path",
                            "~/.local/state/lts/alpaca-paper-lab.sqlite",
                        )
                    )
                )
            )
        )
        return cls(
            api_key_env=api_key_env,
            api_secret_env=api_secret_env,
            database_path=database_path,
            instruments=selections,
            data_location=data_location,
            timeout_seconds=timeout_seconds,
        )

    def credentials(self, environment: Optional[Mapping[str, str]] = None) -> tuple[str, str]:
        source = environment if environment is not None else os.environ
        api_key = source.get(self.api_key_env, "")
        api_secret = source.get(self.api_secret_env, "")
        if not api_key or not api_secret:
            raise AlpacaPaperError(
                f"Set {self.api_key_env} and {self.api_secret_env} before connecting"
            )
        return api_key, api_secret


class AlpacaPaperClient:
    """Minimal GET-only client with endpoint allowlisting and probe capture."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        session: Optional[requests.Session] = None,
        timeout_seconds: float = 20.0,
        trading_base_url: str = PAPER_BASE_URL,
        data_base_url: str = DATA_BASE_URL,
    ) -> None:
        if trading_base_url.rstrip("/") != PAPER_BASE_URL:
            raise AlpacaPaperError("The client cannot connect to Alpaca Live")
        if data_base_url.rstrip("/") != DATA_BASE_URL:
            raise AlpacaPaperError("Unapproved Alpaca data endpoint")
        if not api_key or not api_secret:
            raise AlpacaPaperError("Alpaca Paper API key and secret are required")
        self.timeout_seconds = timeout_seconds
        self.trading_base_url = PAPER_BASE_URL
        self.data_base_url = DATA_BASE_URL
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": api_secret,
                "Accept": "application/json",
                "User-Agent": f"lts/{ADAPTER_VERSION}",
            }
        )
        self.probes: list[Dict[str, Any]] = []

    def _get(
        self,
        endpoint: str,
        path: str,
        *,
        data_api: bool = False,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        base_url = self.data_base_url if data_api else self.trading_base_url
        started = time.perf_counter()
        observed_at = _utc_now()
        try:
            response = self.session.request(
                "GET",
                f"{base_url}{path}",
                params=params,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            self.probes.append(
                {
                    "endpoint": endpoint,
                    "observed_at": observed_at,
                    "latency_ms": (time.perf_counter() - started) * 1000.0,
                    "http_status": None,
                    "success": False,
                    "request_id_fingerprint": None,
                    "error_kind": type(exc).__name__,
                }
            )
            raise AlpacaPaperError(f"{endpoint} request failed: {type(exc).__name__}") from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        request_id = response.headers.get("X-Request-ID", "")
        request_id_fingerprint = (
            hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]
            if request_id
            else None
        )
        success = 200 <= response.status_code < 300
        self.probes.append(
            {
                "endpoint": endpoint,
                "observed_at": observed_at,
                "latency_ms": latency_ms,
                "http_status": response.status_code,
                "success": success,
                "request_id_fingerprint": request_id_fingerprint,
                "error_kind": None if success else "http_error",
            }
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AlpacaPaperError(f"{endpoint} returned invalid JSON") from exc
        if not success:
            message = payload.get("message", "request rejected") if isinstance(payload, dict) else ""
            raise AlpacaPaperError(f"{endpoint} returned HTTP {response.status_code}: {message}")
        return payload

    @staticmethod
    def account_fingerprint(account: Mapping[str, Any]) -> str:
        identity = str(account.get("id") or account.get("account_number") or "")
        if not identity:
            raise AlpacaPaperError("Alpaca account response has no stable identity")
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    def account(self) -> Dict[str, Any]:
        return dict(self._get("account", "/v2/account"))

    def clock(self) -> Dict[str, Any]:
        return dict(self._get("clock", "/v2/clock"))

    def assets(self, asset_class: str) -> list[Dict[str, Any]]:
        payload = self._get(
            f"assets.{asset_class}",
            "/v2/assets",
            params={"status": "active", "asset_class": asset_class},
        )
        return [dict(item) for item in payload]

    def asset(self, symbol: str) -> Dict[str, Any]:
        return dict(self._get(f"asset.{symbol}", f"/v2/assets/{symbol.upper()}"))

    def positions(self) -> list[Dict[str, Any]]:
        payload = self._get("positions", "/v2/positions")
        return [dict(item) for item in payload]

    def open_orders(self) -> list[Dict[str, Any]]:
        payload = self._get(
            "orders.open",
            "/v2/orders",
            params={"status": "open", "limit": 500, "nested": "true"},
        )
        return [dict(item) for item in payload]

    def latest_crypto_quotes(
        self,
        symbols: Sequence[str],
        *,
        location: str = "us",
    ) -> Dict[str, Any]:
        if not symbols:
            return {}
        payload = self._get(
            "crypto.latest_quotes",
            f"/v1beta3/crypto/{location}/latest/quotes",
            data_api=True,
            params={"symbols": ",".join(sorted(set(symbols)))},
        )
        return dict(payload.get("quotes", {}))

    def stock_bars(
        self,
        symbol: str,
        *,
        timeframe: str,
        start: str,
        end: Optional[str] = None,
        feed: str = "iex",
        limit: int = 10000,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        if feed != "iex":
            raise AlpacaPaperError("Only the configured IEX live-equity feed is allowed")
        params: Dict[str, Any] = {
            "timeframe": timeframe,
            "start": start,
            "feed": feed,
            "adjustment": "raw",
            "limit": min(max(int(limit), 1), 10000),
        }
        if end:
            params["end"] = end
        if page_token:
            params["page_token"] = page_token
        return dict(self._get(
            f"stocks.{symbol}.bars",
            f"/v2/stocks/{symbol.upper()}/bars",
            data_api=True,
            params=params,
        ))

    def latest_stock_quote(self, symbol: str, *, feed: str = "iex") -> Dict[str, Any]:
        if feed != "iex":
            raise AlpacaPaperError("Only the configured IEX live-equity feed is allowed")
        return dict(self._get(
            f"stocks.{symbol}.latest_quote",
            f"/v2/stocks/{symbol.upper()}/quotes/latest",
            data_api=True,
            params={"feed": feed},
        ).get("quote", {}))


class AlpacaPaperOlap:
    """Local restart-safe analytical store containing no credentials or account IDs."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS lab_sessions (
                session_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                adapter_version TEXT NOT NULL,
                phase TEXT NOT NULL,
                account_fingerprint TEXT NOT NULL,
                config_sha256 TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL,
                detail_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS endpoint_probes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                latency_ms REAL NOT NULL,
                http_status INTEGER,
                success INTEGER NOT NULL,
                request_id_fingerprint TEXT,
                error_kind TEXT,
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS account_capabilities (
                session_id TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                status TEXT,
                currency TEXT,
                account_blocked INTEGER NOT NULL,
                trading_blocked INTEGER NOT NULL,
                transfers_blocked INTEGER NOT NULL,
                shorting_enabled INTEGER NOT NULL,
                pattern_day_trader INTEGER,
                equity REAL,
                cash REAL,
                buying_power REAL,
                capability_json TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS instrument_capabilities (
                session_id TEXT NOT NULL,
                cell_id TEXT NOT NULL,
                canonical_asset TEXT NOT NULL,
                symbol TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                role TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                priority INTEGER NOT NULL,
                available INTEGER NOT NULL,
                tradable INTEGER,
                shortable INTEGER,
                marginable INTEGER,
                fractionable INTEGER,
                min_order_size REAL,
                min_trade_increment REAL,
                price_increment REAL,
                protected_execution_eligible INTEGER NOT NULL,
                capability_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (session_id, cell_id),
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS quote_observations (
                session_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                broker_time TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                bid REAL,
                ask REAL,
                mid REAL,
                spread REAL,
                spread_bps REAL,
                bid_size REAL,
                ask_size REAL,
                quote_json TEXT NOT NULL,
                PRIMARY KEY (session_id, symbol, broker_time),
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS reconciliation_snapshots (
                session_id TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                equity_market_open INTEGER,
                equity_clock_timestamp TEXT,
                open_positions INTEGER NOT NULL,
                open_orders INTEGER NOT NULL,
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE VIEW IF NOT EXISTS alpaca_endpoint_health_olap AS
            SELECT
                endpoint,
                COUNT(*) AS requests,
                SUM(success) AS successful_requests,
                AVG(latency_ms) AS mean_latency_ms,
                MAX(latency_ms) AS max_latency_ms,
                MAX(observed_at) AS last_observed_at
            FROM endpoint_probes
            GROUP BY endpoint;
            CREATE VIEW IF NOT EXISTS alpaca_capability_olap AS
            SELECT
                cell_id,
                canonical_asset,
                symbol,
                asset_class,
                role,
                timeframe,
                COUNT(*) AS observations,
                SUM(available) AS available_observations,
                MAX(tradable) AS tradable,
                MAX(shortable) AS shortable,
                MAX(marginable) AS marginable,
                MAX(fractionable) AS fractionable,
                MAX(protected_execution_eligible) AS protected_execution_eligible,
                MAX(observed_at) AS last_observed_at
            FROM instrument_capabilities
            GROUP BY cell_id, canonical_asset, symbol, asset_class, role, timeframe;
            CREATE VIEW IF NOT EXISTS alpaca_quote_summary_olap AS
            SELECT
                symbol,
                COUNT(*) AS observations,
                MIN(broker_time) AS first_broker_time,
                MAX(broker_time) AS last_broker_time,
                AVG(spread_bps) AS mean_spread_bps,
                MIN(spread_bps) AS min_spread_bps,
                MAX(spread_bps) AS max_spread_bps
            FROM quote_observations
            GROUP BY symbol;
            """
        )
        self.connection.commit()

    def start_session(
        self,
        phase: str,
        account_fingerprint: str,
        config: Mapping[str, Any],
    ) -> str:
        session_id = f"{phase}-{uuid.uuid4().hex[:16]}"
        config_json = _canonical_json(config)
        self.connection.execute(
            """
            INSERT INTO lab_sessions VALUES
            (?, ?, ?, ?, ?, ?, ?, NULL, 'running', '{}')
            """,
            (
                session_id,
                SCHEMA_VERSION,
                ADAPTER_VERSION,
                phase,
                account_fingerprint,
                hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
                _utc_now(),
            ),
        )
        self.connection.commit()
        return session_id

    def finish_session(self, session_id: str, status: str, detail: Mapping[str, Any]) -> None:
        self.connection.execute(
            """
            UPDATE lab_sessions SET ended_at=?, status=?, detail_json=? WHERE session_id=?
            """,
            (_utc_now(), status, _canonical_json(detail), session_id),
        )
        self.connection.commit()

    def record_probes(self, session_id: str, probes: Sequence[Mapping[str, Any]]) -> None:
        self.connection.executemany(
            """
            INSERT INTO endpoint_probes(
                session_id,endpoint,observed_at,latency_ms,http_status,success,
                request_id_fingerprint,error_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    session_id,
                    probe["endpoint"],
                    probe["observed_at"],
                    probe["latency_ms"],
                    probe.get("http_status"),
                    int(bool(probe["success"])),
                    probe.get("request_id_fingerprint"),
                    probe.get("error_kind"),
                )
                for probe in probes
            ],
        )
        self.connection.commit()

    def record_account(self, session_id: str, account: Mapping[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO account_capabilities VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                _utc_now(),
                account.get("status"),
                account.get("currency"),
                int(bool(account.get("account_blocked"))),
                int(bool(account.get("trading_blocked"))),
                int(bool(account.get("transfers_blocked"))),
                int(bool(account.get("shorting_enabled"))),
                (
                    int(bool(account.get("pattern_day_trader")))
                    if account.get("pattern_day_trader") is not None
                    else None
                ),
                _as_float(account.get("equity")),
                _as_float(account.get("cash")),
                _as_float(account.get("buying_power")),
                _canonical_json(_redact_account(account)),
            ),
        )
        self.connection.commit()

    def record_capability(
        self,
        session_id: str,
        selection: InstrumentSelection,
        asset: Optional[Mapping[str, Any]],
    ) -> None:
        normalized = {
            "api_asset": dict(asset) if asset else None,
            "documented_order_types": (
                ["market", "limit", "stop_limit"]
                if selection.asset_class == "crypto"
                else ["market", "limit", "stop", "stop_limit", "trailing_stop"]
            ),
            "documented_time_in_force": (
                ["gtc", "ioc"]
                if selection.asset_class == "crypto"
                else ["day", "gtc", "opg", "cls", "ioc", "fok"]
            ),
            "native_sl_tp_verified": False,
            "protected_execution_eligible": False,
        }
        self.connection.execute(
            """
            INSERT INTO instrument_capabilities(
                session_id,cell_id,canonical_asset,symbol,asset_class,role,
                timeframe,priority,available,tradable,shortable,marginable,
                fractionable,min_order_size,min_trade_increment,price_increment,
                protected_execution_eligible,capability_json,observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                selection.cell_id,
                selection.canonical_asset,
                selection.alpaca_symbol,
                selection.asset_class,
                selection.role,
                selection.timeframe,
                selection.priority,
                int(asset is not None),
                int(bool(asset.get("tradable"))) if asset else None,
                int(bool(asset.get("shortable"))) if asset else None,
                int(bool(asset.get("marginable"))) if asset else None,
                int(bool(asset.get("fractionable"))) if asset else None,
                _as_float(asset.get("min_order_size")) if asset else None,
                _as_float(asset.get("min_trade_increment")) if asset else None,
                _as_float(asset.get("price_increment")) if asset else None,
                0,
                _canonical_json(normalized),
                _utc_now(),
            ),
        )
        self.connection.commit()

    def record_quote(
        self,
        session_id: str,
        symbol: str,
        quote: Mapping[str, Any],
    ) -> None:
        bid = _as_float(quote.get("bp"))
        ask = _as_float(quote.get("ap"))
        mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        spread = ask - bid if bid is not None and ask is not None else None
        spread_bps = spread / mid * 10000.0 if spread is not None and mid else None
        broker_time = str(quote.get("t") or _utc_now())
        self.connection.execute(
            """
            INSERT OR REPLACE INTO quote_observations VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                symbol,
                broker_time,
                _utc_now(),
                bid,
                ask,
                mid,
                spread,
                spread_bps,
                _as_float(quote.get("bs")),
                _as_float(quote.get("as")),
                _canonical_json(quote),
            ),
        )
        self.connection.commit()

    def record_reconciliation(
        self,
        session_id: str,
        clock: Mapping[str, Any],
        positions: Sequence[Mapping[str, Any]],
        orders: Sequence[Mapping[str, Any]],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO reconciliation_snapshots VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                _utc_now(),
                int(bool(clock.get("is_open"))),
                clock.get("timestamp"),
                len(positions),
                len(orders),
            ),
        )
        self.connection.commit()

    def report(self) -> Dict[str, Any]:
        latest = self.connection.execute(
            """
            SELECT s.session_id,s.status,s.started_at,s.ended_at,
                   a.status AS account_status,a.currency,a.account_blocked,
                   a.trading_blocked,a.shorting_enabled,
                   r.equity_market_open,r.open_positions,r.open_orders
            FROM lab_sessions s
            LEFT JOIN account_capabilities a ON a.session_id=s.session_id
            LEFT JOIN reconciliation_snapshots r ON r.session_id=s.session_id
            ORDER BY s.started_at DESC LIMIT 1
            """
        ).fetchone()
        endpoints = self.connection.execute(
            "SELECT * FROM alpaca_endpoint_health_olap ORDER BY endpoint"
        ).fetchall()
        capabilities = self.connection.execute(
            "SELECT * FROM alpaca_capability_olap ORDER BY role,timeframe,symbol"
        ).fetchall()
        quotes = self.connection.execute(
            "SELECT * FROM alpaca_quote_summary_olap ORDER BY symbol"
        ).fetchall()
        return {
            "schema_version": SCHEMA_VERSION,
            "adapter_version": ADAPTER_VERSION,
            "database_path": str(self.path),
            "latest_session": dict(latest) if latest else None,
            "endpoint_health": [dict(row) for row in endpoints],
            "instrument_capabilities": [dict(row) for row in capabilities],
            "quote_summary": [dict(row) for row in quotes],
        }


class AlpacaPaperLab:
    """Coordinates one immutable read-only Paper capability preflight."""

    def __init__(
        self,
        config: AlpacaPaperLabConfig,
        client: AlpacaPaperClient,
        store: AlpacaPaperOlap,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store

    def _config_evidence(self) -> Dict[str, Any]:
        return {
            "schema": "lts.alpaca.paper_lab_config.v1",
            "environment": "paper",
            "adapter_version": ADAPTER_VERSION,
            "trading_base_url": PAPER_BASE_URL,
            "data_base_url": DATA_BASE_URL,
            "data_location": self.config.data_location,
            "secret_env_names": [
                self.config.api_key_env,
                self.config.api_secret_env,
            ],
            "instruments": [
                {
                    "cell_id": item.cell_id,
                    "canonical_asset": item.canonical_asset,
                    "alpaca_symbol": item.alpaca_symbol,
                    "asset_class": item.asset_class,
                    "role": item.role,
                    "timeframe": item.timeframe,
                    "priority": item.priority,
                }
                for item in self.config.instruments
            ],
        }

    def preflight(self) -> Dict[str, Any]:
        account = self.client.account()
        fingerprint = self.client.account_fingerprint(account)
        session_id = self.store.start_session(
            "preflight",
            fingerprint,
            self._config_evidence(),
        )
        try:
            clock = self.client.clock()
            assets_by_class: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for asset_class in sorted({item.asset_class for item in self.config.instruments}):
                assets_by_class[asset_class] = {
                    item["symbol"]: item for item in self.client.assets(asset_class)
                }
            positions = self.client.positions()
            open_orders = self.client.open_orders()
            crypto_symbols = sorted(
                {
                    item.alpaca_symbol
                    for item in self.config.instruments
                    if item.asset_class == "crypto"
                }
            )
            quotes = self.client.latest_crypto_quotes(
                crypto_symbols,
                location=self.config.data_location,
            )

            self.store.record_account(session_id, account)
            available: list[str] = []
            missing: list[str] = []
            for selection in self.config.instruments:
                asset = assets_by_class[selection.asset_class].get(selection.alpaca_symbol)
                self.store.record_capability(session_id, selection, asset)
                (available if asset else missing).append(selection.cell_id)
            for symbol, quote in quotes.items():
                self.store.record_quote(session_id, symbol, quote)
            self.store.record_reconciliation(session_id, clock, positions, open_orders)
            self.store.record_probes(session_id, self.client.probes)

            result = {
                "session_id": session_id,
                "environment": "paper",
                "adapter_version": ADAPTER_VERSION,
                "account_fingerprint": fingerprint,
                "account_status": account.get("status"),
                "account_blocked": bool(account.get("account_blocked")),
                "trading_blocked": bool(account.get("trading_blocked")),
                "account_shorting_enabled": bool(account.get("shorting_enabled")),
                "available_cells": available,
                "missing_cells": missing,
                "quotes_received": sorted(quotes),
                "open_positions": len(positions),
                "open_orders": len(open_orders),
                "equity_market_open": bool(clock.get("is_open")),
                "protected_execution_eligible": False,
                "orders_submitted": 0,
                "database_path": str(self.store.path),
            }
            self.store.finish_session(session_id, "complete", result)
            return result
        except Exception as exc:
            self.store.record_probes(session_id, self.client.probes)
            self.store.finish_session(
                session_id,
                "failed",
                {"error_kind": type(exc).__name__},
            )
            raise
