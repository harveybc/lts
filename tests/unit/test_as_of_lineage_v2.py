"""Adversarial proofs for the v2 as-of lineage table.

Corrects AUD-F2-20260816-259 (one due decision could retain contradictory
as-of inputs) and AUD-F2-20260816-260 (loss of as-of evidence was neither
durable nor visible in health).

Every case below is an attack on the identity binding:

- same due bar, different input hash;
- changed artifact SHA-256;
- changed config SHA-256;
- account/route collision (same instrument + bar, different account
  fingerprint) — two REAL decisions that must never lend each other bars;
- crash between the two writes, in both places it can happen;
- exact byte-identical replay (idempotent, no incident);
- durable health degradation that survives a restart.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta

import pytest

from app import as_of_lineage
from app.as_of_lineage import AsOfLineageContradiction, AsOfLineageError
from app.demo_execution_service import DemoExecutionError
from app.ibkr_l1_journal import L1ExecutionOlap

BAR_CLOSE = "2026-08-10T20:00:00+00:00"
MODEL = "usdcad-4h-linear-live-v1"
DECISION_ID = f"{MODEL}:{BAR_CLOSE}"


def bars(n=60, start="2026-08-01T00:00:00+00:00", base=1.40):
    t0 = datetime.fromisoformat(start)
    return [{
        "time": (t0 + timedelta(hours=4 * i)).isoformat(),
        "open": base - 0.0005, "high": base + 0.001, "low": base - 0.001,
        "close": base + 0.002 * math.sin(i / 3.0),
        "volume": 1000.0 + 10 * (i % 7), "complete": True,
    } for i in range(n)]


def decision(**overrides):
    fact = {
        "venue": "ibkr_paper", "account_fingerprint": "c0ff137a3cc1a363",
        "asset_id": "fx:USD/CAD", "instrument": "USD.CAD", "timeframe": "4h",
        "bar_close": BAR_CLOSE, "decided_at": "2026-08-10T20:00:05+00:00",
        "feature_cutoff": BAR_CLOSE, "input_sha256": "a" * 64,
        "config_sha256": "b" * 64, "model_id": MODEL,
        "artifact_sha256": "c" * 64, "action": "long", "score": 0.61,
        "outcome": "would_be_order", "decision_id": DECISION_ID,
        "effect_or_command_id": "l1e-abc",
    }
    fact.update(overrides)
    return fact


def as_of(**overrides):
    fact = {
        "venue": "ibkr_paper", "account_fingerprint": "c0ff137a3cc1a363",
        "instrument": "USD.CAD", "decision_id": DECISION_ID,
        "model_id": MODEL, "artifact_sha256": "c" * 64,
        "config_sha256": "b" * 64, "timeframe": "4h", "bar_close": BAR_CLOSE,
        "input_sha256": "a" * 64,
        "feature_contract": "prediction_provider.closed_bars.linear.v1",
        "source": "ibkr_tws_historical_closed_bars", "bars": bars(),
    }
    fact.update(overrides)
    return fact


def open_reasons(store):
    return sorted(item["reason_code"] for item
                  in as_of_lineage.open_incidents(store.as_of_lineage_events()))


# ------------------------------------------------- identity is normalized

def test_v2_row_binds_the_normalized_due_decision_identity(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    result = store.record_due_bar_decision_with_as_of(decision(), as_of())
    assert result["as_of_appended"] is True
    assert result["as_of_state"] == "bound"
    row = store.as_of_rows(row_state="bound")[0]
    for field in (as_of_lineage.IDENTITY_FIELDS
                  + as_of_lineage.LINEAGE_FIELDS
                  + as_of_lineage.CONTENT_FIELDS):
        assert row[field], f"{field} is not bound into the v2 row"
    assert row["schema"] == "lts.as_of_input_bars.v2"
    assert len(store.as_of_bound_row(decision())["bars"]) == 60
    assert open_reasons(store) == []
    store.close()


def test_exact_replay_is_idempotent_and_lands_no_incident(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    store.record_due_bar_decision_with_as_of(decision(), as_of())
    for _restart in range(5):
        replay = store.record_due_bar_decision_with_as_of(decision(), as_of())
        assert replay["as_of_state"] == "idempotent"
        assert replay["as_of_appended"] is False
    assert len(store.as_of_rows(row_state="bound")) == 1
    assert open_reasons(store) == []
    assert store.as_of_lineage_health()["comparison_lineage_state"] \
        == "healthy"
    store.close()


# --------------------------------------------------------- 259 adversarial

@pytest.mark.parametrize("mutation, field", [
    ({"input_sha256": "d" * 64}, "input_sha256"),
    ({"artifact_sha256": "e" * 64}, "artifact_sha256"),
    ({"config_sha256": "f" * 64}, "config_sha256"),
    ({"timeframe": "1h"}, "timeframe"),
    ({"model_id": "other-model"}, "model_id"),
    ({"bars": bars(base=1.55)}, "bars_sha256"),
])
def test_same_identity_with_changed_lineage_or_bars_refuses(
        tmp_path, mutation, field):
    """Finding 259's exact counterexample: the SAME due decision must never
    end up with a second, contradictory as-of row."""
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    store.record_due_bar_decision_with_as_of(decision(), as_of())
    tampered = as_of(**mutation)
    with pytest.raises(AsOfLineageContradiction) as raised:
        # the tampered decision fact carries the same lineage as the as-of
        # fact, so the ONLY thing under test is the identity binding
        store.record_due_bar_decision_with_as_of(
            decision(**{k: v for k, v in mutation.items() if k != "bars"}),
            tampered)
    assert field in raised.value.diverging
    # exactly one row survives, and it is the original content
    rows = store.as_of_rows(row_state="bound")
    assert len(rows) == 1
    assert rows[0]["input_sha256"] == "a" * 64
    assert rows[0]["artifact_sha256"] == "c" * 64
    assert rows[0]["config_sha256"] == "b" * 64
    # exactly ONE durable incident, deduplicated across repeated attacks
    for _retry in range(3):
        with pytest.raises(AsOfLineageContradiction):
            store.record_due_bar_decision_with_as_of(
                decision(**{k: v for k, v in mutation.items()
                            if k != "bars"}), tampered)
    incidents = as_of_lineage.open_incidents(store.as_of_lineage_events())
    assert len(incidents) == 1
    assert incidents[0]["reason_code"] == "as_of_lineage_contradiction"
    store.close()


def test_contradiction_never_blocks_the_trading_decision_fact(tmp_path):
    """Trading safety is unchanged: the C1 decision fact still lands even
    when the as-of evidence is refused."""
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    store.record_due_bar_decision_with_as_of(
        decision(outcome="deferred", reason="halted:hold",
                 effect_or_command_id=None), as_of())
    outcome = as_of_lineage.persist_due_bar(
        store, decision(), as_of(bars=bars(base=1.77)))
    assert outcome["ok"] is False
    assert outcome["reason"] == "as_of_lineage_contradiction"
    assert outcome["decision_appended"] is True
    assert store.due_bar_decisions()[0]["outcome"] == "would_be_order"
    store.close()


def test_account_route_collision_never_shares_bars(tmp_path):
    """Same venue, instrument, model, timeframe and bar — two accounts.

    v1 keyed on (venue, model, timeframe, bar_close, input_sha256), so the
    account was INVISIBLE to it: the second route either collided with the
    first row or silently reused its bars. v2 keys on the account
    fingerprint, so the two routes are two identities that coexist and can
    never be served each other's bars.
    """
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    account_a, account_b = "aaaa000000000001", "bbbb000000000002"
    a_fact = as_of(account_fingerprint=account_a)
    b_fact = as_of(account_fingerprint=account_b, bars=bars(base=1.62))

    assert store.record_as_of_pending(a_fact)["appended"] is True
    assert store.record_as_of_pending(b_fact)["appended"] is True
    rows = store.as_of_rows(row_state="pending")
    assert len(rows) == 2
    assert len({row["identity_sha256"] for row in rows}) == 2
    # neither route contaminated the other, and this is NOT an incident:
    # two accounts on one instrument are two legitimate due decisions
    assert open_reasons(store) == []

    # the same collision under v1: the account is not in the key at all, so
    # the second route's DIFFERENT bars hit the first route's row
    v1 = {"venue": "ibkr_paper", "model_id": MODEL, "timeframe": "4h",
          "bar_close": BAR_CLOSE, "input_sha256": "a" * 64,
          "feature_contract": "fc.v1", "source": "s", "bars": bars()}
    assert store.record_as_of_input_bars(v1) is True
    with pytest.raises(DemoExecutionError, match="immutable"):
        store.record_as_of_input_bars({**v1, "bars": bars(base=1.62)})

    # bind account A's decision; account B's identity stays evidence-free
    store.record_due_bar_decision_with_as_of(
        decision(account_fingerprint=account_a), a_fact)
    a_bars = store.as_of_bound_row(
        decision(account_fingerprint=account_a))["bars"]
    assert a_bars[0]["close"] == bars()[0]["close"]
    assert store.as_of_bound_row(
        decision(account_fingerprint=account_b)) is None
    store.close()


def test_as_of_fact_must_describe_this_due_decision(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    with pytest.raises(AsOfLineageError, match="does not describe"):
        store.record_due_bar_decision_with_as_of(
            decision(), as_of(decision_id="someone-elses:bar"))
    assert store.as_of_rows() == []
    assert store.due_bar_decisions() == []
    store.close()


def test_incomplete_identity_never_reaches_the_table(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    for field in as_of_lineage.IDENTITY_FIELDS + as_of_lineage.LINEAGE_FIELDS:
        with pytest.raises(AsOfLineageError, match="missing"):
            store.record_as_of_pending(as_of(**{field: None}))
    assert store.as_of_rows() == []
    store.close()


# ------------------------------------------------------- crash / recovery

def test_crash_inside_the_atomic_write_leaves_no_orphan(tmp_path):
    """The due-decision fact and the as-of bars are ONE logical operation:
    a crash inside it rolls both back — never a half-written pair."""
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    store.record_as_of_pending(as_of())

    real_insert = store._insert_as_of

    def explode(normalized, *, row_state, origin):
        if row_state == as_of_lineage.BOUND:
            raise sqlite3.OperationalError("disk I/O error (injected crash)")
        return real_insert(normalized, row_state=row_state, origin=origin)

    store._insert_as_of = explode
    outcome = as_of_lineage.persist_due_bar(store, decision(), as_of())
    store._insert_as_of = real_insert

    assert outcome["ok"] is False
    assert outcome["reason"] == "as_of_persistence_failure"
    # BOTH writes rolled back: no bound row AND no decision fact
    assert store.as_of_rows(row_state="bound") == []
    assert store.due_bar_decisions() == []
    # the loss is durable, not a scrolled stdout line
    assert open_reasons(store) == ["as_of_persistence_failure"]
    assert store.as_of_lineage_health()["comparison_lineage_state"] \
        == "degraded"

    # the retried tick completes the same identity and heals the incident
    assert as_of_lineage.persist_due_bar(store, decision(), as_of())["ok"]
    assert len(store.as_of_rows(row_state="bound")) == 1
    assert open_reasons(store) == []
    store.close()


def test_crash_between_pending_and_bind_recovers_without_orphan(tmp_path):
    """The other crash window: the pending linkage was written, then the
    process died before the decision fact. Recovery must bind it exactly
    once when the decision later exists, and never invent evidence when it
    does not."""
    db = tmp_path / "ledger.sqlite"
    store = L1ExecutionOlap(db)
    store.record_as_of_pending(as_of())
    store.close()                                     # crash

    store = L1ExecutionOlap(db)                       # restart
    # the decision never landed: the pending row stays typed, is NEVER
    # promoted, and is never readable as evidence
    recovery = store.resolve_pending_as_of()
    assert recovery["bound"] == []
    assert len(recovery["still_pending"]) == 1
    assert store.as_of_rows(row_state="bound") == []
    assert store.as_of_bound_row(decision()) is None
    assert open_reasons(store) == []

    # the same bar is re-decided; recovery binds the pending row once
    store.record_due_bar_decision(decision())
    first = store.resolve_pending_as_of()
    assert len(first["bound"]) == 1
    second = store.resolve_pending_as_of()
    assert second == {"bound": [], "contradicted": [], "still_pending": []}
    assert len(store.as_of_rows(row_state="bound")) == 1
    assert store.as_of_bound_row(decision()) is not None
    store.close()


def test_recovery_refuses_to_bind_a_contradicting_pending(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    store.record_as_of_pending(as_of())
    # the decision that actually landed used a different artifact
    store.record_due_bar_decision(decision(artifact_sha256="9" * 64))
    recovery = store.resolve_pending_as_of()
    assert recovery["bound"] == []
    assert len(recovery["contradicted"]) == 1
    assert store.as_of_rows(row_state="bound") == []
    assert open_reasons(store) == ["as_of_lineage_contradiction"]
    store.close()


def test_pending_write_refuses_before_the_risk_action(tmp_path):
    """A contradiction discovered at PENDING time is caught before anything
    is submitted, and is already durable."""
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    store.record_due_bar_decision_with_as_of(decision(), as_of())
    outcome = as_of_lineage.begin_as_of(store, as_of(bars=bars(base=1.9)))
    assert outcome["ok"] is False
    assert outcome["diverging"] == ["bars_sha256"]
    assert open_reasons(store) == ["as_of_lineage_contradiction"]
    store.close()


# ------------------------------------------------------- 260 durable health

def test_degraded_health_survives_a_restart_and_needs_an_explicit_close(
        tmp_path):
    db = tmp_path / "ledger.sqlite"
    store = L1ExecutionOlap(db)
    store.record_due_bar_decision_with_as_of(decision(), as_of())
    with pytest.raises(AsOfLineageContradiction):
        store.record_due_bar_decision_with_as_of(decision(),
                                                 as_of(bars=bars(base=1.5)))
    identity = store.as_of_rows(row_state="bound")[0]["identity_sha256"]
    store.close()

    store = L1ExecutionOlap(db)                       # restart
    health = store.as_of_lineage_health()
    assert health["comparison_lineage_state"] == "degraded"
    assert health["comparison_lineage_reason"] == "as_of_lineage_contradiction"
    assert health["comparison_lineage_open_incidents"] == 1
    assert health["comparison_lineage_last_incident"]["decision_id"] \
        == DECISION_ID
    # a contradiction is NOT self-healing: a later exact replay leaves it open
    store.record_due_bar_decision_with_as_of(decision(), as_of())
    assert store.as_of_lineage_health()["comparison_lineage_state"] \
        == "degraded"
    # only an explicit, recorded resolution closes it
    assert store.resolve_as_of_lineage_incident(
        identity_sha256=identity,
        reason_code=as_of_lineage.REASON_CONTRADICTION,
        note="owner reviewed the divergent window") is True
    assert store.as_of_lineage_health()["comparison_lineage_state"] \
        == "healthy"
    store.close()


def test_incident_is_durable_even_when_the_ledger_cannot_record_it(tmp_path):
    """Finding 260's worst case: the store itself is refusing writes. The
    sidecar journal beside the ledger still holds exactly one incident."""
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    fact = as_of()

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    store._insert_incident = broken
    incident = store.record_as_of_lineage_incident(
        reason_code=as_of_lineage.REASON_PERSISTENCE_FAILURE,
        identity=fact, detail={"phase": "bind"})
    assert incident["persisted"] is False
    assert incident["journaled"] is True
    assert store.as_of_journal_path.is_file()
    # deduplicated: a second identical failure adds no second incident
    store.record_as_of_lineage_incident(
        reason_code=as_of_lineage.REASON_PERSISTENCE_FAILURE,
        identity=fact, detail={"phase": "bind"})
    journal = as_of_lineage.read_journal(store.as_of_journal_path)
    assert len(journal) == 1
    assert as_of_lineage.health(journal)["comparison_lineage_state"] \
        == "degraded"
    store.close()


# -------------------------------------------------------------- migration

def test_v1_rows_migrate_forward_and_v1_is_never_destroyed(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    store.record_due_bar_decision(decision())
    assert store.record_as_of_input_bars({
        "venue": "ibkr_paper", "model_id": MODEL, "timeframe": "4h",
        "bar_close": BAR_CLOSE, "input_sha256": "a" * 64,
        "feature_contract": "prediction_provider.closed_bars.linear.v1",
        "source": "ibkr_tws_historical_closed_bars", "bars": bars(),
    }) is True

    report = store.migrate_as_of_v1_to_v2()
    assert report["migrated"] == 1
    assert report["unbindable"] == [] and report["contradictory"] == []
    row = store.as_of_bound_row(decision())
    assert row["origin"] == "v1_migration"
    assert row["account_fingerprint"] == "c0ff137a3cc1a363"
    assert len(row["bars"]) == 60
    # v1 is still readable, untouched
    assert len(store.legacy_as_of_input_bars()) == 1
    assert store.as_of_input_bars_row(
        venue="ibkr_paper", model_id=MODEL, timeframe="4h",
        bar_close=BAR_CLOSE, input_sha256="a" * 64) is not None
    # re-running the migration adds nothing
    assert store.migrate_as_of_v1_to_v2()["already_present"] == 1
    assert len(store.as_of_rows(row_state="bound")) == 1
    store.close()


def test_v1_row_without_a_binding_decision_is_reported_not_guessed(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    store.record_as_of_input_bars({
        "venue": "ibkr_paper", "model_id": MODEL, "timeframe": "4h",
        "bar_close": BAR_CLOSE, "input_sha256": "a" * 64,
        "feature_contract": "fc.v1", "source": "s", "bars": bars(),
    })
    report = store.migrate_as_of_v1_to_v2()
    assert report["migrated"] == 0
    assert report["unbindable"][0]["candidate_decisions"] == 0
    assert store.as_of_rows() == []
    store.close()


def test_legacy_v1_writer_still_refuses_divergent_content(tmp_path):
    store = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    fact = {"venue": "ibkr_paper", "model_id": MODEL, "timeframe": "4h",
            "bar_close": BAR_CLOSE, "input_sha256": "a" * 64,
            "feature_contract": "fc.v1", "source": "s", "bars": bars()}
    assert store.record_as_of_input_bars(fact) is True
    assert store.record_as_of_input_bars(dict(fact)) is False
    with pytest.raises(DemoExecutionError, match="immutable"):
        store.record_as_of_input_bars({**fact, "bars": bars(base=1.5)})
    store.close()
