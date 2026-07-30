"""OANDA Practice execution-observation laboratory.

The lab is independent from model training. It discovers the broker contract,
records broker-observed facts in SQLite, and submits a minimal protected canary
only after two independent opt-ins.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import statistics
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import requests


PRACTICE_BASE_URL = "https://api-fxpractice.oanda.com"
ORDER_CONFIRMATION = "ENABLE_PROTECTED_OANDA_PRACTICE_ORDERS"
SCHEMA_VERSION = "lts.oanda.practice_olap.v1"
_SENSITIVE_KEYS = {
    "accountid",
    "account_id",
    "authorization",
    "requestid",
    "token",
    "access_token",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    return float(value)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: ("<redacted>" if key.lower() in _SENSITIVE_KEYS else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_redact(value), sort_keys=True, separators=(",", ":"))


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class OandaPracticeError(RuntimeError):
    """Raised when the Practice API contract or a safety gate fails."""


@dataclass(frozen=True)
class InstrumentSelection:
    canonical_asset: str
    oanda_instrument: str
    role: str
    timeframe: str
    priority: int
    artifact_job_id: Optional[str] = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InstrumentSelection":
        required = ("canonical_asset", "oanda_instrument", "role", "timeframe", "priority")
        missing = [key for key in required if value.get(key) in (None, "")]
        if missing:
            raise OandaPracticeError(
                f"Instrument selection is missing required fields: {', '.join(missing)}"
            )
        return cls(
            canonical_asset=str(value["canonical_asset"]).lower(),
            oanda_instrument=str(value["oanda_instrument"]).upper(),
            role=str(value["role"]),
            timeframe=str(value["timeframe"]),
            priority=int(value["priority"]),
            artifact_job_id=(
                str(value["artifact_job_id"]) if value.get("artifact_job_id") else None
            ),
        )


@dataclass(frozen=True)
class PracticeLabConfig:
    account_id_env: str
    access_token_env: str
    database_path: Path
    instruments: tuple[InstrumentSelection, ...]
    price_poll_seconds: float
    account_poll_seconds: float
    transaction_poll_seconds: float
    orders_enabled: bool

    @classmethod
    def load(cls, path: Path | str) -> "PracticeLabConfig":
        source = Path(path)
        data = json.loads(source.read_text(encoding="utf-8"))
        if data.get("schema") != "lts.oanda.practice_lab_config.v1":
            raise OandaPracticeError("Unsupported or missing Practice lab config schema")
        if data.get("environment") != "practice":
            raise OandaPracticeError("The execution laboratory is practice-only")

        secrets = data.get("secrets", {})
        account_id_env = str(secrets.get("account_id_env", ""))
        access_token_env = str(secrets.get("access_token_env", ""))
        if not account_id_env or not access_token_env:
            raise OandaPracticeError("Secret environment-variable names are required")
        if "account_id" in data or "access_token" in data:
            raise OandaPracticeError("Credentials must not be embedded in the config")

        polling = data.get("polling", {})
        price_poll = float(polling.get("price_seconds", 5.0))
        account_poll = float(polling.get("account_seconds", 60.0))
        transaction_poll = float(polling.get("transaction_seconds", 5.0))
        if min(price_poll, account_poll, transaction_poll) <= 0:
            raise OandaPracticeError("Polling intervals must be positive")

        selections = tuple(
            sorted(
                (InstrumentSelection.from_dict(item) for item in data.get("instruments", [])),
                key=lambda item: item.priority,
            )
        )
        if not selections:
            raise OandaPracticeError("At least one instrument selection is required")
        symbols = [item.oanda_instrument for item in selections]
        if len(symbols) != len(set(symbols)):
            raise OandaPracticeError("OANDA instruments must be unique")

        database_path = Path(
            os.path.expandvars(
                os.path.expanduser(
                    str(
                        data.get(
                            "database_path",
                            "~/.local/state/lts/oanda-practice-lab.sqlite",
                        )
                    )
                )
            )
        )
        return cls(
            account_id_env=account_id_env,
            access_token_env=access_token_env,
            database_path=database_path,
            instruments=selections,
            price_poll_seconds=price_poll,
            account_poll_seconds=account_poll,
            transaction_poll_seconds=transaction_poll,
            orders_enabled=bool(data.get("orders", {}).get("enabled", False)),
        )

    def credentials(self, environment: Optional[Mapping[str, str]] = None) -> tuple[str, str]:
        source = environment if environment is not None else os.environ
        account_id = source.get(self.account_id_env, "")
        access_token = source.get(self.access_token_env, "")
        if not account_id or not access_token:
            raise OandaPracticeError(
                f"Set {self.account_id_env} and {self.access_token_env} before connecting"
            )
        return account_id, access_token


class OandaPracticeClient:
    """REST-v20 Practice client with an injectable HTTP session."""

    def __init__(
        self,
        account_id: str,
        access_token: str,
        *,
        session: Optional[requests.Session] = None,
        timeout_seconds: float = 20.0,
        base_url: str = PRACTICE_BASE_URL,
    ) -> None:
        if base_url.rstrip("/") != PRACTICE_BASE_URL:
            raise OandaPracticeError("The execution laboratory cannot connect to OANDA live")
        if not account_id or not access_token:
            raise OandaPracticeError("OANDA account ID and access token are required")
        self.account_id = account_id
        self.timeout_seconds = timeout_seconds
        self.base_url = PRACTICE_BASE_URL
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept-Datetime-Format": "RFC3339",
                "User-Agent": "lts-oanda-practice-lab/1",
            }
        )

    @property
    def account_fingerprint(self) -> str:
        return hashlib.sha256(self.account_id.encode("utf-8")).hexdigest()[:16]

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            params=params,
            json=body,
            timeout=self.timeout_seconds,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OandaPracticeError(
                f"OANDA returned non-JSON HTTP {response.status_code}"
            ) from exc
        if response.status_code >= 400:
            message = payload.get("errorMessage") or payload.get("errorCode") or payload
            raise OandaPracticeError(f"OANDA HTTP {response.status_code}: {message}")
        return payload

    def account_details(self) -> Dict[str, Any]:
        return self._request("GET", f"/v3/accounts/{self.account_id}")

    def instruments(self) -> Dict[str, Any]:
        return self._request("GET", f"/v3/accounts/{self.account_id}/instruments")

    def prices(self, instruments: Sequence[str]) -> Dict[str, Any]:
        if not instruments:
            return {"prices": [], "time": _utc_now()}
        return self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/pricing",
            params={"instruments": ",".join(instruments), "includeHomeConversions": "true"},
        )

    def transactions_since(self, transaction_id: str) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/transactions/sinceid",
            params={"id": transaction_id},
        )

    def create_order(self, order: Mapping[str, Any]) -> Dict[str, Any]:
        if not order.get("stopLossOnFill") or not order.get("takeProfitOnFill"):
            raise OandaPracticeError("Every risk-increasing order requires SL and TP")
        return self._request(
            "POST",
            f"/v3/accounts/{self.account_id}/orders",
            body={"order": dict(order)},
        )


class PracticeOlap:
    """Restart-safe local analytical store for Practice observations."""

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
                phase TEXT NOT NULL,
                account_fingerprint TEXT NOT NULL,
                config_sha256 TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL,
                detail_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lab_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS instrument_capabilities (
                session_id TEXT NOT NULL,
                canonical_asset TEXT NOT NULL,
                instrument TEXT NOT NULL,
                role TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                priority INTEGER NOT NULL,
                available INTEGER NOT NULL,
                display_precision INTEGER,
                pip_location INTEGER,
                minimum_trade_size REAL,
                margin_rate REAL,
                capability_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (session_id, instrument),
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                broker_time TEXT,
                balance REAL,
                nav REAL,
                unrealized_pl REAL,
                realized_pl REAL,
                financing REAL,
                commission REAL,
                margin_used REAL,
                margin_available REAL,
                margin_closeout_percent REAL,
                open_trade_count INTEGER,
                open_position_count INTEGER,
                pending_order_count INTEGER,
                last_transaction_id TEXT,
                snapshot_json TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS price_observations (
                session_id TEXT NOT NULL,
                instrument TEXT NOT NULL,
                broker_time TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                status TEXT,
                bid REAL,
                ask REAL,
                mid REAL,
                spread REAL,
                spread_bps REAL,
                bid_liquidity REAL,
                ask_liquidity REAL,
                price_json TEXT NOT NULL,
                PRIMARY KEY (session_id, instrument, broker_time),
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS broker_transactions (
                account_fingerprint TEXT NOT NULL,
                transaction_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                broker_time TEXT,
                transaction_type TEXT,
                instrument TEXT,
                reason TEXT,
                units REAL,
                price REAL,
                pl REAL,
                financing REAL,
                commission REAL,
                half_spread_cost REAL,
                transaction_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (account_fingerprint, transaction_id),
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS order_intents (
                intent_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                instrument TEXT NOT NULL,
                side TEXT NOT NULL,
                units REAL NOT NULL,
                order_type TEXT NOT NULL,
                reference_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                intent_json TEXT NOT NULL,
                status TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS execution_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                broker_order_id TEXT,
                broker_trade_id TEXT,
                fill_price REAL,
                implementation_shortfall REAL,
                rejection_reason TEXT,
                report_json TEXT NOT NULL,
                FOREIGN KEY (intent_id) REFERENCES order_intents(intent_id),
                FOREIGN KEY (session_id) REFERENCES lab_sessions(session_id)
            );
            CREATE VIEW IF NOT EXISTS practice_price_summary_olap AS
            SELECT
                instrument,
                COUNT(*) AS observations,
                MIN(broker_time) AS first_broker_time,
                MAX(broker_time) AS last_broker_time,
                AVG(spread) AS mean_spread,
                AVG(spread_bps) AS mean_spread_bps,
                MIN(spread_bps) AS min_spread_bps,
                MAX(spread_bps) AS max_spread_bps
            FROM price_observations
            GROUP BY instrument;
            CREATE VIEW IF NOT EXISTS practice_execution_summary_olap AS
            SELECT
                i.instrument,
                i.side,
                i.order_type,
                COUNT(*) AS submitted_orders,
                SUM(r.accepted) AS accepted_orders,
                AVG(r.implementation_shortfall) AS mean_implementation_shortfall
            FROM order_intents i
            LEFT JOIN execution_reports r ON r.intent_id=i.intent_id
            GROUP BY i.instrument, i.side, i.order_type;
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
            INSERT INTO lab_sessions VALUES (?, ?, ?, ?, ?, ?, NULL, 'running', '{}')
            """,
            (
                session_id,
                SCHEMA_VERSION,
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

    def get_state(self, key: str) -> Optional[str]:
        row = self.connection.execute(
            "SELECT value FROM lab_state WHERE key=?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO lab_state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (key, value, _utc_now()),
        )
        self.connection.commit()

    def record_capability(
        self,
        session_id: str,
        selection: InstrumentSelection,
        capability: Optional[Mapping[str, Any]],
    ) -> None:
        value = capability or {}
        self.connection.execute(
            """
            INSERT OR REPLACE INTO instrument_capabilities VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                selection.canonical_asset,
                selection.oanda_instrument,
                selection.role,
                selection.timeframe,
                selection.priority,
                int(capability is not None),
                value.get("displayPrecision"),
                value.get("pipLocation"),
                _as_float(value.get("minimumTradeSize")),
                _as_float(value.get("marginRate")),
                _canonical_json(value),
                _utc_now(),
            ),
        )
        self.connection.commit()

    def record_account(self, session_id: str, response: Mapping[str, Any]) -> str:
        account = response.get("account", response)
        persisted_account = dict(account)
        if "id" in persisted_account:
            persisted_account["id"] = "<redacted>"
        transaction_id = str(
            response.get("lastTransactionID") or account.get("lastTransactionID") or ""
        )
        self.connection.execute(
            """
            INSERT INTO account_snapshots(
                session_id,observed_at,broker_time,balance,nav,unrealized_pl,
                realized_pl,financing,commission,margin_used,margin_available,
                margin_closeout_percent,open_trade_count,open_position_count,
                pending_order_count,last_transaction_id,snapshot_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_id,
                _utc_now(),
                account.get("createdTime"),
                _as_float(account.get("balance")),
                _as_float(account.get("NAV")),
                _as_float(account.get("unrealizedPL")),
                _as_float(account.get("pl")),
                _as_float(account.get("financing")),
                _as_float(account.get("commission")),
                _as_float(account.get("marginUsed")),
                _as_float(account.get("marginAvailable")),
                _as_float(account.get("marginCloseoutPercent")),
                account.get("openTradeCount"),
                account.get("openPositionCount"),
                account.get("pendingOrderCount"),
                transaction_id or None,
                _canonical_json(persisted_account),
            ),
        )
        self.connection.commit()
        return transaction_id

    def record_prices(self, session_id: str, response: Mapping[str, Any]) -> int:
        count = 0
        for price in response.get("prices", []):
            bids = price.get("bids") or []
            asks = price.get("asks") or []
            if not bids or not asks or not price.get("time"):
                continue
            bid = float(bids[0]["price"])
            ask = float(asks[0]["price"])
            mid = (bid + ask) / 2.0
            spread = ask - bid
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO price_observations VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    price.get("instrument"),
                    price["time"],
                    _utc_now(),
                    price.get("status"),
                    bid,
                    ask,
                    mid,
                    spread,
                    (spread / mid * 10000.0) if mid else None,
                    sum(float(level.get("liquidity", 0)) for level in bids),
                    sum(float(level.get("liquidity", 0)) for level in asks),
                    _canonical_json(price),
                ),
            )
            count += cursor.rowcount
        self.connection.commit()
        return count

    def record_transactions(
        self,
        session_id: str,
        account_fingerprint: str,
        response: Mapping[str, Any],
    ) -> int:
        count = 0
        for transaction in response.get("transactions", []):
            transaction_id = str(transaction.get("id", ""))
            if not transaction_id:
                continue
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO broker_transactions VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_fingerprint,
                    transaction_id,
                    session_id,
                    transaction.get("time"),
                    transaction.get("type"),
                    transaction.get("instrument"),
                    transaction.get("reason"),
                    _as_float(transaction.get("units")),
                    _as_float(transaction.get("price")),
                    _as_float(transaction.get("pl")),
                    _as_float(transaction.get("financing")),
                    _as_float(transaction.get("commission")),
                    _as_float(transaction.get("halfSpreadCost")),
                    _canonical_json(transaction),
                    _utc_now(),
                ),
            )
            count += cursor.rowcount
        self.connection.commit()
        return count

    def record_intent(self, session_id: str, intent: Mapping[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                intent["intent_id"],
                session_id,
                intent["created_at"],
                intent["instrument"],
                intent["side"],
                intent["units"],
                intent["order_type"],
                intent["reference_price"],
                intent["stop_loss"],
                intent["take_profit"],
                _canonical_json(intent),
                "submitted",
            ),
        )
        self.connection.commit()

    def record_execution(
        self,
        session_id: str,
        intent: Mapping[str, Any],
        *,
        response: Optional[Mapping[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        payload = response or {}
        fill = payload.get("orderFillTransaction", {})
        create = payload.get("orderCreateTransaction", {})
        reject = payload.get("orderRejectTransaction", {})
        fill_price = _as_float(fill.get("price"))
        reference = float(intent["reference_price"])
        side_sign = 1.0 if intent["side"] == "buy" else -1.0
        shortfall = (
            side_sign * (fill_price - reference)
            if fill_price is not None
            else None
        )
        accepted = error is None and not reject
        self.connection.execute(
            """
            INSERT INTO execution_reports(
                intent_id,session_id,observed_at,accepted,broker_order_id,
                broker_trade_id,fill_price,implementation_shortfall,
                rejection_reason,report_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                intent["intent_id"],
                session_id,
                _utc_now(),
                int(accepted),
                fill.get("orderID") or create.get("id"),
                (fill.get("tradeOpened") or {}).get("tradeID"),
                fill_price,
                shortfall,
                error or reject.get("rejectReason"),
                _canonical_json(payload if response is not None else {"error": error}),
            ),
        )
        self.connection.execute(
            "UPDATE order_intents SET status=? WHERE intent_id=?",
            ("accepted" if accepted else "rejected", intent["intent_id"]),
        )
        self.connection.commit()

    def _account_performance(self) -> Dict[str, Any]:
        points = [
            (_parse_timestamp(row["observed_at"]), float(row["nav"]))
            for row in self.connection.execute(
                """
                SELECT observed_at,nav FROM account_snapshots
                WHERE nav IS NOT NULL ORDER BY observed_at,id
                """
            )
        ]
        empty = {
            "observed_hours": 0.0,
            "observed_return_fraction": None,
            "observed_max_drawdown_fraction": None,
            "observed_rap_fraction": None,
            "complete_weeks": 0,
            "mean_weekly_return_fraction": None,
            "annual_return_fraction_additive_52w": None,
            "mean_weekly_rap_fraction": None,
            "annual_rap_fraction_additive_52w": None,
        }
        if len(points) < 2 or points[0][1] <= 0:
            return empty

        observed_hours = (points[-1][0] - points[0][0]).total_seconds() / 3600.0
        observed_return = points[-1][1] / points[0][1] - 1.0
        peak = points[0][1]
        observed_drawdown = 0.0
        for _, nav in points:
            peak = max(peak, nav)
            if peak > 0:
                observed_drawdown = max(observed_drawdown, 1.0 - nav / peak)

        week_seconds = 7.0 * 24.0 * 3600.0
        complete_week_count = int(
            max(0.0, (points[-1][0] - points[0][0]).total_seconds()) // week_seconds
        )
        weekly_returns: list[float] = []
        weekly_rap: list[float] = []
        for week_index in range(complete_week_count):
            start = points[0][0].timestamp() + week_index * week_seconds
            end = start + week_seconds
            week_points = [
                (timestamp, nav)
                for timestamp, nav in points
                if start <= timestamp.timestamp() <= end
            ]
            if len(week_points) < 2 or week_points[0][1] <= 0:
                continue
            week_return = week_points[-1][1] / week_points[0][1] - 1.0
            week_peak = week_points[0][1]
            week_drawdown = 0.0
            for _, nav in week_points:
                week_peak = max(week_peak, nav)
                if week_peak > 0:
                    week_drawdown = max(week_drawdown, 1.0 - nav / week_peak)
            weekly_returns.append(week_return)
            weekly_rap.append(week_return - week_drawdown)

        mean_weekly_return = (
            statistics.mean(weekly_returns) if weekly_returns else None
        )
        mean_weekly_rap = statistics.mean(weekly_rap) if weekly_rap else None
        return {
            "observed_hours": observed_hours,
            "observed_return_fraction": observed_return,
            "observed_max_drawdown_fraction": observed_drawdown,
            "observed_rap_fraction": observed_return - observed_drawdown,
            "complete_weeks": len(weekly_returns),
            "mean_weekly_return_fraction": mean_weekly_return,
            "annual_return_fraction_additive_52w": (
                52.0 * mean_weekly_return
                if mean_weekly_return is not None
                else None
            ),
            "mean_weekly_rap_fraction": mean_weekly_rap,
            "annual_rap_fraction_additive_52w": (
                52.0 * mean_weekly_rap if mean_weekly_rap is not None else None
            ),
        }

    def report(self) -> Dict[str, Any]:
        sessions = self.connection.execute(
            """
            SELECT phase,status,COUNT(*) count,MIN(started_at) first_started,
                   MAX(COALESCE(ended_at,started_at)) last_observed
            FROM lab_sessions GROUP BY phase,status ORDER BY phase,status
            """
        ).fetchall()
        prices = self.connection.execute(
            """
            SELECT instrument,COUNT(*) observations,MIN(broker_time) first_time,
                   MAX(broker_time) last_time,AVG(spread_bps) mean_spread_bps
            FROM price_observations GROUP BY instrument ORDER BY instrument
            """
        ).fetchall()
        distributions: Dict[str, Dict[str, Optional[float]]] = {}
        for row in prices:
            values = [
                float(item[0])
                for item in self.connection.execute(
                    "SELECT spread_bps FROM price_observations "
                    "WHERE instrument=? AND spread_bps IS NOT NULL ORDER BY spread_bps",
                    (row["instrument"],),
                )
            ]
            distributions[row["instrument"]] = {
                "p50_spread_bps": statistics.median(values) if values else None,
                "p95_spread_bps": (
                    values[min(len(values) - 1, int(len(values) * 0.95))]
                    if values
                    else None
                ),
            }
        session_total = sum(int(row["count"]) for row in sessions)
        failed_sessions = sum(
            int(row["count"]) for row in sessions if row["status"] == "failed"
        )
        capabilities = self.connection.execute(
            """
            WITH latest AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY instrument ORDER BY observed_at DESC
                       ) AS latest_rank
                FROM instrument_capabilities
            )
            SELECT instrument,canonical_asset,role,timeframe,available,
                   display_precision,pip_location,minimum_trade_size,margin_rate
            FROM latest WHERE latest_rank=1 ORDER BY priority,instrument
            """
        ).fetchall()
        execution = self.connection.execute(
            """
            SELECT
                COUNT(r.id) AS reports,
                COALESCE(SUM(r.accepted),0) AS accepted,
                SUM(CASE WHEN i.stop_loss IS NOT NULL AND i.take_profit IS NOT NULL
                         THEN 1 ELSE 0 END) AS protected_intents,
                COUNT(i.intent_id) AS intents,
                AVG(r.implementation_shortfall) AS mean_implementation_shortfall
            FROM order_intents i
            LEFT JOIN execution_reports r ON r.intent_id=i.intent_id
            """
        ).fetchone()
        last_cursor = self.get_state("last_transaction_id")
        return {
            "schema": SCHEMA_VERSION,
            "database_path": str(self.path),
            "sessions": [dict(row) for row in sessions],
            "operational_health": {
                "session_count": session_total,
                "failed_sessions": failed_sessions,
                "session_failure_rate": (
                    failed_sessions / session_total if session_total else None
                ),
                "transaction_cursor_present": last_cursor is not None,
                "last_transaction_id": last_cursor,
            },
            "instrument_capabilities": [dict(row) for row in capabilities],
            "prices": [
                {**dict(row), **distributions.get(row["instrument"], {})}
                for row in prices
            ],
            "account_snapshots": self.connection.execute(
                "SELECT COUNT(*) FROM account_snapshots"
            ).fetchone()[0],
            "transactions": self.connection.execute(
                "SELECT COUNT(*) FROM broker_transactions"
            ).fetchone()[0],
            "order_intents": self.connection.execute(
                "SELECT COUNT(*) FROM order_intents"
            ).fetchone()[0],
            "execution_reports": self.connection.execute(
                "SELECT COUNT(*) FROM execution_reports"
            ).fetchone()[0],
            "execution_health": {
                **dict(execution),
                "acceptance_rate": (
                    execution["accepted"] / execution["reports"]
                    if execution["reports"]
                    else None
                ),
                "sl_tp_attachment_rate": (
                    execution["protected_intents"] / execution["intents"]
                    if execution["intents"]
                    else None
                ),
            },
            "account_performance": {
                "unit": "fraction",
                "weekly_period": "7 complete observed days",
                "annualization": "52 * mean_complete_week",
                **self._account_performance(),
            },
        }


class OandaPracticeLab:
    def __init__(
        self,
        config: PracticeLabConfig,
        client: OandaPracticeClient,
        store: PracticeOlap,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store

    def _config_evidence(self) -> Dict[str, Any]:
        return {
            "schema": "lts.oanda.practice_lab_config.v1",
            "environment": "practice",
            "account_fingerprint": self.client.account_fingerprint,
            "instruments": [item.__dict__ for item in self.config.instruments],
            "polling": {
                "price_seconds": self.config.price_poll_seconds,
                "account_seconds": self.config.account_poll_seconds,
                "transaction_seconds": self.config.transaction_poll_seconds,
            },
            "orders_enabled": self.config.orders_enabled,
        }

    def preflight(self) -> Dict[str, Any]:
        session_id = self.store.start_session(
            "preflight", self.client.account_fingerprint, self._config_evidence()
        )
        try:
            account_response = self.client.account_details()
            last_transaction_id = self.store.record_account(session_id, account_response)
            if last_transaction_id:
                self.store.set_state("last_transaction_id", last_transaction_id)

            instrument_response = self.client.instruments()
            capabilities = {
                item["name"]: item for item in instrument_response.get("instruments", [])
            }
            available: list[str] = []
            missing: list[str] = []
            for selection in self.config.instruments:
                capability = capabilities.get(selection.oanda_instrument)
                self.store.record_capability(session_id, selection, capability)
                (available if capability else missing).append(selection.oanda_instrument)

            price_response = self.client.prices(available)
            price_rows = self.store.record_prices(session_id, price_response)
            result = {
                "session_id": session_id,
                "account_fingerprint": self.client.account_fingerprint,
                "available_instruments": available,
                "missing_instruments": missing,
                "price_rows": price_rows,
                "hedging_enabled": bool(
                    account_response.get("account", {}).get("hedgingEnabled", False)
                ),
                "currency": account_response.get("account", {}).get("currency"),
            }
            self.store.finish_session(session_id, "completed", result)
            return result
        except Exception as exc:
            self.store.finish_session(session_id, "failed", {"error": str(exc)})
            raise

    def observe(self, duration_seconds: float) -> Dict[str, Any]:
        if duration_seconds <= 0:
            raise OandaPracticeError("Observation duration must be positive")
        session_id = self.store.start_session(
            "observe", self.client.account_fingerprint, self._config_evidence()
        )
        instruments = [item.oanda_instrument for item in self.config.instruments]
        started = time.monotonic()
        next_price = next_account = next_transaction = started
        price_rows = transaction_rows = account_rows = 0
        try:
            while time.monotonic() - started < duration_seconds:
                now = time.monotonic()
                if now >= next_price:
                    price_rows += self.store.record_prices(
                        session_id, self.client.prices(instruments)
                    )
                    next_price = now + self.config.price_poll_seconds
                if now >= next_account:
                    last_id = self.store.record_account(
                        session_id, self.client.account_details()
                    )
                    account_rows += 1
                    if last_id and self.store.get_state("last_transaction_id") is None:
                        self.store.set_state("last_transaction_id", last_id)
                    next_account = now + self.config.account_poll_seconds
                if now >= next_transaction:
                    last_id = self.store.get_state("last_transaction_id")
                    if last_id:
                        response = self.client.transactions_since(last_id)
                        transaction_rows += self.store.record_transactions(
                            session_id, self.client.account_fingerprint, response
                        )
                        new_id = response.get("lastTransactionID")
                        if new_id:
                            self.store.set_state("last_transaction_id", str(new_id))
                    next_transaction = now + self.config.transaction_poll_seconds
                wake_at = min(next_price, next_account, next_transaction)
                time.sleep(max(0.05, min(1.0, wake_at - time.monotonic())))
            result = {
                "session_id": session_id,
                "duration_seconds": time.monotonic() - started,
                "price_rows": price_rows,
                "account_rows": account_rows,
                "transaction_rows": transaction_rows,
            }
            self.store.finish_session(session_id, "completed", result)
            return result
        except KeyboardInterrupt:
            result = {
                "session_id": session_id,
                "duration_seconds": time.monotonic() - started,
                "price_rows": price_rows,
                "account_rows": account_rows,
                "transaction_rows": transaction_rows,
            }
            self.store.finish_session(session_id, "interrupted", result)
            return result
        except Exception as exc:
            self.store.finish_session(session_id, "failed", {"error": str(exc)})
            raise

    def protected_market_canary(
        self,
        *,
        instrument: str,
        side: str,
        units: float,
        stop_distance_pips: float,
        reward_risk_ratio: float,
        confirmation: str,
    ) -> Dict[str, Any]:
        if not self.config.orders_enabled or confirmation != ORDER_CONFIRMATION:
            raise OandaPracticeError(
                "Practice orders require config enablement and the confirmation phrase"
            )
        side = side.lower()
        instrument = instrument.upper()
        if side not in {"buy", "sell"}:
            raise OandaPracticeError("Canary side must be buy or sell")
        if units <= 0 or stop_distance_pips <= 0 or reward_risk_ratio <= 0:
            raise OandaPracticeError("Canary sizing and protection values must be positive")
        selection = next(
            (
                item
                for item in self.config.instruments
                if item.oanda_instrument == instrument
            ),
            None,
        )
        if selection is None:
            raise OandaPracticeError("Canary instrument is not in the approved config")

        capability_response = self.client.instruments()
        capabilities = {
            item["name"]: item for item in capability_response.get("instruments", [])
        }
        capability = capabilities.get(instrument)
        if capability is None:
            raise OandaPracticeError("Canary instrument is unavailable in the account")
        precision = int(capability["displayPrecision"])
        pip_size = 10.0 ** int(capability["pipLocation"])
        minimum_units = float(capability.get("minimumTradeSize", 1))
        order_units = max(float(units), minimum_units)

        prices = self.client.prices([instrument]).get("prices", [])
        if not prices or not prices[0].get("bids") or not prices[0].get("asks"):
            raise OandaPracticeError("No tradeable price is available for the canary")
        price = prices[0]
        reference = float(
            price["asks"][0]["price"] if side == "buy" else price["bids"][0]["price"]
        )
        stop_distance = stop_distance_pips * pip_size
        direction = 1.0 if side == "buy" else -1.0
        stop_loss = reference - direction * stop_distance
        take_profit = reference + direction * stop_distance * reward_risk_ratio
        intent_id = f"lts-practice-{uuid.uuid4().hex[:20]}"
        intent = {
            "intent_id": intent_id,
            "created_at": _utc_now(),
            "instrument": instrument,
            "side": side,
            "units": order_units,
            "order_type": "MARKET",
            "reference_price": reference,
            "stop_loss": round(stop_loss, precision),
            "take_profit": round(take_profit, precision),
            "stop_distance_pips": stop_distance_pips,
            "reward_risk_ratio": reward_risk_ratio,
        }
        session_id = self.store.start_session(
            "protected_canary", self.client.account_fingerprint, self._config_evidence()
        )
        self.store.record_intent(session_id, intent)
        order = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(int(order_units) * (1 if side == "buy" else -1)),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "clientExtensions": {
                "id": intent_id,
                "tag": "lts-practice-canary",
            },
            "stopLossOnFill": {
                "price": f"{stop_loss:.{precision}f}",
                "timeInForce": "GTC",
            },
            "takeProfitOnFill": {
                "price": f"{take_profit:.{precision}f}",
                "timeInForce": "GTC",
            },
        }
        try:
            response = self.client.create_order(order)
            self.store.record_execution(session_id, intent, response=response)
            result = {
                "session_id": session_id,
                "intent": intent,
                "accepted": not bool(response.get("orderRejectTransaction")),
                "response": _redact(response),
            }
            self.store.finish_session(session_id, "completed", result)
            return result
        except Exception as exc:
            self.store.record_execution(session_id, intent, error=str(exc))
            self.store.finish_session(session_id, "failed", {"error": str(exc)})
            raise
