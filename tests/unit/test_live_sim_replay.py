"""Order C2 proofs: lineage-only joins, identity-mismatch rejection
(order §6 item 16), explicit replay gaps, latency/slippage residuals."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "live_sim_replay", REPO_ROOT / "tools" / "live_sim_replay.py")
replay = importlib.util.module_from_spec(_SPEC)
sys.modules["live_sim_replay"] = replay
_SPEC.loader.exec_module(replay)

from app.ibkr_l1_journal import L1ExecutionOlap  # noqa: E402


def _seed(tmp_path, *, timeframe="4h", model="m1", effect=True):
    db = tmp_path / "ledger.sqlite"
    store = L1ExecutionOlap(db)
    effect_id = None
    if effect:
        effect_id = "l1e-abcdefabcdefabcd"
        store.create_effect(effect_id, "k1", "bracket_entry", [])
        for state in ("submitted_pending_ack", "acknowledged",
                      "terminal_flat"):
            store.advance_effect(effect_id, state)
        store.record_broker_fact(effect_id, "recovery_reconciled_flat",
                                 {"remaining_units": 0.0,
                                  "filled_price": 1.4052})
    store.record_due_bar_decision({
        "venue": "ibkr_paper", "account_fingerprint": "c0ff137a",
        "asset_id": "fx:USD/CAD", "instrument": "USD.CAD",
        "timeframe": timeframe,
        "bar_close": "2026-08-05T04:00:00+00:00",
        "decided_at": "2026-08-05T08:00:02+00:00",
        "input_sha256": "a" * 64, "config_sha256": "b" * 64,
        "model_id": model, "artifact_sha256": "c" * 64,
        "action": "short", "outcome": "would_be_order",
        "quote": {"bid": 1.4049, "ask": 1.4051},
        "decision_id": f"{model}:2026-08-05T04:00:00+00:00",
        "effect_or_command_id": effect_id,
    })
    store.close()
    return {
        "schema": "lts.live_sim_replay_config.v1",
        "venues": [{"venue": "ibkr_paper", "timeframe": "4h",
                    "instrument": "USD.CAD",
                    "expected_asset_id": "fx:USD/CAD",
                    "execution_ledger": str(db)}],
    }


def test_lineage_join_and_residuals(tmp_path):
    config = _seed(tmp_path)
    report = replay.build_replay(
        config, venue="ibkr_paper", since="2026-08-05T00:00:00+00:00",
        until="2026-08-06T00:00:00+00:00")
    assert report["decisions_joined"] == 1
    row = report["rows"][0]
    assert row["lifecycle"]["effect_id"] == "l1e-abcdefabcdefabcd"
    assert row["lifecycle"]["decision_to_effect_seconds"] is not None
    assert row["quoted_spread"] == 0.0002
    assert row["entry_slippage_vs_mid"] == round(1.4052 - 1.4050, 8)
    assert row["lifecycle"]["exit_reason"] == "recovery_reconciled_flat"
    assert row["replay"] == "unavailable"          # no bars source: honest
    assert "never annualized" in report["period_label"]


def test_identity_mismatch_rejects_never_joins(tmp_path):
    config = _seed(tmp_path, timeframe="1h")       # wrong timeframe fact
    report = replay.build_replay(
        config, venue="ibkr_paper", since="2026-08-05T00:00:00+00:00",
        until="2026-08-06T00:00:00+00:00")
    assert report["decisions_joined"] == 0
    assert report["decisions_rejected"][0]["errors"] == [
        "timeframe mismatch"]


def test_model_and_config_mismatch_reject(tmp_path):
    config = _seed(tmp_path)
    config["venues"][0]["expected_model_id"] = "other-model"
    config["venues"][0]["expected_config_sha256"] = "f" * 64
    report = replay.build_replay(
        config, venue="ibkr_paper", since="2026-08-05T00:00:00+00:00",
        until="2026-08-06T00:00:00+00:00")
    errors = report["decisions_rejected"][0]["errors"]
    assert "model mismatch" in errors
    assert "config mismatch" in errors


def test_missing_lifecycle_is_reported_not_timestamp_joined(tmp_path):
    config = _seed(tmp_path, effect=False)
    db = config["venues"][0]["execution_ledger"]
    store = L1ExecutionOlap(db)
    store._con.execute(
        "UPDATE due_bar_decisions SET effect_or_command_id='l1e-ghost'")
    store._con.commit()
    store.close()
    report = replay.build_replay(
        config, venue="ibkr_paper", since="2026-08-05T00:00:00+00:00",
        until="2026-08-06T00:00:00+00:00")
    row = report["rows"][0]
    assert row["lifecycle"] == "missing"
    assert "never joined by timestamp" in row["note"]

# ------------------------------------------------------- WO2 extensions
# v2 as-of lineage (findings 259/260): the comparator reads only BOUND
# rows joined on the normalized due-decision identity, and every gap is
# typed — a durable incident and a pending linkage are never collapsed
# into generic "missing data".

import hashlib  # noqa: E402
import math  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

import pytest  # noqa: E402

from prediction_provider_mechanics import (  # noqa: E402
    FEATURE_NAMES,
    build_closed_bar_features,
)

from app import as_of_lineage  # noqa: E402

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


def _artifact(tmp_path, model_id=MODEL):
    payload = json.dumps({
        "schema": "prediction_provider.live_linear_policy.v1",
        "model_id": model_id, "asset_id": "fx:USD/CAD",
        "timeframe": "4h", "feature_names": list(FEATURE_NAMES),
        "means": [0.0] * 11, "scales": [1.0] * 11,
        "coefficients": [0.0] * 11, "intercept": 0.0,
        "probability_threshold": 0.5,
    })
    path = tmp_path / "model.json"
    path.write_text(payload, encoding="utf-8")
    return str(path), hashlib.sha256(payload.encode()).hexdigest()


def _decision_fact(observation, artifact_sha, effect_id):
    bar_close = observation["last_closed_bar"]
    return {
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


def _as_of_fact(observation, artifact_sha, bars):
    bar_close = observation["last_closed_bar"]
    return {
        "venue": "ibkr_paper", "account_fingerprint": ACCOUNT,
        "instrument": "USD.CAD", "decision_id": f"{MODEL}:{bar_close}",
        "model_id": MODEL, "artifact_sha256": artifact_sha,
        "config_sha256": "b" * 64, "timeframe": "4h", "bar_close": bar_close,
        "input_sha256": observation["input_sha256"],
        "feature_contract": observation["feature_contract"],
        "source": "ibkr_tws_historical_closed_bars", "bars": bars,
    }


def _seed_asof(tmp_path, *, mode="bound"):
    """``mode``: bound | tampered | absent | pending | incident."""
    bars = _bars()
    observation = build_closed_bar_features(bars)
    artifact_file, artifact_sha = _artifact(tmp_path)
    db = tmp_path / "ledger.sqlite"
    store = L1ExecutionOlap(db)
    effect_id = "l1e-asofasofasofasof"
    store.create_effect(effect_id, "k-asof", "bracket_entry", [])
    for state in ("submitted_pending_ack", "acknowledged"):
        store.advance_effect(effect_id, state)
    store.record_broker_fact(effect_id, "l0_reconciled",
                             {"filled_price": 1.4052})
    decision = _decision_fact(observation, artifact_sha, effect_id)
    as_of = _as_of_fact(observation, artifact_sha, bars)
    if mode == "absent":
        store.record_due_bar_decision(decision)
    elif mode == "pending":
        store.record_as_of_pending(as_of)
        store.record_due_bar_decision(decision)
    elif mode == "tampered":
        # the as-of row DECLARES this decision's input hash but carries a
        # different window: the replay recomputes and reports the divergence
        store.record_due_bar_decision_with_as_of(
            decision, {**as_of, "bars": _bars(base=1.50)})
    elif mode == "incident":
        store.record_due_bar_decision_with_as_of(decision, as_of)
        with pytest.raises(as_of_lineage.AsOfLineageContradiction):
            store.record_due_bar_decision_with_as_of(
                decision, {**as_of, "bars": _bars(base=1.61)})
    else:
        store.record_due_bar_decision_with_as_of(decision, as_of)
    store.close()
    return {
        "schema": "lts.live_sim_replay_config.v1",
        "venues": [{"venue": "ibkr_paper", "timeframe": "4h",
                    "instrument": "USD.CAD",
                    "expected_asset_id": "fx:USD/CAD",
                    "bars_source": "asof_ledger",
                    "artifact_file": artifact_file,
                    "execution_ledger": str(db)}],
    }


def _replay(config):
    return replay.build_replay(
        config, venue="ibkr_paper", since="2026-08-10T00:00:00+00:00",
        until="2026-08-11T00:00:00+00:00")


def test_asof_v2_replay_hash_match_and_artifact_repredict(tmp_path):
    report = _replay(_seed_asof(tmp_path))
    assert report["decisions_joined"] == 1
    row = report["rows"][0]
    assert row["replay"] == "computed"
    assert row["input_hash_matches"] is True
    assert row["as_of_source"] == "ibkr_tws_historical_closed_bars"
    assert row["as_of_bars_sha256"]
    assert row["sim_action_source"] == "artifact_repredict"
    assert row["sim_action"] == "long"          # p=0.5 >= threshold
    assert row["sim_action_matches"] is True
    assert row["quoted_spread"] == 0.0002
    assert row["entry_slippage_vs_mid"] == round(1.4052 - 1.4050, 8)
    assert report["as_of_lineage_incidents"] == []


def test_asof_v2_divergent_bars_reported_never_joined(tmp_path):
    row = _replay(_seed_asof(tmp_path, mode="tampered"))["rows"][0]
    assert row["replay"] == "computed"
    assert row["input_hash_matches"] is False   # divergence is a fact


def test_absent_as_of_is_typed_no_asof_bars(tmp_path):
    row = _replay(_seed_asof(tmp_path, mode="absent"))["rows"][0]
    assert row["replay"] == "unavailable"
    assert row["reason_code"] == "NO_ASOF_BARS"
    assert "as_of_incident" not in row


def test_pending_only_linkage_is_typed_and_never_used_as_evidence(tmp_path):
    row = _replay(_seed_asof(tmp_path, mode="pending"))["rows"][0]
    assert row["replay"] == "unavailable"
    assert row["reason_code"] == "AS_OF_PENDING_UNRESOLVED"
    assert "sim_action" not in row


def test_lineage_incident_is_named_not_reported_as_missing_data(tmp_path):
    report = _replay(_seed_asof(tmp_path, mode="incident"))
    row = report["rows"][0]
    assert row["replay"] == "unavailable"
    assert row["reason_code"] == "AS_OF_LINEAGE_INCIDENT"
    incident = row["as_of_incident"]
    assert incident["reason_code"] == "as_of_lineage_contradiction"
    assert incident["detail"]["diverging_fields"] == ["bars_sha256"]
    assert len(report["as_of_lineage_incidents"]) == 1


def test_incident_journal_alone_is_enough_to_refuse(tmp_path):
    """The ledger table can be unreadable/absent: the sidecar journal beside
    it still makes the incident visible to the comparator."""
    config = _seed_asof(tmp_path)
    ledger = Path(config["venues"][0]["execution_ledger"])
    store = L1ExecutionOlap(ledger)
    identity = store.as_of_rows(row_state="bound")[0]["identity_sha256"]
    store.close()
    record = {
        "schema": as_of_lineage.INCIDENT_SCHEMA, "event": "opened",
        "recorded_at": "2026-08-10T21:00:00+00:00",
        "incident_key": as_of_lineage.incident_key(
            identity, as_of_lineage.REASON_PERSISTENCE_FAILURE),
        "identity_sha256": identity,
        "reason_code": as_of_lineage.REASON_PERSISTENCE_FAILURE,
        "venue": "ibkr_paper", "account_fingerprint": ACCOUNT,
        "instrument": "USD.CAD", "decision_id": "unknown",
        "detail_json": json.dumps({"phase": "bind"}),
    }
    as_of_lineage.append_journal(
        as_of_lineage.journal_path_for(ledger), record)
    row = _replay(config)["rows"][0]
    assert row["reason_code"] == "AS_OF_LINEAGE_INCIDENT"
    assert row["as_of_incident"]["reason_code"] == "as_of_persistence_failure"


def test_mt5_bridge_snapshot_replay_and_command_lineage(tmp_path):
    bars = _bars(base=1890.0)
    # MT5 geometry: prices ~1890, keep low > 0 and OHLC sane
    for bar in bars:
        bar["open"] = bar["close"] - 0.5
        bar["high"] = bar["close"] + 1.0
        bar["low"] = bar["close"] - 1.0
    observation = build_closed_bar_features(bars)
    bar_close_z = observation["last_closed_bar"].replace("+00:00", "Z")
    db = tmp_path / "bridge.sqlite"
    store = L1ExecutionOlap(db)   # provides due_bar_decisions schema
    store.record_due_bar_decision({
        "venue": "mt5_demo", "account_fingerprint": "c88e492a",
        "asset_id": "crypto:ETHUSD", "instrument": "ETHUSD",
        "timeframe": "4h", "bar_close": bar_close_z,
        "decided_at": "2026-08-10T20:00:41+00:00",
        "input_sha256": observation["input_sha256"],
        "config_sha256": "c" * 64,
        "model_id": "ethusdt-4h-linear-live-v1",
        "artifact_sha256": "d" * 64,
        "action": "short", "outcome": "would_be_order",
        "quote": {"bid": 1887.25, "ask": 1891.2},
        "decision_id": f"ethusdt-4h-linear-live-v1:{bar_close_z}",
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
    config = {
        "schema": "lts.live_sim_replay_config.v1",
        "venues": [{"venue": "mt5_demo", "timeframe": "4h",
                    "instrument": "ETHUSD",
                    "expected_asset_id": "crypto:ETHUSD",
                    "bars_source": "mt5_bridge", "bars_count": 60,
                    "execution_ledger": str(db)}],
    }
    report = replay.build_replay(
        config, venue="mt5_demo", since="2026-08-10T00:00:00+00:00",
        until="2026-08-11T00:00:00+00:00")
    assert report["decisions_joined"] == 1
    row = report["rows"][0]
    assert row["replay"] == "computed"
    assert row["input_hash_matches"] is True
    assert row["lifecycle"]["state"] == "succeeded"
    assert row["lifecycle"]["broker_result_code"] == 10009
    assert row["fill_price"] == 1887.2
    mid = (1891.2 + 1887.25) / 2.0
    assert row["entry_slippage_vs_mid"] == round(1887.2 - mid, 8)
    # the MT5 path is snapshot-reconstructed and never touches the v2 table
    assert report["as_of_lineage_incidents"] == []
