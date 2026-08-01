import json
from decimal import Decimal

import pytest

from app.social_trading_lab import (
    CopyAllocationContract,
    SocialPlatformRegistry,
    SocialTradingLabError,
    SocialTradingOlap,
    SocialTradingScenario,
    UnitizedPammLedger,
    run_scenario,
)


def test_unitized_flows_preserve_nav_and_adjust_high_water_mark():
    ledger = UnitizedPammLedger()
    ledger.deposit("a", "1000")
    ledger.apply_strategy_return("0.10")

    fee = ledger.crystallize_performance_fee("a", "0.20")
    assert Decimal(fee["amount"]) == Decimal("20")
    assert ledger.equity("a") == Decimal("1080")
    assert Decimal(
        ledger.crystallize_performance_fee("a", "0.20")["amount"]
    ) == 0

    nav_before = ledger.unit_nav
    ledger.deposit("a", "500")
    assert ledger.unit_nav == nav_before
    assert ledger.investors["a"].high_water_mark == Decimal("1580")

    ledger.withdraw("a", "158")
    assert ledger.unit_nav == nav_before
    assert ledger.investors["a"].high_water_mark == Decimal("1422")


def test_manager_capital_shares_pool_return_but_cannot_pay_itself_fee():
    ledger = UnitizedPammLedger()
    ledger.deposit("manager", "5000", role="manager")
    ledger.deposit("investor", "10000")
    ledger.apply_strategy_return("0.10")

    snapshot = ledger.snapshot()
    assert snapshot["manager_capital_equity"] == "5500"
    assert snapshot["investors"]["manager"]["role"] == "manager"
    with pytest.raises(SocialTradingLabError, match="manager capital"):
        ledger.crystallize_performance_fee("manager", "0.20")


def test_performance_fee_is_only_charged_above_net_high_water_mark():
    ledger = UnitizedPammLedger()
    ledger.deposit("a", "1000")
    ledger.apply_strategy_return("0.10")
    ledger.crystallize_performance_fee("a", "0.20")
    ledger.apply_strategy_return("-0.10")
    assert Decimal(
        ledger.crystallize_performance_fee("a", "0.20")["amount"]
    ) == 0
    ledger.apply_strategy_return("0.20")
    assert Decimal(
        ledger.crystallize_performance_fee("a", "0.20")["amount"]
    ) == Decimal("17.28")


def test_management_fee_is_prorated_and_does_not_lower_hwm():
    ledger = UnitizedPammLedger()
    ledger.deposit("a", "36500")
    hwm_before = ledger.investors["a"].high_water_mark
    result = ledger.charge_management_fee("a", "0.02", "30")
    assert Decimal(result["amount"]) == Decimal("60")
    assert ledger.equity("a") == Decimal("36440")
    assert ledger.investors["a"].high_water_mark == hwm_before


def test_copy_allocation_uses_equity_ratio_and_fails_closed_on_protection():
    protected = CopyAllocationContract.from_dict(
        {
            "platform_id": "mql5",
            "minimum_volume": "0.01",
            "maximum_volume": "10",
            "volume_step": "0.01",
            "below_minimum_policy": "round_up_minimum",
            "native_sltp_replication": True,
        }
    )
    result = protected.allocate(
        provider_volume="2",
        provider_equity="100000",
        investor_equity="25000",
    )
    assert result["status"] == "allocated"
    assert result["allocated_volume"] == "0.5"

    unprotected = CopyAllocationContract.from_dict(
        {
            "platform_id": "native-copy",
            "minimum_volume": "0.01",
            "maximum_volume": "10",
            "volume_step": "0.01",
            "native_sltp_replication": False,
            "local_protection_overlay": False,
        }
    )
    rejected = unprotected.allocate(
        provider_volume="2",
        provider_equity="100000",
        investor_equity="25000",
    )
    assert rejected["status"] == "rejected"
    assert rejected["reason"] == "protected_entry_unavailable"


def test_platform_registry_filters_live_capital_and_protection(tmp_path):
    registry_path = (
        tmp_path / "registry.json"
    )
    registry_path.write_text(
        json.dumps(
            {
                "schema": "lts.social_platform_registry.v1",
                "research_order": ["safe", "live"],
                "platforms": [
                    {
                        "platform_id": "safe",
                        "display_name": "Safe demo",
                        "disposition": "now",
                        "modes": ["copy"],
                        "account_environment": "demo",
                        "automation": "api",
                        "instrument_classes": ["fx"],
                        "protection": {
                            "native_sltp_replication": True,
                            "local_protection_overlay_possible": False,
                        },
                        "provider_requirements": {"requires_live_capital": False},
                        "evidence_urls": ["https://example.com/safe"],
                    },
                    {
                        "platform_id": "live",
                        "display_name": "Live PAMM",
                        "disposition": "later",
                        "modes": ["pamm"],
                        "account_environment": "live",
                        "automation": "platform_managed",
                        "instrument_classes": ["fx"],
                        "protection": {
                            "native_sltp_replication": False,
                            "local_protection_overlay_possible": False,
                        },
                        "provider_requirements": {"requires_live_capital": True},
                        "evidence_urls": ["https://example.com/live"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = SocialPlatformRegistry.load(registry_path)
    assert [
        item.platform_id
        for item in registry.eligible(
            mode="copy", require_automation=True, require_protected_entries=True
        )
    ] == ["safe"]
    assert registry.eligible(mode="pamm") == []
    assert registry.eligible(mode="pamm", allow_live_capital=True)[0].platform_id == "live"


def test_scenario_is_no_order_and_persists_olap(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema": "lts.social_platform_registry.v1",
                "research_order": ["safe"],
                "platforms": [
                    {
                        "platform_id": "safe",
                        "display_name": "Safe",
                        "disposition": "now",
                        "modes": ["copy"],
                        "account_environment": "demo",
                        "automation": "api",
                        "instrument_classes": ["fx"],
                        "protection": {
                            "native_sltp_replication": True,
                            "local_protection_overlay_possible": False,
                        },
                        "provider_requirements": {"requires_live_capital": False},
                        "evidence_urls": ["https://example.com/safe"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(
        json.dumps(
            {
                "schema": "lts.social_trading_scenario.v1",
                "scenario_id": "test",
                "mode": "accounting_simulation_no_orders",
                "database_path": str(tmp_path / "olap.sqlite"),
                "registry_path": "registry.json",
                "orders": {"enabled": False},
                "events": [
                    {"type": "subscribe", "investor_id": "a", "amount": "1000"},
                    {"type": "strategy_return", "rate": "0.10"},
                    {"type": "performance_fee", "investor_id": "a", "rate": "0.20"},
                ],
            }
        ),
        encoding="utf-8",
    )
    scenario = SocialTradingScenario.load(scenario_path)
    store = SocialTradingOlap(scenario.database_path)
    try:
        result = run_scenario(
            scenario, SocialPlatformRegistry.load(registry_path), store
        )
        assert result["orders_submitted"] == 0
        assert result["final_snapshot"]["investors"]["a"]["equity"] == "1080"
        report = store.report()
        assert report["latest_run"]["status"] == "complete"
        assert report["latest_run"]["orders_submitted"] == 0
    finally:
        store.close()


def test_scenario_rejects_order_enablement(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "schema": "lts.social_trading_scenario.v1",
                "mode": "accounting_simulation_no_orders",
                "registry_path": "registry.json",
                "orders": {"enabled": True},
                "events": [{"type": "strategy_return", "rate": "0"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SocialTradingLabError, match="forbidden"):
        SocialTradingScenario.load(path)
