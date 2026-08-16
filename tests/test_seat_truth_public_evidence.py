"""Fail-closed tests for the public/private split of WO1 seat evidence.

Correction of AUD-SEC-20260816-255.

The repository test at the bottom of this file is the gate: it walks the
committed public evidence directory and validates every artifact against
the STRUCTURED public schema (allowlist of fields and types) plus a
structural denylist of private field names. It is not a regex over prose
-- documents are parsed and walked. If anyone later commits an evidence
file carrying a balance, an equity/margin figure, a size, a price, a
ticket, a broker order id or an account/server fingerprint (or simply a
field nobody declared), the test fails.

No network, no broker session, no live store: the sanitizer tests drive
synthetic inventories that deliberately contain fake private values.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.seat_truth_inventory import (  # noqa: E402
    PRIVATE_SCHEMA,
    PrivateStoreError,
    assemble_inventory,
    fact,
    harden_evidence_tree,
    rebuild_public_evidence,
    resolve_ibkr_order_exposure,
    secure_mkdir,
    secure_write_text,
    unavailable,
    verify_private_mode,
)
from tools.seat_truth_public_evidence import (  # noqa: E402
    PUBLIC_EVIDENCE_DIRNAME,
    PUBLIC_SCHEMA,
    PUBLIC_DOCUMENT_SPEC,
    PublicEvidenceViolation,
    assert_public_document,
    iter_spec_field_names,
    key_is_forbidden,
    render_public_table,
    sanitize_inventory,
    scan_account_shapes,
    scan_forbidden_keys,
    scan_public_evidence_dir,
    validate_public_document,
)

NOW = datetime(2026, 8, 16, 2, 0, 0, tzinfo=timezone.utc)
PUBLIC_DIR = REPO_ROOT / PUBLIC_EVIDENCE_DIRNAME

# Fake private values planted in the synthetic inventory; none of them may
# survive into a public document.
FAKE_ACCOUNT_FINGERPRINT = "fp00112233445566"
FAKE_BALANCE = "918273.64"
FAKE_PRICE = 1.11111
FAKE_SIZE = -1000.0
FAKE_TICKET = 4242


PRIVATE_PACKET = {
    "sha256": "b" * 64,
    "byte_length": 4096,
    "path": "~/.local/state/lts/evidence/seat_truth/"
            "seat_truth_private_20260816T020000Z.json",
    "mode_verified": True,
}


def _hb(value, **kwargs):
    return fact(value, source="direct_runner:/state/hb.json",
                observed_at=NOW.isoformat(), budget_seconds=300.0, now=NOW,
                **kwargs)


def _venue(value, **kwargs):
    return fact(value, source="direct_venue:https://paper-api.example/v2",
                observed_at=NOW.isoformat(), budget_seconds=120.0, now=NOW,
                **kwargs)


def _ledger(value, **kwargs):
    return fact(value, source="direct_runner_ledger:/state/x.sqlite#rows",
                now=NOW, **kwargs)


def _private_seat() -> dict:
    """A seat carrying every private field the finding names."""
    return {
        "venue": _hb("alpaca_paper"),
        "account": {
            "fingerprint_redacted": _hb(FAKE_ACCOUNT_FINGERPRINT),
            "fingerprint_matches_expected": _hb(True),
            "environment_class": _hb("paper"),
            "binding_verified_by_runner": _hb(True),
            "write_enabled": _hb(True),
            "direct_venue_fingerprint_matches": _venue(True),
        },
        "identity": {
            "symbol": _hb("SPY"),
            "timeframe": fact("1d", source="direct_file:/cfg.json"),
            "model_id": _hb("spy-daily-linear-live-v1"),
            "artifact_sha256": _hb("a" * 64),
            "config_sha256": _hb("c" * 64),
            "code_revision": fact("d" * 40, source="git:/repo", now=NOW),
            "execution_tier": fact("paper_l1_execution",
                                   source="direct_file:/cfg.json", now=NOW),
            "strategy": fact({"stop_fraction": 0.004},
                             source="direct_file:/cfg.json", now=NOW),
        },
        "model_artifact_join": {
            "join": fact({"checks": {
                "heartbeat_artifact_equals_manifest_artifact": True,
                "manifest_artifact_equals_file_bytes": True,
                "heartbeat_manifest_sha_equals_manifest_bytes": True,
                "heartbeat_config_equals_manifest_config": "unavailable"},
                "gaps": ["heartbeat_config_equals_manifest_config"]},
                source="direct_file:/manifest.json", now=NOW),
            "eligibility": fact({"live_inference_eligible": False,
                                 "live_execution_eligible": False,
                                 "research_validated": True},
                                source="direct_file:/manifest.json",
                                now=NOW),
            "proof_notes": ["F-P1-04 gap remains: ..."],
        },
        "bars_and_decisions": {
            "last_closed_input_bar": _ledger("2026-08-14T12:00:00+00:00"),
            "due_decision_identity": _ledger(
                "spy-daily-linear-live-v1:2026-08-14T12:00:00+00:00"),
            "last_recorded_decision": _ledger({
                "decision_id": "spy:2026-08-14T12:00:00+00:00",
                "bar_close": "2026-08-14T12:00:00+00:00",
                "action": "short", "outcome": "would_be_order",
                "reason": None}),
            "decision_current_for_last_closed_bar": _ledger(True),
        },
        "broker_state": {
            "heartbeat_positions": _hb(0),
            "heartbeat_orders": _hb(1),
            "open_exposure": _ledger({"exposure_id": "exp-1",
                                      "instrument": "SPY",
                                      "units_open": FAKE_SIZE,
                                      "state": "open",
                                      "opened_at": "2026-08-14T18:40:00Z"}),
            "latest_effect": _ledger({"effect_id": "l1e-1",
                                      "order_ids_json": "[4241, 4242]",
                                      "state": "acknowledged"}),
            "direct_venue": {
                "endpoint_class": _venue("paper"),
                "account_status": _venue({
                    "status": "ACTIVE", "trading_blocked": False,
                    "account_blocked": False, "equity": FAKE_BALANCE,
                    "currency": "USD"}),
                "positions": _venue([{
                    "symbol": "SPY", "qty": "3", "side": "long",
                    "avg_entry_price": "642.11",
                    "market_value": "1926.33",
                    "unrealized_pl": "-4.02"}]),
                "open_orders": _venue([{
                    "symbol": "SPY", "side": "sell", "type": "limit",
                    "qty": "3", "limit_price": str(FAKE_PRICE),
                    "status": "held"}]),
                "native_protection_evidence": _venue([
                    {"symbol": "SPY", "type": "limit",
                     "limit_price": "651.00", "status": "held"},
                    {"symbol": "SPY", "type": "stop",
                     "stop_price": "630.00", "status": "held"}]),
                "fills_today": _venue([]),
            },
        },
        "control_state": {
            "halt": _ledger("none"),
            "last_resume": _ledger({"at": "2026-08-15T18:00:00Z",
                                    "effect_id": "l1e-1",
                                    "evidence_sha256": "e" * 64}),
        },
        "runner_state": _hb("monitoring"),
    }


def _private_mt5_seat() -> dict:
    return {
        "venue": _hb("mt5_demo"),
        "account": {
            "fingerprint_redacted": _hb("fp0011223344556677889900"),
            "fingerprint_matches_expected": _hb(True),
            "bridge_fingerprint_matches_heartbeat": _ledger(True),
            "environment_class": _ledger("demo"),
            "write_enabled": _ledger({"connected": 1, "trade_allowed": 1}),
        },
        "identity": {
            "symbol": _hb("ETHUSD"),
            "timeframe": fact("4h", source="direct_file:/cfg.json"),
            "model_id": _hb("ethusdt-4h-linear-live-v1"),
            "artifact_sha256": _hb("f" * 64),
            "config_sha256": _hb("9" * 64),
            "adapter_version": _ledger("mt5-bridge-adapter-1.4"),
        },
        "model_artifact_join": {
            "join": unavailable("manifest_unreadable",
                                source_attempted="direct_file:/m.json"),
        },
        "bars_and_decisions": {
            "last_closed_input_bar": _ledger({
                "bar_time": "2026-08-16 00:00:00", "open": 4711.2,
                "high": 4780.0, "low": 4700.1, "close": 4750.5,
                "volume": 1234}),
            "due_decision_identity": _ledger("eth:2026-08-16T00:00:00+00:00"),
            "last_recorded_decision": unavailable(
                "no_decision_rows",
                source_attempted="direct_runner_ledger:/x#due"),
        },
        "broker_state": {
            "heartbeat_positions": _hb(1),
            "heartbeat_orders": _hb(0),
            "account_snapshot": _ledger({
                "currency": "USD", "balance": FAKE_BALANCE,
                "equity": FAKE_BALANCE, "margin": "100.5",
                "free_margin": "900.25", "positions_total": 1,
                "orders_total": 0}),
            "positions": _ledger([{
                "ticket": FAKE_TICKET, "symbol": "ETHUSD", "side": "buy",
                "volume": 7.25, "price_open": FAKE_PRICE,
                "stop_loss": 4600.0, "take_profit": 4900.0,
                "profit": -12.5}]),
            "pending_orders": _ledger([]),
            "native_protection_evidence": _ledger([{
                "ticket": FAKE_TICKET, "stop_loss": 4600.0,
                "take_profit": 4900.0}]),
        },
        "control_state": {
            "halt": _ledger("no_halt_row_recorded"),
            "kill_switch_trade_allowed": _ledger(1),
        },
        "runner_state": _hb("monitoring"),
        "fleet_readable_evidence": _ledger(
            "/home/someone/.local/state/lts/evidence/mt5-direct/x.json"),
    }


def _private_resolution() -> dict:
    return resolve_ibkr_order_exposure(
        direct_positions=_venue([]),
        direct_portfolio=_venue([]),
        heartbeat_position=_hb(0),
        heartbeat_orders=_hb(1),
        ledger_open_exposure=_ledger({"exposure_id": "exp-1",
                                      "units_open": FAKE_SIZE}),
        stuck_order=_ledger({
            "order_id": FAKE_TICKET, "cancel_attempts": 44,
            "leg_identity": {"leg": "take_profit", "action": "BUY",
                             "lmt_price": FAKE_PRICE,
                             "quantity": abs(FAKE_SIZE)}}),
        venue_open_orders=unavailable(
            "reqAllOpenOrders_rebinding_risk",
            detail="rebinding risk on clientId 78",
            source_attempted="direct_venue:tws://127.0.0.1:7497"))


def _private_inventory() -> dict:
    return assemble_inventory(
        {"alpaca_paper_spy_1d": _private_seat(),
         "mt5_demo_ethusd_4h": _private_mt5_seat()},
        collector={"host": "omega", "tool": "tools/seat_truth_inventory.py",
                   "tool_code_revision": "1" * 40, "python": "3.11.9",
                   "read_only": True},
        ibkr_resolution=_private_resolution(), now=NOW)


@pytest.fixture()
def public_document() -> dict:
    return sanitize_inventory(_private_inventory(),
                              private_packet=dict(PRIVATE_PACKET))


# --------------------------------------------------------------------------
# schema hygiene
# --------------------------------------------------------------------------

def test_public_schema_declares_no_denylisted_field_name():
    """The allowlist and the denylist must never contradict each other."""
    offenders = {name: key_is_forbidden(name)
                 for name in iter_spec_field_names(PUBLIC_DOCUMENT_SPEC)
                 if key_is_forbidden(name)}
    assert offenders == {}


@pytest.mark.parametrize("name", [
    "balance", "equity", "margin", "free_margin", "freeMargin",
    "avg_entry_price", "limit_price", "stop_price", "lmt_price", "aux_price",
    "market_value", "unrealized_pl", "qty", "filled_qty", "quantity",
    "volume", "units_open", "position", "positions", "ticket", "order_id",
    "orderId", "exec_id", "perm_id", "account_fingerprint",
    "server_fingerprint", "account_number", "api_key", "secret",
    "stop_loss", "take_profit", "net_liquidation", "available_funds",
    "buying_power", "value", "source", "detail", "note",
])
def test_denylist_catches_every_named_private_field(name):
    assert key_is_forbidden(name), name


# --------------------------------------------------------------------------
# sanitizer: private in, public-safe out
# --------------------------------------------------------------------------

def test_public_document_passes_both_gates(public_document):
    assert validate_public_document(public_document) == []
    assert scan_forbidden_keys(public_document) == []
    assert scan_account_shapes(public_document) == []
    assert public_document["schema"] == PUBLIC_SCHEMA


def test_sanitizer_omits_every_private_value(public_document):
    text = json.dumps(public_document, sort_keys=True)
    for planted in (FAKE_ACCOUNT_FINGERPRINT, FAKE_BALANCE, str(FAKE_PRICE),
                    str(FAKE_SIZE), "4750.5", "4600.0", "4900.0", "642.11",
                    "1926.33", "fp0011223344556677889900", "651.00",
                    "630.00", "7.25"):
        assert planted not in text, planted
    # the ticket/order id must not appear as a value either
    assert str(FAKE_TICKET) not in text


def test_sanitizer_carries_the_private_packet_digest_not_the_packet(
        public_document):
    packet = public_document["private_packet"]
    assert packet["sha256"] == PRIVATE_PACKET["sha256"]
    assert packet["path"].startswith("~/.local/state/lts/evidence/")
    assert packet["mode_verified"] is True
    assert "inventory" not in public_document
    assert "seats" in public_document


def test_sanitizer_keeps_the_facts_a_reviewer_needs(public_document):
    alpaca = public_document["seats"]["alpaca_paper_spy_1d"]
    # typed availability + freshness
    assert alpaca["availability"]["direct_venue_probe"]["state"] \
        == "available"
    assert alpaca["availability"]["direct_venue_probe"]["source_class"] \
        == "direct_venue"
    assert alpaca["availability"]["runner_heartbeat"]["fresh"] is True
    # hashes that joined their manifest, as hashes + booleans
    assert alpaca["identity"]["artifact_sha256"] == "a" * 64
    checks = alpaca["model_artifact_join"]["checks"]
    assert checks["heartbeat_artifact_equals_manifest_artifact"] is True
    assert checks["heartbeat_config_equals_manifest_config"] == "unavailable"
    assert alpaca["model_artifact_join"]["gap_count"] == 1
    assert alpaca["model_artifact_join"]["live_execution_eligible"] is False
    # counts, never sizes or prices
    assert alpaca["counts"]["venue_position_rows"] == 1
    assert alpaca["counts"]["venue_open_orders"] == 1
    assert alpaca["counts"]["venue_native_protection_orders"] == 2
    assert alpaca["counts"]["venue_fills_today"] == 0
    assert alpaca["counts"]["heartbeat_open_orders"] == 1
    assert alpaca["counts"]["ledger_open_exposure_rows_present"] is True
    # binding as a boolean, never as an identifier
    assert alpaca["account_binding"]["matches_expected_seat_binding"] is True
    assert alpaca["account_binding"]["direct_venue_binding_matches"] is True
    assert alpaca["account_binding"]["identifier_disclosed_in_git"] is False
    assert "fingerprint_redacted" not in json.dumps(alpaca)
    # decisions stay typed
    assert alpaca["bars_and_decisions"]["last_decision_action"] == "short"
    assert alpaca["bars_and_decisions"]["last_decision_outcome"] \
        == "would_be_order"
    assert alpaca["control_state"]["halt_active"] is False
    assert alpaca["control_state"]["runner_state"] == "monitoring"


def test_sanitizer_reduces_mt5_bar_to_its_timestamp(public_document):
    mt5 = public_document["seats"]["mt5_demo_ethusd_4h"]
    bars = mt5["bars_and_decisions"]
    assert bars["last_closed_input_bar_time"] == "2026-08-16T00:00:00"
    assert bars["last_decision"]["state"] == "unavailable"
    assert bars["last_decision"]["reason"] == "no_decision_rows"
    # one position, protection present, no size/price/ticket
    assert mt5["counts"]["venue_position_rows"] == 1
    assert mt5["counts"]["venue_native_protection_orders"] == 1
    assert mt5["counts"]["venue_reports_flat"] is False
    assert mt5["control_state"]["trade_allowed"] is True
    assert mt5["availability"]["manifest_join"]["state"] == "unavailable"


def test_sanitized_resolution_keeps_counts_and_drops_the_order(
        public_document):
    resolution = public_document["ibkr_order_exposure_resolution"]
    assert resolution["state"] \
        == "flat_at_venue__ledger_exposure_row_stale_open"
    assert resolution["direct_flatness"] is True
    assert resolution["venue_position_rows"] == 0
    assert resolution["runner_tracked_open_order_present"] is True
    assert resolution["runner_tracked_cancel_attempts"] == 44
    assert resolution["venue_scoped_open_orders"]["reason"] \
        == "reqAllOpenOrders_rebinding_risk"
    text = json.dumps(resolution)
    assert str(FAKE_TICKET) not in text and str(FAKE_PRICE) not in text
    # the typed unavailable's free-text detail never survives
    assert "rebinding risk on clientId" not in json.dumps(public_document)


def test_unavailable_index_keeps_paths_and_reason_codes_only(
        public_document):
    index = public_document["unavailable_index"]
    assert index, "typed unavailability must remain visible"
    assert public_document["unavailable_count"] == len(index)
    assert all(set(entry) == {"path", "reason"} for entry in index)


# --------------------------------------------------------------------------
# fail-closed behaviour of the gates
# --------------------------------------------------------------------------

def test_validator_rejects_an_undeclared_field(public_document):
    public_document["seats"]["alpaca_paper_spy_1d"]["counts"][
        "net_liquidation"] = 918273.64
    errors = validate_public_document(public_document)
    assert any("not in the public-evidence allowlist" in err
               for err in errors)


def test_validator_rejects_a_reintroduced_fact_envelope(public_document):
    public_document["seats"]["alpaca_paper_spy_1d"]["identity"][
        "symbol"] = {"value": "SPY", "source": "direct_runner:/hb"}
    assert validate_public_document(public_document)
    assert scan_forbidden_keys(public_document)


def test_validator_rejects_prose_in_a_token_field(public_document):
    public_document["seats"]["alpaca_paper_spy_1d"]["control_state"][
        "runner_state"] = ("monitoring; account DU1234567 equity 918273.64"
                           " at broker")
    errors = validate_public_document(public_document)
    assert any("token" in err for err in errors)
    assert scan_account_shapes(public_document)


def test_denylist_catches_a_private_field_nested_in_a_value():
    document = {"seats": {"a": {"counts": [{"free_margin": 1.0}]}}}
    errors = scan_forbidden_keys(document)
    assert errors and "free_margin" in errors[0]


def test_assert_public_document_raises_and_names_the_field(public_document):
    public_document["seats"]["alpaca_paper_spy_1d"]["counts"][
        "equity"] = "918273.64"
    with pytest.raises(PublicEvidenceViolation) as excinfo:
        assert_public_document(public_document)
    assert "equity" in str(excinfo.value)


def test_assert_public_document_accepts_the_clean_document(public_document):
    assert_public_document(public_document)          # must not raise


# --------------------------------------------------------------------------
# rendered table is a pure function of the validated document
# --------------------------------------------------------------------------

def test_public_table_is_derived_only_from_the_public_document(
        public_document):
    table = render_public_table(public_document)
    assert render_public_table(json.loads(json.dumps(public_document))) \
        == table
    assert PRIVATE_PACKET["sha256"] in table
    for planted in (FAKE_ACCOUNT_FINGERPRINT, FAKE_BALANCE, str(FAKE_PRICE),
                    str(FAKE_TICKET)):
        assert planted not in table
    assert "TYPED UNAVAILABLE FACTS:" in table
    assert "IBKR ORDER/EXPOSURE RESOLUTION" in table


# --------------------------------------------------------------------------
# private store: 0700 dirs / 0600 files, verified programmatically
# --------------------------------------------------------------------------

def test_secure_write_text_creates_a_0600_file(tmp_path):
    target = tmp_path / "store" / "packet.json"
    digest, length = secure_write_text(target, '{"a": 1}')
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert length == len('{"a": 1}\n')
    assert digest == __import__("hashlib").sha256(
        b'{"a": 1}\n').hexdigest()


def test_secure_mkdir_tightens_an_existing_loose_directory(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    secure_mkdir(loose)
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700


def test_verify_private_mode_refuses_a_world_readable_file(tmp_path):
    leaky = tmp_path / "leaky.json"
    leaky.write_text("{}")
    os.chmod(leaky, 0o644)
    with pytest.raises(PrivateStoreError):
        verify_private_mode(leaky)


def test_harden_evidence_tree_tightens_prior_world_readable_evidence(
        tmp_path):
    root = tmp_path / "evidence"
    (root / "mt5-direct").mkdir(parents=True)
    leaky = root / "mt5-direct" / "mt5_direct_evidence.json"
    leaky.write_text("{}")
    os.chmod(leaky, 0o644)
    os.chmod(root / "mt5-direct", 0o755)
    changed = harden_evidence_tree(root)
    assert len(changed) == 3                      # root, subdir, file
    assert stat.S_IMODE(leaky.stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "mt5-direct").stat().st_mode) == 0o700
    assert harden_evidence_tree(root) == []       # idempotent


def _write_private_packet(home: Path) -> Path:
    store = home / ".local/state/lts/evidence/seat_truth"
    packet_path = store / "seat_truth_private_20260816T020000Z.json"
    packet = {"schema": PRIVATE_SCHEMA, "schema_version": 1,
              "generated_at": NOW.isoformat(),
              "inventory": _private_inventory()}
    secure_write_text(packet_path, json.dumps(packet, sort_keys=True))
    return packet_path


def test_rebuild_refuses_a_packet_stored_outside_the_private_store(
        tmp_path, monkeypatch):
    """A summary may only point at the local 0600 evidence store."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    packet_path = tmp_path / "elsewhere/seat_truth_private_20260816T020000Z.json"
    packet = {"schema": PRIVATE_SCHEMA, "inventory": _private_inventory()}
    secure_write_text(packet_path, json.dumps(packet, sort_keys=True))
    with pytest.raises(PublicEvidenceViolation, match="private_packet.path"):
        rebuild_public_evidence(packet_path, tmp_path / "public")


def test_public_evidence_is_re_derivable_offline_from_the_private_packet(
        tmp_path, monkeypatch):
    """An auditor can rebuild the committed summary from the 0600 packet
    without touching any venue, and must get the same bytes."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    packet_path = _write_private_packet(home)
    out = tmp_path / "public"
    assert rebuild_public_evidence(packet_path, out) == 0
    document = json.loads(
        (out / "seat_truth_public_20260816T020000Z.json").read_text())
    assert validate_public_document(document) == []
    assert scan_forbidden_keys(document) == []
    assert document["private_packet"]["sha256"] == __import__(
        "hashlib").sha256(packet_path.read_bytes()).hexdigest()
    assert scan_public_evidence_dir(out) == []
    first = (out / "seat_truth_public_20260816T020000Z.json").read_bytes()
    rebuild_public_evidence(packet_path, out)                 # idempotent
    assert (out / "seat_truth_public_20260816T020000Z.json").read_bytes() \
        == first


# --------------------------------------------------------------------------
# THE REPOSITORY GATE: every committed evidence artifact must validate
# --------------------------------------------------------------------------

def test_committed_public_evidence_directory_exists_and_is_populated():
    assert PUBLIC_DIR.is_dir(), f"{PUBLIC_DIR} must hold the public packet"
    documents = sorted(PUBLIC_DIR.glob("seat_truth_public_*.json"))
    assert documents, "no committed public seat-truth packet"


def test_committed_public_evidence_passes_the_structural_scan():
    errors = scan_public_evidence_dir(PUBLIC_DIR)
    assert errors == []


@pytest.mark.parametrize(
    "document_path",
    sorted(PUBLIC_DIR.glob("seat_truth_public_*.json"))
    if PUBLIC_DIR.is_dir() else [],
    ids=lambda path: path.name)
def test_each_committed_document_validates(document_path):
    document = json.loads(document_path.read_text(encoding="utf-8"))
    assert validate_public_document(document) == []
    assert scan_forbidden_keys(document) == []
    assert scan_account_shapes(document) == []
    assert document["private_packet"]["path"].startswith(
        "~/.local/state/lts/evidence/")


def test_repository_scan_fails_closed_on_a_committed_private_field(
        tmp_path, public_document):
    public_document["seats"]["alpaca_paper_spy_1d"]["counts"][
        "free_margin"] = 900.25
    (tmp_path / "seat_truth_public_20260816T020000Z.json").write_text(
        json.dumps(public_document), encoding="utf-8")
    errors = scan_public_evidence_dir(tmp_path)
    assert any("free_margin" in err for err in errors)


def test_repository_scan_fails_closed_on_an_unknown_evidence_file(tmp_path):
    (tmp_path / "mt5_direct_evidence_20260816T014844Z.json").write_text(
        json.dumps({"payload": {"balance": 1.0}}), encoding="utf-8")
    errors = scan_public_evidence_dir(tmp_path)
    assert any("not an allowed public evidence artifact" in err
               for err in errors)


def test_repository_scan_fails_closed_on_a_hand_edited_table(
        tmp_path, public_document):
    (tmp_path / "seat_truth_public_20260816T020000Z.json").write_text(
        json.dumps(public_document), encoding="utf-8")
    (tmp_path / "seat_truth_public_table_20260816T020000Z.txt").write_text(
        render_public_table(public_document) + "\nequity 918273.64\n",
        encoding="utf-8")
    errors = scan_public_evidence_dir(tmp_path)
    assert any("not the rendering of its validated JSON sibling" in err
               for err in errors)


def test_repository_scan_accepts_a_freshly_generated_pair(
        tmp_path, public_document):
    (tmp_path / "seat_truth_public_20260816T020000Z.json").write_text(
        json.dumps(public_document, indent=1, sort_keys=True) + "\n",
        encoding="utf-8")
    (tmp_path / "seat_truth_public_table_20260816T020000Z.txt").write_text(
        render_public_table(public_document) + "\n", encoding="utf-8")
    assert scan_public_evidence_dir(tmp_path) == []
