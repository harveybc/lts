"""The Alpaca SPY 1d runner consuming a modular policy, end to end, shadow only.

Selection refusals run without TensorFlow.  The end-to-end section needs the
predictor engine (``LTS_MODULAR_ENGINE``) and drives the real runner
``__init__`` and ``tick()`` against a broker double that serves bars and
refuses everything else.
"""
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.live_model_selection import LiveModelSelectionError
from tests.unit.test_modular_inference_adapter import ENGINE, _contract, _export, needs_engine
from tools.modular_runner_shadow_e2e import (
    ReplayBrokerClient, build_runner, decisions, shadow_config,
)


def _bars(count=80):
    end = datetime.now(timezone.utc).date() - timedelta(days=3)
    days, day = [], end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    bars, price = [], 100.0
    for i, d in enumerate(reversed(days)):
        price *= math.exp(0.004 * math.sin(i / 2.7))
        bars.append({"time": datetime(d.year, d.month, d.day, 4, tzinfo=timezone.utc).isoformat(),
                     "open": price * 0.999, "high": price * 1.005, "low": price * 0.994,
                     "close": price, "volume": 1000 + 53 * (i % 9)})
    return bars


def _write(tmp_path, data):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(data))
    return path


# ------------------------------------------------------------ selection gate (no TensorFlow)


@pytest.mark.parametrize("tier", ["demo_research_canary", "promoted_paper", "live", ""])
def test_modular_family_refuses_every_tier_but_shadow_before_tick(tmp_path, tier):
    config = shadow_config(_write(tmp_path, _contract()), tmp_path / "w", tier=tier)
    with pytest.raises(LiveModelSelectionError, match="shadow_inference_only"):
        build_runner(config, _bars())
    assert ReplayBrokerClient.calls == []


def test_contract_claiming_execution_authority_is_refused_at_selection(tmp_path):
    contract = _write(tmp_path, _contract(**{"evidence.execution_authorized": True}))
    with pytest.raises(LiveModelSelectionError, match="execution authority"):
        build_runner(shadow_config(contract, tmp_path / "w"), _bars())
    assert ReplayBrokerClient.calls == []


def test_unknown_family_is_refused(tmp_path):
    from app.alpaca_model_runner import AlpacaModelRunnerError

    config = shadow_config(_write(tmp_path, _contract()), tmp_path / "w", family="sac")
    with pytest.raises(AlpacaModelRunnerError, match="unknown model family"):
        build_runner(config, _bars())


def test_linear_policy_never_takes_the_shadow_branch():
    from prediction_provider_mechanics import LiveLinearPolicy

    assert getattr(LiveLinearPolicy, "shadow_only", False) is False


# ------------------------------------------------------------ end to end (TensorFlow)


@needs_engine
def test_runner_decides_from_bars_to_action_in_shadow_and_touches_no_broker_state(tmp_path, monkeypatch):
    from app.modular_inference_adapter import ModularPolicy, build_observation

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    (tmp_path / "m").mkdir()
    contract_path, _, _ = _export(tmp_path / "m")
    bars = _bars()
    config = shadow_config(contract_path, tmp_path / "w")
    runner = build_runner(config, bars)
    try:
        first = runner.tick(allow_execution=True)   # execution requested: must be ignored
        second = runner.tick(allow_execution=False)
        runner.write_heartbeat(first)
    finally:
        runner.close()
    assert first["state"] == second["state"] == "shadow_inference_only"
    assert first["orders_submitted"] == 0 and first["execution_authorized"] is False
    assert ReplayBrokerClient.calls == ["stock_bars", "stock_bars"]
    direct = ModularPolicy.load(contract_path)
    expected = direct.predict(build_observation(
        direct.contract, [dict(bar, complete=True) for bar in bars]))
    assert first["inference"]["output_sha256"] == expected["output_sha256"]
    assert first["inference"]["action"] == expected["action"]
    rows = decisions(tmp_path / "w" / "shadow-ledger.sqlite")
    assert len(rows) == 1  # one fact per due bar, idempotent across ticks
    assert rows[0]["outcome"] == "shadow_inference_only"
    assert rows[0]["bar_close"] == bars[-1]["time"]
    assert rows[0]["input_sha256"] == expected["input_sha256"]
    heartbeat = json.loads((tmp_path / "w" / "heartbeat.json").read_text())
    assert heartbeat["read_only"] is True and heartbeat["model_family"] == "modular"
    assert heartbeat["execution_authorized"] is False
    with sqlite3.connect(tmp_path / "w" / "shadow-ledger.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM live_model_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM l1_effects").fetchone()[0] == 0


@needs_engine
def test_runner_shadow_branch_is_causal_and_follows_new_bars(tmp_path, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    (tmp_path / "m").mkdir()
    contract_path, _, _ = _export(tmp_path / "m")
    bars = _bars(90)
    runner = build_runner(shadow_config(contract_path, tmp_path / "w"), bars[:80])
    try:
        a = runner.tick()
        b = runner.tick()
        from tools.modular_runner_shadow_e2e import to_wire
        ReplayBrokerClient.bars = to_wire(bars)
        c = runner.tick()
    finally:
        runner.close()
    assert a["inference"]["input_sha256"] == b["inference"]["input_sha256"]
    assert c["inference"]["last_closed_bar"] == bars[-1]["time"] != a["inference"]["last_closed_bar"]
    assert len(decisions(tmp_path / "w" / "shadow-ledger.sqlite")) == 2
    assert ENGINE
