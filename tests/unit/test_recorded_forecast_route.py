"""Recorded-forecast heuristic policy on the dormant shadow routes (MT5 demo, Alpaca paper).

A verified forecast cell's recorded predictions drive the heuristic strategy's decision
rule inside the REAL runners' shadow branch:
- the naive gate selects the readable horizons, and no passing horizon means dormant;
- the state is persisted after every bar, so a crashed replay resumes identically;
- every decision goes to a JSONL observability file;
- zero orders or commands are ever produced.
"""
import csv
import hashlib
import json
import math
import socket
from datetime import datetime, timedelta, timezone

import pytest

from app.live_model_selection import LiveModelSelectionError
from app.recorded_forecast_policy import (
    CONTRACT_SCHEMA, RecordedForecastPolicy, SelectedRecordedForecastPolicy, heuristic_step,
)
from tests.unit.test_forecast_naive_gate import _horizon, seal

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*_a, **_k):
        raise AssertionError("network operation attempted")
    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


def _bars(n=120):
    out = []
    for i in range(n):
        c = 2000.0 * math.exp(0.02 * math.sin(i / 3.0))
        t = START + timedelta(hours=4 * i)
        out.append({"time": t.isoformat(), "naive": t.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": c * 0.999, "high": c * 1.01, "low": c * 0.99, "close": c})
    return out


def _contract(tmp_path, bars, passing=(1, 2, 3), *, tier="shadow_inference_only", horizons=(1, 2, 3),
              poison=None):
    pred = tmp_path / "PREDICTIONS_cell.csv"
    with open(pred, "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["row_id", "DATE_TIME"] + [f"close_hat_h{h}" for h in horizons])
        for i, b in enumerate(bars[:-3]):
            nxt = [1e9 if h == poison else bars[min(i + h, len(bars) - 1)]["close"]
                   for h in horizons]  # oracle-like fixture values; a poisoned column must never be read
            w.writerow([f"r{i}", b["naive"]] + nxt)
    record = seal({
        "schema": "predictor.forecast_naive_evidence.v1",
        "frozen_metric": {"primary": "MAE", "secondary": "MSE", "frozen": "declared before selection"},
        "artifact": {"model_sha256": "e" * 64, "candidate_cid": "c" * 64},
        "population": {"dataset_id": "eth", "asset": "ETHUSDT 4h", "targets": ["log_return_1"], "rows": 500,
                       "row_ids_sha256": "a" * 64, "first_origin": "2024", "last_origin": "2024",
                       "sample_hours": 4.0, "horizon_unit": "steps of 4.0 h"},
        "scale": {"metric_space": "z_train", "scaler_identity": "eth-train"},
        "split": {"provenance": "held_out_validation", "test_used": False, "reserved_trading_test": False},
        "naive": {"definition": "strict minimum"},
        "per_horizon": [_horizon(h, 0.5 if h in passing else 1.2, 1.0) for h in horizons]})
    (tmp_path / "EVIDENCE_cell.json").write_text(json.dumps(record))
    contract = {
        "schema": CONTRACT_SCHEMA, "model_id": "eth-cell-recorded", "asset_id": "crypto:ETHUSD", "timeframe": "4h",
        "execution_tier": tier, "execution_authorized": False,
        "predictions": {"file": "PREDICTIONS_cell.csv", "sha256": hashlib.sha256(pred.read_bytes()).hexdigest(),
                        "time_column": "DATE_TIME", "time_zone": "UTC", "price_column": "close_hat_h{h}"},
        "evidence": {"record_file": "EVIDENCE_cell.json", "declared": {
            "asset": "ETHUSDT 4h", "families": {"forecast": {
                "evidence_sha256": record["evidence_sha256"], "model_sha256": "e" * 64, "period_hours": 4.0,
                "metric_space": "z_train", "scaler_identity": "eth-train", "horizons": list(horizons)}}}},
        "heuristic": {"profit_threshold_frac": 0.005, "min_drawdown_frac": 0.0025, "tp_multiplier": 0.9,
                      "sl_multiplier": 2.0, "direction": "both"},
        "episode": {"start": bars[1]["time"]},
        "state_file": "state.json", "observability_file": "decisions.jsonl"}
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    return path


def _replay(policy, bars, upto, start=0):
    out = []
    for t in range(max(start, 1), upto):
        obs = policy.build_observation([{**b, "complete": True} for b in bars[:t + 1]])
        out.append(policy.predict(obs))
    return out


# ------------------------------------------------------------ gate, decision rule, persistence


def test_no_passing_horizon_keeps_the_route_dormant(tmp_path):
    with pytest.raises(LiveModelSelectionError, match="dormant"):
        SelectedRecordedForecastPolicy(contract_file=_contract(tmp_path, _bars(), passing=()),
                                       expected_asset_id="crypto:ETHUSD", expected_timeframe="4h",
                                       execution_tier="shadow_inference_only")


def test_only_shadow_tier_and_no_execution_claim(tmp_path):
    with pytest.raises(LiveModelSelectionError, match="shadow_inference_only"):
        SelectedRecordedForecastPolicy(contract_file=_contract(tmp_path, _bars()), expected_asset_id="crypto:ETHUSD",
                                       expected_timeframe="4h", execution_tier="demo_research_canary")


def test_failing_horizons_are_excluded_and_never_read(tmp_path):
    selector = SelectedRecordedForecastPolicy(contract_file=_contract(tmp_path, _bars(), passing=(1, 3)),
                                              expected_asset_id="crypto:ETHUSD", expected_timeframe="4h",
                                              execution_tier="shadow_inference_only")
    assert selector.policy.consumed_horizons == [1, 3]
    assert selector.identity()["reduced_input_experiment"] == {"declared": True, "excluded": [2]}


def test_policy_matches_the_reference_rule_and_writes_every_decision(tmp_path):
    bars = _bars()
    policy = SelectedRecordedForecastPolicy(contract_file=_contract(tmp_path, bars),
                                            expected_asset_id="crypto:ETHUSD", expected_timeframe="4h",
                                            execution_tier="shadow_inference_only").policy
    decisions = _replay(policy, bars, len(bars))
    state = {"position": 0, "tp": None, "sl": None}
    preds = {}
    with open(tmp_path / "PREDICTIONS_cell.csv") as handle:
        for row in csv.DictReader(handle):
            preds[row["DATE_TIME"]] = [float(row[f"close_hat_h{h}"]) for h in (1, 2, 3)]
    expected = []
    for b in bars[1:]:
        state, target, _ = heuristic_step(state, b["close"], preds.get(b["naive"], []), policy.params)
        expected.append(target)
    assert [d["target"] for d in decisions] == expected
    assert set(expected) >= {1} and all(d["execution_authorized"] is False for d in decisions)
    lines = (tmp_path / "decisions.jsonl").read_text().splitlines()
    assert len(lines) == len(bars) - 1 and json.loads(lines[0])["kind"] == "bar"


def test_a_crashed_replay_resumes_from_persisted_state_identically(tmp_path):
    bars = _bars()
    contract = _contract(tmp_path, bars)
    kwargs = dict(expected_asset_id="crypto:ETHUSD", expected_timeframe="4h", execution_tier="shadow_inference_only")
    full = [d["target"] for d in _replay(SelectedRecordedForecastPolicy(contract_file=contract, **kwargs).policy,
                                         bars, len(bars))]
    (tmp_path / "state.json").unlink()
    (tmp_path / "decisions.jsonl").unlink()
    first = SelectedRecordedForecastPolicy(contract_file=contract, **kwargs).policy
    part1 = [d["target"] for d in _replay(first, bars, 50)]
    del first  # crash: the process is gone, only the persisted state remains
    resumed = SelectedRecordedForecastPolicy(contract_file=contract, **kwargs).policy
    assert resumed.state["processed"] == 49
    part2 = [d["target"] for d in _replay(resumed, bars, len(bars), start=50)]
    assert part1 + part2 == full
    assert len((tmp_path / "decisions.jsonl").read_text().splitlines()) == len(bars) - 1


def test_a_skipped_tick_catches_up_bar_by_bar(tmp_path):
    bars = _bars()
    kwargs = dict(expected_asset_id="crypto:ETHUSD", expected_timeframe="4h", execution_tier="shadow_inference_only")
    contract = _contract(tmp_path, bars)
    full = _replay(SelectedRecordedForecastPolicy(contract_file=contract, **kwargs).policy, bars, len(bars))
    (tmp_path / "state.json").unlink()
    policy = SelectedRecordedForecastPolicy(contract_file=contract, **kwargs).policy
    jump = policy.predict(policy.build_observation([{**b, "complete": True} for b in bars]))  # one late tick
    assert jump["target"] == full[-1]["target"] and policy.state["processed"] == len(bars) - 1


# ------------------------------------------------------------ the real MT5 runner, offline


def _mt5(tmp_path, bars, contract):
    from app.mt5_bridge_lab import SnapshotPayload
    from app.mt5_execution_bridge import Mt5ExecutionStore
    from tests.unit.test_mt5_modular_shadow import ACCOUNT, _config

    (tmp_path / "bridge.json").write_text(json.dumps({
        "schema": "lts.mt5.execution_bridge_config.v2", "environment": "demo", "execution_enabled": True,
        "database_path": str(tmp_path / "mt5.sqlite"), "secret_env": "SECRET", "bind_host": "127.0.0.1",
        "port": 8766, "account_fingerprint": ACCOUNT, "allowed_symbols": ["ETHUSD"], "max_volume": 0.01,
        "max_open_commands_per_day": 4}))
    config = _config(tmp_path, contract)
    config["model"]["family"] = "recorded_forecast"

    def snapshot(store, upto):
        now = datetime.now(timezone.utc)
        store.record_snapshot(SnapshotPayload.model_validate({
            "schema": "lts.mt5.snapshot.v1", "account_fingerprint": ACCOUNT, "observed_at": now, "currency": "USD",
            "balance": 10000, "equity": 10000, "margin": 0, "free_margin": 10000, "positions": [], "orders": [],
            "bars": [{"symbol": "ETHUSD", "timeframe": "4h", "time": b["time"], "open": b["open"], "high": b["high"],
                      "low": b["low"], "close": b["close"], "volume": 1.0} for b in bars[max(0, upto - 59):upto + 1]],
            "symbols": [{"symbol": "ETHUSD", "bid": 1.0, "ask": 1.0, "point": 0.01, "volume_min": 0.01,
                         "volume_max": 65, "volume_step": 0.01, "trade_mode": 4, "observed_at": now}]}))
    return config, snapshot, Mt5ExecutionStore(tmp_path / "mt5.sqlite")


def test_mt5_route_replays_recorded_bars_in_shadow_and_queues_nothing(tmp_path):
    from app.mt5_model_runner import Mt5ModelRunner

    bars = _bars(80)
    config, snapshot, store = _mt5(tmp_path, bars, _contract(tmp_path, bars))
    runner = Mt5ModelRunner(config)
    try:
        states = []
        for t in range(1, len(bars)):
            snapshot(runner.bridge_store, t)
            states.append(runner.tick()["state"])
    finally:
        runner.close()
    assert set(states) == {"shadow_inference_only"}
    assert store.connection.execute("SELECT COUNT(*) FROM execution_commands").fetchone()[0] == 0
    store.close()
    lines = (tmp_path / "decisions.jsonl").read_text().splitlines()
    assert len(lines) == len(bars) - 1


# ------------------------------------------------------------ the real Alpaca runner, offline broker double


def test_alpaca_route_replays_with_the_offline_broker_double(tmp_path):
    from tools.modular_runner_shadow_e2e import ReplayBrokerClient, build_runner, shadow_config

    bars = _bars(80)
    contract = _contract(tmp_path, bars)
    data = json.loads(contract.read_text())
    data["asset_id"], data["timeframe"] = "equity:SPY", "1d"
    contract.write_text(json.dumps(data))
    config = shadow_config(contract, tmp_path / "w", family="recorded_forecast")
    wire = [{"time": b["time"], "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
             "volume": 1.0} for b in bars]
    runner = build_runner(config, wire[:2])
    try:
        for t in range(2, len(bars) + 1):
            from tools.modular_runner_shadow_e2e import to_wire
            ReplayBrokerClient.bars = to_wire(wire[:t])
            assert runner.tick(allow_execution=True)["state"] == "shadow_inference_only"
    finally:
        runner.close()
    assert set(ReplayBrokerClient.calls) == {"stock_bars"}


def test_a_poisoned_failing_column_never_reaches_a_decision(tmp_path):
    bars = _bars()
    policy = SelectedRecordedForecastPolicy(contract_file=_contract(tmp_path, bars, passing=(1, 3), poison=2),
                                            expected_asset_id="crypto:ETHUSD", expected_timeframe="4h",
                                            execution_tier="shadow_inference_only").policy
    decisions = _replay(policy, bars, len(bars))
    lines = [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert all(1e9 not in line["forecasts"] for line in lines)
    assert len(decisions) == len(bars) - 1
