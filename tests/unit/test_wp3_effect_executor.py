"""WP3 effect executor — effect-free acceptance.

Every venue interaction here goes through FAKE ports that record what
was asked of them; the only shipped port is the refusing dry-run
interface, and a structural test proves the executor imports no
network client. Nothing is deployed and no venue is contacted.
"""
from __future__ import annotations

import json
import socket
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from app.effect_executor import (
    EffectExecutor, EffectJournal, ExecutorError, PlanAlreadyClaimed,
    PlanStopped, directive_digest)
from app.live_flatten_custody import (
    LiveFlattenCustody, VenueObligationBinding)
from app.session_authority_adapter import (
    VenueDirective, derive_directive, load_authority)
from app.venue_direct_evidence import VenueEvidenceError

from tests.unit.test_wp3_session_adapter import (
    AUTHORITY_ROOT, block, reviewed_identity, session_policy)
from tests.unit.test_wp3_venue_direct_evidence import (
    ALPACA_FP, ALPACA_ORDERS, ALPACA_POSITIONS,
    ALPACA_PROTECTIVE_CHILD, ALPACA_SHORT_POSITION, MT5_ORDERS,
    MT5_POSITIONS, NOW, OBSERVED, evidence, mt5_evidence, mt5_policy,
    policy)

pytestmark = pytest.mark.skipif(
    not (AUTHORITY_ROOT / "app" / "session_exposure.py").is_file(),
    reason="the accepted session authority checkout is not present")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*_args, **_kwargs):
        raise AssertionError(
            "network operation attempted in an executor test")
    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


@pytest.fixture(scope="module")
def authority():
    return load_authority(AUTHORITY_ROOT,
                          expected_code_identity=reviewed_identity())


# ------------------------------------------------------------------ #
# fakes and builders                                                  #
# ------------------------------------------------------------------ #

class FakePort:
    """Records every effect; can be told to raise at a boundary."""

    def __init__(self, *, world=None):
        self.cancel_calls = []
        self.submit_calls = []
        self.close_calls = []
        self.raise_on_cancel = None
        self.raise_on_submit = None
        self.raise_on_close = None
        self.world = world          # mutable venue state, optional

    def cancel_order(self, identity):
        if self.raise_on_cancel is not None:
            raise self.raise_on_cancel
        self.cancel_calls.append(identity)
        return {"acknowledged": identity}

    def submit_decision(self, command):
        if self.raise_on_submit is not None:
            exc, self.raise_on_submit = self.raise_on_submit, None
            raise exc
        self.submit_calls.append(command)
        return {"acknowledged": command}

    def request_close(self):
        if self.raise_on_close is not None:
            raise self.raise_on_close
        self.close_calls.append("close")
        if self.world is not None:
            self.world["flat"] = True       # the close changes reality
        return {"acknowledged": "close"}


def book(extra_protective=True):
    payload = json.loads(json.dumps(ALPACA_ORDERS))
    if extra_protective:
        payload["orders"].append(json.loads(json.dumps(
            ALPACA_PROTECTIVE_CHILD))["orders"][0])
    return payload


def alpaca_directive(authority, *, state="WIND_DOWN", command=0,
                     orders_payload=None, positions_payload=None):
    orders_ev = evidence("alpaca_paper", "open_orders",
                         orders_payload or book())
    positions_ev = evidence("alpaca_paper", "positions",
                            positions_payload or ALPACA_POSITIONS)
    return derive_directive(
        authority,
        policy=authority.session_exposure.validate_policy(
            session_policy()),
        state_block=block(state), venue="alpaca_paper",
        account_fingerprint=ALPACA_FP, symbol="SPY",
        raw_model_output=float(command), mapped_command=command,
        positions=positions_ev.facts, orders=orders_ev.facts,
        provenance=positions_ev.provenance())


def make_executor(authority, tmp_path, directive, port, *,
                  plan_id="plan-1",
                  orders_payload=None, positions_payload=None,
                  outcome_map=None, world=None, custody=None,
                  venue_policy=None, evidence_fn=None):
    venue_policy = venue_policy or policy()
    evidence_fn = evidence_fn or evidence
    world = world if world is not None else {"flat": False}

    def fresh_orders():
        if world.get("orders_raise") is not None:
            exc = world.pop("orders_raise")
            raise exc
        payload = ({"observed_at": OBSERVED, "orders": []}
                   if world.get("flat")
                   else (orders_payload or book()))
        return evidence_fn(directive.venue, "open_orders", payload) \
            if evidence_fn is evidence else evidence_fn(
                "open_orders", payload)

    def fresh_positions():
        if world.get("positions_raise") is not None:
            exc = world.pop("positions_raise")
            raise exc
        payload = ({"observed_at": OBSERVED, "positions": []}
                   if world.get("flat")
                   else (positions_payload or ALPACA_POSITIONS))
        return evidence_fn(directive.venue, "positions", payload) \
            if evidence_fn is evidence else evidence_fn(
                "positions", payload)

    def outcomes():
        if world.get("outcomes_raise") is not None:
            exc = world.pop("outcomes_raise")
            raise exc
        if outcome_map is not None:
            return dict(outcome_map)
        return {identity: "cancelled"
                for identity in directive.cancel_order_identities}

    return EffectExecutor(
        journal_root=tmp_path / "journal", plan_id=plan_id,
        directive=directive, policy=venue_policy,
        authority_root=AUTHORITY_ROOT,
        expected_code_identity=reviewed_identity(),
        port=port, fresh_orders=fresh_orders,
        fresh_positions=fresh_positions, outcomes=outcomes,
        custody=custody, clock=lambda: NOW)


def alpaca_custody(authority, tmp_path):
    return LiveFlattenCustody(
        authority, tmp_path / "custody",
        binding=VenueObligationBinding(
            venue="alpaca_paper", account_fingerprint=ALPACA_FP,
            symbol="SPY", position_identity="synthetic-asset-uuid",
            evidence_policy_digest=policy().policy_digest,
            calendar_identity="cal-venue-v1",
            authority_code_identity=authority.code_identity),
        episode_identity="ep-exec-1")


# ================================================================== #
# ordering: persist, cancel, gate, recheck, effect                   #
# ================================================================== #

class TestOrderingAndPersistence:

    def test_the_plan_is_persisted_before_any_effect(self, authority,
                                                     tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port)
        executor.execute()
        plan = json.loads(
            (executor.journal.root / "plan.json").read_text())
        assert plan["directive_digest"] == directive_digest(directive)
        assert plan["effects"] == ["cancel_pending_entries",
                                   "submit_decision"]
        chain = executor.journal.records()
        kinds = [r["kind"] for r in chain]
        assert kinds.index("cancel_acknowledged") < \
            kinds.index("gate_verdict") < \
            kinds.index("effect_acknowledged")

    def test_only_the_named_entry_is_cancelled(self, authority,
                                               tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        make_executor(authority, tmp_path, directive, port).execute()
        assert port.cancel_calls == ["parent-order-id"]
        for protective in ("stop-leg-id", "limit-leg-id",
                           "synthetic-order-0001"):
            assert protective not in port.cancel_calls
        assert port.submit_calls == [0]

    def test_a_protective_identity_is_structurally_impossible(
            self, authority, tmp_path):
        """Even a hand-built directive naming a protective identity
        stops at the live order book, and the port never sees it."""
        directive = VenueDirective(
            venue="alpaca_paper", account_fingerprint=ALPACA_FP,
            symbol="SPY", session_state="WIND_DOWN",
            raw_model_output=0.0, mapped_command=0,
            mapped_action={"kind": "hold", "risk_increasing": False},
            overlay="pass_through", final_command=0,
            effects=("cancel_pending_entries", "submit_decision"),
            cancel_order_identities=("stop-leg-id",),
            blocks_risk_increase=False,
            requires_direct_confirmation=False,
            preserve_protection=True, reason="adversary",
            evidence_provenance={"venue_direct": True})
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port)
        with pytest.raises(PlanStopped,
                           match="never submitted for cancellation"):
            executor.execute()
        assert port.cancel_calls == []
        assert port.submit_calls == []

    def test_long_and_short_books_both_run(self, authority, tmp_path):
        short_book = json.loads(json.dumps(ALPACA_ORDERS))
        short_book["orders"][0]["side"] = "sell"
        short_book["orders"][0]["position_intent"] = "sell_to_open"
        for leg in short_book["orders"][0]["legs"]:
            leg["side"] = "buy"
        for name, orders_payload, positions_payload in (
                ("long", book(), ALPACA_POSITIONS),
                ("short", short_book, ALPACA_SHORT_POSITION)):
            directive = alpaca_directive(
                authority, orders_payload=orders_payload,
                positions_payload=positions_payload)
            port = FakePort()
            make_executor(authority, tmp_path, directive, port,
                          plan_id=f"plan-{name}",
                          orders_payload=orders_payload,
                          positions_payload=positions_payload
                          ).execute()
            assert port.cancel_calls == ["parent-order-id"], name


# ================================================================== #
# the gate stops the plan                                            #
# ================================================================== #

class TestGateStopsThePlan:

    @pytest.mark.parametrize("outcome", [
        "rejected", "filled_before_cancel", "still_open",
        "gone_without_verdict"])
    def test_a_failed_cancellation_stops_everything(self, authority,
                                                    tmp_path,
                                                    outcome):
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(
            authority, tmp_path, directive, port,
            outcome_map={"parent-order-id": outcome})
        with pytest.raises(PlanStopped, match="unresolved"):
            executor.execute()
        assert port.submit_calls == [], (
            "the dependent effect must never run")
        assert executor.journal.find("plan_stopped") is not None

    def test_a_missing_verdict_stops_everything(self, authority,
                                                tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port,
                                 outcome_map={})
        with pytest.raises(PlanStopped):
            executor.execute()
        assert port.submit_calls == []

    def test_partial_cancellation_stops_and_names_the_rest(
            self, authority, tmp_path):
        payload = book()
        payload["orders"].append({
            "id": "second-entry", "symbol": "SPY", "side": "buy",
            "qty": "5", "status": "new", "order_class": "bracket",
            "type": "market", "position_intent": "buy_to_open",
            "legs": [{"id": "second-stop", "side": "sell",
                      "type": "stop", "qty": "5", "status": "held"}]})
        directive = alpaca_directive(authority,
                                     orders_payload=payload)
        port = FakePort()
        executor = make_executor(
            authority, tmp_path, directive, port,
            orders_payload=payload,
            outcome_map={"parent-order-id": "cancelled",
                         "second-entry": "still_open"})
        with pytest.raises(PlanStopped, match="second-entry"):
            executor.execute()
        assert port.submit_calls == []

    def test_a_stale_snapshot_stops_before_any_cancellation(
            self, authority, tmp_path):
        stale = book()
        stale["observed_at"] = (NOW - timedelta(days=3)).isoformat()
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port,
                                 orders_payload=stale)
        with pytest.raises(PlanStopped, match="stale"):
            executor.execute()
        assert port.cancel_calls == []
        assert port.submit_calls == []


# ================================================================== #
# crash and restart at every boundary                                #
# ================================================================== #

class TestCrashAndRestart:

    def test_crash_before_cancellation(self, authority, tmp_path):
        directive = alpaca_directive(authority)
        world = {"flat": False,
                 "orders_raise": RuntimeError("crash")}
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port,
                                 world=world)
        with pytest.raises(RuntimeError, match="crash"):
            executor.execute()
        assert port.cancel_calls == []
        resumed = make_executor(authority, tmp_path, directive, port,
                                world=world)
        with pytest.raises(PlanAlreadyClaimed):
            resumed.execute()
        assert resumed.resume() == {"state": "completed"}
        assert port.cancel_calls == ["parent-order-id"]
        assert port.submit_calls == [0]

    def test_crash_between_cancellation_and_verdict(self, authority,
                                                    tmp_path):
        directive = alpaca_directive(authority)
        world = {"flat": False,
                 "outcomes_raise": RuntimeError("crash")}
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port,
                                 world=world)
        with pytest.raises(RuntimeError, match="crash"):
            executor.execute()
        assert port.cancel_calls == ["parent-order-id"]
        resumed = make_executor(authority, tmp_path, directive, port,
                                world=world)
        assert resumed.resume() == {"state": "completed"}
        assert port.cancel_calls == ["parent-order-id"], (
            "an acknowledged cancellation is never re-issued")
        assert port.submit_calls == [0]

    def test_crash_after_verdict_before_the_dependent_effect(
            self, authority, tmp_path, monkeypatch):
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port)
        original = EffectExecutor._recheck_identities

        def crashing(self):
            raise RuntimeError("crash after gate")

        monkeypatch.setattr(EffectExecutor, "_recheck_identities",
                            crashing)
        with pytest.raises(RuntimeError, match="crash after gate"):
            executor.execute()
        monkeypatch.setattr(EffectExecutor, "_recheck_identities",
                            original)
        assert executor.journal.find("gate_verdict") is not None
        assert port.submit_calls == []
        resumed = make_executor(authority, tmp_path, directive, port)
        assert resumed.resume() == {"state": "completed"}
        assert port.submit_calls == [0], (
            "the decision runs exactly once")

    def test_an_unacknowledged_decision_is_never_reissued(
            self, authority, tmp_path):
        """The ambiguous window: the decision was requested and the
        venue never acknowledged. Submitting adds risk, so it is
        at-most-once — the resume fails closed instead of doubling."""
        directive = alpaca_directive(authority)
        port = FakePort()
        port.raise_on_submit = RuntimeError("crash mid-submit")
        executor = make_executor(authority, tmp_path, directive, port)
        with pytest.raises(RuntimeError, match="crash mid-submit"):
            executor.execute()
        assert executor.journal.find("effect_requested",
                                     "submit_decision") is not None
        resumed = make_executor(authority, tmp_path, directive, port)
        outcome = resumed.resume()
        assert outcome["state"] == "unresolved"
        assert "not re-issued" in outcome["incident"]
        assert port.submit_calls == [], (
            "whether the venue received it is unknown; it must not "
            "be sent again")

    def test_crash_after_close_request_before_confirmation(
            self, authority, tmp_path):
        directive = alpaca_directive(authority,
                                     state="FORCED_FLATTEN",
                                     command=1)
        assert "request_close" in directive.effects
        custody = alpaca_custody(authority, tmp_path)
        world = {"flat": False}
        port = FakePort(world=world)
        executor = make_executor(authority, tmp_path, directive, port,
                                 world=world, custody=custody)

        # the close is acknowledged, then the confirmation read crashes
        real_close = port.request_close

        def close_then_poison():
            ack = real_close()
            world["positions_raise"] = RuntimeError(
                "crash before confirmation")
            return ack

        port.request_close = close_then_poison
        with pytest.raises(RuntimeError,
                           match="crash before confirmation"):
            executor.execute()
        assert port.close_calls == ["close"]

        port.request_close = real_close
        resumed = make_executor(authority, tmp_path, directive, port,
                                world=world, custody=custody)
        outcome = resumed.resume()
        assert outcome["state"] == "completed"
        assert port.close_calls == ["close"], (
            "an acknowledged close is not re-requested")
        record = custody.read(outcome["obligation_id"])
        assert record["state"] == "flatten_confirmed"
        # no sibling obligation was minted
        assert len(list(custody._store.root.glob("*.json"))) == 1

    def test_a_tampered_journal_refuses_to_resume(self, authority,
                                                  tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port)
        executor.execute()
        target = sorted(executor.journal.root.glob("00*.json"))[1]
        record = json.loads(target.read_text())
        record["payload"] = {"forged": True}
        target.write_text(json.dumps(record))
        resumed = make_executor(authority, tmp_path, directive, port)
        with pytest.raises(ExecutorError, match="digest broken"):
            resumed.resume()


# ================================================================== #
# forced flatten through the accepted custody                        #
# ================================================================== #

class TestForcedFlatten:

    def test_custody_opens_before_the_close_and_confirms_on_flat(
            self, authority, tmp_path):
        directive = alpaca_directive(authority,
                                     state="FORCED_FLATTEN",
                                     command=1)
        custody = alpaca_custody(authority, tmp_path)
        world = {"flat": False}
        port = FakePort(world=world)
        executor = make_executor(authority, tmp_path, directive, port,
                                 world=world, custody=custody)
        outcome = executor.execute()
        assert outcome["state"] == "completed"
        chain = [r["kind"] for r in executor.journal.records()]
        assert chain.index("custody_opened") < \
            chain.index("close_acknowledged") < \
            chain.index("flatten_confirmed")
        record = custody.read(outcome["obligation_id"])
        assert record["state"] == "flatten_confirmed"
        assert record["reconciliation"]["positions"] == 0
        assert record["reconciliation"]["orders"] == 0

    def test_a_close_that_does_not_flatten_stays_unresolved(
            self, authority, tmp_path):
        directive = alpaca_directive(authority,
                                     state="FORCED_FLATTEN",
                                     command=1)
        custody = alpaca_custody(authority, tmp_path)
        world = {"flat": False}
        port = FakePort(world=None)        # the close changes nothing
        executor = make_executor(authority, tmp_path, directive, port,
                                 world=world, custody=custody)
        outcome = executor.execute()
        assert outcome["state"] == "unresolved"
        assert "FLATTEN_INCOMPLETE" in outcome["incident"]
        assert custody.read(outcome["obligation_id"])["state"] in (
            "flatten_requested", "flatten_in_flight")
        # a later resume with the world actually flat confirms
        world["flat"] = True
        resumed = make_executor(authority, tmp_path, directive, port,
                                world=world, custody=custody)
        assert resumed.resume()["state"] == "completed"

    def test_a_flatten_without_custody_refuses(self, authority,
                                               tmp_path):
        directive = alpaca_directive(authority,
                                     state="FORCED_FLATTEN",
                                     command=1)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port)
        with pytest.raises(PlanStopped,
                           match="requires the accepted live custody"):
            executor.execute()
        assert port.close_calls == []


# ================================================================== #
# duplicate and concurrent invocation                                #
# ================================================================== #

class TestSingleEffectElection:

    def test_duplicate_invocation_refuses(self, authority, tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        make_executor(authority, tmp_path, directive, port).execute()
        with pytest.raises(PlanAlreadyClaimed):
            make_executor(authority, tmp_path, directive,
                          FakePort()).execute()
        assert port.submit_calls == [0]

    def test_concurrent_invocation_elects_exactly_one(self, authority,
                                                      tmp_path):
        directive = alpaca_directive(authority)
        ports = [FakePort(), FakePort()]
        barrier = threading.Barrier(2)
        results = [None, None]

        def contend(index):
            executor = make_executor(authority, tmp_path, directive,
                                     ports[index])
            barrier.wait()
            try:
                executor.execute()
                results[index] = "won"
            except PlanAlreadyClaimed:
                results[index] = "refused"

        threads = [threading.Thread(target=contend, args=(i,))
                   for i in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert sorted(results) == ["refused", "won"], results
        total_submits = sum(len(p.submit_calls) for p in ports)
        assert total_submits == 1, "exactly one effect"


# ================================================================== #
# both venues, one decision                                          #
# ================================================================== #

class TestBothVenuesOneDecision:

    def test_same_policy_decision_different_representations(
            self, authority, tmp_path):
        alpaca = alpaca_directive(authority, command=1)
        mt5_orders_ev = mt5_evidence("open_orders", MT5_ORDERS)
        mt5_positions_ev = mt5_evidence("positions", MT5_POSITIONS)
        mt5 = derive_directive(
            authority,
            policy=authority.session_exposure.validate_policy(
                session_policy()),
            state_block=block("WIND_DOWN"), venue="mt5_demo",
            account_fingerprint="sanitizedfp01", symbol="USDCAD",
            raw_model_output=1.0, mapped_command=1,
            positions=mt5_positions_ev.facts,
            orders=mt5_orders_ev.facts,
            provenance=mt5_positions_ev.provenance())

        # the DECISION is identical
        assert alpaca.overlay == mt5.overlay
        assert alpaca.final_command == mt5.final_command
        assert alpaca.effects == mt5.effects
        # the REPRESENTATION is not
        assert set(alpaca.cancel_order_identities) == {
            "parent-order-id"}
        assert set(mt5.cancel_order_identities) == {"200001"}

        def mt5_ev(kind, payload):
            return mt5_evidence(kind, payload)

        for name, directive, venue_policy, ev, orders_payload, \
                positions_payload in (
                ("alpaca", alpaca, policy(), evidence, book(),
                 ALPACA_POSITIONS),
                ("mt5", mt5, mt5_policy(), mt5_ev, MT5_ORDERS,
                 MT5_POSITIONS)):
            port = FakePort()
            make_executor(authority, tmp_path, directive, port,
                          plan_id=f"plan-{name}",
                          orders_payload=orders_payload,
                          positions_payload=positions_payload,
                          venue_policy=venue_policy,
                          evidence_fn=ev).execute()
            assert len(port.cancel_calls) == 1, name
        # each venue cancelled ITS identity


# ================================================================== #
# no-write end to end                                                #
# ================================================================== #

class TestNoWriteEndToEnd:

    def test_the_executor_imports_no_network_client(self):
        import app.effect_executor as module
        source = Path(module.__file__).read_text()
        for forbidden in ("import requests", "import socket",
                          "import http", "urllib", "AlpacaPaper",
                          "Mt5Bridge", "sqlite3", ".post(",
                          ".delete(", "_write_request"):
            assert forbidden not in source, forbidden

    def test_a_protection_only_plan_completes_without_any_port_call(
            self, authority, tmp_path):
        from tools.session_directive_dry_run import (
            NoWriteVenueInterface)
        directive = alpaca_directive(
            authority, command=1,
            orders_payload=json.loads(
                json.dumps(ALPACA_PROTECTIVE_CHILD)),
            positions_payload=ALPACA_SHORT_POSITION)
        assert directive.effects == ("none",)
        executor = make_executor(
            authority, tmp_path, directive, NoWriteVenueInterface(),
            orders_payload=json.loads(
                json.dumps(ALPACA_PROTECTIVE_CHILD)),
            positions_payload=ALPACA_SHORT_POSITION)
        assert executor.execute() == {"state": "completed"}

    def test_the_refusing_port_stops_a_write_plan_loudly(
            self, authority, tmp_path):
        from tools.session_directive_dry_run import (
            NoWriteVenueInterface, WriteAttempted)
        directive = alpaca_directive(authority)
        executor = make_executor(authority, tmp_path, directive,
                                 NoWriteVenueInterface())
        with pytest.raises(WriteAttempted, match="cancel_order"):
            executor.execute()

    def test_the_refusing_port_satisfies_the_port_protocol(self):
        from tools.session_directive_dry_run import (
            NoWriteVenueInterface)
        port = NoWriteVenueInterface()
        for name in ("cancel_order", "submit_decision",
                     "request_close"):
            assert callable(getattr(port, name))
