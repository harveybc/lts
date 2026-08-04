"""AUD-F2-20260804-091: connection refusal at runner construction enters
an advancing degraded heartbeat loop with bounded backoff and zero broker
submissions, then reconnects automatically when construction succeeds."""
from __future__ import annotations

import json
import threading

from app.ibkr_model_runner import build_runner_with_backoff


def _config(tmp_path, loop_seconds=0.01):
    return {
        "schema": "lts.ibkr.model_runner.v1",
        "loop_seconds": loop_seconds,
        "connect_backoff_max_seconds": 0.04,
        "heartbeat_path": str(tmp_path / "heartbeat.json"),
    }


def _read_heartbeat(tmp_path):
    return json.loads((tmp_path / "heartbeat.json").read_text())


def test_refusals_write_advancing_degraded_heartbeats(tmp_path):
    config = _config(tmp_path)
    stopped = threading.Event()
    observed = []
    attempts = {"count": 0}
    sentinel = object()

    def factory(_config):
        attempts["count"] += 1
        if (tmp_path / "heartbeat.json").exists():
            observed.append(_read_heartbeat(tmp_path)["observed_at"])
        if attempts["count"] < 4:
            raise ConnectionRefusedError("[Errno 111] Connection refused")
        return sentinel

    runner = build_runner_with_backoff(config, stopped,
                                       runner_factory=factory)
    assert runner is sentinel
    assert attempts["count"] == 4
    # Each retry saw a heartbeat from the previous failure: the loop
    # advances instead of freezing (the 2295-restart night's defect).
    assert len(observed) == 3
    assert len(set(observed)) == len(observed)          # strictly advancing
    final = _read_heartbeat(tmp_path)
    assert final["state"] == "degraded_error"
    assert final["phase"] == "connect"
    assert final["account_binding_verified"] is False
    assert "Connection refused" in final["error"]


def test_backoff_is_bounded_by_config_ceiling(tmp_path):
    config = _config(tmp_path, loop_seconds=0.01)
    stopped = threading.Event()
    delays = []
    attempts = {"count": 0}

    def factory(_config):
        attempts["count"] += 1
        if attempts["count"] < 6:
            raise ConnectionRefusedError("refused")
        return object()

    original_wait = stopped.wait

    def capture_wait(seconds):
        delays.append(seconds)
        return original_wait(0)

    stopped.wait = capture_wait
    build_runner_with_backoff(config, stopped, runner_factory=factory)
    assert delays == [0.01, 0.02, 0.04, 0.04, 0.04]     # capped at ceiling


def test_stop_during_backoff_returns_none(tmp_path):
    config = _config(tmp_path)
    stopped = threading.Event()

    def factory(_config):
        stopped.set()                                    # SIGTERM arrives
        raise ConnectionRefusedError("refused")

    assert build_runner_with_backoff(config, stopped,
                                     runner_factory=factory) is None
    assert _read_heartbeat(tmp_path)["state"] == "degraded_error"


def test_immediate_success_writes_no_degraded_heartbeat(tmp_path):
    config = _config(tmp_path)
    sentinel = object()
    runner = build_runner_with_backoff(
        config, threading.Event(), runner_factory=lambda _config: sentinel)
    assert runner is sentinel
    assert not (tmp_path / "heartbeat.json").exists()
