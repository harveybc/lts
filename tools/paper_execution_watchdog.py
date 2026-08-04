#!/usr/bin/env python3
"""Monitor Paper/Shadow execution facts and notify the Hermes Telegram group."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import socket
import sqlite3
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


STATE_SCHEMA = "lts.paper_execution_watchdog.v1"
EXPECTED_ALPACA_SYMBOLS = {
    "ADA/USD",
    "BTC/USD",
    "DOGE/USD",
    "ETH/USD",
    "SOL/USD",
    "XRP/USD",
}
DEFAULT_ALPACA_DB = Path.home() / ".local/state/lts/alpaca-paper-lab.sqlite"
DEFAULT_ALPACA_RUNTIME = (
    Path.home() / ".local/state/lts/alpaca-model-runner-heartbeat.json"
)
DEFAULT_IBKR_DB = Path.home() / ".local/state/lts/ibkr-paper-lab.sqlite"
DEFAULT_IBKR_RUNTIME = (
    Path.home() / ".local/state/lts/ibkr-model-runner-heartbeat.json"
)
DEFAULT_OANDA_DB = Path.home() / ".local/state/lts/oanda-practice-lab.sqlite"
DEFAULT_OANDA_ENV = Path.home() / ".config/lts/oanda-practice.env"
DEFAULT_MT5_DB = Path.home() / ".local/state/lts/mt5-bridge.sqlite"
DEFAULT_SHADOW_DB = Path.home() / ".local/state/lts/multi-venue-shadow.sqlite"
DEFAULT_CAPITAL_DB = Path.home() / ".local/state/lts/capital-demo-lab.sqlite"
DEFAULT_CAPITAL_ENV = Path.home() / ".config/lts/capital-demo.env"
DEFAULT_STATE = Path.home() / ".local/state/lts/paper-execution-watchdog/state.json"
DEFAULT_LATEST = Path.home() / ".local/state/lts/paper-execution-watchdog/latest.json"
DEFAULT_DISCUSSION = Path.home() / ".local/state/lts/hermes/live-trading-discussion.json"
DEFAULT_MONITOR_DB = Path.home() / ".local/state/lts/paper-execution-monitor.sqlite"
MT5_STATUS_SCHEMA = "lts.mt5.operational_status.v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_execution_runtime(
    path: Path,
    expected_schema: str,
    *,
    now: float,
    stale_seconds: float,
) -> dict[str, Any]:
    payload = _read_json(path)
    if not payload:
        return {"available": False, "reason": "heartbeat_missing"}
    if payload.get("schema") != expected_schema:
        return {"available": False, "reason": "schema_mismatch"}
    observed_at = _parse_time(payload.get("observed_at"))
    if observed_at is None:
        return {"available": False, "reason": "observed_at_missing"}
    age_seconds = max(0.0, now - observed_at)
    return {
        **payload,
        "available": age_seconds <= stale_seconds,
        "reason": None if age_seconds <= stale_seconds else "heartbeat_stale",
        "age_seconds": age_seconds,
    }


def _alpaca_exposure_authorized(
    detail: Mapping[str, Any], runtime: Mapping[str, Any]
) -> bool:
    positions = int(detail.get("open_positions") or 0)
    orders = int(detail.get("open_orders") or 0)
    return bool(
        runtime.get("available")
        and runtime.get("venue") == "alpaca_paper"
        and runtime.get("environment") == "paper"
        and runtime.get("read_only") is False
        and runtime.get("account_binding_verified") is True
        and runtime.get("account_fingerprint") == detail.get("account_fingerprint")
        and runtime.get("instrument")
        and runtime.get("model_id")
        and runtime.get("selection_error") is None
        and runtime.get("state") in {"decided", "monitoring"}
        and int(runtime.get("positions") or 0) == positions
        and int(runtime.get("orders") or 0) == orders
    )


def _ibkr_exposure_authorized(
    latest: Mapping[str, Any], runtime: Mapping[str, Any]
) -> bool:
    positions = int(latest.get("open_positions") or 0)
    observer_orders = int(latest.get("open_orders") or 0)
    runtime_position = float(runtime.get("position") or 0.0)
    runtime_orders = int(runtime.get("orders") or 0)
    reconciled_fill = any(
        bool((item.get("result") or {}).get("position_reconciled"))
        for item in (runtime.get("l1") or {}).get("fills") or []
        if isinstance(item, Mapping)
    )
    return bool(
        runtime.get("available")
        and runtime.get("venue") == "ibkr_paper"
        and runtime.get("environment") == "paper"
        and runtime.get("read_only") is False
        and runtime.get("account_binding_verified") is True
        and runtime.get("account_fingerprint")
        and runtime.get("instrument")
        and runtime.get("model_id")
        and runtime.get("selection_error") is None
        and runtime.get("state") == "monitoring"
        and positions == int(runtime_position != 0.0)
        and observer_orders <= runtime_orders
        and (positions == 0 or (runtime_orders >= 2 and reconciled_fill))
    )


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def _load_notification_environment() -> None:
    _load_env_file(Path.home() / ".hermes/.env")
    _load_env_file(
        Path.home() / "Documents/GitHub/financial-data/_metadata/.env"
    )


def _split_message(text: str, limit: int = 3800) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _send_telegram(text: str) -> None:
    _load_notification_environment()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (
        os.environ.get("PROJECT3_TELEGRAM_CHAT_ID", "").strip()
        or os.environ.get("TELEGRAM_HOME_CHANNEL", "").strip()
    )
    if not token or not chat_id:
        raise RuntimeError("Hermes Telegram bot or home channel is not configured")
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in _split_message(text):
        body = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body, method="POST")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if not result.get("ok"):
                    raise RuntimeError("Telegram rejected the notification")
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(2**attempt)


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _event(
    key: str,
    title: str,
    detail: str,
    *,
    severity: str,
    category: str,
    discussion: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "detail": detail,
        "severity": severity,
        "category": category,
        "discussion": discussion,
    }


def read_alpaca_snapshot(path: Path, now: float) -> dict[str, Any]:
    if not path.exists():
        return {"available": False, "reason": "database_missing"}
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        latest = connection.execute(
            """
            SELECT session_id,status,started_at,ended_at,detail_json
            FROM lab_sessions ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
        if latest is None:
            return {"available": False, "reason": "no_sessions"}
        detail = json.loads(latest["detail_json"])
        probes = connection.execute(
            """
            SELECT endpoint,latency_ms,http_status,success,error_kind
            FROM endpoint_probes WHERE session_id=? ORDER BY id
            """,
            (latest["session_id"],),
        ).fetchall()
        quotes = connection.execute(
            """
            SELECT symbol,observed_at,broker_time,mid,spread_bps
            FROM quote_observations WHERE session_id=? ORDER BY symbol
            """,
            (latest["session_id"],),
        ).fetchall()
        sessions = connection.execute(
            "SELECT COUNT(*) FROM lab_sessions WHERE status='complete'"
        ).fetchone()[0]
        history = connection.execute(
            """
            SELECT symbol,observed_at,mid,spread_bps
            FROM quote_observations
            WHERE observed_at >= ?
            ORDER BY symbol,observed_at
            """,
            (
                datetime.fromtimestamp(now - 5 * 3600, timezone.utc).isoformat(),
            ),
        ).fetchall()
        return {
            "available": True,
            "session_id": latest["session_id"],
            "status": latest["status"],
            "started_at": latest["started_at"],
            "ended_at": latest["ended_at"],
            "detail": detail,
            "probes": [dict(row) for row in probes],
            "quotes": [dict(row) for row in quotes],
            "history": [dict(row) for row in history],
            "complete_sessions": sessions,
        }
    finally:
        connection.close()


def read_ibkr_socket(host: str, port: int, timeout: float = 1.0) -> dict[str, Any]:
    started = time.perf_counter()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
    return {
        "available": result == 0,
        "host": host,
        "port": port,
        "latency_ms": (time.perf_counter() - started) * 1000.0,
        "connect_errno": result,
    }


def read_ibkr_snapshot(
    path: Path,
    host: str,
    port: int,
) -> dict[str, Any]:
    socket_status = read_ibkr_socket(host, port)
    if not path.exists():
        return {
            "available": False,
            "reason": "database_missing",
            "socket": socket_status,
        }
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        latest = connection.execute(
            """
            SELECT session_id,status,started_at,ended_at
            FROM lab_sessions ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
        latest_complete = connection.execute(
            """
            SELECT s.session_id,s.status,s.started_at,s.ended_at,
                   r.observed_at AS reconciliation_observed_at,
                   r.open_positions,r.open_orders
            FROM lab_sessions s
            INNER JOIN reconciliation_snapshots r ON r.session_id=s.session_id
            WHERE s.status='complete'
            ORDER BY s.ended_at DESC LIMIT 1
            """
        ).fetchone()
        complete_sessions = connection.execute(
            "SELECT COUNT(*) FROM lab_sessions WHERE status='complete'"
        ).fetchone()[0]
        return {
            "available": latest_complete is not None,
            "reason": (
                None
                if latest_complete is not None
                else (
                    "no_reconciled_complete_sessions"
                    if complete_sessions
                    else "no_complete_sessions"
                )
            ),
            "socket": socket_status,
            "latest_session": dict(latest) if latest is not None else None,
            "latest_complete": (
                dict(latest_complete) if latest_complete is not None else None
            ),
            "complete_sessions": int(complete_sessions),
        }
    except sqlite3.OperationalError as exc:
        return {
            "available": False,
            "reason": "schema_unavailable",
            "detail": str(exc),
            "socket": socket_status,
        }
    finally:
        connection.close()


def read_shadow_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"available": False, "reason": "database_missing"}
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        latest = connection.execute(
            """
            SELECT p.*,r.status
            FROM portfolio_snapshots p
            JOIN shadow_runs r ON r.run_id=p.run_id
            ORDER BY p.observed_at DESC LIMIT 1
            """
        ).fetchone()
        return {
            "available": latest is not None,
            "reason": None if latest is not None else "no_snapshots",
            "latest": dict(latest) if latest is not None else None,
        }
    except sqlite3.OperationalError as exc:
        return {
            "available": False,
            "reason": "schema_unavailable",
            "detail": str(exc),
        }
    finally:
        connection.close()


def read_capital_snapshot(path: Path, environment_path: Path) -> dict[str, Any]:
    configured = environment_path.exists()
    if not path.exists():
        return {
            "available": False,
            "configured": configured,
            "reason": "database_missing" if configured else "not_configured",
        }
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        latest = connection.execute(
            """
            SELECT s.session_id,s.status,s.started_at,s.ended_at,
                   r.open_positions,r.working_orders
            FROM lab_sessions s
            LEFT JOIN reconciliation_snapshots r ON r.session_id=s.session_id
            ORDER BY s.started_at DESC LIMIT 1
            """
        ).fetchone()
        return {
            "available": latest is not None,
            "configured": configured,
            "reason": None if latest is not None else "no_sessions",
            "latest": dict(latest) if latest is not None else None,
        }
    except sqlite3.OperationalError as exc:
        return {
            "available": False,
            "configured": configured,
            "reason": "schema_unavailable",
            "detail": str(exc),
        }
    finally:
        connection.close()


def read_oanda_snapshot(
    path: Path,
    environment_path: Path,
) -> dict[str, Any]:
    configured = environment_path.exists()
    if not path.exists():
        return {
            "available": False,
            "configured": configured,
            "reason": "database_missing" if configured else "not_configured",
        }
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        latest_session = connection.execute(
            """
            SELECT session_id,phase,status,started_at,ended_at
            FROM lab_sessions ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
        latest_price = connection.execute(
            """
            SELECT observed_at,broker_time,instrument,spread_bps
            FROM price_observations ORDER BY observed_at DESC LIMIT 1
            """
        ).fetchone()
        latest_account = connection.execute(
            """
            SELECT observed_at,nav,open_trade_count,open_position_count,
                   pending_order_count
            FROM account_snapshots ORDER BY observed_at DESC LIMIT 1
            """
        ).fetchone()
        price_count = connection.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0]
        return {
            "available": latest_session is not None,
            "configured": configured,
            "reason": None if latest_session is not None else "no_sessions",
            "latest_session": (
                dict(latest_session) if latest_session is not None else None
            ),
            "latest_price": dict(latest_price) if latest_price is not None else None,
            "latest_account": (
                dict(latest_account) if latest_account is not None else None
            ),
            "price_observations": int(price_count),
        }
    finally:
        connection.close()


def read_mt5_remote_status(url: str, timeout: float = 5.0) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"available": False, "reason": "remote_status_url_invalid"}
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        ConnectionError,
        TimeoutError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        return {
            "available": False,
            "reason": "remote_status_unavailable",
            "error_kind": type(exc).__name__,
        }
    if not isinstance(payload, dict) or payload.get("schema") != MT5_STATUS_SCHEMA:
        return {"available": False, "reason": "remote_status_schema_invalid"}
    return payload


def read_mt5_snapshot(
    path: Path,
    status_url: str = "",
) -> dict[str, Any]:
    if status_url.strip():
        return read_mt5_remote_status(status_url.strip())
    if not path.exists():
        return {"available": False, "reason": "database_missing"}
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        heartbeat = connection.execute(
            """
            SELECT account_fingerprint,server_fingerprint,environment,connected,
                   trade_allowed,terminal_build,terminal_ping_ms,received_at
            FROM heartbeats ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        snapshot = connection.execute(
            """
            SELECT account_fingerprint,received_at,currency,balance,equity,
                   margin,free_margin,positions_total,orders_total,symbols_total
            FROM account_snapshots ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if heartbeat is None:
            return {
                "available": False,
                "reason": "no_heartbeat",
                "latest_snapshot": dict(snapshot) if snapshot is not None else None,
            }
        return {
            "available": True,
            "heartbeat": dict(heartbeat),
            "latest_snapshot": dict(snapshot) if snapshot is not None else None,
        }
    except sqlite3.OperationalError as exc:
        return {
            "available": False,
            "reason": "schema_unavailable",
            "detail": str(exc),
        }
    finally:
        connection.close()


def _return_over_horizon(
    rows: Sequence[Mapping[str, Any]],
    now: float,
    horizon_seconds: float,
) -> Optional[float]:
    usable = [
        row
        for row in rows
        if row.get("mid") not in (None, 0) and _parse_time(row.get("observed_at"))
    ]
    if len(usable) < 3:
        return None
    latest = usable[-1]
    latest_time = _parse_time(latest["observed_at"])
    if latest_time is None:
        return None
    target = latest_time - horizon_seconds
    eligible = [
        row
        for row in usable
        if (_parse_time(row["observed_at"]) or now) <= target
    ]
    if not eligible:
        return None
    prior = max(eligible, key=lambda row: _parse_time(row["observed_at"]) or 0)
    return float(latest["mid"]) / float(prior["mid"]) - 1.0


def evaluate_mt5(
    mt5: Mapping[str, Any],
    *,
    now: float,
    stale_seconds: float,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not mt5.get("available"):
        events.append(
            _event(
                "mt5_bridge_missing",
                "LTS PAPER ACTION REQUIRED: MT5 BRIDGE HAS NO HEARTBEAT",
                f"reason: {mt5.get('reason', 'unknown')}",
                severity="warning",
                category="operations",
            )
        )
        return events

    heartbeat = mt5.get("heartbeat") or {}
    received_at = _parse_time(heartbeat.get("received_at"))
    if received_at is None or now - received_at > stale_seconds:
        age = (
            "unknown"
            if received_at is None
            else f"{(now - received_at) / 60:.1f} min"
        )
        events.append(
            _event(
                "mt5_bridge_stale",
                "LTS PAPER ALERT: MT5 BRIDGE HEARTBEAT STALE",
                f"latest MT5 heartbeat age: {age}",
                severity="critical",
                category="operations",
            )
        )
    if not heartbeat.get("connected"):
        events.append(
            _event(
                "mt5_terminal_disconnected",
                "LTS PAPER ALERT: MT5 TERMINAL DISCONNECTED",
                (
                    "The EA reports no broker connection. "
                    "Inspect the Windows VM and MT5 demo session."
                ),
                severity="critical",
                category="broker",
            )
        )
    snapshot = mt5.get("latest_snapshot") or {}
    positions = int(snapshot.get("positions_total") or 0)
    orders = int(snapshot.get("orders_total") or 0)
    reconciliation = mt5.get("exposure_reconciliation") or {}
    exposure_authorized = bool(
        mt5.get("execution_enabled")
        and mt5.get("read_only") is False
        and reconciliation.get("available")
        and reconciliation.get("all_authorized")
        and int(reconciliation.get("positions_total") or 0) == positions
        and int(reconciliation.get("orders_total") or 0) == orders
    )
    if (positions or orders) and not exposure_authorized:
        events.append(
            _event(
                "mt5_unexpected_exposure",
                "LTS PAPER ALERT: UNVERIFIED MT5 EXPOSURE",
                (
                    f"open positions: {positions}\n"
                    f"pending orders: {orders}\n"
                    "The exposure does not reconcile to successful protected "
                    "model commands; inspect MT5 immediately."
                ),
                severity="critical",
                category="reconciliation",
            )
        )
    return events


def evaluate(
    alpaca: Mapping[str, Any],
    ibkr: Mapping[str, Any],
    oanda: Optional[Mapping[str, Any]] = None,
    mt5: Optional[Mapping[str, Any]] = None,
    shadow: Optional[Mapping[str, Any]] = None,
    capital: Optional[Mapping[str, Any]] = None,
    *,
    now: float,
    stale_seconds: float,
    oanda_rest_required: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    discussions: list[dict[str, Any]] = []
    if not alpaca.get("available"):
        events.append(
            _event(
                "alpaca_observer_missing",
                "LTS PAPER ALERT: ALPACA OBSERVER HAS NO DATA",
                f"reason: {alpaca.get('reason', 'unknown')}",
                severity="critical",
                category="operations",
            )
        )
    else:
        ended_at = _parse_time(alpaca.get("ended_at"))
        if ended_at is None or now - ended_at > stale_seconds:
            age = "unknown" if ended_at is None else f"{(now - ended_at) / 60:.1f} min"
            events.append(
                _event(
                    "alpaca_observer_stale",
                    "LTS PAPER ALERT: ALPACA OBSERVER STALE",
                    f"latest completed observation age: {age}",
                    severity="critical",
                    category="operations",
                )
            )
        if alpaca.get("status") != "complete":
            events.append(
                _event(
                    "alpaca_session_failed",
                    "LTS PAPER ALERT: ALPACA PREFLIGHT FAILED",
                    f"session status: {alpaca.get('status')}",
                    severity="critical",
                    category="operations",
                )
            )
        detail = alpaca.get("detail") or {}
        if detail.get("account_blocked") or detail.get("trading_blocked"):
            events.append(
                _event(
                    "alpaca_account_blocked",
                    "LTS PAPER ALERT: ALPACA ACCOUNT BLOCKED",
                    (
                        f"account_blocked: {detail.get('account_blocked')}\n"
                        f"trading_blocked: {detail.get('trading_blocked')}"
                    ),
                    severity="critical",
                    category="broker",
                )
            )
        missing = sorted(detail.get("missing_cells") or [])
        if missing:
            events.append(
                _event(
                    "alpaca_cells_missing",
                    "LTS PAPER ALERT: ALPACA CELLS MISSING",
                    "missing: " + ", ".join(missing),
                    severity="warning",
                    category="broker",
                )
            )
        received = set(detail.get("quotes_received") or [])
        missing_quotes = sorted(EXPECTED_ALPACA_SYMBOLS - received)
        if missing_quotes:
            events.append(
                _event(
                    "alpaca_quotes_missing",
                    "LTS PAPER ALERT: ALPACA QUOTES MISSING",
                    "missing: " + ", ".join(missing_quotes),
                    severity="warning",
                    category="market_data",
                )
            )
        positions = int(detail.get("open_positions") or 0)
        orders = int(detail.get("open_orders") or 0)
        runtime = alpaca.get("execution_runtime") or {}
        if (positions or orders) and not _alpaca_exposure_authorized(
            detail, runtime
        ):
            events.append(
                _event(
                    "alpaca_unexpected_exposure",
                    "LTS PAPER ALERT: UNEXPECTED ALPACA EXPOSURE",
                    (
                        f"open positions: {positions}\n"
                        f"open orders: {orders}\n"
                        "Exposure is not reconciled to a fresh account-bound "
                        "writable model runner."
                    ),
                    severity="critical",
                    category="reconciliation",
                )
            )
        failed_endpoints = [
            item["endpoint"]
            for item in alpaca.get("probes") or []
            if not item.get("success")
        ]
        if failed_endpoints:
            events.append(
                _event(
                    "alpaca_endpoint_failure",
                    "LTS PAPER ALERT: ALPACA API FAILURE",
                    "failed endpoints: " + ", ".join(sorted(failed_endpoints)),
                    severity="warning",
                    category="operations",
                )
            )

        history_by_symbol: dict[str, list[Mapping[str, Any]]] = {}
        for row in alpaca.get("history") or []:
            history_by_symbol.setdefault(str(row["symbol"]), []).append(row)
        for symbol, rows in sorted(history_by_symbol.items()):
            one_hour = _return_over_horizon(rows, now, 3600)
            four_hour = _return_over_horizon(rows, now, 4 * 3600)
            if one_hour is not None and abs(one_hour) >= 0.02:
                discussions.append(
                    _event(
                        f"rush_1h:{symbol}:{int(now // 3600)}",
                        "LTS DISCUSSION: ONE-HOUR MOVE",
                        (
                            f"symbol: {symbol}\n"
                            f"one-hour mid return: {one_hour:+.2%}\n"
                            "No order was submitted. Review as a rush/event-study candidate."
                        ),
                        severity="info",
                        category="research",
                        discussion=True,
                    )
                )
            if four_hour is not None and abs(four_hour) >= 0.05:
                discussions.append(
                    _event(
                        f"rush_4h:{symbol}:{int(now // (4 * 3600))}",
                        "LTS DISCUSSION: FOUR-HOUR MOVE",
                        (
                            f"symbol: {symbol}\n"
                            f"four-hour mid return: {four_hour:+.2%}\n"
                            "No order was submitted. Review causal/event context before queueing research."
                        ),
                        severity="info",
                        category="research",
                        discussion=True,
                    )
                )

    ibkr_socket = ibkr.get("socket") or {}
    if not ibkr_socket.get("available"):
        events.append(
            _event(
                "ibkr_paper_offline",
                "LTS PAPER ACTION REQUIRED: IBKR TWS OFFLINE",
                (
                    f"endpoint: {ibkr_socket.get('host')}:{ibkr_socket.get('port')}\n"
                    "Start TWS in Paper mode and enable socket clients; the execution "
                    "runner independently verifies its writable Paper mandate."
                ),
                severity="warning",
                category="operations",
            )
        )
    if not ibkr.get("available"):
        events.append(
            _event(
                "ibkr_observer_missing",
                "LTS PAPER ALERT: IBKR OBSERVER HAS NO FUNCTIONAL DATA",
                (
                    f"reason: {ibkr.get('reason', 'unknown')}\n"
                    f"TWS socket reachable: {bool(ibkr_socket.get('available'))}"
                ),
                severity="critical",
                category="operations",
            )
        )
    else:
        latest_complete = ibkr.get("latest_complete") or {}
        ended_at = _parse_time(latest_complete.get("ended_at"))
        if ended_at is None or now - ended_at > stale_seconds:
            age = (
                "unknown"
                if ended_at is None
                else f"{(now - ended_at) / 60:.1f} min"
            )
            events.append(
                _event(
                    "ibkr_observer_stale",
                    "LTS PAPER ALERT: IBKR OBSERVER STALE",
                    (
                        f"latest successful authenticated snapshot age: {age}\n"
                        f"TWS socket reachable: {bool(ibkr_socket.get('available'))}"
                    ),
                    severity="critical",
                    category="operations",
                )
            )
        positions = int(latest_complete.get("open_positions") or 0)
        orders = int(latest_complete.get("open_orders") or 0)
        runtime = ibkr.get("execution_runtime") or {}
        if (positions or orders) and not _ibkr_exposure_authorized(
            latest_complete, runtime
        ):
            events.append(
                _event(
                    "ibkr_unexpected_exposure",
                    "LTS PAPER ALERT: UNEXPECTED IBKR EXPOSURE",
                    (
                        f"open positions: {positions}\n"
                        f"open orders: {orders}\n"
                        "Exposure is not reconciled to a fresh account-bound "
                        "writable model runner."
                    ),
                    severity="critical",
                    category="reconciliation",
                )
            )
    if oanda is not None:
        if not oanda.get("configured"):
            if oanda_rest_required:
                events.append(
                    _event(
                        "oanda_practice_not_configured",
                        "LTS PAPER ACTION REQUIRED: OANDA PRACTICE NOT CONFIGURED",
                        (
                            "Run examples/scripts/configure_oanda_practice.sh after "
                            "creating the REST-v20 Practice token."
                        ),
                        severity="warning",
                        category="operations",
                    )
                )
        elif not oanda.get("available"):
            events.append(
                _event(
                    "oanda_observer_missing",
                    "LTS PAPER ALERT: OANDA OBSERVER HAS NO DATA",
                    f"reason: {oanda.get('reason', 'unknown')}",
                    severity="critical",
                    category="operations",
                )
            )
        else:
            latest_price = oanda.get("latest_price") or {}
            latest_account = oanda.get("latest_account") or {}
            timestamps = [
                parsed
                for parsed in (
                    _parse_time(latest_price.get("observed_at")),
                    _parse_time(latest_account.get("observed_at")),
                )
                if parsed is not None
            ]
            newest = max(timestamps) if timestamps else None
            if newest is None or now - newest > stale_seconds:
                age = (
                    "unknown"
                    if newest is None
                    else f"{(now - newest) / 60:.1f} min"
                )
                events.append(
                    _event(
                        "oanda_observer_stale",
                        "LTS PAPER ALERT: OANDA OBSERVER STALE",
                        f"latest broker observation age: {age}",
                        severity="critical",
                        category="operations",
                    )
                )
            exposure = {
                "open trades": int(latest_account.get("open_trade_count") or 0),
                "open positions": int(
                    latest_account.get("open_position_count") or 0
                ),
                "pending orders": int(
                    latest_account.get("pending_order_count") or 0
                ),
            }
            if any(exposure.values()):
                events.append(
                    _event(
                        "oanda_unexpected_exposure",
                        "LTS PAPER ALERT: UNEXPECTED OANDA EXPOSURE",
                        "\n".join(
                            f"{name}: {value}" for name, value in exposure.items()
                        ),
                        severity="critical",
                        category="reconciliation",
                    )
                )
    if mt5 is not None:
        events.extend(evaluate_mt5(mt5, now=now, stale_seconds=stale_seconds))
    if shadow is not None:
        latest_shadow = shadow.get("latest") or {}
        observed_at = _parse_time(latest_shadow.get("observed_at"))
        if not shadow.get("available") or observed_at is None:
            events.append(
                _event(
                    "multi_venue_shadow_missing",
                    "LTS PAPER ALERT: SHADOW PORTFOLIO HAS NO DATA",
                    f"reason: {shadow.get('reason', 'unknown')}",
                    severity="critical",
                    category="portfolio",
                )
            )
        elif now - observed_at > stale_seconds:
            events.append(
                _event(
                    "multi_venue_shadow_stale",
                    "LTS PAPER ALERT: SHADOW PORTFOLIO STALE",
                    f"snapshot age: {(now - observed_at) / 60:.1f} min",
                    severity="critical",
                    category="portfolio",
                )
            )
        missing = int(latest_shadow.get("missing_cells") or 0)
        if missing:
            events.append(
                _event(
                    "multi_venue_shadow_incomplete",
                    "LTS PAPER ALERT: SHADOW PORTFOLIO COVERAGE INCOMPLETE",
                    (
                        f"missing cells: {missing}\n"
                        f"fresh portfolio weight: "
                        f"{float(latest_shadow.get('available_weight') or 0):.1%}"
                    ),
                    severity="warning",
                    category="portfolio",
                )
            )
        if int(latest_shadow.get("orders_submitted") or 0):
            events.append(
                _event(
                    "multi_venue_shadow_order_violation",
                    "LTS PAPER CRITICAL: SHADOW PORTFOLIO SUBMITTED AN ORDER",
                    "Stop the shadow observer and inspect immediately.",
                    severity="critical",
                    category="reconciliation",
                )
            )
    if capital is not None and capital.get("configured"):
        latest_capital = capital.get("latest") or {}
        ended_at = _parse_time(latest_capital.get("ended_at"))
        if not capital.get("available") or ended_at is None:
            events.append(
                _event(
                    "capital_demo_missing",
                    "LTS PAPER ALERT: CAPITAL.COM DEMO HAS NO DATA",
                    f"reason: {capital.get('reason', 'unknown')}",
                    severity="warning",
                    category="operations",
                )
            )
        elif now - ended_at > stale_seconds:
            events.append(
                _event(
                    "capital_demo_stale",
                    "LTS PAPER ALERT: CAPITAL.COM DEMO STALE",
                    f"observation age: {(now - ended_at) / 60:.1f} min",
                    severity="warning",
                    category="operations",
                )
            )
        if int(latest_capital.get("open_positions") or 0) or int(
            latest_capital.get("working_orders") or 0
        ):
            events.append(
                _event(
                    "capital_demo_unexpected_exposure",
                    "LTS PAPER ALERT: UNEXPECTED CAPITAL.COM EXPOSURE",
                    (
                        f"positions/orders: "
                        f"{latest_capital.get('open_positions', 0)}/"
                        f"{latest_capital.get('working_orders', 0)}"
                    ),
                    severity="critical",
                    category="reconciliation",
                )
            )
    return events, discussions


def format_summary(
    alpaca: Mapping[str, Any],
    ibkr: Mapping[str, Any],
    oanda: Optional[Mapping[str, Any]] = None,
    mt5: Optional[Mapping[str, Any]] = None,
    shadow: Optional[Mapping[str, Any]] = None,
    capital: Optional[Mapping[str, Any]] = None,
) -> str:
    if not alpaca.get("available"):
        alpaca_text = f"Alpaca: unavailable ({alpaca.get('reason', 'unknown')})"
    else:
        detail = alpaca.get("detail") or {}
        latencies = [
            float(item["latency_ms"])
            for item in alpaca.get("probes") or []
            if item.get("success")
        ]
        spread_rows = [
            row for row in alpaca.get("quotes") or [] if row.get("spread_bps") is not None
        ]
        spreads = [float(row["spread_bps"]) for row in spread_rows]
        alpaca_text = (
            "Alpaca Paper: healthy\n"
            f"observations: {alpaca.get('complete_sessions', 0)}\n"
            f"cells: {len(detail.get('available_cells') or [])} available, "
            f"{len(detail.get('missing_cells') or [])} missing\n"
            f"quotes: {len(detail.get('quotes_received') or [])}/{len(EXPECTED_ALPACA_SYMBOLS)}\n"
            f"positions/orders: {detail.get('open_positions', 0)}/"
            f"{detail.get('open_orders', 0)}\n"
            f"API latency p50/p95: {_percentile(latencies, 0.5) or 0:.1f}/"
            f"{_percentile(latencies, 0.95) or 0:.1f} ms\n"
            f"spread bps p50/p95: {_percentile(spreads, 0.5) or 0:.3f}/"
            f"{_percentile(spreads, 0.95) or 0:.3f}"
        )
    ibkr_socket = ibkr.get("socket") or {}
    latest_ibkr = ibkr.get("latest_complete") or {}
    ibkr_text = (
        f"IBKR Paper observer: {'healthy' if ibkr.get('available') else 'unavailable'}\n"
        f"authenticated observations: {ibkr.get('complete_sessions', 0)}\n"
        f"last successful snapshot: {latest_ibkr.get('ended_at', 'none')}\n"
        f"TWS socket: {'online' if ibkr_socket.get('available') else 'offline'} "
        f"at {ibkr_socket.get('host')}:{ibkr_socket.get('port')}\n"
        f"positions/orders: {latest_ibkr.get('open_positions', 0)}/"
        f"{latest_ibkr.get('open_orders', 0)}"
    )
    if oanda is None:
        oanda_text = "OANDA Practice: not monitored"
    elif not oanda.get("configured"):
        oanda_text = "OANDA REST-v20 Practice: optional and not configured"
    elif not oanda.get("available"):
        oanda_text = (
            f"OANDA Practice: unavailable ({oanda.get('reason', 'unknown')})"
        )
    else:
        session = oanda.get("latest_session") or {}
        oanda_text = (
            f"OANDA Practice: {session.get('status', 'unknown')}\n"
            f"phase: {session.get('phase', 'unknown')}\n"
            f"price observations: {oanda.get('price_observations', 0)}"
        )
    if mt5 is None:
        mt5_text = "MT5 Demo: not monitored"
    elif not mt5.get("available"):
        mt5_text = f"MT5 Demo: unavailable ({mt5.get('reason', 'unknown')})"
    else:
        heartbeat = mt5.get("heartbeat") or {}
        snapshot = mt5.get("latest_snapshot") or {}
        mt5_text = (
            f"MT5 Demo: {'connected' if heartbeat.get('connected') else 'disconnected'}\n"
            f"terminal build/ping: {heartbeat.get('terminal_build', 'unknown')}/"
            f"{float(heartbeat.get('terminal_ping_ms') or 0):.1f} ms\n"
            f"symbols observed: {snapshot.get('symbols_total', 0)}\n"
            f"positions/orders: {snapshot.get('positions_total', 0)}/"
            f"{snapshot.get('orders_total', 0)}"
        )
    shadow_latest = (shadow or {}).get("latest") or {}
    shadow_text = (
        "Multi-venue shadow: "
        f"{(shadow or {}).get('available', False)}\n"
        f"NAV/return: {float(shadow_latest.get('nav') or 0):.2f}/"
        f"{float(shadow_latest.get('total_return') or 0):+.4%}\n"
        f"fresh weight: {float(shadow_latest.get('available_weight') or 0):.1%}; "
        f"missing/stale: {shadow_latest.get('missing_cells', 0)}/"
        f"{shadow_latest.get('stale_cells', 0)}\n"
        f"orders submitted: {shadow_latest.get('orders_submitted', 0)}"
    )
    capital_latest = (capital or {}).get("latest") or {}
    capital_text = (
        "Capital.com Demo: "
        f"{'healthy' if (capital or {}).get('available') else (capital or {}).get('reason', 'not monitored')}\n"
        f"configured: {bool((capital or {}).get('configured'))}; "
        f"positions/orders: {capital_latest.get('open_positions', 0)}/"
        f"{capital_latest.get('working_orders', 0)}"
    )
    return (
        "LTS PAPER/SHADOW STATUS\n\n"
        + alpaca_text
        + "\n\n"
        + ibkr_text
        + "\n\n"
        + oanda_text
        + "\n\n"
        + mt5_text
        + "\n\n"
        + shadow_text
        + "\n\n"
        + capital_text
    )


def format_mt5_summary(mt5: Mapping[str, Any]) -> str:
    if not mt5.get("available"):
        return (
            "LTS MT5 DEMO STATUS\n\n"
            f"unavailable ({mt5.get('reason', 'unknown')})"
        )
    heartbeat = mt5.get("heartbeat") or {}
    snapshot = mt5.get("latest_snapshot") or {}
    return (
        "LTS MT5 DEMO STATUS\n\n"
        f"connection: {'online' if heartbeat.get('connected') else 'offline'}\n"
        f"terminal build: {heartbeat.get('terminal_build', 'unknown')}\n"
        f"terminal ping: {float(heartbeat.get('terminal_ping_ms') or 0):.1f} ms\n"
        f"symbols observed: {snapshot.get('symbols_total', 0)}\n"
        f"positions/orders: {snapshot.get('positions_total', 0)}/"
        f"{snapshot.get('orders_total', 0)}"
    )


class MonitorStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS monitor_events (
                event_id TEXT PRIMARY KEY,
                event_key TEXT NOT NULL,
                transition TEXT NOT NULL,
                severity TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT NOT NULL,
                discussion INTEGER NOT NULL,
                observed_at TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def record(self, event: Mapping[str, Any], transition: str, now: float) -> None:
        event_id = hashlib.sha256(
            f"{event['key']}|{transition}|{now}".encode("utf-8")
        ).hexdigest()
        self.connection.execute(
            "INSERT INTO monitor_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                event["key"],
                transition,
                event["severity"],
                event["category"],
                event["title"],
                event["detail"],
                int(bool(event.get("discussion"))),
                datetime.fromtimestamp(now, timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


def process_events(
    events: Sequence[Mapping[str, Any]],
    state: dict[str, Any],
    store: MonitorStore,
    *,
    now: float,
    repeat_seconds: float,
) -> list[str]:
    event_state = state.setdefault("events", {})
    messages: list[str] = []
    current = {str(item["key"]): item for item in events}
    for key, item in current.items():
        previous = event_state.get(key) or {}
        digest = hashlib.sha256(
            f"{item['title']}|{item['detail']}".encode("utf-8")
        ).hexdigest()
        due = (
            not previous.get("active")
            or previous.get("digest") != digest
            or now - float(previous.get("last_sent_at", 0)) >= repeat_seconds
        )
        if due:
            messages.append(f"{item['title']}\n{item['detail']}")
            transition = "activated" if not previous.get("active") else "repeated"
            store.record(item, transition, now)
            previous["last_sent_at"] = now
        previous.update(
            {
                "active": True,
                "digest": digest,
                "severity": item["severity"],
                "category": item["category"],
                "last_observed_at": now,
            }
        )
        event_state[key] = previous
    for key, previous in list(event_state.items()):
        if previous.get("active") and key not in current:
            recovery = {
                "key": key,
                "title": "LTS PAPER RECOVERED",
                "detail": f"event cleared: {key}",
                "severity": "info",
                "category": previous.get("category", "operations"),
                "discussion": False,
            }
            messages.append(f"{recovery['title']}\n{recovery['detail']}")
            store.record(recovery, "recovered", now)
            previous.update({"active": False, "last_sent_at": now})
    return messages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alpaca-db", type=Path, default=DEFAULT_ALPACA_DB)
    parser.add_argument(
        "--alpaca-runtime", type=Path, default=DEFAULT_ALPACA_RUNTIME
    )
    parser.add_argument("--ibkr-db", type=Path, default=DEFAULT_IBKR_DB)
    parser.add_argument("--ibkr-runtime", type=Path, default=DEFAULT_IBKR_RUNTIME)
    parser.add_argument("--oanda-db", type=Path, default=DEFAULT_OANDA_DB)
    parser.add_argument("--oanda-env", type=Path, default=DEFAULT_OANDA_ENV)
    parser.add_argument("--mt5-db", type=Path, default=DEFAULT_MT5_DB)
    parser.add_argument(
        "--mt5-status-url",
        default=os.environ.get("LTS_MT5_STATUS_URL", ""),
        help="Optional fleet-reachable MT5 operational status endpoint.",
    )
    parser.add_argument("--shadow-db", type=Path, default=DEFAULT_SHADOW_DB)
    parser.add_argument("--capital-db", type=Path, default=DEFAULT_CAPITAL_DB)
    parser.add_argument("--capital-env", type=Path, default=DEFAULT_CAPITAL_ENV)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--latest-file", type=Path, default=DEFAULT_LATEST)
    parser.add_argument("--discussion-file", type=Path, default=DEFAULT_DISCUSSION)
    parser.add_argument("--monitor-db", type=Path, default=DEFAULT_MONITOR_DB)
    parser.add_argument("--stale-minutes", type=float, default=15.0)
    parser.add_argument("--repeat-minutes", type=float, default=60.0)
    parser.add_argument("--summary-hours", type=float, default=6.0)
    parser.add_argument("--ibkr-host", default="127.0.0.1")
    parser.add_argument("--ibkr-port", type=int, default=7497)
    parser.add_argument(
        "--require-oanda-rest",
        action="store_true",
        help=(
            "Alert when REST-v20 Practice credentials are absent. Leave disabled "
            "for OANDA Global Markets, whose supported automation path is MT5."
        ),
    )
    parser.add_argument("--mt5-only", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    args = parser.parse_args()

    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.state_file.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        now = time.time()
        state = _read_json(args.state_file)
        if state.get("schema") != STATE_SCHEMA:
            state = {"schema": STATE_SCHEMA, "events": {}}
        mt5 = read_mt5_snapshot(args.mt5_db, args.mt5_status_url)
        if args.mt5_only:
            alpaca: dict[str, Any] = {}
            ibkr: dict[str, Any] = {}
            oanda: Optional[dict[str, Any]] = None
            shadow: dict[str, Any] = {}
            capital: dict[str, Any] = {}
            events = evaluate_mt5(
                mt5,
                now=now,
                stale_seconds=args.stale_minutes * 60.0,
            )
            discussions: list[dict[str, Any]] = []
        else:
            alpaca = read_alpaca_snapshot(args.alpaca_db, now)
            alpaca["execution_runtime"] = read_execution_runtime(
                args.alpaca_runtime,
                "lts.alpaca.model_runner.heartbeat.v1",
                now=now,
                stale_seconds=args.stale_minutes * 60.0,
            )
            ibkr = read_ibkr_snapshot(
                args.ibkr_db,
                args.ibkr_host,
                args.ibkr_port,
            )
            ibkr["execution_runtime"] = read_execution_runtime(
                args.ibkr_runtime,
                "lts.ibkr.model_runner.heartbeat.v1",
                now=now,
                stale_seconds=args.stale_minutes * 60.0,
            )
            oanda = read_oanda_snapshot(args.oanda_db, args.oanda_env)
            shadow = read_shadow_snapshot(args.shadow_db)
            capital = read_capital_snapshot(args.capital_db, args.capital_env)
            events, discussions = evaluate(
                alpaca,
                ibkr,
                oanda,
                mt5,
                shadow,
                capital,
                now=now,
                stale_seconds=args.stale_minutes * 60.0,
                oanda_rest_required=args.require_oanda_rest,
            )
        store = MonitorStore(args.monitor_db)
        try:
            messages = process_events(
                [*events, *discussions],
                state,
                store,
                now=now,
                repeat_seconds=args.repeat_minutes * 60.0,
            )
            last_summary = float(state.get("last_summary_at", 0))
            if now - last_summary >= args.summary_hours * 3600.0:
                if args.mt5_only:
                    messages.append(format_mt5_summary(mt5))
                else:
                    messages.append(
                        format_summary(
                            alpaca, ibkr, oanda, mt5, shadow, capital
                        )
                    )
                state["last_summary_at"] = now
            packet = {
                "schema": "lts.hermes.live_trading_discussion.v1",
                "generated_at": _utc_now(),
                "policy": {
                    "can_place_orders": False,
                    "can_change_risk": False,
                    "can_enqueue_optimization": False,
                    "requires_human_review": True,
                },
                "active_events": events,
                "research_discussions": discussions,
                "suggested_questions": [
                    "Is this event operational, market-wide, or asset-specific?",
                    "Does an event-calendar or cross-asset explanation precede it?",
                    "Is the evidence sufficient to propose a bounded offline experiment?",
                    "What falsification test should run before queueing optimization?",
                ],
            }
            latest = {
                "schema": STATE_SCHEMA,
                "generated_at": _utc_now(),
                "alpaca": {
                    key: value
                    for key, value in alpaca.items()
                    if key not in {"history"}
                },
                "ibkr": ibkr,
                "oanda": oanda,
                "mt5": mt5,
                "shadow": shadow,
                "capital": capital,
                "active_event_keys": sorted(item["key"] for item in events),
                "discussion_event_keys": sorted(item["key"] for item in discussions),
            }
            _atomic_json(args.discussion_file, packet)
            _atomic_json(args.latest_file, latest)
            if messages and not args.no_telegram:
                _send_telegram("\n\n".join(messages))
            state["last_run_at"] = now
            _atomic_json(args.state_file, state)
            print(
                json.dumps(
                    {
                        "active_events": len(events),
                        "discussion_events": len(discussions),
                        "notifications": len(messages),
                        "telegram_enabled": not args.no_telegram,
                    },
                    sort_keys=True,
                )
            )
            return 0
        finally:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
