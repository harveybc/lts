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
