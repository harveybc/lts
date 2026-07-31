"""GET-only Capital.com Demo capability, quote and reconciliation observer."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import requests


DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"
API_PREFIX = "/api/v1"
CONFIG_SCHEMA = "lts.capital.demo_lab_config.v1"
OLAP_SCHEMA = "lts.capital.demo_olap.v1"
ADAPTER_VERSION = "lts.capital.demo.get_only.v1"
GET_ENDPOINTS = {
    "accounts": "/accounts",
    "positions": "/positions",
    "working_orders": "/workingorders",
    "markets": "/markets",
}


class CapitalDemoError(RuntimeError):
    """Raised when a Capital.com Demo safety or API contract fails."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _redact(value: Any) -> Any:
    sensitive = {
        "accountid",
        "accountname",
        "identifier",
        "password",
        "apikey",
        "cst",
        "x-security-token",
    }
    if isinstance(value, Mapping):
        return {
            key: (
                "<redacted>"
                if key.lower().replace("_", "") in sensitive
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


@dataclass(frozen=True)
class CapitalDemoConfig:
    api_key_env: str
    identifier_env: str
    password_env: str
    database_path: Path
    timeout_seconds: float
    search_terms: tuple[str, ...]

    @classmethod
    def load(cls, path: Path | str) -> "CapitalDemoConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema") != CONFIG_SCHEMA:
            raise CapitalDemoError("Unsupported Capital.com Demo config schema")
        if data.get("environment") != "demo":
            raise CapitalDemoError("Capital.com observer is demo-only")
        if data.get("base_url", DEMO_BASE_URL).rstrip("/") != DEMO_BASE_URL:
            raise CapitalDemoError("Only the official Capital.com Demo API is allowed")
        if data.get("mode") != "get_only":
            raise CapitalDemoError("Capital.com observer must remain GET-only")
        if data.get("orders", {}).get("enabled", False):
            raise CapitalDemoError("Capital.com order routes are disabled")
        secrets = data.get("secrets") or {}
        names = (
            str(secrets.get("api_key_env", "")),
            str(secrets.get("identifier_env", "")),
            str(secrets.get("password_env", "")),
        )
        if not all(names):
            raise CapitalDemoError("Capital.com secret environment names are required")
        forbidden = {"api_key", "identifier", "password"}
        if forbidden.intersection(data) or forbidden.intersection(secrets):
            raise CapitalDemoError("Credentials cannot be stored in tracked config")
        timeout = float(data.get("timeout_seconds", 20))
        if timeout <= 0:
            raise CapitalDemoError("timeout_seconds must be positive")
        terms = tuple(
            str(term).strip()
            for term in data.get("market_search_terms", [])
            if str(term).strip()
        )
        if not terms:
            raise CapitalDemoError("At least one market search term is required")
        database_path = Path(
            os.path.expandvars(
                os.path.expanduser(
                    str(
                        data.get(
                            "database_path",
                            "~/.local/state/lts/capital-demo-lab.sqlite",
                        )
                    )
                )
            )
        )
        return cls(
            api_key_env=names[0],
            identifier_env=names[1],
            password_env=names[2],
            database_path=database_path,
            timeout_seconds=timeout,
            search_terms=terms,
        )

    def credentials(
        self, environment: Optional[Mapping[str, str]] = None
    ) -> tuple[str, str, str]:
        source = environment if environment is not None else os.environ
        values = (
            source.get(self.api_key_env, ""),
            source.get(self.identifier_env, ""),
            source.get(self.password_env, ""),
        )
        if not all(values):
            raise CapitalDemoError(
                f"Set {self.api_key_env}, {self.identifier_env}, and "
                f"{self.password_env} before connecting"
            )
        return values


class CapitalDemoClient:
    """Capital.com client whose only POST is mandatory session authentication."""

    def __init__(
        self,
        api_key: str,
        identifier: str,
        password: str,
        *,
        session: Optional[requests.Session] = None,
        timeout_seconds: float = 20,
        base_url: str = DEMO_BASE_URL,
    ) -> None:
        if base_url.rstrip("/") != DEMO_BASE_URL:
            raise CapitalDemoError("Refusing a non-Demo Capital.com endpoint")
        if not api_key or not identifier or not password:
            raise CapitalDemoError("Capital.com Demo credentials are required")
        self.api_key = api_key
        self.identifier = identifier
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-CAP-API-KEY": api_key,
                "User-Agent": f"lts/{ADAPTER_VERSION}",
            }
        )
        self.probes: list[dict[str, Any]] = []
        self.authenticated = False

    def _record(
        self,
        endpoint: str,
        started: float,
        response: Optional[requests.Response],
        error_kind: Optional[str],
    ) -> None:
        status = response.status_code if response is not None else None
        self.probes.append(
            {
                "endpoint": endpoint,
                "observed_at": _utc_now(),
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "http_status": status,
                "success": bool(status is not None and 200 <= status < 300),
                "error_kind": error_kind,
            }
        )

    def authenticate(self) -> None:
        started = time.perf_counter()
        response = None
        try:
            response = self.session.request(
                "POST",
                f"{DEMO_BASE_URL}{API_PREFIX}/session",
                json={
                    "identifier": self.identifier,
                    "password": self.password,
                    "encryptedPassword": False,
                },
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            self._record("session.create", started, None, type(exc).__name__)
            raise CapitalDemoError(
                f"Capital.com Demo authentication failed: {type(exc).__name__}"
            ) from exc
        self._record(
            "session.create",
            started,
            response,
            None if 200 <= response.status_code < 300 else "http_error",
        )
        if not 200 <= response.status_code < 300:
            raise CapitalDemoError(
                f"Capital.com Demo authentication returned HTTP {response.status_code}"
            )
        cst = response.headers.get("CST")
        security_token = response.headers.get("X-SECURITY-TOKEN")
        if not cst or not security_token:
            raise CapitalDemoError(
                "Capital.com Demo session response omitted security tokens"
            )
        self.session.headers.update(
            {"CST": cst, "X-SECURITY-TOKEN": security_token}
        )
        self.authenticated = True

    def _get(
        self,
        endpoint: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        if endpoint not in GET_ENDPOINTS:
            raise CapitalDemoError(f"Unapproved Capital.com GET endpoint: {endpoint}")
        if not self.authenticated:
            raise CapitalDemoError("Authenticate before Capital.com GET requests")
        started = time.perf_counter()
        response = None
        try:
            response = self.session.request(
                "GET",
                f"{DEMO_BASE_URL}{API_PREFIX}{GET_ENDPOINTS[endpoint]}",
                params=params,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            self._record(endpoint, started, None, type(exc).__name__)
            raise CapitalDemoError(
                f"Capital.com {endpoint} failed: {type(exc).__name__}"
            ) from exc
        self._record(
            endpoint,
            started,
            response,
            None if 200 <= response.status_code < 300 else "http_error",
        )
        if not 200 <= response.status_code < 300:
            raise CapitalDemoError(
                f"Capital.com {endpoint} returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CapitalDemoError(
                f"Capital.com {endpoint} returned invalid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise CapitalDemoError(
                f"Capital.com {endpoint} response must be an object"
            )
        return payload

    def snapshot(self, search_terms: Sequence[str]) -> dict[str, Any]:
        self.authenticate()
        accounts = list(self._get("accounts").get("accounts", []))
        positions_payload = self._get("positions")
        orders_payload = self._get("working_orders")
        markets = {}
        for term in search_terms:
            payload = self._get("markets", params={"searchTerm": term})
            markets[term] = [
                dict(item)
                for item in payload.get("markets", [])
                if isinstance(item, Mapping)
            ]
        return {
            "account_fingerprints": sorted(
                _fingerprint(
                    str(item.get("accountId") or item.get("accountName") or index)
                )
                for index, item in enumerate(accounts)
            ),
            "accounts": [_redact(item) for item in accounts],
            "positions": _redact(
                list(positions_payload.get("positions", []))
            ),
            "working_orders": _redact(
                list(orders_payload.get("workingOrders", []))
            ),
            "markets": markets,
        }


class CapitalDemoOlap:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
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
                account_fingerprint TEXT NOT NULL,
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
                error_kind TEXT
            );
            CREATE TABLE IF NOT EXISTS reconciliation_snapshots (
                session_id TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                open_positions INTEGER NOT NULL,
                working_orders INTEGER NOT NULL,
                accounts_json TEXT NOT NULL,
                reconciliation_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_observations (
                session_id TEXT NOT NULL,
                search_term TEXT NOT NULL,
                epic TEXT NOT NULL,
                instrument_name TEXT,
                market_status TEXT,
                bid REAL,
                offer REAL,
                mid REAL,
                spread REAL,
                spread_bps REAL,
                observed_at TEXT NOT NULL,
                market_json TEXT NOT NULL,
                PRIMARY KEY (session_id,search_term,epic)
            );
            CREATE VIEW IF NOT EXISTS capital_endpoint_health_olap AS
            SELECT endpoint,COUNT(*) AS requests,SUM(success) AS successes,
                   AVG(latency_ms) AS mean_latency_ms,
                   MAX(latency_ms) AS max_latency_ms,
                   MAX(observed_at) AS last_observed_at
            FROM endpoint_probes GROUP BY endpoint;
            CREATE VIEW IF NOT EXISTS capital_market_olap AS
            SELECT search_term,epic,instrument_name,COUNT(*) AS observations,
                   AVG(spread_bps) AS mean_spread_bps,
                   MAX(observed_at) AS last_observed_at
            FROM market_observations
            GROUP BY search_term,epic,instrument_name;
            """
        )
        self.connection.commit()

    def record(
        self,
        snapshot: Mapping[str, Any],
        probes: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        session_id = f"capital-{uuid.uuid4().hex[:16]}"
        started_at = _utc_now()
        fleet = _fingerprint(
            "|".join(snapshot.get("account_fingerprints", [])) or "unknown"
        )
        result = {
            "session_id": session_id,
            "environment": "demo",
            "mode": "get_only",
            "account_fingerprint": fleet,
            "open_positions": len(snapshot["positions"]),
            "working_orders": len(snapshot["working_orders"]),
            "search_terms": len(snapshot["markets"]),
            "orders_submitted": 0,
            "database_path": str(self.path),
        }
        self.connection.execute(
            "INSERT INTO lab_sessions VALUES (?,?,?,?,?,?,?,?)",
            (
                session_id,
                OLAP_SCHEMA,
                ADAPTER_VERSION,
                fleet,
                started_at,
                _utc_now(),
                "complete",
                _canonical_json(result),
            ),
        )
        self.connection.executemany(
            "INSERT INTO endpoint_probes VALUES (NULL,?,?,?,?,?,?,?)",
            [
                (
                    session_id,
                    probe["endpoint"],
                    probe["observed_at"],
                    probe["latency_ms"],
                    probe.get("http_status"),
                    int(bool(probe["success"])),
                    probe.get("error_kind"),
                )
                for probe in probes
            ],
        )
        reconciliation = {
            "positions": snapshot["positions"],
            "working_orders": snapshot["working_orders"],
        }
        self.connection.execute(
            "INSERT INTO reconciliation_snapshots VALUES (?,?,?,?,?,?)",
            (
                session_id,
                _utc_now(),
                len(snapshot["positions"]),
                len(snapshot["working_orders"]),
                _canonical_json(snapshot["accounts"]),
                _canonical_json(reconciliation),
            ),
        )
        for term, markets in snapshot["markets"].items():
            for market in markets:
                epic = str(market.get("epic") or "").strip()
                if not epic:
                    continue
                snapshot_data = market.get("snapshot") or {}
                bid = _as_float(
                    market.get("bid")
                    if market.get("bid") is not None
                    else snapshot_data.get("bid")
                )
                offer = _as_float(
                    market.get("offer")
                    if market.get("offer") is not None
                    else snapshot_data.get("offer")
                )
                mid = (
                    (bid + offer) / 2.0
                    if bid is not None and offer is not None
                    else None
                )
                spread = (
                    offer - bid
                    if bid is not None and offer is not None
                    else None
                )
                self.connection.execute(
                    """
                    INSERT INTO market_observations VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        session_id,
                        term,
                        epic,
                        market.get("instrumentName"),
                        market.get("marketStatus")
                        or snapshot_data.get("marketStatus"),
                        bid,
                        offer,
                        mid,
                        spread,
                        spread / mid * 10000.0 if spread is not None and mid else None,
                        _utc_now(),
                        _canonical_json(market),
                    ),
                )
        self.connection.commit()
        return result

    def report(self) -> dict[str, Any]:
        latest = self.connection.execute(
            """
            SELECT s.session_id,s.status,s.started_at,s.ended_at,
                   r.open_positions,r.working_orders
            FROM lab_sessions s
            LEFT JOIN reconciliation_snapshots r ON r.session_id=s.session_id
            ORDER BY s.started_at DESC LIMIT 1
            """
        ).fetchone()
        endpoints = self.connection.execute(
            "SELECT * FROM capital_endpoint_health_olap ORDER BY endpoint"
        ).fetchall()
        markets = self.connection.execute(
            "SELECT * FROM capital_market_olap ORDER BY search_term,epic"
        ).fetchall()
        return {
            "schema_version": OLAP_SCHEMA,
            "adapter_version": ADAPTER_VERSION,
            "database_path": str(self.path),
            "latest_session": dict(latest) if latest else None,
            "endpoint_health": [dict(row) for row in endpoints],
            "market_capabilities": [dict(row) for row in markets],
        }
