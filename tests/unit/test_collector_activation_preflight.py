"""The activation preflight is a judge, not an actor: every failed
precondition is named, an open position always yields
COORDINATED_WINDOW_REQUIRED, and GO only ever covers the read-only
collector."""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

from tools.collector_activation_preflight import evaluate

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
FP = "a" * 16


def snapshot(**kw):
    base = {
        "schema": "lts.mt5_snapshot.v1",
        "account_fingerprint": FP,
        "observed_at": NOW.isoformat(),
        "currency": "USD",
        "balance": 1000.0, "equity": 1000.0,
        "margin": 0.0, "free_margin": 1000.0,
        "positions": [], "orders": [], "symbols": [], "bars": [],
    }
    base.update(kw)
    return base


def kit():
    return {
        "backup_manifest": {"artifacts": [
            {"name": "ea", "sha256": "b" * 64},
            {"name": "bridge_config", "sha256": "c" * 64}]},
        "ea_diff_review": {
            "differs_only_by": "session_evidence_publication",
            "reviewed_by": "auditor"},
        "rollback_evidence": {"tested": True, "order_effects": 0},
    }


def run(snap=None, **overrides):
    base = dict(kit())
    base.update(overrides)
    return evaluate(snap or snapshot(),
                    expected_account_fingerprint=FP, now=NOW,
                    **base)


class TestVerdicts:

    def test_flat_with_full_kit_is_go_collector_only(self):
        result = run()
        assert result["verdict"] == "GO_READ_ONLY_COLLECTOR_ONLY"
        assert result["failures"] == []
        assert "weekly-flat trading logic stays blocked" in \
            result["scope"]

    def test_an_open_position_is_coordinated_window(self):
        snap = snapshot(positions=[{
            "ticket": "t1", "symbol": "ETHUSD", "side": "sell",
            "volume": 1.0, "price_open": 100.0,
            "time_open_unix": 1_700_000_000,
            "stop_loss": 120.0, "take_profit": 80.0,
            "profit": 0.0}])
        result = run(snap)
        assert result["verdict"] == "COORDINATED_WINDOW_REQUIRED"
        assert any("never restart or replace" in f
                   for f in result["failures"])

    def test_a_pending_order_blocks(self):
        snap = snapshot(orders=[{
            "ticket": "o1", "symbol": "ETHUSD",
            "order_type": "buy_limit", "volume": 1.0,
            "price_open": 90.0, "stop_loss": 0.0,
            "take_profit": 0.0, "state": "placed"}])
        result = run(snap)
        assert result["verdict"] == "COORDINATED_WINDOW_REQUIRED"

    def test_unprotected_position_is_named(self):
        snap = snapshot(positions=[{
            "ticket": "t1", "symbol": "ETHUSD", "side": "sell",
            "volume": 1.0, "price_open": 100.0,
            "time_open_unix": 1_700_000_000,
            "stop_loss": 0.0, "take_profit": 0.0,
            "profit": 0.0}])
        result = run(snap)
        assert any("native protection" in f
                   for f in result["failures"])

    def test_stale_snapshot_blocks(self):
        snap = snapshot(observed_at=(NOW - timedelta(
            minutes=30)).isoformat())
        result = run(snap)
        assert any("not fresh" in f for f in result["failures"])

    def test_foreign_account_blocks(self):
        result = evaluate(snapshot(),
                          expected_account_fingerprint="z" * 16,
                          now=NOW, **kit())
        assert any("foreign account" in f
                   for f in result["failures"])

    def test_missing_backup_review_or_rollback_block(self):
        assert any("backup manifest" in f for f in
                   run(backup_manifest=None)["failures"])
        assert any("differs only by" in f for f in
                   run(ea_diff_review={"differs_only_by": "other",
                                       "reviewed_by": "x"})
                   ["failures"])
        assert any("rollback" in f for f in
                   run(rollback_evidence={"tested": False,
                                          "order_effects": 0})
                   ["failures"])

    def test_the_judge_itself_cannot_act(self):
        import tools.collector_activation_preflight as mod
        source = inspect.getsource(mod)
        for forbidden in ("OrderSend", "requests.", "urllib",
                          "socket", "subprocess", "connect("):
            assert forbidden not in source.replace(
                'forbidden in ("OrderSend", "PositionClose", '
                '"TradeReq",\n                      "requests.", '
                '"urllib", "socket",\n                      '
                '"subprocess", "connect("):', ""), forbidden
