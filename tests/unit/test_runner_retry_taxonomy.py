"""AUD-F2-20260804-103: only explicit transient connection/session
exceptions retry; account/config/security/programming failures are fatal
with an advancing slow-cadence heartbeat. AUD-F2-20260804-101: budget
exhaustion is a durable rejected decision, never an exception."""
from __future__ import annotations

import errno
import json
import threading

import pytest

from app.runner_retry_taxonomy import classify_runner_exception
from app.ibkr_model_runner import build_runner_with_backoff


class FakeIbTimeout(Exception):
    pass


FakeIbTimeout.__name__ = "ApiTimeout"


def test_transient_classification():
    for exc in (
        ConnectionRefusedError("refused"),
        ConnectionResetError("reset"),
        ConnectionAbortedError("aborted"),
        TimeoutError("timed out"),
        BrokenPipeError("pipe"),
        OSError(errno.EHOSTUNREACH, "unreachable"),
        FakeIbTimeout("session timeout"),
    ):
        assert classify_runner_exception(exc) == "transient", exc


def test_fatal_classification():
    from app.ibkr_l1_adapter import L1AuthorizationError, L1ExecutionError

    for exc in (
        L1ExecutionError("IBKR L1 may connect only to local TWS Paper"),
        L1AuthorizationError("capability account fingerprint mismatch"),
        KeyError("profile_file"),
        ValueError("bad schema"),
        FileNotFoundError("missing artifact"),
        RuntimeError("Alpaca account fingerprint changed"),
        ZeroDivisionError(),
    ):
        assert classify_runner_exception(exc) == "fatal", exc


def _config(tmp_path):
    return {
        "schema": "lts.ibkr.model_runner.v1",
        "loop_seconds": 0.01,
        "connect_backoff_max_seconds": 0.04,
        "fatal_retry_seconds": 0.5,
        "heartbeat_path": str(tmp_path / "heartbeat.json"),
    }


def test_fatal_construction_uses_slow_cadence_and_fatal_phase(tmp_path):
    config = _config(tmp_path)
    stopped = threading.Event()
    delays = []
    attempts = {"count": 0}

    def factory(_config):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ValueError("invalid runner config")   # fatal
        return object()

    original_wait = stopped.wait

    def capture_wait(seconds):
        delays.append(seconds)
        return original_wait(0)

    stopped.wait = capture_wait
    runner = build_runner_with_backoff(config, stopped,
                                       runner_factory=factory)
    assert runner is not None
    assert delays == [0.5]                     # fatal cadence, not backoff
    heartbeat = json.loads((tmp_path / "heartbeat.json").read_text())
    assert heartbeat["phase"] == "fatal"
    assert heartbeat["state"] == "degraded_error"


def test_transient_construction_keeps_connect_backoff(tmp_path):
    config = _config(tmp_path)
    stopped = threading.Event()
    delays = []
    attempts = {"count": 0}

    def factory(_config):
        attempts["count"] += 1
        if attempts["count"] < 4:
            raise ConnectionRefusedError("refused")
        return object()

    original_wait = stopped.wait

    def capture_wait(seconds):
        delays.append(seconds)
        return original_wait(0)

    stopped.wait = capture_wait
    build_runner_with_backoff(config, stopped, runner_factory=factory)
    assert delays == [0.01, 0.02, 0.04]
    heartbeat = json.loads((tmp_path / "heartbeat.json").read_text())
    assert heartbeat["phase"] == "connect"


def test_budget_exhaustion_is_a_durable_rejected_decision(tmp_path):
    from app.ibkr_l1_journal import L1ExecutionOlap

    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    key = "spy-daily-linear-live-v1:2026-08-05T04:00:00+00:00:w"
    store._con.execute(
        "INSERT INTO decisions (idempotency_key, decided_at, outcome)"
        " VALUES (?, '2026-08-05T20:00:00+00:00', 'would_be_order')",
        (key,))
    assert store.reject_decision(key, "order_budget_exhausted:2026-08-05")
    outcome, reason = store._con.execute(
        "SELECT outcome, reason FROM decisions WHERE idempotency_key=?",
        (key,)).fetchone()
    assert outcome == "rejected"
    assert reason.startswith("order_budget_exhausted")
    # Idempotent: a second rejection finds nothing pending.
    assert not store.reject_decision(key, "again")
    store.close()
