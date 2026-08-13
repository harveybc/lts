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


def test_deferred_bar_can_be_revised_once_after_hold_clears(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    assert store.record_due_bar_decision(_fact(
        outcome="deferred", reason="halted:hold",
        effect_or_command_id=None,
    ))
    assert store.record_due_bar_decision(_fact())

    rows = store.due_bar_decisions()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "would_be_order"
    assert rows[0]["reason"] is None
    assert rows[0]["effect_or_command_id"] == "l1e-abc"
    revision = store._con.execute(
        "SELECT prior_fact_json, replacement_fact_json "
        "FROM due_bar_decision_revisions"
    ).fetchone()
    assert '"outcome":"deferred"' in revision[0]
    assert '"outcome":"would_be_order"' in revision[1]
    store.close()


def test_legacy_hold_rejection_can_be_revised_but_final_fact_cannot(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    assert store.record_due_bar_decision(_fact(
        outcome="rejected", reason="halted:hold",
        effect_or_command_id=None,
    ))
    assert store.record_due_bar_decision(_fact())
    with pytest.raises(DemoExecutionError, match="cannot be revised"):
        store.record_due_bar_decision(_fact(
            outcome="rejected", reason="some_new_terminal_reason",
            effect_or_command_id=None,
        ))
    assert store.due_bar_decisions()[0]["outcome"] == "would_be_order"
    store.close()


def test_due_bar_revision_refuses_lineage_drift(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    assert store.record_due_bar_decision(_fact(
        outcome="deferred", reason="halted:hold",
        effect_or_command_id=None,
    ))
    with pytest.raises(DemoExecutionError, match="lineage drift"):
        store.record_due_bar_decision(_fact(artifact_sha256="d" * 64))
    assert store.due_bar_decisions()[0]["outcome"] == "deferred"
    store.close()


def test_l1_outbox_reads_effective_revision_not_legacy_rejection(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    store.record_decision(
        "idem-legacy", "rejected", "halted:hold", "{}", None,
        reference_price=1.0, quote_time="2026-08-05T08:00:00+00:00",
        capability_evidence="live_observed",
    )
    assert store.supersede_transient_rejection(
        "idem-legacy", cause="owner_resume_reconciled"
    )
    store.record_decision(
        "idem-legacy", "would_be_order", None,
        '{"object_id":"oi2-revised"}', {"adapter": "ibkr"},
        reference_price=1.0, quote_time="2026-08-05T08:00:01+00:00",
        capability_evidence="live_observed",
    )
    pending = store.l1_pending_decisions("would_be_order")
    assert [row["idempotency_key"] for row in pending] == ["idem-legacy"]
    assert store.decision_intent_json("idem-legacy") == (
        '{"object_id":"oi2-revised"}'
    )
    store.close()
