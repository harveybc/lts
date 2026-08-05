"""Order C4: the rolling report is reproducible from persisted facts,
labels periods exactly, and reports unavailable rather than inventing."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "rolling_evidence_report",
    REPO_ROOT / "tools" / "rolling_evidence_report.py")
report_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["rolling_evidence_report"] = report_mod
_SPEC.loader.exec_module(report_mod)

from app.ibkr_l1_journal import L1ExecutionOlap  # noqa: E402

AS_OF = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


def _seed(tmp_path, monkeypatch):
    # AUD-F2-20260805-116: the lifecycle rows must carry DETERMINISTIC
    # event timestamps inside the queried window, not the wall clock —
    # otherwise every run after 12:00 UTC on the fixture date places
    # them in AS_OF's future and the report reads zero.
    import app.ibkr_l1_journal as journal_mod
    monkeypatch.setattr(
        journal_mod, "_utc_now",
        lambda: AS_OF - timedelta(hours=1))
    ledger = L1ExecutionOlap(tmp_path / "ibkr.sqlite")
    for hour, outcome in ((0, "would_be_order"), (4, "hold"),
                          (8, "rejected")):
        ledger.record_due_bar_decision({
            "venue": "ibkr_paper", "account_fingerprint": "c0ff137a",
            "asset_id": "fx:USD/CAD", "instrument": "USD.CAD",
            "timeframe": "4h",
            "bar_close": f"2026-08-05T{hour:02d}:00:00+00:00",
            "decided_at": f"2026-08-05T{hour:02d}:00:05+00:00",
            "input_sha256": "a" * 64, "config_sha256": "b" * 64,
            "model_id": "m1", "artifact_sha256": "c" * 64,
            "action": "short", "outcome": outcome,
            "decision_id": f"m1:{hour}",
        })
    ledger.create_effect("l1e-x", "k1", "bracket_entry", [])
    for state in ("submitted_pending_ack", "acknowledged",
                  "terminal_flat"):
        ledger.advance_effect("l1e-x", state)
    ledger.close()
    return {
        "schema": "lts.rolling_evidence_config.v1",
        "incident_ledger": str(tmp_path / "absent-incidents.sqlite"),
        "venues": [{"venue": "ibkr_paper", "timeframe": "4h",
                    "execution_ledger": str(tmp_path / "ibkr.sqlite")}],
    }


def test_report_counts_and_labels(tmp_path, monkeypatch):
    config = _seed(tmp_path, monkeypatch)
    report = report_mod.build_report(config, AS_OF)
    day = report["windows"]["24h"]["ibkr_paper"]
    assert day["available"] is True
    assert day["due_bar_facts"]["delivered_bars"] == 3
    assert day["due_bar_facts"]["by_outcome"]["hold"] == 1
    assert day["expected_bars_upper_bound"] == 6
    assert "not annualized" in day["period_label"]
    assert day["lifecycles"]["created"] == 1
    assert day["unresolved"]["nonterminal_effects"] == 0
    week = report["windows"]["7d"]["ibkr_paper"]
    assert week["expected_bars_upper_bound"] == 42
    # Reproducible: identical inputs give identical output.
    assert report_mod.build_report(config, AS_OF) == report


def test_unreadable_sources_are_unavailable_never_invented(tmp_path):
    config = {
        "schema": "lts.rolling_evidence_config.v1",
        "incident_ledger": str(tmp_path / "no.sqlite"),
        "venues": [{"venue": "ghost", "timeframe": "4h",
                    "execution_ledger": str(tmp_path / "ghost.sqlite")}],
    }
    report = report_mod.build_report(config, AS_OF)
    entry = report["windows"]["24h"]["ghost"]
    assert entry["available"] is True or entry["available"] is False
    # sqlite connect on a missing file CREATES nothing in ro mode:
    assert (entry.get("available") is False
            or entry.get("due_bar_facts") == "unavailable")
