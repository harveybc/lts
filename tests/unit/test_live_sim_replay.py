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
