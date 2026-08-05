"""AUD-F2-20260804-102 independent re-proof: the historical
four-round-trip defect shape (one bar, four terminal lifecycles, one
queued defect-era duplicate) plus process restart and one fresh due bar
produce ZERO new submissions for the old bar and exactly one decision
identity for the fresh bar. No order is forced."""
from __future__ import annotations

import pytest

from app.alpaca_model_runner import l0_retry_suffix
from app.ibkr_l1_journal import L1ExecutionOlap

BAR = "2026-08-03T04:00:00+00:00"
FRESH_BAR = "2026-08-05T04:00:00+00:00"
MODEL = "spy-daily-linear-live-v1"
WINDOW = "2026-08-03T20:00:00+00:00"


def _seed_defect_era(store: L1ExecutionOlap) -> str:
    """Reproduce the exact historical ledger shape of 2026-08-04."""
    suffixes = ["", ":l0-reconciled-ede7eec9", ":l0-reconciled-41582493",
                ":l0-reconciled-3e2fa4cb"]
    for index, suffix in enumerate(suffixes):
        key = f"{MODEL}:{BAR}{suffix}:{WINDOW}"
        store._con.execute(
            "INSERT INTO decisions (idempotency_key, decided_at, outcome)"
            " VALUES (?, ?, 'would_be_order')",
            (key, f"2026-08-04T0{index}:00:00+00:00"))
        effect = f"alpaca-effect-{index:02d}{'a' * 12}"[:24]
        store.create_effect(effect, key, "alpaca_bracket_entry", [])
        for state in ("submitted_pending_ack", "acknowledged",
                      "terminal_flat"):
            store.advance_effect(effect, state)
    queued = f"{MODEL}:{BAR}:l0-reconciled-65674b98:{WINDOW}"
    store._con.execute(
        "INSERT INTO decisions (idempotency_key, decided_at, outcome)"
        " VALUES (?, '2026-08-04T17:49:08+00:00', 'would_be_order')",
        (queued,))
    store._con.commit()
    return queued


def test_four_round_trip_fixture_restart_and_fresh_bar(tmp_path):
    db = tmp_path / "ledger.sqlite"
    store = L1ExecutionOlap(db)
    queued = _seed_defect_era(store)

    # 1. The retry gate mints NOTHING for the satisfied historical bar,
    #    no matter how many reconciliation repairs fire.
    for _ in range(5):
        assert l0_retry_suffix(
            store, MODEL, BAR, [{"reservation_id": "rsv-any"}]) == ""

    # 2. The queued defect-era duplicate is superseded through the
    #    ledger, never submitted.
    assert store.effect_exists_with_key_prefix(f"{MODEL}:{BAR}")
    assert store.supersede_decision(
        queued, "superseded: bar signal already satisfied")

    # 3. Process restart: reopen the database; nothing changes.
    store.close()
    store = L1ExecutionOlap(db)
    assert l0_retry_suffix(
        store, MODEL, BAR, [{"reservation_id": "rsv-any"}]) == ""
    assert not store.supersede_decision(queued, "again")
    outcome = store._con.execute(
        "SELECT outcome FROM decisions WHERE idempotency_key=?",
        (queued,)).fetchone()[0]
    assert outcome == "superseded"

    # 4. Effects for the historical bar never grew past four.
    count = store._con.execute(
        "SELECT COUNT(*) FROM l1_effects WHERE idempotency_key LIKE ?",
        (f"{MODEL}:{BAR}%",)).fetchone()[0]
    assert count == 4

    # 5. One fresh due bar is a NEW identity, unaffected by history: the
    #    gate offers its single deterministic retry only if it is ever
    #    blocked, and no effect exists for it yet.
    assert not store.effect_exists_with_key_prefix(f"{MODEL}:{FRESH_BAR}")
    assert l0_retry_suffix(
        store, MODEL, FRESH_BAR, [{"reservation_id": "rsv-x"}]
    ) == ":l0-retry-1"
    assert l0_retry_suffix(store, MODEL, FRESH_BAR, []) == ""
    store.close()
