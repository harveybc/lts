"""Read-only IBKR TWS Paper capability and reconciliation laboratory."""

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
from typing import Any, Dict, Mapping, Optional, Sequence


SCHEMA_VERSION = "lts.ibkr.paper_olap.v1"
ADAPTER_VERSION = "lts.ibkr.paper.readonly.v1"
IB_ASYNC_VERSION = "2.1.0"
PAPER_PORTS = {7497: "tws", 4002: "ib_gateway"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _as_price(value: Any) -> Optional[float]:
    result = _as_float(value)
    return result if result is not None and result > 0 else None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class IbkrPaperError(RuntimeError):
    """Raised when the TWS Paper contract or a safety gate fails."""


@dataclass(frozen=True)
class ContractSelection:
    cell_id: str
    canonical_asset: str
    symbol: str
    security_type: str
    currency: str
    exchange: str
    role: str
    timeframe: str
    priority: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ContractSelection":
        required = (
            "cell_id",
            "canonical_asset",
            "symbol",
            "security_type",
            "currency",
            "exchange",
            "role",
            "timeframe",
            "priority",
        )
        missing = [key for key in required if value.get(key) in (None, "")]
        if missing:
            raise IbkrPaperError(
                f"IBKR contract selection is missing: {', '.join(missing)}"
            )
        security_type = str(value["security_type"]).upper()
        if security_type not in {"STK", "CASH"}:
            raise IbkrPaperError(f"Unsupported initial IBKR security type: {security_type}")
        return cls(
            cell_id=str(value["cell_id"]),
            canonical_asset=str(value["canonical_asset"]).lower(),
            symbol=str(value["symbol"]).upper(),
            security_type=security_type,
            currency=str(value["currency"]).upper(),
            exchange=str(value["exchange"]).upper(),
            role=str(value["role"]),
            timeframe=str(value["timeframe"]),
            priority=int(value["priority"]),
        )


@dataclass(frozen=True)
class IbkrPaperLabConfig:
    host: str
    port: int
    client_id: int
    timeout_seconds: float
    market_data_wait_seconds: float
    database_path: Path
    contracts: tuple[ContractSelection, ...]

    @classmethod
    def load(cls, path: Path | str) -> "IbkrPaperLabConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema") != "lts.ibkr.paper_lab_config.v1":
            raise IbkrPaperError("Unsupported or missing IBKR Paper config schema")
        if data.get("environment") != "paper":
            raise IbkrPaperError("The IBKR execution laboratory is paper-only")
        if not bool(data.get("read_only", True)):
            raise IbkrPaperError("IBKR API must remain read-only during capability discovery")
        if data.get("account") or data.get("username") or data.get("password"):
            raise IbkrPaperError("IBKR identity and credentials cannot be tracked in config")

        host = str(data.get("host", "127.0.0.1"))
        if host not in {"127.0.0.1", "localhost"}:
            raise IbkrPaperError("Initial IBKR TWS access is restricted to localhost")
        port = int(data.get("port", 7497))
        if port not in PAPER_PORTS:
            raise IbkrPaperError(
                f"Port {port} is not an approved TWS/IB Gateway Paper port"
            )
        client_id = int(data.get("client_id", 71))
        if not 0 <= client_id <= 31:
            raise IbkrPaperError("IBKR client_id must be between 0 and 31")
        timeout_seconds = float(data.get("timeout_seconds", 15.0))
        if timeout_seconds <= 0:
            raise IbkrPaperError("timeout_seconds must be positive")
        market_data_wait_seconds = float(data.get("market_data_wait_seconds", 4.0))
        if not 1.0 <= market_data_wait_seconds <= 10.0:
            raise IbkrPaperError(
                "market_data_wait_seconds must be between 1 and 10"
            )

        contracts = tuple(
            sorted(
                (ContractSelection.from_dict(item) for item in data.get("contracts", [])),
                key=lambda item: item.priority,
            )
        )
        if not contracts:
            raise IbkrPaperError("At least one IBKR contract selection is required")
        cell_ids = [item.cell_id for item in contracts]
        if len(cell_ids) != len(set(cell_ids)):
            raise IbkrPaperError("IBKR contract cell_id values must be unique")

        database_path = Path(
            os.path.expandvars(
                os.path.expanduser(
                    str(
                        data.get(
                            "database_path",
                            "~/.local/state/lts/ibkr-paper-lab.sqlite",
                        )
                    )
                )
            )
        )
        return cls(
            host=host,
            port=port,
            client_id=client_id,
            timeout_seconds=timeout_seconds,
            market_data_wait_seconds=market_data_wait_seconds,
            database_path=database_path,
            contracts=contracts,
        )


class IbkrTwsPaperClient:
    """Synchronous read-only snapshot client backed by current ``ib_async``."""

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        *,
        timeout_seconds: float = 15.0,
        market_data_wait_seconds: float = 4.0,
    ) -> None:
        if host not in {"127.0.0.1", "localhost"}:
            raise IbkrPaperError("IBKR TWS must be local during initial integration")
        if port not in PAPER_PORTS:
            raise IbkrPaperError("Refusing to connect to a non-Paper IBKR port")
        self.host = host
        self.port = port
        self.client_id = client_id
        self.timeout_seconds = timeout_seconds
        self.market_data_wait_seconds = market_data_wait_seconds
        self.probes: list[Dict[str, Any]] = []

    def _measure(self, endpoint: str, operation) -> Any:
        observed_at = _utc_now()
        started = time.perf_counter()
        try:
            result = operation()
        except Exception as exc:
            self.probes.append(
                {
                    "endpoint": endpoint,
                    "observed_at": observed_at,
                    "latency_ms": (time.perf_counter() - started) * 1000.0,
                    "success": False,
                    "error_kind": type(exc).__name__,
                }
            )
            raise
        self.probes.append(
            {
                "endpoint": endpoint,
                "observed_at": observed_at,
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "success": True,
                "error_kind": None,
            }
        )
        return result

    @staticmethod
    def _make_contract(selection: ContractSelection):
        from ib_async import Forex, Stock

        if selection.security_type == "CASH":
            return Forex(selection.symbol, exchange=selection.exchange)
        return Stock(selection.symbol, selection.exchange, selection.currency)

    @staticmethod
    def _contract_dict(contract: Any) -> Dict[str, Any]:
        return {
            "con_id": int(getattr(contract, "conId", 0) or 0),
            "symbol": getattr(contract, "symbol", ""),
            "local_symbol": getattr(contract, "localSymbol", ""),
            "security_type": getattr(contract, "secType", ""),
            "currency": getattr(contract, "currency", ""),
            "exchange": getattr(contract, "exchange", ""),
            "primary_exchange": getattr(contract, "primaryExchange", ""),
            "trading_class": getattr(contract, "tradingClass", ""),
        }

    def snapshot(self, selections: Sequence[ContractSelection]) -> Dict[str, Any]:
        try:
            import ib_async
            from ib_async import IB
        except ImportError as exc:
            raise IbkrPaperError(
                f"Install ib_async=={IB_ASYNC_VERSION} in the trading-stack environment"
            ) from exc
        if ib_async.__version__ != IB_ASYNC_VERSION:
            raise IbkrPaperError(
                f"Expected ib_async {IB_ASYNC_VERSION}, found {ib_async.__version__}"
            )

        ib = IB()
        try:
            self._measure(
                "connect",
                lambda: ib.connect(
                    self.host,
                    self.port,
                    clientId=self.client_id,
                    timeout=self.timeout_seconds,
                    readonly=True,
                    raiseSyncErrors=True,
                ),
            )
            accounts = list(ib.managedAccounts())
            if not accounts:
                raise IbkrPaperError("TWS returned no managed Paper account")
            if any(not account.upper().startswith("DU") for account in accounts):
                raise IbkrPaperError(
                    "Connected account does not have an IBKR Paper DU identifier"
                )

            account_values = self._measure("account_summary", ib.accountSummary)
            positions = self._measure("positions", ib.positions)
            open_trades = self._measure("orders.all_open", ib.reqAllOpenOrders)
            open_orders = [trade.order for trade in open_trades]

            selection_by_cell = {
                selection.cell_id: selection for selection in selections
            }
            qualified: Dict[str, Optional[Dict[str, Any]]] = {}
            qualified_objects: Dict[str, Any] = {}
            for selection in selections:
                contract = self._make_contract(selection)
                matches = self._measure(
                    f"contract.{selection.cell_id}",
                    lambda contract=contract: ib.qualifyContracts(contract),
                )
                qualified[selection.cell_id] = (
                    self._contract_dict(matches[0]) if matches else None
                )
                if matches:
                    qualified_objects[selection.cell_id] = matches[0]

            quotes: Dict[str, Dict[str, Any]] = {}
            tickers: Dict[str, Any] = {}
            if qualified_objects:
                ib.reqMarketDataType(3)
                for cell_id, contract in qualified_objects.items():
                    tickers[cell_id] = self._measure(
                        f"quote.request.{cell_id}",
                        lambda contract=contract: ib.reqMktData(
                            contract,
                            genericTickList="",
                            snapshot=False,
                            regulatorySnapshot=False,
                        ),
                    )
                ib.sleep(self.market_data_wait_seconds)
                for cell_id, ticker in tickers.items():
                    bid = _as_price(getattr(ticker, "bid", None))
                    ask = _as_price(getattr(ticker, "ask", None))
                    last = _as_price(getattr(ticker, "last", None))
                    close = _as_price(getattr(ticker, "close", None))
                    mid = (
                        (bid + ask) / 2.0
                        if bid is not None and ask is not None
                        else None
                    )
                    spread = (
                        ask - bid
                        if bid is not None and ask is not None
                        else None
                    )
                    mark_price = mid or last or close
                    broker_time = getattr(ticker, "time", None)
                    quotes[cell_id] = {
                        "cell_id": cell_id,
                        "symbol": selection_by_cell[cell_id].symbol,
                        "broker_time": (
                            broker_time.isoformat()
                            if hasattr(broker_time, "isoformat")
                            else None
                        ),
                        "observed_at": _utc_now(),
                        "bid": bid,
                        "ask": ask,
                        "mid": mid,
                        "last": last,
                        "close": close,
                        "mark_price": mark_price,
                        "spread": spread,
                        "spread_bps": (
                            spread / mid * 10000.0
                            if spread is not None and mid
                            else None
                        ),
                        "bid_size": _as_float(getattr(ticker, "bidSize", None)),
                        "ask_size": _as_float(getattr(ticker, "askSize", None)),
                        "market_data_type": getattr(
                            ticker, "marketDataType", None
                        ),
                    }
                for ticker in tickers.values():
                    ib.cancelMktData(ticker.contract)

            summaries: list[Dict[str, Any]] = []
            for item in account_values:
                summaries.append(
                    {
                        "account_fingerprint": _fingerprint(str(item.account)),
                        "tag": item.tag,
                        "value": item.value,
                        "currency": item.currency,
                        "model_code": item.modelCode,
                    }
                )
            normalized_positions = [
                {
                    "account_fingerprint": _fingerprint(str(item.account)),
                    "contract": self._contract_dict(item.contract),
                    "position": float(item.position),
                    "average_cost": float(item.avgCost),
                }
                for item in positions
            ]
            normalized_orders = [
                {
                    "order_fingerprint": _fingerprint(str(item.orderId)),
                    "action": item.action,
                    "quantity": float(item.totalQuantity),
                    "order_type": item.orderType,
                    "limit_price": _as_float(item.lmtPrice),
                    "aux_price": _as_float(item.auxPrice),
                    "time_in_force": item.tif,
                    "transmit": bool(item.transmit),
                }
                for item in open_orders
            ]
            return {
                "account_fingerprints": sorted(_fingerprint(item) for item in accounts),
                "api_version": ib_async.__version__,
                "server_version": ib.client.serverVersion(),
                "platform": PAPER_PORTS[self.port],
                "account_summary": summaries,
                "positions": normalized_positions,
                "open_orders": normalized_orders,
                "contracts": qualified,
                "quotes": quotes,
            }
        except IbkrPaperError:
            raise
        except Exception as exc:
            raise IbkrPaperError(f"IBKR Paper snapshot failed: {type(exc).__name__}") from exc
        finally:
            if ib.isConnected():
                ib.disconnect()


class IbkrPaperOlap:
    """Local IBKR Paper facts without usernames, account IDs or credentials."""

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
                success INTEGER NOT NULL,
                error_kind TEXT,
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS account_summary (
                session_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                value TEXT NOT NULL,
                currency TEXT,
                model_code TEXT,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (session_id,tag,currency,model_code),
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS contract_capabilities (
                session_id TEXT NOT NULL,
                cell_id TEXT NOT NULL,
                canonical_asset TEXT NOT NULL,
                symbol TEXT NOT NULL,
                security_type TEXT NOT NULL,
                currency TEXT NOT NULL,
                exchange_name TEXT NOT NULL,
                role TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                priority INTEGER NOT NULL,
                available INTEGER NOT NULL,
                con_id INTEGER,
                local_symbol TEXT,
                trading_class TEXT,
                protected_execution_eligible INTEGER NOT NULL,
                capability_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (session_id,cell_id),
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS reconciliation_snapshots (
                session_id TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                open_positions INTEGER NOT NULL,
                open_orders INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS quote_observations (
                session_id TEXT NOT NULL,
                cell_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                broker_time TEXT,
                observed_at TEXT NOT NULL,
                bid REAL,
                ask REAL,
                mid REAL,
                last REAL,
                close REAL,
                mark_price REAL,
                spread REAL,
                spread_bps REAL,
                bid_size REAL,
                ask_size REAL,
                market_data_type INTEGER,
                quote_json TEXT NOT NULL,
                PRIMARY KEY (session_id,cell_id),
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE VIEW IF NOT EXISTS ibkr_endpoint_health_olap AS
            SELECT endpoint,COUNT(*) AS requests,SUM(success) AS successful_requests,
                   AVG(latency_ms) AS mean_latency_ms,MAX(latency_ms) AS max_latency_ms,
                   MAX(observed_at) AS last_observed_at
            FROM endpoint_probes GROUP BY endpoint;
            CREATE VIEW IF NOT EXISTS ibkr_capability_olap AS
            SELECT cell_id,canonical_asset,symbol,security_type,currency,role,timeframe,
                   COUNT(*) AS observations,SUM(available) AS available_observations,
                   MAX(protected_execution_eligible) AS protected_execution_eligible,
                   MAX(observed_at) AS last_observed_at
            FROM contract_capabilities
            GROUP BY cell_id,canonical_asset,symbol,security_type,currency,role,timeframe;
            CREATE VIEW IF NOT EXISTS ibkr_quote_summary_olap AS
            SELECT cell_id,symbol,COUNT(*) AS observations,
                   MIN(observed_at) AS first_observed_at,
                   MAX(observed_at) AS last_observed_at,
                   AVG(spread_bps) AS mean_spread_bps,
                   MIN(spread_bps) AS min_spread_bps,
                   MAX(spread_bps) AS max_spread_bps,
                   SUM(CASE WHEN mark_price IS NOT NULL THEN 1 ELSE 0 END)
                       AS priced_observations
            FROM quote_observations GROUP BY cell_id,symbol;
            """
        )
        self.connection.commit()

    def start_session(
        self,
        account_fingerprint: str,
        config: Mapping[str, Any],
    ) -> str:
        session_id = f"preflight-{uuid.uuid4().hex[:16]}"
        config_json = _canonical_json(config)
        self.connection.execute(
            """
            INSERT INTO lab_sessions VALUES
            (?, ?, ?, ?, ?, ?, NULL, 'running', '{}')
            """,
            (
                session_id,
                SCHEMA_VERSION,
                ADAPTER_VERSION,
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
            UPDATE lab_sessions SET ended_at=?,status=?,detail_json=? WHERE session_id=?
            """,
            (_utc_now(), status, _canonical_json(detail), session_id),
        )
        self.connection.commit()

    def record_snapshot(
        self,
        session_id: str,
        selections: Sequence[ContractSelection],
        snapshot: Mapping[str, Any],
        probes: Sequence[Mapping[str, Any]],
    ) -> None:
        self.connection.executemany(
            """
            INSERT INTO endpoint_probes(
                session_id,endpoint,observed_at,latency_ms,success,error_kind
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    session_id,
                    probe["endpoint"],
                    probe["observed_at"],
                    probe["latency_ms"],
                    int(bool(probe["success"])),
                    probe.get("error_kind"),
                )
                for probe in probes
            ],
        )
        self.connection.executemany(
            """
            INSERT INTO account_summary VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    session_id,
                    item["tag"],
                    item["value"],
                    item.get("currency"),
                    item.get("model_code"),
                    _utc_now(),
                )
                for item in snapshot["account_summary"]
            ],
        )
        contracts = snapshot["contracts"]
        for selection in selections:
            contract = contracts.get(selection.cell_id)
            normalized = {
                "contract": contract,
                "documented_bracket_support": True,
                "native_sl_tp_verified": False,
                "protected_execution_eligible": False,
            }
            self.connection.execute(
                """
                INSERT INTO contract_capabilities(
                    session_id,cell_id,canonical_asset,symbol,security_type,currency,
                    exchange_name,role,timeframe,priority,available,con_id,local_symbol,
                    trading_class,protected_execution_eligible,capability_json,observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    selection.cell_id,
                    selection.canonical_asset,
                    selection.symbol,
                    selection.security_type,
                    selection.currency,
                    selection.exchange,
                    selection.role,
                    selection.timeframe,
                    selection.priority,
                    int(contract is not None),
                    contract.get("con_id") if contract else None,
                    contract.get("local_symbol") if contract else None,
                    contract.get("trading_class") if contract else None,
                    0,
                    _canonical_json(normalized),
                    _utc_now(),
                ),
            )
        for cell_id, quote in snapshot.get("quotes", {}).items():
            self.connection.execute(
                """
                INSERT OR REPLACE INTO quote_observations VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    cell_id,
                    quote["symbol"],
                    quote.get("broker_time"),
                    quote["observed_at"],
                    quote.get("bid"),
                    quote.get("ask"),
                    quote.get("mid"),
                    quote.get("last"),
                    quote.get("close"),
                    quote.get("mark_price"),
                    quote.get("spread"),
                    quote.get("spread_bps"),
                    quote.get("bid_size"),
                    quote.get("ask_size"),
                    quote.get("market_data_type"),
                    _canonical_json(quote),
                ),
            )
        reconciliation = {
            "positions": snapshot["positions"],
            "open_orders": snapshot["open_orders"],
        }
        self.connection.execute(
            """
            INSERT INTO reconciliation_snapshots VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                _utc_now(),
                len(snapshot["positions"]),
                len(snapshot["open_orders"]),
                _canonical_json(reconciliation),
            ),
        )
        self.connection.commit()

    def report(self) -> Dict[str, Any]:
        latest = self.connection.execute(
            """
            SELECT s.session_id,s.status,s.started_at,s.ended_at,
                   r.open_positions,r.open_orders
            FROM lab_sessions s
            LEFT JOIN reconciliation_snapshots r ON r.session_id=s.session_id
            ORDER BY s.started_at DESC LIMIT 1
            """
        ).fetchone()
        endpoints = self.connection.execute(
            "SELECT * FROM ibkr_endpoint_health_olap ORDER BY endpoint"
        ).fetchall()
        capabilities = self.connection.execute(
            "SELECT * FROM ibkr_capability_olap ORDER BY role,timeframe,symbol"
        ).fetchall()
        quotes = self.connection.execute(
            "SELECT * FROM ibkr_quote_summary_olap ORDER BY cell_id"
        ).fetchall()
        latest_quotes = self.connection.execute(
            """
            SELECT q.cell_id,q.symbol,q.observed_at,q.mark_price,q.spread_bps,
                   q.market_data_type
            FROM quote_observations q
            JOIN (
                SELECT cell_id,MAX(observed_at) AS observed_at
                FROM quote_observations GROUP BY cell_id
            ) latest
              ON latest.cell_id=q.cell_id AND latest.observed_at=q.observed_at
            ORDER BY q.cell_id
            """
        ).fetchall()
        return {
            "schema_version": SCHEMA_VERSION,
            "adapter_version": ADAPTER_VERSION,
            "database_path": str(self.path),
            "latest_session": dict(latest) if latest else None,
            "endpoint_health": [dict(row) for row in endpoints],
            "contract_capabilities": [dict(row) for row in capabilities],
            "quote_summary": [dict(row) for row in quotes],
            "latest_quotes": [dict(row) for row in latest_quotes],
        }


class IbkrPaperLab:
    """Runs a read-only TWS Paper snapshot and persists normalized evidence."""

    def __init__(
        self,
        config: IbkrPaperLabConfig,
        client: IbkrTwsPaperClient,
        store: IbkrPaperOlap,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store

    def _config_evidence(self) -> Dict[str, Any]:
        return {
            "schema": "lts.ibkr.paper_lab_config.v1",
            "environment": "paper",
            "read_only": True,
            "adapter_version": ADAPTER_VERSION,
            "ib_async_version": IB_ASYNC_VERSION,
            "host": self.config.host,
            "port": self.config.port,
            "platform": PAPER_PORTS[self.config.port],
            "market_data_wait_seconds": self.config.market_data_wait_seconds,
            "contracts": [item.__dict__ for item in self.config.contracts],
        }

    def preflight(self) -> Dict[str, Any]:
        snapshot = self.client.snapshot(self.config.contracts)
        fingerprints = snapshot["account_fingerprints"]
        fleet_fingerprint = _fingerprint("|".join(fingerprints))
        session_id = self.store.start_session(
            fleet_fingerprint,
            self._config_evidence(),
        )
        try:
            self.store.record_snapshot(
                session_id,
                self.config.contracts,
                snapshot,
                self.client.probes,
            )
            available = [
                item.cell_id
                for item in self.config.contracts
                if snapshot["contracts"].get(item.cell_id)
            ]
            missing = [
                item.cell_id
                for item in self.config.contracts
                if not snapshot["contracts"].get(item.cell_id)
            ]
            result = {
                "session_id": session_id,
                "environment": "paper",
                "read_only": True,
                "adapter_version": ADAPTER_VERSION,
                "api_version": snapshot["api_version"],
                "server_version": snapshot["server_version"],
                "platform": snapshot["platform"],
                "account_fingerprint": fleet_fingerprint,
                "available_cells": available,
                "missing_cells": missing,
                "priced_cells": sorted(
                    cell_id
                    for cell_id, quote in snapshot.get("quotes", {}).items()
                    if quote.get("mark_price") is not None
                ),
                "open_positions": len(snapshot["positions"]),
                "open_orders": len(snapshot["open_orders"]),
                "protected_execution_eligible": False,
                "orders_submitted": 0,
                "database_path": str(self.store.path),
            }
            self.store.finish_session(session_id, "complete", result)
            return result
        except Exception as exc:
            self.store.finish_session(
                session_id,
                "failed",
                {"error_kind": type(exc).__name__},
            )
            raise
