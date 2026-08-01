"""Provider-neutral accounting and allocation lab for social trading.

The module deliberately contains no broker client and cannot submit orders.
It models the money and replication semantics that LTS must understand before
enabling copy, PAMM, MAM, or signal-provider workflows on a real account.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, getcontext
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


REGISTRY_SCHEMA = "lts.social_platform_registry.v1"
SCENARIO_SCHEMA = "lts.social_trading_scenario.v1"
OLAP_SCHEMA = "lts.social_trading_olap.v1"
ENGINE_VERSION = "lts.social_trading_lab.v1"
SUPPORTED_MODES = {"copy", "signal", "pamm", "mam", "provider_index"}
SUPPORTED_EVENT_TYPES = {
    "subscribe",
    "deposit",
    "withdraw",
    "strategy_return",
    "performance_fee",
    "management_fee",
    "copy_allocation",
}

getcontext().prec = 34


class SocialTradingLabError(RuntimeError):
    """Raised when a social-trading contract or accounting invariant fails."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise SocialTradingLabError(f"{field} must be a decimal number") from exc
    if not result.is_finite():
        raise SocialTradingLabError(f"{field} must be finite")
    return result


def _positive(value: Any, field: str, *, allow_zero: bool = False) -> Decimal:
    result = _decimal(value, field)
    if result < 0 or (result == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise SocialTradingLabError(f"{field} must be {qualifier}")
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class PlatformCapability:
    platform_id: str
    display_name: str
    disposition: str
    modes: tuple[str, ...]
    account_environment: str
    automation: str
    instrument_classes: tuple[str, ...]
    native_sltp_replication: bool
    local_protection_overlay_possible: bool
    provider_requires_live_capital: bool
    evidence_urls: tuple[str, ...]
    limitations: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlatformCapability":
        required = (
            "platform_id",
            "display_name",
            "disposition",
            "modes",
            "account_environment",
            "automation",
            "instrument_classes",
            "protection",
            "provider_requirements",
            "evidence_urls",
        )
        missing = [key for key in required if value.get(key) in (None, "", [])]
        if missing:
            raise SocialTradingLabError(
                f"Platform capability is missing: {', '.join(missing)}"
            )
        modes = tuple(str(mode) for mode in value["modes"])
        unsupported = sorted(set(modes) - SUPPORTED_MODES)
        if unsupported:
            raise SocialTradingLabError(
                f"Unsupported social modes for {value['platform_id']}: "
                f"{', '.join(unsupported)}"
            )
        protection = value["protection"]
        provider = value["provider_requirements"]
        evidence = tuple(str(url) for url in value["evidence_urls"])
        if any(not url.startswith("https://") for url in evidence):
            raise SocialTradingLabError("Evidence URLs must use HTTPS")
        return cls(
            platform_id=str(value["platform_id"]),
            display_name=str(value["display_name"]),
            disposition=str(value["disposition"]),
            modes=modes,
            account_environment=str(value["account_environment"]),
            automation=str(value["automation"]),
            instrument_classes=tuple(
                str(item) for item in value["instrument_classes"]
            ),
            native_sltp_replication=bool(
                protection.get("native_sltp_replication", False)
            ),
            local_protection_overlay_possible=bool(
                protection.get("local_protection_overlay_possible", False)
            ),
            provider_requires_live_capital=bool(
                provider.get("requires_live_capital", False)
            ),
            evidence_urls=evidence,
            limitations=tuple(str(item) for item in value.get("limitations", [])),
        )

    def supports_protected_entries(self) -> bool:
        return self.native_sltp_replication or self.local_protection_overlay_possible


@dataclass(frozen=True)
class SocialPlatformRegistry:
    platforms: tuple[PlatformCapability, ...]
    source_path: Path
    research_order: tuple[str, ...]

    @classmethod
    def load(cls, path: Path | str) -> "SocialPlatformRegistry":
        source_path = Path(path)
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        if payload.get("schema") != REGISTRY_SCHEMA:
            raise SocialTradingLabError("Unsupported platform registry schema")
        platforms = tuple(
            PlatformCapability.from_dict(item)
            for item in payload.get("platforms", [])
        )
        if not platforms:
            raise SocialTradingLabError("Platform registry cannot be empty")
        ids = [platform.platform_id for platform in platforms]
        if len(ids) != len(set(ids)):
            raise SocialTradingLabError("Platform IDs must be unique")
        research_order = tuple(str(item) for item in payload.get("research_order", []))
        unknown = sorted(set(research_order) - set(ids))
        if unknown:
            raise SocialTradingLabError(
                f"Research order references unknown platforms: {', '.join(unknown)}"
            )
        return cls(platforms, source_path, research_order)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.source_path.read_bytes()).hexdigest()

    def get(self, platform_id: str) -> PlatformCapability:
        for platform in self.platforms:
            if platform.platform_id == platform_id:
                return platform
        raise SocialTradingLabError(f"Unknown platform: {platform_id}")

    def eligible(
        self,
        *,
        mode: str,
        require_automation: bool = False,
        require_protected_entries: bool = False,
        allow_live_capital: bool = False,
    ) -> list[PlatformCapability]:
        if mode not in SUPPORTED_MODES:
            raise SocialTradingLabError(f"Unsupported social mode: {mode}")
        result = []
        for platform in self.platforms:
            if mode not in platform.modes:
                continue
            if require_automation and platform.automation == "manual_only":
                continue
            if require_protected_entries and not platform.supports_protected_entries():
                continue
            if platform.provider_requires_live_capital and not allow_live_capital:
                continue
            result.append(platform)
        return result

    def report(self) -> dict[str, Any]:
        return {
            "schema": REGISTRY_SCHEMA,
            "registry_sha256": self.fingerprint(),
            "research_order": list(self.research_order),
            "platform_count": len(self.platforms),
            "platforms": [
                {
                    "platform_id": item.platform_id,
                    "display_name": item.display_name,
                    "disposition": item.disposition,
                    "modes": list(item.modes),
                    "account_environment": item.account_environment,
                    "automation": item.automation,
                    "instrument_classes": list(item.instrument_classes),
                    "protected_entries_supported": item.supports_protected_entries(),
                    "native_sltp_replication": item.native_sltp_replication,
                    "provider_requires_live_capital": (
                        item.provider_requires_live_capital
                    ),
                    "limitations": list(item.limitations),
                }
                for item in self.platforms
            ],
        }


@dataclass
class InvestorAccount:
    investor_id: str
    units: Decimal = Decimal("0")
    high_water_mark: Decimal = Decimal("0")
    cumulative_deposits: Decimal = Decimal("0")
    cumulative_withdrawals: Decimal = Decimal("0")
    performance_fees: Decimal = Decimal("0")
    management_fees: Decimal = Decimal("0")


class UnitizedPammLedger:
    """Unitized pooled accounting with investor-level high-water marks."""

    def __init__(self, *, unit_nav: Any = "1") -> None:
        self.unit_nav = _positive(unit_nav, "unit_nav")
        self.investors: dict[str, InvestorAccount] = {}
        self.manager_fee_balance = Decimal("0")
        self.sequence = 0

    @property
    def total_units(self) -> Decimal:
        return sum((item.units for item in self.investors.values()), Decimal("0"))

    @property
    def investor_equity(self) -> Decimal:
        return self.total_units * self.unit_nav

    def _account(self, investor_id: str) -> InvestorAccount:
        if not investor_id:
            raise SocialTradingLabError("investor_id is required")
        return self.investors.setdefault(investor_id, InvestorAccount(investor_id))

    def equity(self, investor_id: str) -> Decimal:
        account = self._account(investor_id)
        return account.units * self.unit_nav

    def deposit(self, investor_id: str, amount: Any) -> dict[str, Any]:
        value = _positive(amount, "deposit amount")
        account = self._account(investor_id)
        units = value / self.unit_nav
        account.units += units
        account.high_water_mark += value
        account.cumulative_deposits += value
        self.sequence += 1
        return self._event("deposit", investor_id, value, units=units)

    def withdraw(self, investor_id: str, amount: Any) -> dict[str, Any]:
        value = _positive(amount, "withdraw amount")
        account = self._account(investor_id)
        equity_before = account.units * self.unit_nav
        if value > equity_before:
            raise SocialTradingLabError("withdraw amount exceeds investor equity")
        units_before = account.units
        units = value / self.unit_nav
        fraction = units / units_before
        account.units -= units
        account.high_water_mark *= Decimal("1") - fraction
        account.cumulative_withdrawals += value
        self.sequence += 1
        return self._event("withdraw", investor_id, value, units=units)

    def apply_strategy_return(self, rate: Any) -> dict[str, Any]:
        value = _decimal(rate, "strategy return")
        if value <= Decimal("-1"):
            raise SocialTradingLabError("strategy return must be greater than -1")
        unit_nav_before = self.unit_nav
        self.unit_nav *= Decimal("1") + value
        self.sequence += 1
        return {
            "event_type": "strategy_return",
            "sequence": self.sequence,
            "rate": _decimal_text(value),
            "unit_nav_before": _decimal_text(unit_nav_before),
            "unit_nav_after": _decimal_text(self.unit_nav),
        }

    def crystallize_performance_fee(
        self, investor_id: str, rate: Any
    ) -> dict[str, Any]:
        fee_rate = _positive(rate, "performance fee rate", allow_zero=True)
        if fee_rate > 1:
            raise SocialTradingLabError("performance fee rate cannot exceed 1")
        account = self._account(investor_id)
        gross_equity = account.units * self.unit_nav
        eligible_profit = max(gross_equity - account.high_water_mark, Decimal("0"))
        fee = eligible_profit * fee_rate
        if fee:
            account.units -= fee / self.unit_nav
            account.performance_fees += fee
            self.manager_fee_balance += fee
        net_equity = account.units * self.unit_nav
        account.high_water_mark = max(account.high_water_mark, net_equity)
        self.sequence += 1
        return self._event(
            "performance_fee",
            investor_id,
            fee,
            eligible_profit=eligible_profit,
            rate=fee_rate,
        )

    def charge_management_fee(
        self, investor_id: str, annual_rate: Any, elapsed_days: Any
    ) -> dict[str, Any]:
        fee_rate = _positive(
            annual_rate, "annual management fee rate", allow_zero=True
        )
        days = _positive(elapsed_days, "elapsed days", allow_zero=True)
        if fee_rate > 1:
            raise SocialTradingLabError("annual management fee rate cannot exceed 1")
        account = self._account(investor_id)
        gross_equity = account.units * self.unit_nav
        fee = gross_equity * fee_rate * days / Decimal("365")
        if fee >= gross_equity and gross_equity:
            raise SocialTradingLabError("management fee would exhaust investor equity")
        if fee:
            account.units -= fee / self.unit_nav
            account.management_fees += fee
            self.manager_fee_balance += fee
        self.sequence += 1
        return self._event(
            "management_fee",
            investor_id,
            fee,
            rate=fee_rate,
            elapsed_days=days,
        )

    def _event(
        self,
        event_type: str,
        investor_id: str,
        amount: Decimal,
        **extra: Decimal,
    ) -> dict[str, Any]:
        result = {
            "event_type": event_type,
            "sequence": self.sequence,
            "investor_id": investor_id,
            "amount": _decimal_text(amount),
            "unit_nav": _decimal_text(self.unit_nav),
            "investor_equity": _decimal_text(self.equity(investor_id)),
        }
        result.update({key: _decimal_text(value) for key, value in extra.items()})
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "unit_nav": _decimal_text(self.unit_nav),
            "total_units": _decimal_text(self.total_units),
            "investor_equity": _decimal_text(self.investor_equity),
            "manager_fee_balance": _decimal_text(self.manager_fee_balance),
            "sequence": self.sequence,
            "investors": {
                investor_id: {
                    "units": _decimal_text(account.units),
                    "equity": _decimal_text(account.units * self.unit_nav),
                    "high_water_mark": _decimal_text(account.high_water_mark),
                    "cumulative_deposits": _decimal_text(
                        account.cumulative_deposits
                    ),
                    "cumulative_withdrawals": _decimal_text(
                        account.cumulative_withdrawals
                    ),
                    "performance_fees": _decimal_text(account.performance_fees),
                    "management_fees": _decimal_text(account.management_fees),
                }
                for investor_id, account in sorted(self.investors.items())
            },
        }


@dataclass(frozen=True)
class CopyAllocationContract:
    platform_id: str
    minimum_volume: Decimal
    maximum_volume: Decimal
    volume_step: Decimal
    below_minimum_policy: str
    native_sltp_replication: bool
    local_protection_overlay: bool

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CopyAllocationContract":
        policy = str(value.get("below_minimum_policy", "reject"))
        if policy not in {"reject", "round_up_minimum"}:
            raise SocialTradingLabError("Unsupported below-minimum policy")
        minimum = _positive(value.get("minimum_volume"), "minimum_volume")
        maximum = _positive(value.get("maximum_volume"), "maximum_volume")
        step = _positive(value.get("volume_step"), "volume_step")
        if maximum < minimum:
            raise SocialTradingLabError("maximum_volume cannot be below minimum")
        return cls(
            platform_id=str(value.get("platform_id") or "local"),
            minimum_volume=minimum,
            maximum_volume=maximum,
            volume_step=step,
            below_minimum_policy=policy,
            native_sltp_replication=bool(
                value.get("native_sltp_replication", False)
            ),
            local_protection_overlay=bool(
                value.get("local_protection_overlay", False)
            ),
        )

    def allocate(
        self,
        *,
        provider_volume: Any,
        provider_equity: Any,
        investor_equity: Any,
        require_protected_entry: bool = True,
    ) -> dict[str, Any]:
        source_volume = _positive(provider_volume, "provider_volume")
        source_equity = _positive(provider_equity, "provider_equity")
        target_equity = _positive(investor_equity, "investor_equity")
        if require_protected_entry and not (
            self.native_sltp_replication or self.local_protection_overlay
        ):
            return self._rejected(
                "protected_entry_unavailable",
                raw_volume=source_volume * target_equity / source_equity,
            )
        raw = source_volume * target_equity / source_equity
        if raw > self.maximum_volume:
            return self._rejected("above_maximum_volume", raw_volume=raw)
        if raw < self.minimum_volume:
            if self.below_minimum_policy == "reject":
                return self._rejected("below_minimum_volume", raw_volume=raw)
            allocated = self.minimum_volume
        else:
            steps = (raw / self.volume_step).to_integral_value(rounding=ROUND_FLOOR)
            allocated = steps * self.volume_step
            if allocated < self.minimum_volume:
                allocated = self.minimum_volume
        tracking_error = abs(allocated - raw) / raw if raw else Decimal("0")
        return {
            "status": "allocated",
            "platform_id": self.platform_id,
            "raw_volume": _decimal_text(raw),
            "allocated_volume": _decimal_text(allocated),
            "volume_tracking_error": _decimal_text(tracking_error),
            "native_sltp_replication": self.native_sltp_replication,
            "local_protection_overlay": self.local_protection_overlay,
        }

    def _rejected(self, reason: str, *, raw_volume: Decimal) -> dict[str, Any]:
        return {
            "status": "rejected",
            "platform_id": self.platform_id,
            "reason": reason,
            "raw_volume": _decimal_text(raw_volume),
            "allocated_volume": "0",
            "volume_tracking_error": None,
            "native_sltp_replication": self.native_sltp_replication,
            "local_protection_overlay": self.local_protection_overlay,
        }


@dataclass(frozen=True)
class SocialTradingScenario:
    scenario_id: str
    database_path: Path
    registry_path: Path
    events: tuple[Mapping[str, Any], ...]
    orders_enabled: bool

    @classmethod
    def load(cls, path: Path | str) -> "SocialTradingScenario":
        source_path = Path(path)
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        if payload.get("schema") != SCENARIO_SCHEMA:
            raise SocialTradingLabError("Unsupported social scenario schema")
        if payload.get("mode") != "accounting_simulation_no_orders":
            raise SocialTradingLabError("Scenario must be accounting_simulation_no_orders")
        orders_enabled = bool((payload.get("orders") or {}).get("enabled", False))
        if orders_enabled:
            raise SocialTradingLabError("Orders are forbidden in the social lab")
        events = tuple(payload.get("events") or [])
        if not events:
            raise SocialTradingLabError("Scenario requires at least one event")
        unsupported = sorted(
            {
                str(event.get("type"))
                for event in events
                if event.get("type") not in SUPPORTED_EVENT_TYPES
            }
        )
        if unsupported:
            raise SocialTradingLabError(
                f"Unsupported scenario events: {', '.join(unsupported)}"
            )
        return cls(
            scenario_id=str(payload.get("scenario_id") or source_path.stem),
            database_path=_expand_path(
                str(
                    payload.get(
                        "database_path",
                        "~/.local/state/lts/social-trading-lab.sqlite",
                    )
                )
            ),
            registry_path=(source_path.parent / payload["registry_path"]).resolve()
            if not Path(payload["registry_path"]).is_absolute()
            else Path(payload["registry_path"]),
            events=events,
            orders_enabled=orders_enabled,
        )

    def fingerprint(self) -> str:
        return _sha256(
            {
                "scenario_id": self.scenario_id,
                "registry_path": str(self.registry_path),
                "events": self.events,
                "orders_enabled": self.orders_enabled,
            }
        )


class SocialTradingOlap:
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
            CREATE TABLE IF NOT EXISTS social_lab_runs (
                run_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                scenario_id TEXT NOT NULL,
                scenario_sha256 TEXT NOT NULL,
                registry_sha256 TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                status TEXT NOT NULL,
                orders_submitted INTEGER NOT NULL,
                final_snapshot_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS social_lab_events (
                run_id TEXT NOT NULL,
                event_index INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                investor_id TEXT,
                status TEXT NOT NULL,
                event_json TEXT NOT NULL,
                PRIMARY KEY (run_id,event_index),
                FOREIGN KEY (run_id) REFERENCES social_lab_runs(run_id)
            );
            CREATE VIEW IF NOT EXISTS social_lab_run_olap AS
            SELECT scenario_id,COUNT(*) AS runs,MAX(ended_at) AS latest_run,
                   SUM(orders_submitted) AS orders_submitted
            FROM social_lab_runs
            GROUP BY scenario_id;
            CREATE VIEW IF NOT EXISTS social_lab_event_olap AS
            SELECT event_type,status,COUNT(*) AS event_count
            FROM social_lab_events
            GROUP BY event_type,status;
            """
        )
        self.connection.commit()

    def record(
        self,
        *,
        run_id: str,
        scenario: SocialTradingScenario,
        registry_sha256: str,
        started_at: str,
        ended_at: str,
        events: Sequence[Mapping[str, Any]],
        final_snapshot: Mapping[str, Any],
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO social_lab_runs
                (run_id,schema_version,engine_version,scenario_id,
                 scenario_sha256,registry_sha256,started_at,ended_at,status,
                 orders_submitted,final_snapshot_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    OLAP_SCHEMA,
                    ENGINE_VERSION,
                    scenario.scenario_id,
                    scenario.fingerprint(),
                    registry_sha256,
                    started_at,
                    ended_at,
                    "complete",
                    0,
                    _canonical_json(final_snapshot),
                ),
            )
            self.connection.executemany(
                """
                INSERT INTO social_lab_events
                (run_id,event_index,event_type,investor_id,status,event_json)
                VALUES (?,?,?,?,?,?)
                """,
                [
                    (
                        run_id,
                        index,
                        str(event.get("event_type") or event.get("type")),
                        event.get("investor_id"),
                        str(event.get("status") or "complete"),
                        _canonical_json(event),
                    )
                    for index, event in enumerate(events)
                ],
            )

    def report(self) -> dict[str, Any]:
        latest = self.connection.execute(
            """
            SELECT * FROM social_lab_runs ORDER BY ended_at DESC LIMIT 1
            """
        ).fetchone()
        aggregate = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM social_lab_event_olap ORDER BY event_type,status"
            ).fetchall()
        ]
        return {
            "schema": OLAP_SCHEMA,
            "database_path": str(self.path),
            "latest_run": None
            if latest is None
            else {
                "run_id": latest["run_id"],
                "scenario_id": latest["scenario_id"],
                "scenario_sha256": latest["scenario_sha256"],
                "registry_sha256": latest["registry_sha256"],
                "ended_at": latest["ended_at"],
                "status": latest["status"],
                "orders_submitted": latest["orders_submitted"],
                "final_snapshot": json.loads(latest["final_snapshot_json"]),
            },
            "event_aggregate": aggregate,
        }


def run_scenario(
    scenario: SocialTradingScenario,
    registry: SocialPlatformRegistry,
    store: SocialTradingOlap,
) -> dict[str, Any]:
    run_id = f"social-{uuid.uuid4().hex[:16]}"
    started_at = _utc_now()
    ledger = UnitizedPammLedger()
    results: list[dict[str, Any]] = []
    for raw_event in scenario.events:
        event_type = str(raw_event["type"])
        investor_id = str(raw_event.get("investor_id") or "")
        if event_type in {"subscribe", "deposit"}:
            result = ledger.deposit(investor_id, raw_event["amount"])
            result["event_type"] = event_type
        elif event_type == "withdraw":
            result = ledger.withdraw(investor_id, raw_event["amount"])
        elif event_type == "strategy_return":
            result = ledger.apply_strategy_return(raw_event["rate"])
        elif event_type == "performance_fee":
            result = ledger.crystallize_performance_fee(
                investor_id, raw_event["rate"]
            )
        elif event_type == "management_fee":
            result = ledger.charge_management_fee(
                investor_id,
                raw_event["annual_rate"],
                raw_event["elapsed_days"],
            )
        else:
            platform = registry.get(str(raw_event["platform_id"]))
            allocation_payload = dict(raw_event["contract"])
            allocation_payload.update(
                {
                    "platform_id": platform.platform_id,
                    "native_sltp_replication": platform.native_sltp_replication,
                    "local_protection_overlay": (
                        platform.local_protection_overlay_possible
                    ),
                }
            )
            result = CopyAllocationContract.from_dict(
                allocation_payload
            ).allocate(
                provider_volume=raw_event["provider_volume"],
                provider_equity=raw_event["provider_equity"],
                investor_equity=raw_event["investor_equity"],
                require_protected_entry=bool(
                    raw_event.get("require_protected_entry", True)
                ),
            )
            result["event_type"] = "copy_allocation"
            result["investor_id"] = investor_id or None
        results.append(result)
    ended_at = _utc_now()
    snapshot = ledger.snapshot()
    store.record(
        run_id=run_id,
        scenario=scenario,
        registry_sha256=registry.fingerprint(),
        started_at=started_at,
        ended_at=ended_at,
        events=results,
        final_snapshot=snapshot,
    )
    return {
        "schema": OLAP_SCHEMA,
        "run_id": run_id,
        "scenario_id": scenario.scenario_id,
        "status": "complete",
        "orders_submitted": 0,
        "events": results,
        "final_snapshot": snapshot,
        "registry_sha256": registry.fingerprint(),
        "scenario_sha256": scenario.fingerprint(),
    }
