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
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    getcontext,
)
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


REGISTRY_SCHEMA = "lts.social_platform_registry.v1"
SCENARIO_SCHEMA = "lts.social_trading_scenario.v2"
OLAP_SCHEMA = "lts.social_trading_olap.v2"
ENGINE_VERSION = "lts.social_trading_lab.v2"
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
    role: str = "investor"
    units: Decimal = Decimal("0")
    high_water_mark: Decimal = Decimal("0")
    cumulative_deposits: Decimal = Decimal("0")
    cumulative_withdrawals: Decimal = Decimal("0")
    cumulative_gross_withdrawals: Decimal = Decimal("0")
    cumulative_net_withdrawals: Decimal = Decimal("0")
    performance_fees: Decimal = Decimal("0")
    management_fees: Decimal = Decimal("0")


class UnitizedPammLedger:
    """Unitized pooled accounting with investor-level high-water marks."""

    def __init__(
        self,
        *,
        unit_nav: Any = "1",
        currency: str = "USD",
        money_quantum: Any = "0.01",
        performance_fee_rate: Any = "0",
        crystallize_on_withdrawal: bool = True,
    ) -> None:
        self.unit_nav = _positive(unit_nav, "unit_nav")
        self.currency = str(currency).strip().upper()
        if not self.currency or len(self.currency) > 12:
            raise SocialTradingLabError("currency must be a short non-empty code")
        self.money_quantum = _positive(money_quantum, "money_quantum")
        if self.money_quantum.normalize().as_tuple().digits != (1,):
            raise SocialTradingLabError(
                "money_quantum must define a base-10 currency exponent"
            )
        self.performance_fee_rate = _positive(
            performance_fee_rate,
            "performance_fee_rate",
            allow_zero=True,
        )
        if self.performance_fee_rate > 1:
            raise SocialTradingLabError(
                "performance_fee_rate cannot exceed 1"
            )
        self.crystallize_on_withdrawal = bool(crystallize_on_withdrawal)
        self.investors: dict[str, InvestorAccount] = {}
        self.manager_fee_balance = Decimal("0")
        self.sequence = 0

    def _money(self, value: Decimal) -> Decimal:
        return value.quantize(self.money_quantum, rounding=ROUND_HALF_EVEN)

    def _money_text(self, value: Decimal) -> str:
        return _decimal_text(self._money(value))

    @property
    def total_units(self) -> Decimal:
        return sum((item.units for item in self.investors.values()), Decimal("0"))

    @property
    def pool_equity(self) -> Decimal:
        return self.total_units * self.unit_nav

    @property
    def investor_equity(self) -> Decimal:
        return sum(
            (
                account.units * self.unit_nav
                for account in self.investors.values()
                if account.role == "investor"
            ),
            Decimal("0"),
        )

    def _account(
        self, investor_id: str, *, role: Optional[str] = None
    ) -> InvestorAccount:
        if not investor_id:
            raise SocialTradingLabError("investor_id is required")
        if role is not None and role not in {"investor", "manager"}:
            raise SocialTradingLabError("account role must be investor or manager")
        account = self.investors.get(investor_id)
        if account is None:
            account = InvestorAccount(investor_id, role or "investor")
            self.investors[investor_id] = account
        elif role is not None and role != account.role:
            raise SocialTradingLabError("account role cannot change after creation")
        return account

    def equity(self, investor_id: str) -> Decimal:
        account = self._account(investor_id)
        return account.units * self.unit_nav

    def deposit(
        self, investor_id: str, amount: Any, *, role: str = "investor"
    ) -> dict[str, Any]:
        value = self._money(_positive(amount, "deposit amount"))
        if value <= 0:
            raise SocialTradingLabError(
                "deposit amount is below the account currency quantum"
            )
        account = self._account(investor_id, role=role)
        units = value / self.unit_nav
        account.units += units
        account.high_water_mark = self._money(account.high_water_mark + value)
        account.cumulative_deposits = self._money(
            account.cumulative_deposits + value
        )
        self.sequence += 1
        return self._event(
            "deposit", investor_id, value, units=units, account_role=role
        )

    def withdraw(self, investor_id: str, amount: Any) -> dict[str, Any]:
        gross_redemption = self._money(_positive(amount, "withdraw amount"))
        if gross_redemption <= 0:
            raise SocialTradingLabError(
                "withdraw amount is below the account currency quantum"
            )
        account = self._account(investor_id)
        equity_before = self._money(account.units * self.unit_nav)
        if gross_redemption > equity_before:
            raise SocialTradingLabError("withdraw amount exceeds investor equity")
        fraction = gross_redemption / equity_before
        eligible_profit = max(
            equity_before - self._money(account.high_water_mark), Decimal("0")
        )
        withdrawn_eligible_profit = self._money(eligible_profit * fraction)
        performance_fee = Decimal("0")
        if (
            account.role == "investor"
            and self.crystallize_on_withdrawal
            and self.performance_fee_rate
        ):
            performance_fee = self._money(
                withdrawn_eligible_profit * self.performance_fee_rate
            )
        net_disbursement = self._money(gross_redemption - performance_fee)
        units = gross_redemption / self.unit_nav
        if gross_redemption == equity_before:
            account.units = Decimal("0")
        else:
            account.units -= units
        account.high_water_mark = self._money(
            account.high_water_mark * (Decimal("1") - fraction)
        )
        account.performance_fees = self._money(
            account.performance_fees + performance_fee
        )
        self.manager_fee_balance = self._money(
            self.manager_fee_balance + performance_fee
        )
        account.cumulative_withdrawals = self._money(
            account.cumulative_withdrawals + net_disbursement
        )
        account.cumulative_gross_withdrawals = self._money(
            account.cumulative_gross_withdrawals + gross_redemption
        )
        account.cumulative_net_withdrawals = self._money(
            account.cumulative_net_withdrawals + net_disbursement
        )
        self.sequence += 1
        return self._event(
            "withdraw",
            investor_id,
            net_disbursement,
            gross_redemption=gross_redemption,
            performance_fee=performance_fee,
            eligible_profit=eligible_profit,
            withdrawn_eligible_profit=withdrawn_eligible_profit,
            performance_fee_rate=self.performance_fee_rate,
            redeemed_fraction=fraction,
            units=units,
        )

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
        self, investor_id: str, rate: Any = None
    ) -> dict[str, Any]:
        fee_rate = (
            self.performance_fee_rate
            if rate is None
            else _positive(rate, "performance fee rate", allow_zero=True)
        )
        if fee_rate > 1:
            raise SocialTradingLabError("performance fee rate cannot exceed 1")
        if fee_rate != self.performance_fee_rate:
            raise SocialTradingLabError(
                "performance fee rate does not match the ledger policy"
            )
        account = self._account(investor_id)
        if account.role == "manager":
            raise SocialTradingLabError(
                "manager capital cannot be charged a performance fee"
            )
        gross_equity = self._money(account.units * self.unit_nav)
        eligible_profit = max(
            gross_equity - self._money(account.high_water_mark), Decimal("0")
        )
        fee = self._money(eligible_profit * fee_rate)
        if fee:
            account.units -= fee / self.unit_nav
            account.performance_fees = self._money(
                account.performance_fees + fee
            )
            self.manager_fee_balance = self._money(
                self.manager_fee_balance + fee
            )
        net_equity = self._money(account.units * self.unit_nav)
        account.high_water_mark = self._money(
            max(account.high_water_mark, net_equity)
        )
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
        if account.role == "manager":
            raise SocialTradingLabError(
                "manager capital cannot be charged a management fee"
            )
        gross_equity = self._money(account.units * self.unit_nav)
        fee = self._money(
            gross_equity * fee_rate * days / Decimal("365")
        )
        if fee >= gross_equity and gross_equity:
            raise SocialTradingLabError("management fee would exhaust investor equity")
        if fee:
            account.units -= fee / self.unit_nav
            account.management_fees = self._money(
                account.management_fees + fee
            )
            self.manager_fee_balance = self._money(
                self.manager_fee_balance + fee
            )
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
        **extra: Any,
    ) -> dict[str, Any]:
        result = {
            "event_type": event_type,
            "sequence": self.sequence,
            "investor_id": investor_id,
            "amount": self._money_text(amount),
            "currency": self.currency,
            "unit_nav": _decimal_text(self.unit_nav),
            "investor_equity": self._money_text(self.equity(investor_id)),
        }
        result.update(
            {
                key: _decimal_text(value) if isinstance(value, Decimal) else value
                for key, value in extra.items()
            }
        )
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "money_quantum": _decimal_text(self.money_quantum),
            "performance_fee_rate": _decimal_text(self.performance_fee_rate),
            "crystallize_on_withdrawal": self.crystallize_on_withdrawal,
            "unit_nav": _decimal_text(self.unit_nav),
            "total_units": _decimal_text(self.total_units),
            "pool_equity": self._money_text(self.pool_equity),
            "investor_equity": self._money_text(self.investor_equity),
            "manager_capital_equity": self._money_text(
                sum(
                    (
                        account.units * self.unit_nav
                        for account in self.investors.values()
                        if account.role == "manager"
                    ),
                    Decimal("0"),
                )
            ),
            "manager_fee_balance": self._money_text(self.manager_fee_balance),
            "sequence": self.sequence,
            "investors": {
                investor_id: {
                    "role": account.role,
                    "units": _decimal_text(account.units),
                    "equity": self._money_text(account.units * self.unit_nav),
                    "high_water_mark": self._money_text(account.high_water_mark),
                    "cumulative_deposits": self._money_text(
                        account.cumulative_deposits
                    ),
                    "cumulative_withdrawals": self._money_text(
                        account.cumulative_withdrawals
                    ),
                    "cumulative_gross_withdrawals": self._money_text(
                        account.cumulative_gross_withdrawals
                    ),
                    "cumulative_net_withdrawals": self._money_text(
                        account.cumulative_net_withdrawals
                    ),
                    "performance_fees": self._money_text(account.performance_fees),
                    "management_fees": self._money_text(account.management_fees),
                }
                for investor_id, account in sorted(self.investors.items())
            },
        }


@dataclass(frozen=True)
class CopyAllocationContract:
    platform_id: str
    instrument_id: str
    quote_currency: str
    investor_currency: str
    provider_equity_currency: str
    minimum_volume: Decimal
    maximum_volume: Decimal
    volume_step: Decimal
    below_minimum_policy: str
    max_overshoot_ratio: Decimal
    contract_size: Decimal
    reference_price: Decimal
    quote_to_investor_fx_rate: Decimal
    provider_equity_to_investor_fx_rate: Decimal
    provider_leverage: Decimal
    investor_leverage: Decimal
    investor_free_margin: Decimal
    margin_buffer_ratio: Decimal
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
        if minimum % step:
            raise SocialTradingLabError(
                "minimum_volume must be aligned to volume_step"
            )
        if maximum % step:
            raise SocialTradingLabError(
                "maximum_volume must be aligned to volume_step"
            )
        instrument_id = str(value.get("instrument_id") or "").strip().upper()
        if not instrument_id:
            raise SocialTradingLabError("instrument_id is required")
        currencies = {
            field: str(value.get(field) or "").strip().upper()
            for field in (
                "quote_currency",
                "investor_currency",
                "provider_equity_currency",
            )
        }
        invalid_currencies = [
            field
            for field, currency in currencies.items()
            if not currency or len(currency) > 12
        ]
        if invalid_currencies:
            raise SocialTradingLabError(
                "Copy contract requires currency codes: "
                + ", ".join(invalid_currencies)
            )
        max_overshoot_ratio = _positive(
            value.get("max_overshoot_ratio"),
            "max_overshoot_ratio",
            allow_zero=True,
        )
        margin_buffer_ratio = _positive(
            value.get("margin_buffer_ratio"),
            "margin_buffer_ratio",
            allow_zero=True,
        )
        if margin_buffer_ratio >= 1:
            raise SocialTradingLabError("margin_buffer_ratio must be below 1")
        return cls(
            platform_id=str(value.get("platform_id") or "local"),
            instrument_id=instrument_id,
            quote_currency=currencies["quote_currency"],
            investor_currency=currencies["investor_currency"],
            provider_equity_currency=currencies["provider_equity_currency"],
            minimum_volume=minimum,
            maximum_volume=maximum,
            volume_step=step,
            below_minimum_policy=policy,
            max_overshoot_ratio=max_overshoot_ratio,
            contract_size=_positive(value.get("contract_size"), "contract_size"),
            reference_price=_positive(
                value.get("reference_price"), "reference_price"
            ),
            quote_to_investor_fx_rate=_positive(
                value.get("quote_to_investor_fx_rate"),
                "quote_to_investor_fx_rate",
            ),
            provider_equity_to_investor_fx_rate=_positive(
                value.get("provider_equity_to_investor_fx_rate"),
                "provider_equity_to_investor_fx_rate",
            ),
            provider_leverage=_positive(
                value.get("provider_leverage"), "provider_leverage"
            ),
            investor_leverage=_positive(
                value.get("investor_leverage"), "investor_leverage"
            ),
            investor_free_margin=_positive(
                value.get("investor_free_margin"), "investor_free_margin"
            ),
            margin_buffer_ratio=margin_buffer_ratio,
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
        normalized_source_equity = (
            source_equity * self.provider_equity_to_investor_fx_rate
        )
        raw = source_volume * target_equity / normalized_source_equity
        if require_protected_entry and not (
            self.native_sltp_replication or self.local_protection_overlay
        ):
            return self._rejected(
                "protected_entry_unavailable",
                raw_volume=raw,
            )
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
        if allocated > raw and tracking_error > self.max_overshoot_ratio:
            return self._rejected(
                "minimum_volume_overshoot",
                raw_volume=raw,
                volume_tracking_error=tracking_error,
            )
        notional_quote = allocated * self.contract_size * self.reference_price
        notional_investor = notional_quote * self.quote_to_investor_fx_rate
        required_margin = notional_investor / self.investor_leverage
        available_margin = self.investor_free_margin * (
            Decimal("1") - self.margin_buffer_ratio
        )
        if required_margin > available_margin:
            return self._rejected(
                "insufficient_margin_headroom",
                raw_volume=raw,
                volume_tracking_error=tracking_error,
                required_margin=required_margin,
                available_margin=available_margin,
            )
        return {
            "status": "allocated",
            "platform_id": self.platform_id,
            "instrument_id": self.instrument_id,
            "quote_currency": self.quote_currency,
            "investor_currency": self.investor_currency,
            "provider_equity_currency": self.provider_equity_currency,
            "raw_volume": _decimal_text(raw),
            "allocated_volume": _decimal_text(allocated),
            "volume_tracking_error": _decimal_text(tracking_error),
            "contract_size": _decimal_text(self.contract_size),
            "reference_price": _decimal_text(self.reference_price),
            "quote_to_investor_fx_rate": _decimal_text(
                self.quote_to_investor_fx_rate
            ),
            "provider_equity_to_investor_fx_rate": _decimal_text(
                self.provider_equity_to_investor_fx_rate
            ),
            "provider_leverage": _decimal_text(self.provider_leverage),
            "investor_leverage": _decimal_text(self.investor_leverage),
            "notional_investor_currency": _decimal_text(notional_investor),
            "required_margin": _decimal_text(required_margin),
            "available_margin_after_buffer": _decimal_text(available_margin),
            "native_sltp_replication": self.native_sltp_replication,
            "local_protection_overlay": self.local_protection_overlay,
        }

    def _rejected(
        self,
        reason: str,
        *,
        raw_volume: Decimal,
        volume_tracking_error: Optional[Decimal] = None,
        required_margin: Optional[Decimal] = None,
        available_margin: Optional[Decimal] = None,
    ) -> dict[str, Any]:
        return {
            "status": "rejected",
            "platform_id": self.platform_id,
            "instrument_id": self.instrument_id,
            "quote_currency": self.quote_currency,
            "investor_currency": self.investor_currency,
            "provider_equity_currency": self.provider_equity_currency,
            "reason": reason,
            "raw_volume": _decimal_text(raw_volume),
            "allocated_volume": "0",
            "volume_tracking_error": None
            if volume_tracking_error is None
            else _decimal_text(volume_tracking_error),
            "required_margin": None
            if required_margin is None
            else _decimal_text(required_margin),
            "available_margin_after_buffer": None
            if available_margin is None
            else _decimal_text(available_margin),
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
    currency: str
    money_quantum: str
    performance_fee_rate: str
    crystallize_on_withdrawal: bool

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
        idempotency_keys = [
            str(event.get("idempotency_key") or "").strip() for event in events
        ]
        if any(not key for key in idempotency_keys):
            raise SocialTradingLabError(
                "Every scenario event requires an idempotency_key"
            )
        if len(idempotency_keys) != len(set(idempotency_keys)):
            raise SocialTradingLabError(
                "Scenario event idempotency_key values must be unique"
            )
        accounting = payload.get("accounting") or {}
        required_accounting = (
            "currency",
            "money_quantum",
            "performance_fee_rate",
            "crystallize_on_withdrawal",
        )
        missing_accounting = [
            key for key in required_accounting if key not in accounting
        ]
        if missing_accounting:
            raise SocialTradingLabError(
                "Scenario accounting is missing: "
                + ", ".join(missing_accounting)
            )
        # Validate the policy at load time, before any event can be persisted.
        UnitizedPammLedger(
            currency=accounting["currency"],
            money_quantum=accounting["money_quantum"],
            performance_fee_rate=accounting["performance_fee_rate"],
            crystallize_on_withdrawal=accounting["crystallize_on_withdrawal"],
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
            currency=str(accounting["currency"]),
            money_quantum=str(accounting["money_quantum"]),
            performance_fee_rate=str(accounting["performance_fee_rate"]),
            crystallize_on_withdrawal=bool(
                accounting["crystallize_on_withdrawal"]
            ),
        )

    def fingerprint(self) -> str:
        return _sha256(
            {
                "scenario_id": self.scenario_id,
                "registry_path": str(self.registry_path),
                "events": self.events,
                "orders_enabled": self.orders_enabled,
                "accounting": {
                    "currency": self.currency,
                    "money_quantum": self.money_quantum,
                    "performance_fee_rate": self.performance_fee_rate,
                    "crystallize_on_withdrawal": (
                        self.crystallize_on_withdrawal
                    ),
                },
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
                final_snapshot_json TEXT NOT NULL,
                final_event_sha256 TEXT
            );
            CREATE TABLE IF NOT EXISTS social_lab_events (
                run_id TEXT NOT NULL,
                event_index INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                investor_id TEXT,
                status TEXT NOT NULL,
                idempotency_key TEXT,
                input_sha256 TEXT,
                previous_event_sha256 TEXT,
                event_sha256 TEXT,
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
        self._ensure_column(
            "social_lab_runs", "final_event_sha256", "TEXT"
        )
        self._ensure_column(
            "social_lab_events", "idempotency_key", "TEXT"
        )
        self._ensure_column(
            "social_lab_events", "input_sha256", "TEXT"
        )
        self._ensure_column(
            "social_lab_events", "previous_event_sha256", "TEXT"
        )
        self._ensure_column(
            "social_lab_events", "event_sha256", "TEXT"
        )
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            social_lab_events_run_idempotency_uq
            ON social_lab_events(run_id,idempotency_key)
            """
        )
        self.connection.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        if column not in columns:
            self.connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

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
        final_event_sha256: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO social_lab_runs
                (run_id,schema_version,engine_version,scenario_id,
                 scenario_sha256,registry_sha256,started_at,ended_at,status,
                 orders_submitted,final_snapshot_json,final_event_sha256)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
                    final_event_sha256,
                ),
            )
            self.connection.executemany(
                """
                INSERT INTO social_lab_events
                (run_id,event_index,event_type,investor_id,status,
                 idempotency_key,input_sha256,previous_event_sha256,
                 event_sha256,event_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        run_id,
                        int(event["event_index"]),
                        str(event.get("event_type") or event.get("type")),
                        event.get("investor_id"),
                        str(event.get("status") or "complete"),
                        str(event["idempotency_key"]),
                        str(event["input_sha256"]),
                        str(event["previous_event_sha256"]),
                        str(event["event_sha256"]),
                        _canonical_json(event),
                    )
                    for event in events
                ],
            )

    def verify_event_chain(self, run_id: str) -> bool:
        rows = self.connection.execute(
            """
            SELECT event_index,event_sha256,event_json
            FROM social_lab_events
            WHERE run_id=?
            ORDER BY event_index
            """,
            (run_id,),
        ).fetchall()
        if not rows:
            return False
        previous = "0" * 64
        for expected_index, row in enumerate(rows):
            if int(row["event_index"]) != expected_index:
                return False
            event = json.loads(row["event_json"])
            stored_hash = str(event.pop("event_sha256", ""))
            if stored_hash != str(row["event_sha256"] or ""):
                return False
            if event.get("previous_event_sha256") != previous:
                return False
            if _sha256(event) != stored_hash:
                return False
            previous = stored_hash
        final = self.connection.execute(
            "SELECT final_event_sha256 FROM social_lab_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return bool(final and final["final_event_sha256"] == previous)

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
                "final_event_sha256": latest["final_event_sha256"],
                "event_chain_valid": self.verify_event_chain(latest["run_id"]),
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
    ledger = UnitizedPammLedger(
        currency=scenario.currency,
        money_quantum=scenario.money_quantum,
        performance_fee_rate=scenario.performance_fee_rate,
        crystallize_on_withdrawal=scenario.crystallize_on_withdrawal,
    )
    results: list[dict[str, Any]] = []
    previous_event_sha256 = "0" * 64
    for event_index, raw_event in enumerate(scenario.events):
        state_before = ledger.snapshot()
        event_type = str(raw_event["type"])
        investor_id = str(raw_event.get("investor_id") or "")
        if event_type in {"subscribe", "deposit"}:
            result = ledger.deposit(
                investor_id,
                raw_event["amount"],
                role=str(raw_event.get("role") or "investor"),
            )
            result["event_type"] = event_type
        elif event_type == "withdraw":
            result = ledger.withdraw(investor_id, raw_event["amount"])
        elif event_type == "strategy_return":
            result = ledger.apply_strategy_return(raw_event["rate"])
        elif event_type == "performance_fee":
            result = ledger.crystallize_performance_fee(
                investor_id, raw_event.get("rate")
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
            if (
                str(allocation_payload.get("investor_currency") or "").upper()
                != scenario.currency.upper()
            ):
                raise SocialTradingLabError(
                    "copy allocation investor_currency must match scenario currency"
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
        event = {
            **result,
            "event_index": event_index,
            "idempotency_key": str(raw_event["idempotency_key"]),
            "input_sha256": _sha256(raw_event),
            "previous_event_sha256": previous_event_sha256,
            "state_before": state_before,
            "state_after": ledger.snapshot(),
        }
        event_sha256 = _sha256(event)
        event["event_sha256"] = event_sha256
        previous_event_sha256 = event_sha256
        results.append(event)
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
        final_event_sha256=previous_event_sha256,
    )
    return {
        "schema": OLAP_SCHEMA,
        "run_id": run_id,
        "scenario_id": scenario.scenario_id,
        "status": "complete",
        "orders_submitted": 0,
        "events": results,
        "final_snapshot": snapshot,
        "final_event_sha256": previous_event_sha256,
        "event_chain_valid": store.verify_event_chain(run_id),
        "registry_sha256": registry.fingerprint(),
        "scenario_sha256": scenario.fingerprint(),
    }
