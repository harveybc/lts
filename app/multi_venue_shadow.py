"""Continuous no-order portfolio marking across approved paper-data venues."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


CONFIG_SCHEMA = "lts.multi_venue_shadow_config.v1"
OLAP_SCHEMA = "lts.multi_venue_shadow_olap.v1"
ENGINE_VERSION = "lts.multi_venue_shadow.v1"
SUPPORTED_VENUES = {"alpaca_paper", "ibkr_paper"}


class MultiVenueShadowError(RuntimeError):
    """Raised when a shadow-portfolio contract or source fails."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ShadowCell:
    cell_id: str
    venue: str
    symbol: str
    source_key: str
    role: str
    horizon: str
    weight: float
    max_quote_age_seconds: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ShadowCell":
        required = (
            "cell_id",
            "venue",
            "symbol",
            "source_key",
            "role",
            "horizon",
            "weight",
        )
        missing = [key for key in required if value.get(key) in (None, "")]
        if missing:
            raise MultiVenueShadowError(
                f"Shadow cell is missing: {', '.join(missing)}"
            )
        venue = str(value["venue"])
        if venue not in SUPPORTED_VENUES:
            raise MultiVenueShadowError(f"Unsupported shadow venue: {venue}")
        weight = float(value["weight"])
        if not 0 < weight <= 1:
            raise MultiVenueShadowError("Shadow cell weights must be in (0,1]")
        max_age = int(value.get("max_quote_age_seconds", 900))
        if max_age < 60:
            raise MultiVenueShadowError(
                "max_quote_age_seconds must be at least 60"
            )
        return cls(
            cell_id=str(value["cell_id"]),
            venue=venue,
            symbol=str(value["symbol"]),
            source_key=str(value["source_key"]),
            role=str(value["role"]),
            horizon=str(value["horizon"]),
            weight=weight,
            max_quote_age_seconds=max_age,
        )


@dataclass(frozen=True)
class MultiVenueShadowConfig:
    database_path: Path
    source_databases: Mapping[str, Path]
    initial_nav: float
    cells: tuple[ShadowCell, ...]

    @classmethod
    def load(cls, path: Path | str) -> "MultiVenueShadowConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema") != CONFIG_SCHEMA:
            raise MultiVenueShadowError("Unsupported shadow config schema")
        if data.get("mode") != "shadow_no_orders":
            raise MultiVenueShadowError("Portfolio observer must be shadow_no_orders")
        if data.get("orders", {}).get("enabled", False):
            raise MultiVenueShadowError("Orders are forbidden in the shadow portfolio")
        initial_nav = float(data.get("initial_nav", 100000.0))
        if not math.isfinite(initial_nav) or initial_nav <= 0:
            raise MultiVenueShadowError("initial_nav must be positive and finite")
        sources = {
            str(venue): _expand_path(str(source))
            for venue, source in (data.get("source_databases") or {}).items()
        }
        if set(sources) != SUPPORTED_VENUES:
            raise MultiVenueShadowError(
                "Source databases must define alpaca_paper and ibkr_paper"
            )
        cells = tuple(ShadowCell.from_dict(item) for item in data.get("cells", []))
        if not cells:
            raise MultiVenueShadowError("At least one shadow cell is required")
        cell_ids = [cell.cell_id for cell in cells]
        if len(cell_ids) != len(set(cell_ids)):
            raise MultiVenueShadowError("Shadow cell_id values must be unique")
        total_weight = sum(cell.weight for cell in cells)
        if not math.isclose(total_weight, 1.0, rel_tol=0, abs_tol=1e-9):
            raise MultiVenueShadowError(
                f"Shadow weights must sum to 1.0, found {total_weight}"
            )
        return cls(
            database_path=_expand_path(
                str(
                    data.get(
                        "database_path",
                        "~/.local/state/lts/multi-venue-shadow.sqlite",
                    )
                )
            ),
            source_databases=sources,
            initial_nav=initial_nav,
            cells=cells,
        )

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": CONFIG_SCHEMA,
            "mode": "shadow_no_orders",
            "initial_nav": self.initial_nav,
            "source_databases": {
                venue: str(path) for venue, path in self.source_databases.items()
            },
            "cells": [cell.__dict__ for cell in self.cells],
            "orders_enabled": False,
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.evidence()).encode("utf-8")
        ).hexdigest()


class QuoteReader:
    """Read the most recent normalized quote without modifying broker OLAP."""

    def __init__(self, paths: Mapping[str, Path]) -> None:
        self.paths = paths

    def _connect(self, venue: str) -> sqlite3.Connection:
        path = self.paths[venue]
        if not path.is_file():
            raise MultiVenueShadowError(
                f"{venue} source database is missing: {path}"
            )
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def latest(self, cell: ShadowCell) -> Optional[dict[str, Any]]:
        connection = self._connect(cell.venue)
        try:
            if cell.venue == "alpaca_paper":
                row = connection.execute(
                    """
                    SELECT symbol,broker_time,observed_at,mid AS mark_price,
                           spread_bps
                    FROM quote_observations
                    WHERE symbol=?
                    ORDER BY observed_at DESC LIMIT 1
                    """,
                    (cell.source_key,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT symbol,broker_time,observed_at,mark_price,spread_bps
                    FROM quote_observations
                    WHERE cell_id=? AND mark_price IS NOT NULL
                    ORDER BY observed_at DESC LIMIT 1
                    """,
                    (cell.source_key,),
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        finally:
            connection.close()
        if not row:
            return None
        price = _as_float(row["mark_price"])
        if price is None or price <= 0:
            return None
        return {
            "symbol": row["symbol"],
            "broker_time": row["broker_time"],
            "observed_at": row["observed_at"],
            "mark_price": price,
            "spread_bps": _as_float(row["spread_bps"]),
        }


class MultiVenueShadowOlap:
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
            CREATE TABLE IF NOT EXISTS shadow_runs (
                run_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                config_sha256 TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL,
                detail_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS anchor_prices (
                config_sha256 TEXT NOT NULL,
                cell_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (config_sha256,cell_id)
            );
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                run_id TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                initial_nav REAL NOT NULL,
                nav REAL NOT NULL,
                total_return REAL NOT NULL,
                available_weight REAL NOT NULL,
                stale_cells INTEGER NOT NULL,
                missing_cells INTEGER NOT NULL,
                orders_submitted INTEGER NOT NULL,
                FOREIGN KEY (run_id) REFERENCES shadow_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS cell_snapshots (
                run_id TEXT NOT NULL,
                cell_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                symbol TEXT NOT NULL,
                role TEXT NOT NULL,
                horizon TEXT NOT NULL,
                weight REAL NOT NULL,
                quote_observed_at TEXT,
                quote_age_seconds REAL,
                stale INTEGER NOT NULL,
                missing INTEGER NOT NULL,
                price REAL,
                anchor_price REAL,
                price_ratio REAL,
                cell_return REAL,
                contribution REAL NOT NULL,
                spread_bps REAL,
                PRIMARY KEY (run_id,cell_id),
                FOREIGN KEY (run_id) REFERENCES shadow_runs(run_id)
            );
            CREATE VIEW IF NOT EXISTS multi_venue_portfolio_olap AS
            SELECT p.observed_at,p.nav,p.total_return,p.available_weight,
                   p.stale_cells,p.missing_cells,p.orders_submitted,
                   r.config_sha256,r.status
            FROM portfolio_snapshots p
            JOIN shadow_runs r ON r.run_id=p.run_id;
            CREATE VIEW IF NOT EXISTS multi_venue_cell_olap AS
            SELECT cell_id,venue,symbol,role,horizon,COUNT(*) AS observations,
                   AVG(cell_return) AS mean_cell_return,
                   MIN(cell_return) AS min_cell_return,
                   MAX(cell_return) AS max_cell_return,
                   AVG(spread_bps) AS mean_spread_bps,
                   MAX(quote_age_seconds) AS max_quote_age_seconds,
                   SUM(stale) AS stale_observations,
                   SUM(missing) AS missing_observations
            FROM cell_snapshots
            GROUP BY cell_id,venue,symbol,role,horizon;
            """
        )
        self.connection.commit()

    def anchor(
        self,
        config_sha256: str,
        cell: ShadowCell,
        quote: Mapping[str, Any],
    ) -> float:
        row = self.connection.execute(
            """
            SELECT price FROM anchor_prices
            WHERE config_sha256=? AND cell_id=?
            """,
            (config_sha256, cell.cell_id),
        ).fetchone()
        if row:
            return float(row["price"])
        price = float(quote["mark_price"])
        self.connection.execute(
            "INSERT INTO anchor_prices VALUES (?,?,?,?,?,?)",
            (
                config_sha256,
                cell.cell_id,
                cell.venue,
                cell.symbol,
                price,
                quote["observed_at"],
            ),
        )
        self.connection.commit()
        return price

    def record(
        self,
        *,
        config: MultiVenueShadowConfig,
        rows: Sequence[Mapping[str, Any]],
        nav: float,
        available_weight: float,
    ) -> dict[str, Any]:
        run_id = f"shadow-{uuid.uuid4().hex[:16]}"
        observed_at = _utc_now()
        config_sha256 = config.fingerprint()
        stale_count = sum(int(bool(row["stale"])) for row in rows)
        missing_count = sum(int(bool(row["missing"])) for row in rows)
        status = (
            "complete"
            if stale_count == 0 and missing_count == 0
            else "degraded"
        )
        result = {
            "run_id": run_id,
            "status": status,
            "observed_at": observed_at,
            "initial_nav": config.initial_nav,
            "nav": nav,
            "total_return": nav / config.initial_nav - 1.0,
            "available_weight": available_weight,
            "stale_cells": stale_count,
            "missing_cells": missing_count,
            "orders_submitted": 0,
        }
        self.connection.execute(
            "INSERT INTO shadow_runs VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                OLAP_SCHEMA,
                ENGINE_VERSION,
                config_sha256,
                observed_at,
                observed_at,
                status,
                _canonical_json(result),
            ),
        )
        self.connection.execute(
            "INSERT INTO portfolio_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                observed_at,
                config.initial_nav,
                nav,
                result["total_return"],
                available_weight,
                stale_count,
                missing_count,
                0,
            ),
        )
        self.connection.executemany(
            """
            INSERT INTO cell_snapshots VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    run_id,
                    row["cell_id"],
                    row["venue"],
                    row["symbol"],
                    row["role"],
                    row["horizon"],
                    row["weight"],
                    row.get("quote_observed_at"),
                    row.get("quote_age_seconds"),
                    int(bool(row["stale"])),
                    int(bool(row["missing"])),
                    row.get("price"),
                    row.get("anchor_price"),
                    row.get("price_ratio"),
                    row.get("cell_return"),
                    row["contribution"],
                    row.get("spread_bps"),
                )
                for row in rows
            ],
        )
        self.connection.commit()
        return result

    def report(self) -> dict[str, Any]:
        latest = self.connection.execute(
            """
            SELECT p.*,r.status,r.config_sha256
            FROM portfolio_snapshots p
            JOIN shadow_runs r ON r.run_id=p.run_id
            ORDER BY p.observed_at DESC LIMIT 1
            """
        ).fetchone()
        cells = []
        if latest:
            cells = self.connection.execute(
                """
                SELECT * FROM cell_snapshots
                WHERE run_id=? ORDER BY venue,cell_id
                """,
                (latest["run_id"],),
            ).fetchall()
        return {
            "schema_version": OLAP_SCHEMA,
            "engine_version": ENGINE_VERSION,
            "database_path": str(self.path),
            "latest_snapshot": dict(latest) if latest else None,
            "latest_cells": [dict(row) for row in cells],
        }


class MultiVenueShadow:
    def __init__(
        self,
        config: MultiVenueShadowConfig,
        reader: QuoteReader,
        store: MultiVenueShadowOlap,
    ) -> None:
        self.config = config
        self.reader = reader
        self.store = store

    def snapshot(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        config_sha256 = self.config.fingerprint()
        rows: list[dict[str, Any]] = []
        nav = 0.0
        available_weight = 0.0
        for cell in self.config.cells:
            quote = self.reader.latest(cell)
            missing = quote is None
            stale = False
            age_seconds: Optional[float] = None
            price = anchor = ratio = cell_return = spread_bps = None
            contribution = self.config.initial_nav * cell.weight
            quote_observed_at = None
            if quote is not None:
                quote_observed_at = str(quote["observed_at"])
                age_seconds = max(
                    0.0,
                    (current - _parse_time(quote_observed_at)).total_seconds(),
                )
                stale = age_seconds > cell.max_quote_age_seconds
                price = float(quote["mark_price"])
                anchor = self.store.anchor(config_sha256, cell, quote)
                ratio = price / anchor
                cell_return = ratio - 1.0
                contribution *= ratio
                spread_bps = _as_float(quote.get("spread_bps"))
                if not stale:
                    available_weight += cell.weight
            nav += contribution
            rows.append(
                {
                    **cell.__dict__,
                    "quote_observed_at": quote_observed_at,
                    "quote_age_seconds": age_seconds,
                    "stale": stale,
                    "missing": missing,
                    "price": price,
                    "anchor_price": anchor,
                    "price_ratio": ratio,
                    "cell_return": cell_return,
                    "contribution": contribution,
                    "spread_bps": spread_bps,
                }
            )
        result = self.store.record(
            config=self.config,
            rows=rows,
            nav=nav,
            available_weight=available_weight,
        )
        result["cells"] = rows
        result["mode"] = "shadow_no_orders"
        return result
