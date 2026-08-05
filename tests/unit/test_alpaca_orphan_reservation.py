"""AUD-F2-20260804-105: an ACTIVE reservation orphaned by the defect-era
retry sequence is released only under direct flat broker facts plus
immutable terminal lifecycle lineage — with restart, replay,
unknown-linkage, active-effect and concurrent-new-decision fixtures."""
from __future__ import annotations

import pytest

from app.alpaca_l1 import AlpacaL1Executor
from app.ibkr_l1_journal import L1ExecutionOlap

BAR = "2026-08-03T04:00:00+00:00"
MODEL = "spy-daily-linear-live-v1"
ORPHAN_KEY = f"{MODEL}:{BAR}:l0-reconciled-65674b98:2026-08-03T20:00:00+00:00"


class _Recon:
    """Bind only what reconcile_orphan_reservations touches."""

    def __init__(self, store):
        self.store = store

    reconcile_orphan_reservations = (
        AlpacaL1Executor.reconcile_orphan_reservations)


@pytest.fixture()
def store(tmp_path):
    ledger = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    yield ledger
    ledger.close()


def seed_orphan(store, *, effect_state="terminal_flat"):
    store.reserve("rsv-4adc1c4cbcb756ee", ORPHAN_KEY, "2026-08-04",
                  0.0001, 0.01, 0.01)
    sibling_key = f"{MODEL}:{BAR}:l0-reconciled-3a1b803d:2026-08-03T20:00:00+00:00"
    store.create_effect("alpaca-90c86e116676d016", sibling_key,
                        "alpaca_bracket_entry", [])
    states = ["submitted_pending_ack", "acknowledged"]
    if effect_state not in states:
        states.append(effect_state)
    for state in states:
        store.advance_effect("alpaca-90c86e116676d016", state)


def test_orphan_released_under_flat_and_terminal_lineage(store):
    seed_orphan(store)
    released = _Recon(store).reconcile_orphan_reservations(route_flat=True)
    assert [r["reservation_id"] for r in released] == [
        "rsv-4adc1c4cbcb756ee"]
    assert store.active_reservation_intents() == []
    facts = store.broker_facts("alpaca-90c86e116676d016",
                               "l0_reservation_released")
    assert len(facts) == 1
    # Restart/replay: a second pass finds nothing active (idempotent).
    assert _Recon(store).reconcile_orphan_reservations(
        route_flat=True) == []
    assert len(store.broker_facts("alpaca-90c86e116676d016",
                                  "l0_reservation_released")) == 1


def test_not_flat_blocks_release(store):
    seed_orphan(store)
    assert _Recon(store).reconcile_orphan_reservations(
        route_flat=False) == []
    assert len(store.active_reservation_intents()) == 1


def test_active_effect_blocks_release(store):
    seed_orphan(store, effect_state="acknowledged")
    assert _Recon(store).reconcile_orphan_reservations(
        route_flat=True) == []
    assert len(store.active_reservation_intents()) == 1


def test_unknown_linkage_is_skipped_never_guessed(store):
    store.reserve("rsv-no-effects", f"{MODEL}:2026-08-06T04:00:00+00:00:w",
                  "2026-08-06", 0.0001, 0.01, 0.01)
    assert _Recon(store).reconcile_orphan_reservations(
        route_flat=True) == []
    assert len(store.active_reservation_intents()) == 1


def test_concurrent_new_decision_untouched(store):
    """A brand-new bar's reservation (no effects yet) survives a
    reconcile pass that releases the old orphan."""
    seed_orphan(store)
    store.reserve("rsv-fresh-bar", f"{MODEL}:2026-08-05T04:00:00+00:00:w",
                  "2026-08-05", 0.0001, 0.01, 0.01)
    released = _Recon(store).reconcile_orphan_reservations(route_flat=True)
    assert [r["reservation_id"] for r in released] == [
        "rsv-4adc1c4cbcb756ee"]
    remaining = store.active_reservation_intents()
    assert [r["reservation_id"] for r in remaining] == ["rsv-fresh-bar"]


def test_open_exposure_blocks_release(store):
    seed_orphan(store)
    store.open_exposure(
        "exp-live", "oi-live", "rsv-4adc1c4cbcb756ee", "equity:SPY",
        "SPY", "alpaca_paper", "0123456789abcdef", -1.0, 0.0001, 0.01,
        0.01, "2026-08-04", "live_observed")
    assert _Recon(store).reconcile_orphan_reservations(
        route_flat=True) == []
    assert len(store.active_reservation_intents()) == 1
