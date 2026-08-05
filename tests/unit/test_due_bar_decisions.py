"""Order C1 core proofs: exactly one normalized decision fact per due
closed bar per venue/model/timeframe; duplicates, restarts and replays
insert nothing; HOLD facts are first-class; missing lineage refuses."""
from __future__ import annotations

import pytest

from app.demo_execution_service import DemoExecutionError
from app.ibkr_l1_journal import L1ExecutionOlap


def _fact(**overrides):
    fact = {
        "venue": "ibkr_paper", "account_fingerprint": "c0ff137a3cc1a363",
        "asset_id": "fx:USD/CAD", "instrument": "USD.CAD",
        "timeframe": "4h", "bar_close": "2026-08-05T04:00:00+00:00",
        "decided_at": "2026-08-05T08:00:05+00:00",
        "feature_cutoff": "2026-08-05T04:00:00+00:00",
        "input_sha256": "a" * 64, "config_sha256": "b" * 64,
        "model_id": "usdcad-4h-linear-live-v1",
        "artifact_sha256": "c" * 64, "action": "short",
        "score": 0.48, "outcome": "would_be_order",
        "risk_envelope": {"stop_price": 1.41, "take_profit_price": 1.40},
        "quote": {"bid": 1.4049, "ask": 1.4051},
        "decision_id": "usdcad-4h-linear-live-v1:2026-08-05T04:00:00+00:00",
        "effect_or_command_id": "l1e-abc",
    }
    fact.update(overrides)
    return fact


def test_exactly_one_fact_per_bar_across_replay_and_restart(tmp_path):
    db = tmp_path / "ledger.sqlite"
    store = L1ExecutionOlap(db)
    assert store.record_due_bar_decision(_fact()) is True
    for _replay in range(10):                     # duplicate ticks/replays
        assert store.record_due_bar_decision(_fact()) is False
    store.close()
    store = L1ExecutionOlap(db)                   # process restart
    assert store.record_due_bar_decision(_fact()) is False
    rows = store.due_bar_decisions(venue="ibkr_paper")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "would_be_order"
    assert rows[0]["effect_or_command_id"] == "l1e-abc"
    store.close()


def test_hold_is_a_first_class_fact_and_new_bars_are_new_rows(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    assert store.record_due_bar_decision(
        _fact(action="hold", outcome="hold", reason="model_hold",
              effect_or_command_id=None))
    assert store.record_due_bar_decision(
        _fact(bar_close="2026-08-05T08:00:00+00:00",
              decision_id="m:2026-08-05T08:00:00+00:00"))
    rows = store.due_bar_decisions()
    assert [r["outcome"] for r in rows] == ["hold", "would_be_order"]
    store.close()


def test_venues_and_models_are_independent(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    assert store.record_due_bar_decision(_fact())
    assert store.record_due_bar_decision(
        _fact(venue="alpaca_paper", instrument="SPY",
              model_id="spy-daily-linear-live-v1"))
    assert len(store.due_bar_decisions()) == 2
    assert len(store.due_bar_decisions(venue="alpaca_paper")) == 1
    store.close()


def test_missing_lineage_refuses(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    for field in ("bar_close", "input_sha256", "artifact_sha256",
                  "model_id", "outcome"):
        broken = _fact()
        broken[field] = None
        with pytest.raises(DemoExecutionError, match="missing"):
            store.record_due_bar_decision(broken)
    assert store.due_bar_decisions() == []
    store.close()
