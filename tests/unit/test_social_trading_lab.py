import json
import sqlite3
from decimal import Decimal
from pathlib import Path

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


def _copy_contract_payload(**overrides):
    payload = {
        "platform_id": "mql5",
        "instrument_id": "EURUSD",
        "quote_currency": "USD",
        "investor_currency": "USD",
        "provider_equity_currency": "USD",
        "minimum_volume": "0.01",
        "maximum_volume": "10",
        "volume_step": "0.01",
        "below_minimum_policy": "round_up_minimum",
        "max_overshoot_ratio": "0.25",
        "contract_size": "100000",
        "reference_price": "1.15",
        "quote_to_investor_fx_rate": "1",
        "provider_equity_to_investor_fx_rate": "1",
        "provider_leverage": "100",
        "investor_leverage": "100",
        "investor_free_margin": "25000",
        "margin_buffer_ratio": "0.20",
        "native_sltp_replication": True,
        "local_protection_overlay": False,
    }
    payload.update(overrides)
    return payload


def test_unitized_flows_preserve_nav_and_adjust_high_water_mark():
    ledger = UnitizedPammLedger(performance_fee_rate="0.20")
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
    ledger = UnitizedPammLedger(performance_fee_rate="0.20")
    ledger.deposit("manager", "5000", role="manager")
    ledger.deposit("investor", "10000")
    ledger.apply_strategy_return("0.10")

    snapshot = ledger.snapshot()
    assert snapshot["manager_capital_equity"] == "5500"
    assert snapshot["investors"]["manager"]["role"] == "manager"
    with pytest.raises(SocialTradingLabError, match="manager capital"):
        ledger.crystallize_performance_fee("manager", "0.20")


def test_performance_fee_is_only_charged_above_net_high_water_mark():
    ledger = UnitizedPammLedger(performance_fee_rate="0.20")
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
        _copy_contract_payload()
    )
    result = protected.allocate(
        provider_volume="2",
        provider_equity="100000",
        investor_equity="25000",
    )
    assert result["status"] == "allocated"
    assert result["allocated_volume"] == "0.5"

    unprotected = CopyAllocationContract.from_dict(
        _copy_contract_payload(
            platform_id="native-copy",
            native_sltp_replication=False,
            local_protection_overlay=False,
        )
    )
    rejected = unprotected.allocate(
        provider_volume="2",
        provider_equity="100000",
        investor_equity="25000",
    )
    assert rejected["status"] == "rejected"
    assert rejected["reason"] == "protected_entry_unavailable"


def test_withdrawal_crystallizes_fee_and_preserves_money_conservation():
    ledger = UnitizedPammLedger(performance_fee_rate="0.20")
    ledger.deposit("investor", "100")
    ledger.apply_strategy_return("0.50")

    result = ledger.withdraw("investor", "150")

    assert result["gross_redemption"] == "150"
    assert result["amount"] == "140"
    assert result["performance_fee"] == "10"
    assert result["withdrawn_eligible_profit"] == "50"
    assert ledger.manager_fee_balance == Decimal("10")
    assert ledger.equity("investor") == 0
    snapshot = ledger.snapshot()["investors"]["investor"]
    assert snapshot["cumulative_gross_withdrawals"] == "150"
    assert snapshot["cumulative_net_withdrawals"] == "140"
    assert (
        Decimal(result["amount"])
        + Decimal(result["performance_fee"])
        + ledger.equity("investor")
    ) == Decimal("150")


def test_money_quantization_is_explicit_and_base_ten():
    ledger = UnitizedPammLedger(
        money_quantum="0.01", performance_fee_rate="0.20"
    )
    assert ledger.deposit("investor", "100.005")["amount"] == "100"
    ledger.apply_strategy_return("0.10")
    assert ledger.crystallize_performance_fee("investor")["amount"] == "2"
    with pytest.raises(SocialTradingLabError, match="base-10"):
        UnitizedPammLedger(money_quantum="0.05")


def test_copy_allocation_rejects_excessive_minimum_overshoot():
    contract = CopyAllocationContract.from_dict(_copy_contract_payload())
    result = contract.allocate(
        provider_volume="1",
        provider_equity="100000",
        investor_equity="100",
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "minimum_volume_overshoot"
    assert result["volume_tracking_error"] == "9"


@pytest.mark.parametrize(
    ("field", "value"),
    [("minimum_volume", "0.015"), ("maximum_volume", "10.005")],
)
def test_copy_allocation_rejects_step_misalignment(field, value):
    with pytest.raises(SocialTradingLabError, match="aligned"):
        CopyAllocationContract.from_dict(
            _copy_contract_payload(**{field: value})
        )


def test_copy_allocation_rejects_insufficient_margin_with_dimensions():
    contract = CopyAllocationContract.from_dict(
        _copy_contract_payload(investor_free_margin="100")
    )
    result = contract.allocate(
        provider_volume="2",
        provider_equity="100000",
        investor_equity="25000",
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "insufficient_margin_headroom"
    assert result["required_margin"] == "575"
    assert result["available_margin_after_buffer"] == "80"


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


def test_current_registry_excludes_mql5_demo_and_keeps_protected_ctrader_api():
    registry = SocialPlatformRegistry.load(
        Path(__file__).parents[2]
        / "examples"
        / "configs"
        / "social_trading_platform_registry_v1.json"
    )
    protected_demo_ids = {
        item.platform_id
        for item in registry.eligible(
            mode="copy",
            require_automation=True,
            require_protected_entries=True,
        )
    }
    assert "ctrader_open_api_demo" in protected_demo_ids
    assert "mql5_signals_oanda_demo" not in protected_demo_ids
    assert registry.get(
        "mql5_signals_oanda_demo"
    ).provider_requires_live_capital


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
                "schema": "lts.social_trading_scenario.v2",
                "scenario_id": "test",
                "mode": "accounting_simulation_no_orders",
                "database_path": str(tmp_path / "olap.sqlite"),
                "registry_path": "registry.json",
                "orders": {"enabled": False},
                "accounting": {
                    "currency": "USD",
                    "money_quantum": "0.01",
                    "performance_fee_rate": "0.20",
                    "crystallize_on_withdrawal": True,
                },
                "events": [
                    {
                        "idempotency_key": "subscribe-a",
                        "type": "subscribe",
                        "investor_id": "a",
                        "amount": "1000",
                    },
                    {
                        "idempotency_key": "return-1",
                        "type": "strategy_return",
                        "rate": "0.10",
                    },
                    {
                        "idempotency_key": "fee-a",
                        "type": "performance_fee",
                        "investor_id": "a",
                        "rate": "0.20",
                    },
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
        assert report["latest_run"]["event_chain_valid"] is True
        assert result["event_chain_valid"] is True
        assert result["events"][0]["state_before"]["pool_equity"] == "0"
        assert result["events"][0]["state_after"]["pool_equity"] == "1000"
        repeated = run_scenario(
            scenario, SocialPlatformRegistry.load(registry_path), store
        )
        assert repeated["final_event_sha256"] == result["final_event_sha256"]
        store.connection.execute(
            "UPDATE social_lab_events SET event_json='{}' "
            "WHERE run_id=? AND event_index=1",
            (result["run_id"],),
        )
        store.connection.commit()
        assert store.verify_event_chain(result["run_id"]) is False
    finally:
        store.close()


def test_scenario_rejects_order_enablement(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "schema": "lts.social_trading_scenario.v2",
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


def test_scenario_rejects_duplicate_idempotency_keys(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text(
        json.dumps(
            {
                "schema": "lts.social_trading_scenario.v2",
                "mode": "accounting_simulation_no_orders",
                "registry_path": "registry.json",
                "orders": {"enabled": False},
                "accounting": {
                    "currency": "USD",
                    "money_quantum": "0.01",
                    "performance_fee_rate": "0.20",
                    "crystallize_on_withdrawal": True,
                },
                "events": [
                    {
                        "idempotency_key": "same",
                        "type": "strategy_return",
                        "rate": "0.1",
                    },
                    {
                        "idempotency_key": "same",
                        "type": "strategy_return",
                        "rate": "0.2",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SocialTradingLabError, match="must be unique"):
        SocialTradingScenario.load(path)


def test_olap_migrates_v1_tables_without_losing_rows(tmp_path):
    database = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE social_lab_runs (
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
        CREATE TABLE social_lab_events (
            run_id TEXT NOT NULL,
            event_index INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            investor_id TEXT,
            status TEXT NOT NULL,
            event_json TEXT NOT NULL,
            PRIMARY KEY (run_id,event_index)
        );
        INSERT INTO social_lab_runs VALUES
          ('legacy','v1','v1','old','s','r','a','b','complete',0,'{}');
        INSERT INTO social_lab_events VALUES
          ('legacy',0,'deposit','a','complete','{}');
        """
    )
    connection.commit()
    connection.close()

    store = SocialTradingOlap(database)
    try:
        assert store.connection.execute(
            "SELECT COUNT(*) FROM social_lab_runs"
        ).fetchone()[0] == 1
        columns = {
            row["name"]
            for row in store.connection.execute(
                "PRAGMA table_info(social_lab_events)"
            ).fetchall()
        }
        assert {
            "idempotency_key",
            "input_sha256",
            "previous_event_sha256",
            "event_sha256",
        } <= columns
        assert store.verify_event_chain("legacy") is False
    finally:
        store.close()
