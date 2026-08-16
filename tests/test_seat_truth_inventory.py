"""Socket-free unit tests for WO1 seat-truth assembly/refusal logic.

Every identifier, size, price and order id below is SYNTHETIC: these
fixtures never carry a real broker fact (AUD-SEC-20260816-255).

No network, no ssh, no broker session, no live store is touched: every
test drives the pure assembly functions with injected fakes and isolated
temp sqlite stores.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools.seat_truth_inventory as seat_truth  # noqa: E402
from tools.seat_truth_inventory import (  # noqa: E402
    EXPECTED,
    RedactionLeak,
    Redactor,
    _write_enabled_fact,
    account_block,
    expected_account_fingerprint,
    normalize_bar_ts,
    assemble_inventory,
    assert_direct_sources_only,
    decision_block,
    fact,
    ibkr_stuck_order_from_ledger,
    index_unavailable,
    is_unavailable,
    manifest_join,
    render_table,
    resolve_ibkr_order_exposure,
    typed_tws_facts,
    unavailable,
)

NOW = datetime(2026, 8, 16, 2, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# typed fact envelope
# --------------------------------------------------------------------------

def test_fact_freshness_within_budget():
    entry = fact(1, source="direct_runner:/x",
                 observed_at="2026-08-16T01:59:00+00:00",
                 budget_seconds=300.0, now=NOW)
    assert entry["fresh"] is True
    assert entry["age_seconds"] == pytest.approx(60.0)
    assert entry["freshness_budget_seconds"] == 300.0


def test_fact_stale_beyond_budget_is_typed_not_dropped():
    entry = fact("bar", source="direct_runner:/x",
                 observed_at="2026-08-15T01:00:00+00:00",
                 budget_seconds=300.0, now=NOW)
    assert entry["fresh"] is False
    assert entry["value"] == "bar"           # value kept, staleness typed


def test_unavailable_is_typed_never_imputed():
    entry = unavailable("host_unreachable", detail="ssh timeout",
                        source_attempted="ssh://dragon/db")
    assert is_unavailable(entry)
    assert "value" not in entry
    assert entry["unavailable"] == "host_unreachable"


def test_write_enabled_is_negated_read_only_flag():
    hb = {"read_only": False, "observed_at": NOW.isoformat()}
    entry = _write_enabled_fact(hb, "/hb", NOW)
    assert entry["value"] is True          # read_only False => write-enabled
    hb_ro = {"read_only": True, "observed_at": NOW.isoformat()}
    assert _write_enabled_fact(hb_ro, "/hb", NOW)["value"] is False
    assert is_unavailable(_write_enabled_fact(None, "/hb", NOW))


def test_normalize_bar_ts_z_suffix_only():
    assert normalize_bar_ts("2026-08-12T13:00:00Z") \
        == "2026-08-12T13:00:00+00:00"
    assert normalize_bar_ts("2026-08-12T13:00:00+00:00") \
        == "2026-08-12T13:00:00+00:00"


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

def test_redactor_blocks_registered_raw_identifier():
    red = Redactor()
    fp = red.fingerprint("DUR000111")
    assert len(fp) == 16 and fp != "DUR000111"
    with pytest.raises(RedactionLeak):
        red.assert_clean(json.dumps({"account": "DUR000111"}))
    red.assert_clean(json.dumps({"account": fp}))


def test_redactor_blocks_account_shaped_tokens_even_unregistered():
    red = Redactor()
    with pytest.raises(RedactionLeak):
        red.assert_clean("order for DUX123456 pending")
    with pytest.raises(RedactionLeak):
        red.assert_clean("alpaca account PA3ABCDE12F")


# --------------------------------------------------------------------------
# seat binding: compared, never carried and never emitted
# --------------------------------------------------------------------------

def test_seat_registry_carries_no_account_identifier():
    """A fingerprint is a stable account id; the tool keeps none."""
    for seat, entry in EXPECTED.items():
        assert "fingerprint" not in entry, seat
        assert entry["binding_config"].endswith(".json"), seat


def test_expected_binding_comes_from_the_deployed_runner_config(
        tmp_path, monkeypatch):
    config = tmp_path / EXPECTED["alpaca_paper"]["binding_config"]
    config.write_text(json.dumps(
        {"venue": {"account_fingerprint": "fp00112233445566"}}))
    monkeypatch.setattr(seat_truth, "CONFIG_ROOT", tmp_path)
    assert expected_account_fingerprint("alpaca_paper") \
        == "fp00112233445566"
    monkeypatch.setattr(seat_truth, "CONFIG_ROOT", tmp_path / "absent")
    assert expected_account_fingerprint("alpaca_paper") is None


def test_unknown_expected_binding_is_typed_never_a_false_mismatch():
    hb = {"account_fingerprint": "fp00112233445566",
          "observed_at": NOW.isoformat()}
    unknown = account_block(hb, "/hb", None, "paper", NOW)
    assert is_unavailable(unknown["fingerprint_matches_expected"])
    assert unknown["fingerprint_matches_expected"]["unavailable"] \
        == "expected_binding_unknown"
    matched = account_block(hb, "/hb", "fp00112233445566", "paper", NOW)
    assert matched["fingerprint_matches_expected"]["value"] is True
    mismatched = account_block(hb, "/hb", "fp99999999999999", "paper", NOW)
    assert mismatched["fingerprint_matches_expected"]["value"] is False


# --------------------------------------------------------------------------
# direct-source refusal (the consolidated watchdog can never be a source)
# --------------------------------------------------------------------------

def test_assembly_refuses_consolidated_watchdog_source():
    seats = {"seat": {"venue": fact(
        "x", source="~/.local/state/lts/paper-execution-watchdog/out.json",
        now=NOW)}}
    with pytest.raises(ValueError, match="forbidden consolidated source"):
        assert_direct_sources_only(seats)


def test_assembly_refuses_monitor_store_source():
    seats = {"seat": {"orders": fact(
        1, source="sqlite:paper-execution-monitor.sqlite#alerts", now=NOW)}}
    with pytest.raises(ValueError):
        assert_direct_sources_only(seats)


def test_assembly_accepts_direct_sources():
    seats = {"seat": {
        "venue": fact("ibkr_paper",
                      source="direct_venue:tws://127.0.0.1:7497", now=NOW),
        "bar": fact("2026-08-14T12:00:00+00:00",
                    source="direct_runner_ledger:/tmp/x.sqlite#rows",
                    now=NOW),
        "gap": unavailable("no_rows", source_attempted="direct_file:/y"),
    }}
    assert_direct_sources_only(seats)     # must not raise


def test_assemble_inventory_schema_and_unavailable_index():
    seats = {"seat": {
        "ok": fact(1, source="direct_runner:/hb", now=NOW),
        "missing": unavailable("no_heartbeat",
                               source_attempted="direct_runner:/hb"),
    }}
    inventory = assemble_inventory(seats, collector={"host": "test"},
                                   now=NOW)
    assert inventory["schema"] == "lts.seat_truth_inventory.v1"
    assert inventory["schema_version"] == 1
    index = inventory["unavailable_index"]
    assert index == [{"path": "seat.missing", "reason": "no_heartbeat",
                      "detail": None}]


def test_index_unavailable_walks_nested_paths():
    tree = {"a": {"b": [unavailable("r1"), fact(1, source="direct:x")]},
            "c": unavailable("r2", detail="d")}
    found = index_unavailable(tree)
    assert {(f["path"], f["reason"]) for f in found} == {
        ("a.b[0]", "r1"), ("c", "r2")}


# --------------------------------------------------------------------------
# finding-241 manifest join
# --------------------------------------------------------------------------

HB = {"artifact_sha256": "a" * 64, "config_sha256": "c" * 64,
      "manifest_sha256": "m" * 64, "observed_at": NOW.isoformat()}
MF = {"schema": "prediction_provider.live_linear_manifest.v1",
      "artifact_sha256": "a" * 64, "config_sha256": "c" * 64,
      "live_inference_eligible": False, "live_execution_eligible": False,
      "research_validated": True}


def test_manifest_join_exact_match_with_eligibility_gap_typed():
    joined = manifest_join(heartbeat=HB, manifest=MF,
                           manifest_path="/m.json",
                           manifest_bytes_sha256="m" * 64,
                           artifact_recomputed_sha256="a" * 64, now=NOW)
    checks = joined["join"]["value"]["checks"]
    assert checks["heartbeat_artifact_equals_manifest_artifact"] is True
    assert checks["manifest_artifact_equals_file_bytes"] is True
    assert checks["heartbeat_manifest_sha_equals_manifest_bytes"] is True
    # F-P1-04: identity provable but eligibility gap must be typed
    assert any("F-P1-04" in note for note in joined["proof_notes"])


def test_manifest_join_hash_mismatch_is_typed_not_smoothed():
    joined = manifest_join(heartbeat=HB,
                           manifest={**MF, "artifact_sha256": "b" * 64},
                           manifest_path="/m.json",
                           manifest_bytes_sha256="m" * 64,
                           artifact_recomputed_sha256="a" * 64, now=NOW)
    checks = joined["join"]["value"]["checks"]
    assert checks["heartbeat_artifact_equals_manifest_artifact"] is False
    assert "heartbeat_artifact_equals_manifest_artifact" in \
        joined["join"]["value"]["gaps"]


def test_manifest_join_missing_manifest_is_unavailable():
    joined = manifest_join(heartbeat=HB, manifest=None,
                           manifest_path="/m.json",
                           manifest_bytes_sha256=None,
                           artifact_recomputed_sha256=None, now=NOW)
    assert is_unavailable(joined["join"])


def test_manifest_join_absent_hash_fields_type_unavailable_not_false():
    joined = manifest_join(heartbeat={"observed_at": NOW.isoformat()},
                           manifest=MF, manifest_path="/m.json",
                           manifest_bytes_sha256="m" * 64,
                           artifact_recomputed_sha256=None, now=NOW)
    checks = joined["join"]["value"]["checks"]
    assert checks["heartbeat_artifact_equals_manifest_artifact"] \
        == "unavailable"
    assert checks["manifest_artifact_equals_file_bytes"] == "unavailable"


# --------------------------------------------------------------------------
# IBKR order/exposure resolution
# --------------------------------------------------------------------------

def _positions(rows):
    return fact(rows, source="direct_venue:tws positions()", now=NOW)


STUCK = fact({"order_id": 4242, "leg_identity": {"leg": "take_profit"}},
             source="direct_runner_ledger:/l.sqlite#l1_broker_facts",
             now=NOW)
VENUE_ORDERS_GAP = unavailable(
    "reqAllOpenOrders_rebinding_risk",
    source_attempted="direct_venue:tws")


def test_resolution_flat_direct_with_stale_open_exposure():
    result = resolve_ibkr_order_exposure(
        direct_positions=_positions([{"position": 0.0}]),
        direct_portfolio=_positions([]),
        heartbeat_position=fact(0, source="direct_runner:/hb", now=NOW),
        heartbeat_orders=fact(1, source="direct_runner:/hb", now=NOW),
        ledger_open_exposure=fact({"exposure_id": "exp-1",
                                   "units_open": -1000.0},
                                  source="direct_runner_ledger:/l#exp",
                                  now=NOW),
        stuck_order=STUCK, venue_open_orders=VENUE_ORDERS_GAP)
    assert result["state"] == "flat_at_venue__ledger_exposure_row_stale_open"
    assert result["direct_flatness"] is True
    assert is_unavailable(result["venue_scoped_open_orders"])


def test_resolution_never_infers_flat_when_direct_unavailable():
    gap = unavailable("tws_probe_unavailable", source_attempted="tws")
    result = resolve_ibkr_order_exposure(
        direct_positions=gap, direct_portfolio=gap,
        heartbeat_position=fact(0, source="direct_runner:/hb", now=NOW),
        heartbeat_orders=fact(1, source="direct_runner:/hb", now=NOW),
        ledger_open_exposure=fact(None,
                                  source="direct_runner_ledger:/l#exp",
                                  now=NOW),
        stuck_order=STUCK, venue_open_orders=VENUE_ORDERS_GAP)
    assert result["state"] == "unresolved_direct_unavailable"
    assert result["direct_flatness"] == "unavailable"


def test_resolution_nonflat_direct_wins_over_heartbeat_zero():
    result = resolve_ibkr_order_exposure(
        direct_positions=_positions([{"position": -1000.0}]),
        direct_portfolio=_positions([{"position": -1000.0}]),
        heartbeat_position=fact(0, source="direct_runner:/hb", now=NOW),
        heartbeat_orders=fact(1, source="direct_runner:/hb", now=NOW),
        ledger_open_exposure=fact(None,
                                  source="direct_runner_ledger:/l#exp",
                                  now=NOW),
        stuck_order=STUCK, venue_open_orders=VENUE_ORDERS_GAP)
    assert result["state"] == "venue_position_open"


def test_resolution_flat_and_reconciled():
    result = resolve_ibkr_order_exposure(
        direct_positions=_positions([]),
        direct_portfolio=_positions([]),
        heartbeat_position=fact(0, source="direct_runner:/hb", now=NOW),
        heartbeat_orders=fact(0, source="direct_runner:/hb", now=NOW),
        ledger_open_exposure=fact(None,
                                  source="direct_runner_ledger:/l#exp",
                                  now=NOW),
        stuck_order=unavailable("no_protective_cancel_history",
                                source_attempted="ledger"),
        venue_open_orders=VENUE_ORDERS_GAP)
    assert result["state"] == "flat_and_reconciled"


# --------------------------------------------------------------------------
# TWS probe payload -> typed facts (an incomplete request is never "flat")
# --------------------------------------------------------------------------

TWS_SRC = "direct_venue:tws://127.0.0.1:7497 clientId=179 readonly"


def _probe(**completeness):
    complete = {"positions_request_verified": True,
                "executions_request_verified": True,
                "account_updates_received": True}
    complete.update(completeness)
    return {"positions": [], "portfolio": [], "fills_today": [],
            "account_fingerprint": "fp", "paper_account_prefix": True,
            "managed_account_count": 1, "account_values": {},
            "server_version": 176, "evidence_completeness": complete}


def test_typed_tws_facts_types_a_complete_probe():
    typed = typed_tws_facts(_probe(), "", TWS_SRC, "fp", NOW)
    assert typed["positions"]["value"] == []
    assert typed["account"]["value"]["matches_expected"] is True
    assert typed["fills_today"]["fresh"] is True


def test_typed_tws_facts_refuses_empty_positions_from_timed_out_request():
    typed = typed_tws_facts(_probe(positions_request_verified=False), "",
                            TWS_SRC, "fp", NOW)
    assert is_unavailable(typed["positions"])
    assert typed["positions"]["unavailable"] == \
        "positions_request_incomplete"
    assert not is_unavailable(typed["portfolio"])


def test_incomplete_position_request_never_produces_a_flat_verdict():
    typed = typed_tws_facts(
        _probe(positions_request_verified=False,
               account_updates_received=False), "", TWS_SRC, "fp", NOW)
    result = resolve_ibkr_order_exposure(
        direct_positions=typed["positions"],
        direct_portfolio=typed["portfolio"],
        heartbeat_position=fact(0, source="direct_runner:/hb", now=NOW),
        heartbeat_orders=fact(1, source="direct_runner:/hb", now=NOW),
        ledger_open_exposure=fact({"exposure_id": "e"},
                                  source="direct_runner_ledger:/l", now=NOW),
        stuck_order=STUCK, venue_open_orders=VENUE_ORDERS_GAP)
    assert result["state"] == "unresolved_direct_unavailable"
    assert result["direct_flatness"] == "unavailable"


def test_typed_tws_facts_types_a_failed_probe():
    typed = typed_tws_facts(None, "TimeoutError: connect", TWS_SRC, "fp",
                            NOW)
    assert all(is_unavailable(entry) for entry in typed.values())
    assert typed["account"]["unavailable"] == "tws_probe_unavailable"


def test_resolution_reports_direct_row_counts():
    result = resolve_ibkr_order_exposure(
        direct_positions=_positions([{"position": 0.0}]),
        direct_portfolio=_positions([]),
        heartbeat_position=fact(0, source="direct_runner:/hb", now=NOW),
        heartbeat_orders=fact(1, source="direct_runner:/hb", now=NOW),
        ledger_open_exposure=fact(None, source="direct_runner_ledger:/l",
                                  now=NOW),
        stuck_order=STUCK, venue_open_orders=VENUE_ORDERS_GAP)
    assert result["direct_position_rows"] == 1
    assert result["direct_portfolio_rows"] == 0


# --------------------------------------------------------------------------
# ledger readers against isolated temp stores
# --------------------------------------------------------------------------

def _seed_ledger(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE l1_effects(effect_id TEXT, idempotency_key TEXT,
      kind TEXT, state TEXT, order_ids_json TEXT, created_at TEXT,
      updated_at TEXT);
    CREATE TABLE l1_broker_facts(seq INTEGER PRIMARY KEY, effect_id TEXT,
      recorded_at TEXT, fact_kind TEXT, fact_json TEXT);
    CREATE TABLE live_model_inferences(input_sha256 TEXT, session_id TEXT,
      observed_at TEXT, last_closed_bar TEXT, action TEXT,
      probability_up REAL, output_sha256 TEXT, payload_json TEXT);
    CREATE TABLE due_bar_decisions(bar_close TEXT, decided_at TEXT,
      action TEXT, outcome TEXT, reason TEXT, decision_id TEXT,
      artifact_sha256 TEXT, input_sha256 TEXT);
    CREATE TABLE decision_deferrals(id INTEGER PRIMARY KEY, note TEXT);
    """)
    con.execute(
        "INSERT INTO l1_effects VALUES ('l1e-test', 'k', 'bracket_entry',"
        " 'acknowledged', '[4241, 4242, 4243]',"
        " '2026-08-14T18:40:42+00:00', '2026-08-14T18:40:43+00:00')")
    con.execute(
        "INSERT INTO l1_broker_facts(effect_id,recorded_at,fact_kind,"
        "fact_json) VALUES ('l1e-test', '2026-08-14T18:40:42+00:00',"
        " 'call_result', ?)",
        (json.dumps({"leg": "take_profit", "orderId": 4242,
                     "action": "BUY", "orderType": "LMT",
                     "lmtPrice": 1.11111, "auxPrice": None, "tif": "GTC",
                     "totalQuantity": 1000.0}),))
    for stamp in ("2026-08-15T08:00:00+00:00", "2026-08-15T09:30:00+00:00"):
        con.execute(
            "INSERT INTO l1_broker_facts(effect_id,recorded_at,fact_kind,"
            "fact_json) VALUES ('l1e-test', ?,"
            " 'protective_exit_cancel_attempt', ?)",
            (stamp, json.dumps({"orderId": 4242})))
        con.execute(
            "INSERT INTO l1_broker_facts(effect_id,recorded_at,fact_kind,"
            "fact_json) VALUES ('l1e-test', ?,"
            " 'protective_exit_cancel_result', ?)",
            (stamp, json.dumps({"orderId": 4242, "status": "Cancelled"})))
    con.execute(
        "INSERT INTO live_model_inferences VALUES ('i'||'n', 's1',"
        " '2026-08-14T18:40:41+00:00', '2026-08-14T12:00:00+00:00',"
        " 'short', 0.4998, 'o', '{}')")
    con.execute(
        "INSERT INTO due_bar_decisions VALUES"
        " ('2026-08-14T12:00:00+00:00', '2026-08-14T18:40:42+00:00',"
        " 'short', 'would_be_order', NULL,"
        " 'm1:2026-08-14T12:00:00+00:00', 'a', 'i')")
    con.commit()
    con.close()


def test_stuck_order_identified_from_isolated_ledger(tmp_path):
    ledger = tmp_path / "ledger.sqlite"
    _seed_ledger(ledger)
    entry = ibkr_stuck_order_from_ledger(ledger, NOW)
    assert not is_unavailable(entry)
    value = entry["value"]
    assert value["order_id"] == 4242
    assert value["leg_identity"]["leg"] == "take_profit"
    assert value["cancel_attempts"] == 2
    assert value["last_runner_observed_cancel_status"] == "Cancelled"
    assert value["last_cancel_attempt"] == "2026-08-15T09:30:00+00:00"


def test_stuck_order_unavailable_when_no_cancel_history(tmp_path):
    ledger = tmp_path / "empty.sqlite"
    con = sqlite3.connect(ledger)
    con.executescript("""
    CREATE TABLE l1_effects(effect_id TEXT, state TEXT, created_at TEXT);
    CREATE TABLE l1_broker_facts(seq INTEGER PRIMARY KEY, effect_id TEXT,
      recorded_at TEXT, fact_kind TEXT, fact_json TEXT);
    """)
    con.commit(); con.close()
    entry = ibkr_stuck_order_from_ledger(ledger, NOW)
    assert is_unavailable(entry)
    assert entry["unavailable"] == "no_protective_cancel_history"


def test_stuck_order_unreadable_ledger_is_typed(tmp_path):
    entry = ibkr_stuck_order_from_ledger(tmp_path / "absent.sqlite", NOW)
    assert is_unavailable(entry)
    assert entry["unavailable"] == "ledger_unreadable"


def test_decision_block_due_identity_matches_recorded(tmp_path):
    ledger = tmp_path / "ledger.sqlite"
    _seed_ledger(ledger)
    block = decision_block(ledger, "m1", "4h", NOW)
    assert block["last_closed_input_bar"]["value"] \
        == "2026-08-14T12:00:00+00:00"
    assert block["due_decision_identity"]["value"] \
        == "m1:2026-08-14T12:00:00+00:00"
    assert block["decision_current_for_last_closed_bar"]["value"] is True
    # stale by budget (bar is old vs NOW) must be typed, not hidden
    assert block["last_closed_input_bar"]["fresh"] is False


def test_decision_block_empty_ledger_types_unavailable(tmp_path):
    ledger = tmp_path / "bare.sqlite"
    con = sqlite3.connect(ledger)
    con.executescript("""
    CREATE TABLE live_model_inferences(observed_at TEXT,
      last_closed_bar TEXT, action TEXT, probability_up REAL,
      input_sha256 TEXT);
    CREATE TABLE due_bar_decisions(bar_close TEXT, decided_at TEXT,
      action TEXT, outcome TEXT, reason TEXT, decision_id TEXT,
      artifact_sha256 TEXT, input_sha256 TEXT);
    CREATE TABLE decision_deferrals(id INTEGER PRIMARY KEY);
    """)
    con.commit(); con.close()
    block = decision_block(ledger, "m1", "4h", NOW)
    assert block["last_closed_input_bar"]["unavailable"] \
        == "no_inference_rows"
    assert block["last_recorded_decision"]["unavailable"] \
        == "no_decision_rows"


# --------------------------------------------------------------------------
# rendering (no leak, honest unavailable cells)
# --------------------------------------------------------------------------

def _minimal_seat(**overrides):
    seat = {
        "venue": fact("v", source="direct_runner:/hb", now=NOW),
        "account": {
            "fingerprint_redacted": fact("ab" * 8,
                                         source="direct_runner:/hb",
                                         now=NOW),
            "fingerprint_matches_expected": fact(
                True, source="direct_runner:/hb", now=NOW),
            "environment_class": fact("paper", source="direct_runner:/hb",
                                      now=NOW),
            "write_enabled": unavailable("heartbeat_unreadable",
                                         source_attempted="direct:/hb"),
        },
        "identity": {
            "symbol": fact("SPY", source="direct_runner:/hb", now=NOW),
            "timeframe": fact("1d", source="direct_file:/cfg"),
            "model_id": fact("m", source="direct_runner:/hb", now=NOW),
            "artifact_sha256": fact("a" * 64, source="direct_runner:/hb",
                                    now=NOW),
            "config_sha256": fact("c" * 64, source="direct_runner:/hb",
                                  now=NOW),
            "code_revision": fact("deadbeef", source="git:x", now=NOW),
        },
        "model_artifact_join": manifest_join(
            heartbeat=HB, manifest=MF, manifest_path="/m.json",
            manifest_bytes_sha256="m" * 64,
            artifact_recomputed_sha256="a" * 64, now=NOW),
        "bars_and_decisions": {
            "last_closed_input_bar": fact(
                "2026-08-14T12:00:00+00:00", source="direct_runner:/l",
                observed_at="2026-08-14T18:40:41+00:00",
                budget_seconds=28800.0, now=NOW),
            "due_decision_identity": fact(
                "m:2026-08-14T12:00:00+00:00", source="direct_runner:/l",
                now=NOW),
            "last_recorded_decision": unavailable(
                "no_decision_rows", source_attempted="direct_runner:/l"),
        },
        "control_state": {"halt": fact("none", source="direct_runner:/l",
                                       now=NOW)},
        "runner_state": fact("monitoring", source="direct_runner:/hb",
                             now=NOW),
    }
    seat.update(overrides)
    return seat


def test_render_table_shows_unavailable_and_never_raw_accounts():
    inventory = assemble_inventory(
        {"seat_a": _minimal_seat()}, collector={"host": "t"}, now=NOW)
    table = render_table(inventory)
    assert "UNAVAILABLE(heartbeat_unreadable)" in table
    assert "UNAVAILABLE(no_decision_rows)" in table
    Redactor().assert_clean(table)      # no account-shaped token anywhere


def test_render_table_lists_typed_unavailable_index():
    inventory = assemble_inventory(
        {"seat_a": _minimal_seat()}, collector={"host": "t"}, now=NOW)
    table = render_table(inventory)
    assert "TYPED UNAVAILABLE FACTS:" in table
    assert "seat_a.account.write_enabled: heartbeat_unreadable" in table


def test_full_assembly_round_trip_serializes_clean():
    inventory = assemble_inventory(
        {"seat_a": _minimal_seat()}, collector={"host": "t"},
        ibkr_resolution=resolve_ibkr_order_exposure(
            direct_positions=_positions([{"position": 0.0}]),
            direct_portfolio=_positions([]),
            heartbeat_position=fact(0, source="direct_runner:/hb",
                                    now=NOW),
            heartbeat_orders=fact(1, source="direct_runner:/hb", now=NOW),
            ledger_open_exposure=fact({"exposure_id": "e"},
                                      source="direct_runner_ledger:/l",
                                      now=NOW),
            stuck_order=STUCK, venue_open_orders=VENUE_ORDERS_GAP),
        now=NOW)
    text = json.dumps(inventory, sort_keys=True, default=str)
    Redactor().assert_clean(text)
    paths = [e["path"] for e in inventory["unavailable_index"]]
    assert any(p.startswith("ibkr_order_exposure_resolution") for p in paths)
