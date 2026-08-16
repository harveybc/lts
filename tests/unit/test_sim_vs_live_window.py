"""WO2 proofs for the operating wrapper: typed comparability, structural
delta refusal, restart idempotency, append-only completions, rolling
report over comparable rows only, state-change-only emissions, and (after
findings 259/260) as-of lineage incidents reported BY NAME instead of as
generic missing data. Socket-free: every ledger is an injected sqlite
fixture."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prediction_provider_mechanics import (
    FEATURE_NAMES,
    build_closed_bar_features,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


replay = sys.modules.get("live_sim_replay") or _load("live_sim_replay")
svl = _load("sim_vs_live_window")

from app import as_of_lineage  # noqa: E402
from app.ibkr_l1_journal import L1ExecutionOlap  # noqa: E402

NOW = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
MODEL = "usdcad-4h-linear-live-v1"
ACCOUNT = "c0ff137a"


def _bars(n=60, start="2026-08-01T00:00:00+00:00", base=1.40):
    t0 = datetime.fromisoformat(start)
    bars = []
    for i in range(n):
        price = base + 0.002 * math.sin(i / 3.0)
        bars.append({
            "time": (t0 + timedelta(hours=4 * i)).isoformat(),
            "open": price - 0.0005, "high": price + 0.001,
            "low": price - 0.001, "close": price,
            "volume": 1000.0 + 10 * (i % 7), "complete": True,
        })
    return bars


def _artifact(tmp_path, model_id=MODEL, asset_id="fx:USD/CAD"):
    payload = json.dumps({
        "schema": "prediction_provider.live_linear_policy.v1",
        "model_id": model_id, "asset_id": asset_id,
        "timeframe": "4h", "feature_names": list(FEATURE_NAMES),
        "means": [0.0] * 11, "scales": [1.0] * 11,
        "coefficients": [0.0] * 11, "intercept": 0.0,
        "probability_threshold": 0.5,
    })
    path = tmp_path / f"model_{model_id}.json"
    path.write_text(payload, encoding="utf-8")
    return str(path), hashlib.sha256(payload.encode()).hexdigest()


ASSUMPTIONS = {
    "sim_reference_price": "recorded_decision_quote_mid",
    "sim_fill": "entry_slippage_measured_against_recorded_quote_mid",
    "fees": "not_modeled_and_typed_unavailable",
    "latency": "observed_lineage_only",
    "position_sizing": "seat_L0_risk_envelope",
}


def _seed_ledger(tmp_path, *, as_of_mode="bound", with_decision=True):
    """``as_of_mode``: bound | tampered | absent | pending | incident."""
    bars = _bars()
    observation = build_closed_bar_features(bars)
    artifact_file, artifact_sha = _artifact(tmp_path)
    bar_close = observation["last_closed_bar"]
    db = tmp_path / "ledger.sqlite"
    store = L1ExecutionOlap(db)
    effect_id = "l1e-wo2wo2wo2wo2wo2"
    store.create_effect(effect_id, "k-wo2", "bracket_entry", [])
    for state in ("submitted_pending_ack", "acknowledged"):
        store.advance_effect(effect_id, state)
    store.record_broker_fact(effect_id, "l0_reconciled",
                             {"filled_price": 1.4052})
    decision = {
        "venue": "ibkr_paper", "account_fingerprint": ACCOUNT,
        "asset_id": "fx:USD/CAD", "instrument": "USD.CAD",
        "timeframe": "4h", "bar_close": bar_close,
        "decided_at": "2026-08-10T20:00:05+00:00",
        "input_sha256": observation["input_sha256"],
        "config_sha256": "b" * 64, "model_id": MODEL,
        "artifact_sha256": artifact_sha, "action": "long",
        "outcome": "would_be_order",
        "quote": {"bid": 1.4049, "ask": 1.4051},
        "risk_envelope": {"stop_price": 1.395, "take_profit_price": 1.405,
                          "target_exposure": 1.0},
        "decision_id": f"{MODEL}:{bar_close}",
        "effect_or_command_id": effect_id,
    }
    as_of = {
        "venue": "ibkr_paper", "account_fingerprint": ACCOUNT,
        "instrument": "USD.CAD", "decision_id": f"{MODEL}:{bar_close}",
        "model_id": MODEL, "artifact_sha256": artifact_sha,
        "config_sha256": "b" * 64, "timeframe": "4h", "bar_close": bar_close,
        "input_sha256": observation["input_sha256"],
        "feature_contract": observation["feature_contract"],
        "source": "ibkr_tws_historical_closed_bars", "bars": bars,
    }
    if with_decision:
        if as_of_mode == "absent":
            store.record_due_bar_decision(decision)
        elif as_of_mode == "pending":
            store.record_as_of_pending(as_of)
            store.record_due_bar_decision(decision)
        elif as_of_mode == "tampered":
            store.record_due_bar_decision_with_as_of(
                decision, {**as_of, "bars": _bars(base=1.50)})
        elif as_of_mode == "incident":
            store.record_due_bar_decision_with_as_of(decision, as_of)
            with pytest.raises(as_of_lineage.AsOfLineageContradiction):
                store.record_due_bar_decision_with_as_of(
                    decision, {**as_of, "bars": _bars(base=1.61)})
        else:
            store.record_due_bar_decision_with_as_of(decision, as_of)
    store.close()
    return db, observation, artifact_file, effect_id


def _config(tmp_path, db, artifact_file=None, **seat_overrides):
    seat = {
        "seat": f"ibkr_paper:{MODEL}",
        "venue": "ibkr_paper", "instrument": "USD.CAD",
        "timeframe": "4h", "expected_asset_id": "fx:USD/CAD",
        "expected_model_id": MODEL,
        "execution_ledger": str(db), "bars_source": "asof_ledger",
        "lookback_hours": 72, "economic_assumptions": dict(ASSUMPTIONS),
    }
    if artifact_file:
        seat["artifact_file"] = artifact_file
    seat.update(seat_overrides)
    return {
        "schema": "lts.sim_vs_live_window_config.v1",
        "comparison_db": str(tmp_path / "comparison.sqlite"),
        "state_file": str(tmp_path / "state.json"),
        "seats": [seat],
    }


def _rows(config):
    conn = sqlite3.connect(config["comparison_db"])
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM comparison_rows ORDER BY seq")]
    conn.close()
    return rows


# ------------------------------------------------------------------ joins

def test_comparable_row_full_join(tmp_path):
    db, observation, artifact_file, _ = _seed_ledger(tmp_path)
    config = _config(tmp_path, db, artifact_file)
    summary = svl.collect(config, now=NOW, replay_module=replay,
                          emit=False)
    rows = _rows(config)
    assert len(rows) == 1
    row = rows[0]
    assert row["row_kind"] == "decision"
    assert row["comparability"] == "COMPARABLE"
    assert row["bar_close"] == observation["last_closed_bar"]
    assert row["input_sha256"] == observation["input_sha256"]
    # the comparison row itself binds the normalized due-decision identity
    assert row["account_fingerprint"] == ACCOUNT
    assert row["decision_id"] == f"{MODEL}:{observation['last_closed_bar']}"
    payload = svl.subtractable_payload(row)
    assert payload["simulation"]["input_hash_matches"] is True
    assert payload["simulation"]["sim_action"] == "long"
    assert payload["simulation"]["sim_action_matches"] is True
    assert payload["simulation"]["as_of_source"] == \
        "ibkr_tws_historical_closed_bars"
    assert payload["broker"]["quoted_spread"] == 0.0002
    assert payload["broker"]["entry_slippage_vs_mid"] == round(
        1.4052 - 1.4050, 8)
    assert payload["decision"]["proposed_geometry_json"] is not None
    state = summary["seats"][config["seats"][0]["seat"]]["state"]
    assert state["state"] == "COMPARABLE"


def test_no_asof_bars_typed_refusal(tmp_path):
    db, *_ = _seed_ledger(tmp_path, as_of_mode="absent")
    config = _config(tmp_path, db)
    svl.collect(config, now=NOW, replay_module=replay, emit=False)
    row = _rows(config)[0]
    assert row["comparability"] == "NOT_SUBTRACTABLE"
    assert json.loads(row["reason_codes_json"]) == ["NO_ASOF_BARS"]
    with pytest.raises(svl.NotSubtractableError):
        svl.subtractable_payload(row)


def test_pending_as_of_is_its_own_typed_refusal(tmp_path):
    db, *_ = _seed_ledger(tmp_path, as_of_mode="pending")
    config = _config(tmp_path, db)
    svl.collect(config, now=NOW, replay_module=replay, emit=False)
    row = _rows(config)[0]
    assert json.loads(row["reason_codes_json"]) == [
        "AS_OF_PENDING_UNRESOLVED"]
    with pytest.raises(svl.NotSubtractableError):
        svl.subtractable_payload(row)


def test_input_lineage_divergence_typed_refusal(tmp_path):
    db, *_ = _seed_ledger(tmp_path, as_of_mode="tampered")
    config = _config(tmp_path, db)
    svl.collect(config, now=NOW, replay_module=replay, emit=False)
    row = _rows(config)[0]
    assert row["comparability"] == "NOT_SUBTRACTABLE"
    assert json.loads(row["reason_codes_json"]) == [
        "INPUT_LINEAGE_DIVERGENCE"]


def test_model_identity_mismatch_typed_refusal(tmp_path):
    db, *_ = _seed_ledger(tmp_path)
    config = _config(tmp_path, db,
                     expected_model_id="some-other-model")
    svl.collect(config, now=NOW, replay_module=replay, emit=False)
    row = _rows(config)[0]
    assert row["comparability"] == "NOT_SUBTRACTABLE"
    assert "MODEL_IDENTITY_MISMATCH" in json.loads(
        row["reason_codes_json"])


def test_unpinned_economic_assumptions_refuse(tmp_path):
    db, _, artifact_file, _ = _seed_ledger(tmp_path)
    config = _config(tmp_path, db, artifact_file,
                     economic_assumptions=None)
    svl.collect(config, now=NOW, replay_module=replay, emit=False)
    row = _rows(config)[0]
    assert row["comparability"] == "NOT_SUBTRACTABLE"
    assert "ECONOMIC_ASSUMPTIONS_UNPINNED" in json.loads(
        row["reason_codes_json"])


def test_empty_window_and_missing_ledger_refusals(tmp_path):
    db, *_ = _seed_ledger(tmp_path, with_decision=False)
    config = _config(tmp_path, db)
    svl.collect(config, now=NOW, replay_module=replay, emit=False)
    row = _rows(config)[0]
    assert row["row_kind"] == "window_refusal"
    assert json.loads(row["reason_codes_json"]) == [
        "NO_DUE_DECISION_IN_WINDOW"]
    # deterministic due-bar identity: 4h grid floor of NOW
    assert row["bar_close"] == "2026-08-11T00:00:00+00:00"

    gone = _config(tmp_path, tmp_path / "missing.sqlite")
    gone["comparison_db"] = str(tmp_path / "comparison2.sqlite")
    gone["state_file"] = str(tmp_path / "state2.json")
    svl.collect(gone, now=NOW, replay_module=replay, emit=False)
    row = _rows(gone)[0]
    assert json.loads(row["reason_codes_json"]) == ["LEDGER_UNAVAILABLE"]


# ------------------------------------------------- 260: incident reporting

def test_lineage_incident_is_reported_by_name_not_as_missing_data(tmp_path):
    db, *_ = _seed_ledger(tmp_path, as_of_mode="incident")
    config = _config(tmp_path, db)
    summary = svl.collect(config, now=NOW, replay_module=replay, emit=False)
    row = _rows(config)[0]
    assert json.loads(row["reason_codes_json"]) == ["AS_OF_LINEAGE_INCIDENT"]
    incident = json.loads(row["payload_json"])["simulation"]["as_of_incident"]
    assert incident["reason_code"] == "as_of_lineage_contradiction"
    assert incident["detail"]["diverging_fields"] == ["bars_sha256"]

    conn = svl.open_comparison_db(config["comparison_db"])
    report = svl.build_rolling_report(conn, as_of=NOW + timedelta(hours=1))
    conn.close()
    seat = report["seats"][config["seats"][0]["seat"]]
    assert seat["not_subtractable_by_reason"] == {"AS_OF_LINEAGE_INCIDENT": 1}
    assert len(seat["as_of_lineage_incidents"]) == 1
    named = seat["as_of_lineage_incidents"][0]
    assert named["reason_code"] == "as_of_lineage_contradiction"
    assert named["rows"] == 1
    assert named["incident_key"]
    # the seat state handed to the incident router carries the same code
    state = summary["seats"][config["seats"][0]["seat"]]["state"]
    assert state["reasons"] == ["AS_OF_LINEAGE_INCIDENT"]


# -------------------------------------------------- restart / idempotency

def test_restart_never_duplicates_rows(tmp_path):
    db, _, artifact_file, _ = _seed_ledger(tmp_path)
    config = _config(tmp_path, db, artifact_file)
    first = svl.collect(config, now=NOW, replay_module=replay, emit=False)
    seat_rows = first["seats"][config["seats"][0]["seat"]]["rows"]
    assert all(r["appended"] for r in seat_rows)
    # simulated restart: same window re-collected by a fresh pass
    second = svl.collect(config, now=NOW + timedelta(minutes=30),
                         replay_module=replay, emit=False)
    seat_rows = second["seats"][config["seats"][0]["seat"]]["rows"]
    assert not any(r["appended"] for r in seat_rows)
    assert len(_rows(config)) == 1


def test_completion_appends_once_and_never_edits(tmp_path):
    db, _, artifact_file, effect_id = _seed_ledger(tmp_path)
    config = _config(tmp_path, db, artifact_file)
    svl.collect(config, now=NOW, replay_module=replay, emit=False)
    original = _rows(config)[0]

    store = L1ExecutionOlap(db)
    store.advance_effect(effect_id, "terminal_flat")
    store.record_broker_fact(effect_id, "recovery_reconciled_flat",
                             {"remaining_units": 0.0})
    store.close()

    svl.collect(config, now=NOW + timedelta(hours=4),
                replay_module=replay, emit=False)
    rows = _rows(config)
    kinds = sorted(r["row_kind"] for r in rows)
    assert kinds == ["completion", "decision"]
    completion = next(r for r in rows if r["row_kind"] == "completion")
    payload = json.loads(completion["payload_json"])
    assert payload["lifecycle"]["state"] == "terminal_flat"
    assert payload["realized"]["exit_reason"] == \
        "recovery_reconciled_flat"
    # the decision row is untouched, byte for byte
    unchanged = next(r for r in _rows(config)
                     if r["row_kind"] == "decision")
    assert unchanged["payload_sha256"] == original["payload_sha256"]
    # third pass: nothing new
    svl.collect(config, now=NOW + timedelta(hours=8),
                replay_module=replay, emit=False)
    assert len(_rows(config)) == 2


def test_untyped_reasons_are_impossible(tmp_path):
    conn = svl.open_comparison_db(tmp_path / "c.sqlite")
    with pytest.raises(ValueError, match="untyped"):
        svl.append_row(conn, row_kind="decision", venue="v", seat="s",
                       symbol="X", timeframe="4h", bar_close="t",
                       artifact_sha256="a", config_sha256="c",
                       input_sha256="i",
                       comparability="NOT_SUBTRACTABLE",
                       reasons=["MADE_UP_REASON"], payload={},
                       now=NOW)
    with pytest.raises(ValueError, match="requires at least one"):
        svl.append_row(conn, row_kind="decision", venue="v", seat="s",
                       symbol="X", timeframe="4h", bar_close="t",
                       artifact_sha256="a", config_sha256="c",
                       input_sha256="i",
                       comparability="NOT_SUBTRACTABLE", reasons=[],
                       payload={}, now=NOW)
    conn.close()


def test_account_collision_keeps_two_rows_in_the_comparison_table(tmp_path):
    """Two routes on the same instrument and bar are two comparison rows;
    one can never overwrite or absorb the other."""
    conn = svl.open_comparison_db(tmp_path / "c.sqlite")
    common = dict(row_kind="decision", venue="ibkr_paper", seat="seat",
                  symbol="USD.CAD", timeframe="4h",
                  bar_close="2026-08-10T20:00:00+00:00",
                  artifact_sha256="c" * 64, config_sha256="b" * 64,
                  input_sha256="a" * 64, comparability="COMPARABLE",
                  reasons=[], now=NOW, decision_id="m:bar")
    assert svl.append_row(conn, account_fingerprint="aaaa0001",
                          payload={"who": "a"}, **common) is True
    assert svl.append_row(conn, account_fingerprint="bbbb0002",
                          payload={"who": "b"}, **common) is True
    assert svl.append_row(conn, account_fingerprint="aaaa0001",
                          payload={"who": "a"}, **common) is False
    stored = [dict(r) for r in conn.execute(
        "SELECT account_fingerprint FROM comparison_rows ORDER BY seq")]
    assert [r["account_fingerprint"] for r in stored] == ["aaaa0001",
                                                          "bbbb0002"]
    conn.close()


# ----------------------------------------------------------------- report

def test_rolling_report_aggregates_only_comparable_rows(tmp_path):
    db, _, artifact_file, _ = _seed_ledger(tmp_path)
    config = _config(tmp_path, db, artifact_file)
    svl.collect(config, now=NOW, replay_module=replay, emit=False)

    # a second seat whose window refuses: enters counts, NEVER aggregates
    second_dir = tmp_path / "b"
    second_dir.mkdir()
    db2, *_ = _seed_ledger(second_dir, as_of_mode="absent")
    config2 = _config(tmp_path, db2)   # same comparison db, second seat
    config2["seats"][0]["seat"] = "ibkr_paper:refused-seat"
    svl.collect(config2, now=NOW, replay_module=replay, emit=False)

    conn = svl.open_comparison_db(config["comparison_db"])
    report = svl.build_rolling_report(conn, as_of=NOW + timedelta(hours=1))
    conn.close()

    seat = report["seats"][f"ibkr_paper:{MODEL}"]
    assert seat["comparable_rows"] == 1
    assert seat["not_subtractable_rows"] == 0
    assert seat["as_of_lineage_incidents"] == []
    assert seat["decision_match"] == {
        "evaluated": 1, "agreed": 1, "rate": 1.0,
        "note": seat["decision_match"]["note"]}
    assert seat["fill_quality"]["mean_quoted_spread"] == 0.0002
    assert "never annualized" in report["period_label"]

    refused = report["seats"]["ibkr_paper:refused-seat"]
    assert refused["comparable_rows"] == 0
    assert refused["not_subtractable_rows"] == 1
    assert refused["not_subtractable_by_reason"] == {"NO_ASOF_BARS": 1}
    # a refused row leaves NO numeric trace
    assert refused["decision_match"]["rate"] == "unavailable"
    assert refused["fill_quality"]["mean_quoted_spread"] == "unavailable"


# ------------------------------------------------------------ MT5 seat

def _seed_mt5(tmp_path):
    bars = _bars(base=1890.0)
    for bar in bars:
        bar["open"] = bar["close"] - 0.5
        bar["high"] = bar["close"] + 1.0
        bar["low"] = bar["close"] - 1.0
    observation = build_closed_bar_features(bars)
    bar_close_z = observation["last_closed_bar"].replace("+00:00", "Z")
    model = "ethusdt-4h-linear-live-v1"
    artifact_file, artifact_sha = _artifact(tmp_path, model_id=model,
                                            asset_id="crypto:ETHUSD")
    db = tmp_path / "bridge.sqlite"
    store = L1ExecutionOlap(db)
    store.record_due_bar_decision({
        "venue": "mt5_demo", "account_fingerprint": "c88e492a",
        "asset_id": "crypto:ETHUSD", "instrument": "ETHUSD",
        "timeframe": "4h", "bar_close": bar_close_z,
        "decided_at": "2026-08-10T20:00:41+00:00",
        "input_sha256": observation["input_sha256"],
        "config_sha256": "c" * 64, "model_id": model,
        "artifact_sha256": artifact_sha, "action": "long",
        "outcome": "would_be_order",
        "quote": {"bid": 1887.25, "ask": 1891.2},
        "decision_id": f"{model}:{bar_close_z}",
        "effect_or_command_id": "mt5-cmdcmdcmd",
    })
    con = store._con
    con.execute("CREATE TABLE account_snapshots"
                " (id INTEGER PRIMARY KEY, received_at TEXT)")
    con.execute("INSERT INTO account_snapshots VALUES"
                " (1, '2026-08-10T20:00:30+00:00')")
    con.execute("CREATE TABLE bar_snapshots (snapshot_id INTEGER,"
                " symbol TEXT, timeframe TEXT, bar_time TEXT, open REAL,"
                " high REAL, low REAL, close REAL, volume REAL)")
    for bar in bars:
        con.execute(
            "INSERT INTO bar_snapshots VALUES (1,'ETHUSD','4h',?,?,?,?,?,?)",
            (bar["time"].replace("+00:00", "Z"), bar["open"], bar["high"],
             bar["low"], bar["close"], bar["volume"]))
    con.execute("CREATE TABLE execution_commands (command_id TEXT PRIMARY"
                " KEY, state TEXT, created_at TEXT, delivered_at TEXT,"
                " completed_at TEXT, result_json TEXT)")
    con.execute(
        "INSERT INTO execution_commands VALUES ('mt5-cmdcmdcmd',"
        " 'succeeded', '2026-08-10T20:00:41+00:00',"
        " '2026-08-10T20:00:53+00:00', '2026-08-10T20:00:53+00:00',"
        " ?)", (json.dumps({"order_ticket": "40626526",
                            "result_code": 10009}),))
    con.execute("CREATE TABLE trade_events (event_id TEXT PRIMARY KEY,"
                " event_type TEXT, order_ticket TEXT, deal_ticket TEXT,"
                " price REAL, volume REAL, terminal_observed_at TEXT,"
                " received_at TEXT)")
    con.execute(
        "INSERT INTO trade_events VALUES ('e1',"
        " 'TRADE_TRANSACTION_DEAL_ADD', '40626526', '41443640', 1887.2,"
        " 0.01, '2026-08-10T20:00:50+00:00', '2026-08-10T20:00:50+00:00')")
    con.commit()
    store.close()
    return db, artifact_file, model


def test_mt5_seat_still_produces_a_comparable_row_under_v2(tmp_path):
    """The first COMPARABLE MT5 rows came from bridge-snapshot
    reconstruction, not the as-of table. v2 must not have taken that away:
    the MT5 seat still reaches COMPARABLE and still aggregates."""
    db, artifact_file, model = _seed_mt5(tmp_path)
    config = _config(
        tmp_path, db, artifact_file, seat=f"mt5_demo:{model}",
        venue="mt5_demo", instrument="ETHUSD",
        expected_asset_id="crypto:ETHUSD", expected_model_id=model,
        bars_source="mt5_bridge", bars_count=60, lookback_hours=96)
    svl.collect(config, now=NOW, replay_module=replay, emit=False)
    rows = _rows(config)
    assert len(rows) == 1
    row = rows[0]
    assert row["comparability"] == "COMPARABLE"
    assert row["account_fingerprint"] == "c88e492a"
    payload = svl.subtractable_payload(row)
    assert payload["simulation"]["input_hash_matches"] is True
    assert payload["simulation"]["as_of_source"] == "mt5_bridge_snapshot"
    assert payload["simulation"]["sim_action_source"] == "artifact_repredict"
    assert payload["broker"]["lifecycle"]["broker_result_code"] == 10009
    mid = (1891.2 + 1887.25) / 2.0
    assert payload["broker"]["entry_slippage_vs_mid"] == round(
        1887.2 - mid, 8)

    conn = svl.open_comparison_db(config["comparison_db"])
    report = svl.build_rolling_report(conn, as_of=NOW + timedelta(hours=1))
    conn.close()
    seat = report["seats"][f"mt5_demo:{model}"]
    assert seat["comparable_rows"] == 1
    assert seat["as_of_lineage_incidents"] == []
    assert seat["fill_quality"]["rows_with_fill_evidence"] == 1


# -------------------------------------------------------------- emissions

def test_emissions_only_on_state_change():
    comparable = {"state": "COMPARABLE", "reasons": []}
    refused = {"state": "NOT_SUBTRACTABLE:AS_OF_LINEAGE_INCIDENT",
               "reasons": ["AS_OF_LINEAGE_INCIDENT"]}
    seat = f"ibkr_paper:{MODEL}"
    # first sight initializes silently
    assert svl.emission_plan({}, {seat: refused}) == []
    # no change, no noise
    assert svl.emission_plan({seat: dict(refused)}, {seat: refused}) == []
    # degradation observes through the router
    plan = svl.emission_plan({seat: comparable}, {seat: refused})
    assert len(plan) == 1 and plan[0]["action"] == "observe"
    assert plan[0]["payload"]["reasons"] == ["AS_OF_LINEAGE_INCIDENT"]
    # recovery is a recover event, not a page
    plan = svl.emission_plan({seat: refused}, {seat: comparable})
    assert len(plan) == 1 and plan[0]["action"] == "recover"


def test_collect_records_state_and_emits_via_injected_runner(tmp_path):
    db, _, artifact_file, _ = _seed_ledger(tmp_path)
    config = _config(tmp_path, db, artifact_file)
    config["incident"] = {"repo": str(tmp_path), "machine": "testhost"}
    calls = []

    def runner(command, **kwargs):
        calls.append(command)

        class R:
            returncode = 0
            stderr = ""
        return R()

    svl.collect(config, now=NOW, replay_module=replay, emit=True,
                emit_runner=runner)
    assert calls == []          # initialization is silent
    state = json.loads(Path(config["state_file"]).read_text())
    assert state["schema"] == "lts.sim_vs_live_state.v1"
    seat_state = state["seats"][f"ibkr_paper:{MODEL}"]
    assert seat_state["state"] == "COMPARABLE"
