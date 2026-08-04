import sqlite3

import pytest

from app.alpaca_model_runner import AlpacaModelRunnerError, ModelSessionStore


def test_config_change_creates_a_distinct_model_session_with_new_balance():
    connection = sqlite3.connect(":memory:")
    store = ModelSessionStore(connection)
    first = store.activate(
        venue="alpaca_paper", account="account", symbol="SPY",
        model_id="model", artifact_sha256="a" * 64,
        config_sha256="b" * 64, balance=100_000.0, equity=100_000.0,
    )
    store.end(first["session_id"], balance=99_950.0, equity=99_950.0)
    second = store.activate(
        venue="alpaca_paper", account="account", symbol="SPY",
        model_id="model", artifact_sha256="a" * 64,
        config_sha256="c" * 64, balance=99_950.0, equity=99_950.0,
    )
    assert second["session_id"] != first["session_id"]
    assert second["starting_balance"] == 99_950.0
    assert second["starting_equity"] == 99_950.0


def test_old_model_must_be_drained_before_a_different_session_activates():
    connection = sqlite3.connect(":memory:")
    store = ModelSessionStore(connection)
    store.activate(
        venue="alpaca_paper", account="account", symbol="SPY",
        model_id="model-a", artifact_sha256="a" * 64,
        config_sha256="b" * 64, balance=100_000.0, equity=100_000.0,
    )
    with pytest.raises(AlpacaModelRunnerError, match="must be drained"):
        store.activate(
            venue="alpaca_paper", account="account", symbol="SPY",
            model_id="model-b", artifact_sha256="c" * 64,
            config_sha256="d" * 64, balance=100_000.0, equity=100_000.0,
        )


def test_l0_retry_gate_is_exactly_once(tmp_path):
    """Regression for the 2026-08-04 duplicate-lifecycle defect: one bar's
    signal produced four bracket lifecycles because every reconciliation
    minted a fresh idempotency identity. The gate must (a) never retry a
    satisfied bar, (b) mint one deterministic identity for a genuinely
    blocked bar, and (c) collapse repeated repairs into that identity."""
    from app.alpaca_model_runner import l0_retry_suffix
    from app.ibkr_l1_journal import L1ExecutionOlap

    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    bar = "2026-08-03T04:00:00+00:00"
    model = "spy-daily-linear-live-v1"
    repairs = [{"reservation_id": "rsv-1"}]

    # No reconciliations: never a retry identity.
    assert l0_retry_suffix(store, model, bar, []) == ""

    # Blocked bar with no effect ever: exactly one deterministic retry.
    first = l0_retry_suffix(store, model, bar, repairs)
    second = l0_retry_suffix(store, model, bar,
                             [{"reservation_id": "rsv-2"}])
    assert first == second == ":l0-retry-1"       # repairs cannot mint more

    # Once ANY effect exists for the bar (here: the retry's own effect),
    # the signal is satisfied — no further retry identity, ever.
    store.create_effect(
        "alpaca-deadbeefdeadbeef",
        f"{model}:{bar}:l0-retry-1:2026-08-03T20:00:00+00:00",
        "alpaca_bracket_entry", [])
    assert l0_retry_suffix(store, model, bar, repairs) == ""

    # A different bar is independent.
    assert l0_retry_suffix(
        store, model, "2026-08-04T04:00:00+00:00", repairs
    ) == ":l0-retry-1"
    store.close()


def test_effect_prefix_query_escapes_like_wildcards(tmp_path):
    from app.ibkr_l1_journal import L1ExecutionOlap

    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    store.create_effect("e-1", "model%wild:2026-08-03", "kind", [])
    assert store.effect_exists_with_key_prefix("model%wild")
    assert not store.effect_exists_with_key_prefix("model_wild")
    assert not store.effect_exists_with_key_prefix("modelXwild")
    store.close()


def test_consumer_supersedes_satisfied_retry_decisions(tmp_path):
    """Defense in depth: a queued retry-class decision whose bar already
    has an effect is superseded at the ledger and never submitted (the
    defect-era queue must drain without a fifth duplicate)."""
    from app.ibkr_l1_journal import L1ExecutionOlap

    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    bar = "2026-08-03T04:00:00+00:00"
    model = "spy-daily-linear-live-v1"
    stale_key = f"{model}:{bar}:l0-reconciled-65674b982b73:2026-08-03T20:00:00+00:00"
    store._con.execute(
        "INSERT INTO decisions (idempotency_key, decided_at, outcome)"
        " VALUES (?, ?, 'would_be_order')",
        (stale_key, "2026-08-04T17:49:08+00:00"),
    )
    store.create_effect(
        "alpaca-90c86e116676d016",
        f"{model}:{bar}:l0-reconciled-3a1b803dd058:2026-08-03T20:00:00+00:00",
        "alpaca_bracket_entry", [])

    assert store.effect_exists_with_key_prefix(f"{model}:{bar}")
    assert store.supersede_decision(stale_key, "superseded: satisfied")
    row = store._con.execute(
        "SELECT outcome, reason FROM decisions WHERE idempotency_key=?",
        (stale_key,)).fetchone()
    assert row[0] == "superseded"
    # Idempotent: a second supersede finds nothing pending.
    assert not store.supersede_decision(stale_key, "again")
    store.close()
