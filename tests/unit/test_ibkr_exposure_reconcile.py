"""Terminal-exposure reconciliation (finding §6.2 of the 2026-08-04
packet): an L0 exposure left open by a fail-closed recovery is closed and
its reservation released ONLY under direct proof — terminal effect AND
broker route position flat. Nonzero positions, nonterminal effects and
unknown linkage all leave the row untouched."""
from __future__ import annotations

import pytest

from app.ibkr_l1_journal import L1ExecutionOlap
from app.ibkr_model_runner import reconcile_terminal_exposures

KEY = "usdcad-4h-linear-live-v1:2026-08-03T20:00:00+00:00:2026-08-04T00:00:00+00:00"


@pytest.fixture()
def olap(tmp_path):
    ledger = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    yield ledger
    ledger.close()


def seed(olap, *, effect_state="terminal_flat"):
    olap.reserve("rsv-1", KEY, "2026-08-04", 0.0001, 0.01, 0.01)
    olap.open_exposure(
        "exp-1", "oi-1", "rsv-1", "fx:USD/CAD", "USD.CAD", "ibkr_paper",
        "c0ff137a3cc1a363", -25000.0, 0.0001, 0.01, 0.01, "2026-08-04",
        "live_observed",
    )
    olap.create_effect("l1e-f4993c2dda8cdc2a", KEY, "bracket_entry", [])
    states = ["submitted_pending_ack", "acknowledged"]
    if effect_state not in states:
        states.append(effect_state)
    for state in states:
        olap.advance_effect("l1e-f4993c2dda8cdc2a", state)


def test_terminal_effect_and_flat_broker_close_exposure(olap):
    seed(olap)
    repaired = reconcile_terminal_exposures(
        olap, instrument="USD.CAD", route_position_units=0.0)
    assert len(repaired) == 1
    assert repaired[0]["exposure_id"] == "exp-1"
    assert olap.exposure_state("exp-1") == "closed"
    facts = olap.broker_facts("l1e-f4993c2dda8cdc2a", "l0_exposure_closed")
    assert len(facts) == 1
    # Idempotent: a second pass finds nothing open.
    assert reconcile_terminal_exposures(
        olap, instrument="USD.CAD", route_position_units=0.0) == []


def test_nonzero_broker_position_blocks_closure(olap):
    seed(olap)
    assert reconcile_terminal_exposures(
        olap, instrument="USD.CAD", route_position_units=-25000.0) == []
    assert olap.exposure_state("exp-1") == "open"


def test_nonterminal_effect_blocks_closure(olap):
    seed(olap, effect_state="acknowledged")
    assert reconcile_terminal_exposures(
        olap, instrument="USD.CAD", route_position_units=0.0) == []
    assert olap.exposure_state("exp-1") == "open"


def test_other_instrument_untouched(olap):
    seed(olap)
    assert reconcile_terminal_exposures(
        olap, instrument="EUR.USD", route_position_units=0.0) == []
    assert olap.exposure_state("exp-1") == "open"


def test_unknown_linkage_is_skipped_never_guessed(olap):
    olap.open_exposure(
        "exp-orphan", "oi-orphan", "rsv-none", "fx:USD/CAD", "USD.CAD",
        "ibkr_paper", "c0ff137a3cc1a363", -25000.0, 0.0001, 0.01, 0.01,
        "2026-08-04", "live_observed",
    )
    assert reconcile_terminal_exposures(
        olap, instrument="USD.CAD", route_position_units=0.0) == []
    assert olap.exposure_state("exp-orphan") == "open"


def test_released_reservation_does_not_block_closure(olap):
    seed(olap)
    olap.release("rsv-1", "consumed")
    repaired = reconcile_terminal_exposures(
        olap, instrument="USD.CAD", route_position_units=0.0)
    assert len(repaired) == 1
    assert olap.exposure_state("exp-1") == "closed"
