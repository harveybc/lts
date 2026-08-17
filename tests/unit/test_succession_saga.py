"""AUD-F2-20260816-258: the promotion saga can never split authority.

The finding: the ledger commit (capability burn + successor session) and
the filesystem manifest switch were two independent steps. A crash in the
gap burned the capability, moved the active session to the successor and
left the manifest naming the incumbent — permanently, because the re-run
then selected against the CHANGED session with a SPENT capability.

Proven here, one test per ordered claim:

- a crash injected after EVERY boundary of the promotion (facts, capability
  validation, drain, refreshed facts, ledger prepare, capability burn,
  manifest temp write, manifest rename, final ledger state) leaves a state
  that resume either completes or explicitly rolls back — never a split
  authority nobody can reconcile;
- the resume uses NO second capability and never re-selects against the
  already-changed active session (proven by deleting the capability file
  from the owner store before resuming);
- finalization is idempotent, and so is rollback;
- rollback restores a COHERENT session/manifest pair, keeps the consumed
  capability SPENT and records why;
- while a saga is open, ``succession_pending`` reports the split state so
  every runner refuses new risk;
- a second promotion cannot start while a saga is open.

Everything runs against isolated temporary ledgers, manifests, capability
stores and owner keys. Nothing here opens a socket.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.champion_succession import (
    BOUNDARY_CAPABILITY_BURNED,
    BOUNDARY_CAPABILITY_VALIDATED,
    BOUNDARY_DRAIN,
    BOUNDARY_FACTS_OBSERVED,
    BOUNDARY_FACTS_REFRESHED,
    BOUNDARY_LEDGER_FINALIZED,
    BOUNDARY_LEDGER_PREPARED,
    BOUNDARY_MANIFEST_RENAMED,
    BOUNDARY_MANIFEST_TEMP_WRITTEN,
    PROMOTION_BOUNDARIES,
    candidate_activity_report,
    SAGA_ABORTED,
    SAGA_COMPLETED,
    SAGA_MANIFEST_PENDING,
    SAGA_PREPARED,
    SAGA_ROLLED_BACK,
    SuccessionError,
    candidate_shadow_replay,
    open_promotion_saga,
    outgoing_shadow_status,
    preflight_candidate,
    promote_paper_champion,
    resume_promotion_saga,
    succession_pending,
)
from app.ibkr_l1_journal import L1ExecutionOlap
from succession_fixtures import (
    FINGERPRINT,
    GOOD_STRATEGY,
    INCUMBENT,
    NOW,
    SESSIONS_SCHEMA,
    TARGET_MANIFEST,
    FakeVenue,
    capability_payload,
    make_candidate,
    make_seat,
    make_signer,
    seed_incumbent_due_bars,
    seed_incumbent_session,
    shadow_infer,
    sign,
    write_capability,
)


class InjectedCrash(RuntimeError):
    """A power cut, a SIGKILL, an OOM — modelled exactly."""


def crash_at(name: str):
    def boundary(reached: str) -> None:
        if reached == name:
            raise InjectedCrash(reached)
    return boundary


@pytest.fixture()
def world(tmp_path):
    """One seat, one incumbent, one signed capability, one candidate."""
    olap = L1ExecutionOlap(tmp_path / "ledger.sqlite")
    olap._con.executescript(SESSIONS_SCHEMA)
    seat = make_seat(tmp_path)
    candidate = make_candidate(tmp_path)
    key, signers = make_signer(tmp_path)
    store_dir = tmp_path / "promotion-store"
    store_dir.mkdir(mode=0o700)
    seed_incumbent_session(olap, seat)
    seed_incumbent_due_bars(olap, seat, count=3)
    compat = preflight_candidate(seat, candidate, now=NOW)
    shadow = candidate_shadow_replay(
        olap, seat=seat, candidate=candidate, infer=shadow_infer, now=NOW)
    shas = (compat["report_sha256"], shadow["report_sha256"])
    path = write_capability(store_dir, "promotion.json",
                            capability_payload(seat, candidate, shas))
    sign(key, path)
    state = {
        "olap": olap, "seat": seat, "candidate": candidate,
        "compat": compat, "shadow": shadow, "store_dir": store_dir,
        "signers": signers, "key": key, "capability_path": path,
    }
    yield state
    olap.close()


def _valid_activity_report(candidate, now):
    """Finding 269: default VALID activity evidence for saga tests —
    the campaign record whose activity-eligible checkpoint IS the
    candidate artifact."""
    import json as _json
    from pathlib import Path as _Path
    path = _Path(candidate.artifact_file).parent / (
        f"cell_record_{candidate.model_id}.json")
    if not path.exists():
        path.write_text(_json.dumps({
            "schema": "agent_multi.p1_difficulty_lr_cell_record.v2",
            "activity_status": "active",
            "promotion_eligible": True,
            "best_model_path": candidate.artifact_file,
            "best_model_sha256": candidate.artifact_sha256}))
    return candidate_activity_report(candidate, path, now=now)


def promote(world, *, venue=None, boundary=None, **overrides):
    kwargs = dict(
        store=world["olap"], venue=venue or FakeVenue(),
        seat=world["seat"], candidate=world["candidate"],
        compatibility_report=world["compat"],
        activity_report=_valid_activity_report(world["candidate"], NOW),
        shadow_report=world["shadow"],
        strategy_config=GOOD_STRATEGY,
        capability_store_dir=world["store_dir"],
        new_manifest=TARGET_MANIFEST,
        allowed_signers=world["signers"], require_root_pin=False,
        boundary=boundary, now=NOW)
    kwargs.update(overrides)
    return promote_paper_champion(**kwargs)


def manifest_model(seat) -> str:
    return json.loads(Path(seat.manifest_file).read_bytes())["model_id"]


def active_model(olap, seat) -> str | None:
    row = olap._con.execute(
        "SELECT model_id FROM live_model_sessions WHERE venue=? AND"
        " account_fingerprint=? AND symbol=? AND state='active'",
        (seat.venue, FINGERPRINT, seat.instrument)).fetchone()
    return None if row is None else row[0]


def capability_state(olap) -> tuple[int, str | None]:
    rows = olap._con.execute(
        "SELECT state FROM l1_capabilities").fetchall()
    return len(rows), (rows[0][0] if rows else None)


def seat_state(world) -> dict:
    olap, seat = world["olap"], world["seat"]
    saga = open_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument)
    consumed, state = capability_state(olap)
    return {
        "manifest": manifest_model(seat),
        "active": active_model(olap, seat),
        "saga_state": (saga or {}).get("state"),
        "capabilities_consumed": consumed,
        "capability_state": state,
    }


# ── the crash matrix ───────────────────────────────────────────────────

#: boundary -> (saga state observed after the crash, resume outcome)
CRASH_EXPECTATIONS = {
    BOUNDARY_FACTS_OBSERVED: (None, "no_saga"),
    BOUNDARY_CAPABILITY_VALIDATED: (None, "no_saga"),
    BOUNDARY_DRAIN: (None, "no_saga"),
    BOUNDARY_FACTS_REFRESHED: (None, "no_saga"),
    BOUNDARY_LEDGER_PREPARED: (SAGA_PREPARED, "aborted"),
    BOUNDARY_CAPABILITY_BURNED: (SAGA_MANIFEST_PENDING, "promoted"),
    BOUNDARY_MANIFEST_TEMP_WRITTEN: (SAGA_MANIFEST_PENDING, "promoted"),
    BOUNDARY_MANIFEST_RENAMED: (SAGA_MANIFEST_PENDING, "promoted"),
    BOUNDARY_LEDGER_FINALIZED: (None, "no_saga"),
}


def test_every_boundary_is_covered_by_the_crash_matrix():
    assert set(CRASH_EXPECTATIONS) == set(PROMOTION_BOUNDARIES)


@pytest.mark.parametrize("name", PROMOTION_BOUNDARIES)
def test_crash_after_boundary_completes_or_rolls_back(world, name):
    """One test per boundary: crash, observe, resume, prove coherence."""
    expected_saga, expected_resume = CRASH_EXPECTATIONS[name]
    olap, seat = world["olap"], world["seat"]
    with pytest.raises(InjectedCrash):
        promote(world, boundary=crash_at(name))
    after_crash = seat_state(world)
    assert after_crash["saga_state"] == expected_saga

    # Before the burn nothing can be half-done at all.
    if expected_saga in (None, SAGA_PREPARED):
        assert after_crash["manifest"] == INCUMBENT["model_id"] or (
            name == BOUNDARY_LEDGER_FINALIZED)
    if expected_saga == SAGA_PREPARED:
        assert after_crash["capabilities_consumed"] == 0
        assert after_crash["active"] == INCUMBENT["model_id"]

    if expected_saga == SAGA_MANIFEST_PENDING:
        # authority moved, and the split is DECLARED, not invisible
        assert after_crash["active"] == "challenger-linear-v2"
        assert after_crash["capabilities_consumed"] == 1
        pending = succession_pending(
            olap, venue=seat.venue, instrument=seat.instrument,
            account_fingerprint=FINGERPRINT)
        assert pending["state"] == SAGA_MANIFEST_PENDING
        assert pending["ledger_authority"] == "successor"
        assert pending["split_authority"] is (
            name != BOUNDARY_MANIFEST_RENAMED)

    # resume the SAME operation
    if expected_resume == "no_saga":
        if name == BOUNDARY_LEDGER_FINALIZED:
            # the ledger already recorded completion before the crash
            assert manifest_model(seat) == "challenger-linear-v2"
            assert active_model(olap, seat) == "challenger-linear-v2"
        with pytest.raises(SuccessionError, match="no open promotion saga"):
            resume_promotion_saga(
                olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
                instrument=seat.instrument, now=NOW)
        return

    result = resume_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument, now=NOW)
    assert result["state"] == expected_resume
    final = seat_state(world)
    assert final["saga_state"] is None            # nothing left open
    if expected_resume == "promoted":
        assert final["manifest"] == "challenger-linear-v2"
        assert final["active"] == "challenger-linear-v2"
    else:                                          # aborted
        assert final["manifest"] == INCUMBENT["model_id"]
        assert final["active"] == INCUMBENT["model_id"]
        # a capability that entered a saga is SPENT whatever the outcome
        assert final["capabilities_consumed"] == 1
        assert final["capability_state"] == "consumed_saga_aborted"


def test_resume_needs_no_second_capability(world):
    """The owner's signed file is gone; the interrupted promotion still
    completes, because it completes from its own durable saga row."""
    olap, seat = world["olap"], world["seat"]
    with pytest.raises(InjectedCrash):
        promote(world, boundary=crash_at(BOUNDARY_CAPABILITY_BURNED))
    world["capability_path"].unlink()
    Path(str(world["capability_path"]) + ".sig").unlink()
    result = resume_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument, now=NOW)
    assert result["state"] == "promoted"
    assert manifest_model(seat) == "challenger-linear-v2"


def test_resume_does_not_select_against_the_changed_session(world):
    """The old code re-ran selection and refused with 'different incumbent
    model' — the exact permanent split of finding 258."""
    olap, seat = world["olap"], world["seat"]
    with pytest.raises(InjectedCrash):
        promote(world, boundary=crash_at(BOUNDARY_CAPABILITY_BURNED))
    assert active_model(olap, seat) == "challenger-linear-v2"
    # a NAIVE re-run still refuses (the capability is spent) ...
    with pytest.raises(SuccessionError):
        promote(world)
    # ... but the saga resume completes the SAME operation.
    result = resume_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument, now=NOW)
    assert result["state"] == "promoted"
    assert result["saga_state"] == SAGA_COMPLETED


def test_finalization_is_idempotent(world):
    olap, seat = world["olap"], world["seat"]
    with pytest.raises(InjectedCrash):
        promote(world, boundary=crash_at(BOUNDARY_MANIFEST_RENAMED))
    first = resume_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument, action="complete", now=NOW)
    assert first["state"] == "promoted"
    manifest_bytes = Path(seat.manifest_file).read_bytes()
    with pytest.raises(SuccessionError, match="no open promotion saga"):
        resume_promotion_saga(
            olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
            instrument=seat.instrument, action="complete", now=NOW)
    # the completed seat is untouched by the repeated attempt
    assert Path(seat.manifest_file).read_bytes() == manifest_bytes
    rows = olap._con.execute(
        "SELECT COUNT(*) FROM live_model_sessions WHERE state='active'"
    ).fetchone()
    assert rows[0] == 1


def test_rollback_restores_a_coherent_pair_and_keeps_the_capability_spent(
        world):
    olap, seat = world["olap"], world["seat"]
    with pytest.raises(InjectedCrash):
        promote(world, boundary=crash_at(BOUNDARY_MANIFEST_RENAMED))
    assert manifest_model(seat) == "challenger-linear-v2"   # already flipped
    result = resume_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument, action="rollback",
        reason="owner aborted the succession", now=NOW)
    assert result["state"] == "rolled_back"
    assert result["manifest_rollback"]["restored"] is True
    final = seat_state(world)
    assert final["manifest"] == INCUMBENT["model_id"]
    assert final["active"] == INCUMBENT["model_id"]
    assert final["capability_state"] == "consumed_saga_rolled_back"
    assert final["saga_state"] is None
    # the reason is durable
    row = olap._con.execute(
        "SELECT outcome_reason, metadata_json FROM promotion_saga"
        " JOIN l1_capabilities USING (capability_sha256)").fetchone()
    assert row[0] == "owner aborted the succession"
    assert json.loads(row[1])["spent_reason"] == (
        "owner aborted the succession")
    # the displaced-champion shadow registration is withdrawn with it
    assert outgoing_shadow_status(
        olap, seat=seat, now=NOW)["state"] == "none"
    # exactly one active session, and it is the incumbent's original row
    rows = olap._con.execute(
        "SELECT session_id, state, ended_at FROM live_model_sessions"
        " ORDER BY state").fetchall()
    active = [row for row in rows if row[1] == "active"]
    assert len(active) == 1
    assert active[0][0] == "model-session-incumbent0000"
    assert active[0][2] is None


def test_rollback_is_idempotent(world):
    olap, seat = world["olap"], world["seat"]
    with pytest.raises(InjectedCrash):
        promote(world, boundary=crash_at(BOUNDARY_CAPABILITY_BURNED))
    first = resume_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument, action="rollback", reason="r", now=NOW)
    assert first["state"] == "rolled_back"
    with pytest.raises(SuccessionError, match="no open promotion saga"):
        resume_promotion_saga(
            olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
            instrument=seat.instrument, action="rollback", now=NOW)
    assert manifest_model(seat) == INCUMBENT["model_id"]
    assert active_model(olap, seat) == INCUMBENT["model_id"]


def test_a_completed_promotion_is_never_rolled_back(world):
    olap, seat = world["olap"], world["seat"]
    promote(world)
    with pytest.raises(SuccessionError, match="no open promotion saga"):
        resume_promotion_saga(
            olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
            instrument=seat.instrument, action="rollback", now=NOW)


def test_a_rolling_back_saga_is_never_completed_forward(world):
    olap, seat = world["olap"], world["seat"]
    with pytest.raises(InjectedCrash):
        promote(world, boundary=crash_at(BOUNDARY_CAPABILITY_BURNED))
    saga = open_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument)
    olap._con.execute(
        "UPDATE promotion_saga SET state='rolling_back' WHERE saga_id=?",
        (saga["saga_id"],))
    with pytest.raises(SuccessionError, match="only be finished as a"
                                              " rollback"):
        resume_promotion_saga(
            olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
            instrument=seat.instrument, action="complete", now=NOW)
    # and the rollback still finishes coherently
    result = resume_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument, action="rollback", now=NOW)
    assert result["state"] == "rolled_back"
    assert manifest_model(seat) == INCUMBENT["model_id"]


def test_second_promotion_refuses_while_a_saga_is_open(world):
    olap, seat = world["olap"], world["seat"]
    with pytest.raises(InjectedCrash):
        promote(world, boundary=crash_at(BOUNDARY_LEDGER_PREPARED))
    # even a freshly signed capability cannot start a second saga
    key, signers = world["key"], world["signers"]
    path = write_capability(
        world["store_dir"], "second.json",
        capability_payload(
            world["seat"], world["candidate"],
            (world["compat"]["report_sha256"],
             world["shadow"]["report_sha256"])))
    sign(key, path)
    with pytest.raises(SuccessionError, match="open promotion saga"):
        promote(world)


def test_pending_saga_reports_the_split_state_for_runners(world):
    olap, seat = world["olap"], world["seat"]
    assert succession_pending(
        olap, venue=seat.venue, instrument=seat.instrument) is None
    with pytest.raises(InjectedCrash):
        promote(world, boundary=crash_at(BOUNDARY_CAPABILITY_BURNED))
    pending = succession_pending(
        olap, venue=seat.venue, instrument=seat.instrument)
    assert pending["split_authority"] is True
    assert pending["ledger_authority"] == "successor"
    assert pending["manifest_points_at"] == "incumbent"
    assert pending["incumbent_model_id"] == INCUMBENT["model_id"]
    assert pending["successor_model_id"] == "challenger-linear-v2"
    resume_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument, now=NOW)
    assert succession_pending(
        olap, venue=seat.venue, instrument=seat.instrument) is None


def test_target_manifest_bytes_are_durable_and_verified(world):
    """The saga carries the EXACT bytes, so completion cannot drift."""
    olap, seat = world["olap"], world["seat"]
    with pytest.raises(InjectedCrash):
        promote(world, boundary=crash_at(BOUNDARY_CAPABILITY_BURNED))
    saga = open_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument)
    assert hashlib.sha256(
        saga["manifest_target_bytes"]).hexdigest() == (
        saga["manifest_target_sha256"])
    resume_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument, now=NOW)
    assert hashlib.sha256(
        Path(seat.manifest_file).read_bytes()).hexdigest() == (
        saga["manifest_target_sha256"])


def test_third_party_manifest_change_refuses_both_directions(world):
    olap, seat = world["olap"], world["seat"]
    with pytest.raises(InjectedCrash):
        promote(world, boundary=crash_at(BOUNDARY_CAPABILITY_BURNED))
    Path(seat.manifest_file).write_text('{"model_id": "someone-else"}\n')
    with pytest.raises(SuccessionError, match="third party changed it"):
        resume_promotion_saga(
            olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
            instrument=seat.instrument, action="complete", now=NOW)
    with pytest.raises(SuccessionError, match="rollback refused"):
        resume_promotion_saga(
            olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
            instrument=seat.instrument, action="rollback", now=NOW)
    # the saga stays open: an unreconciled seat is never silently closed
    assert open_promotion_saga(
        olap, venue=seat.venue, account_fingerprint=FINGERPRINT,
        instrument=seat.instrument) is not None


def test_post_drain_facts_authorize_the_switch_not_pre_drain_facts(world):
    """A position that appears only AFTER the drain must stop the switch;
    the pre-drain snapshot said the seat was flat."""
    venue = FakeVenue(after_drain={
        "positions": ({"symbol": "SPY", "qty": "5"},)})
    result = promote(world, venue=venue)
    assert result["state"] == "draining_for_succession"
    assert result["capability_consumed"] is False
    assert venue.observations >= 2                 # observed again AFTER
    assert seat_state(world)["saga_state"] is None
    assert manifest_model(world["seat"]) == INCUMBENT["model_id"]


def test_post_drain_balance_is_the_carried_balance(world):
    """The successor starts at the ACTUAL post-drain broker facts."""
    venue = FakeVenue(cash="100000.00", equity="100000.00",
                      after_drain={"cash": "98750.25",
                                   "equity": "98901.10"})
    result = promote(world, venue=venue)
    assert result["state"] == "promoted"
    assert result["audit"]["incoming"]["starting_balance"] == 98750.25
    row = world["olap"]._con.execute(
        "SELECT starting_balance, starting_equity FROM live_model_sessions"
        " WHERE model_id='challenger-linear-v2'").fetchone()
    assert row == (98750.25, 98901.10)


def test_a_venue_that_moves_account_between_observations_refuses(world):
    class MovingVenue(FakeVenue):
        def fetch_facts(self):
            facts = super().fetch_facts()
            if self.drained:
                return type(facts)(
                    **{**facts.__dict__,
                       "account_fingerprint": "0" * 16})
            return facts

    with pytest.raises(SuccessionError, match="account changed"):
        promote(world, venue=MovingVenue())
    assert seat_state(world)["capabilities_consumed"] == 0
