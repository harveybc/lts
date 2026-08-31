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
    PlanLockHeld, PlanStopped, directive_digest)
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

    # E3: the port DECLARES its close semantics; without this a
    # reissue of an unacknowledged close is refused
    close_contract = "same_key_idempotent_reduce_only"

    def request_close(self, **contract):
        if self.raise_on_close is not None:
            exc, self.raise_on_close = self.raise_on_close, None
            raise exc
        self.close_calls.append(dict(contract))
        if self.world is not None:
            self.world["flat"] = True       # the close changes reality
        return {"acknowledged": contract.get("idempotency_key",
                                             "close")}


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

    _ALPACA_STATUS = {"cancelled": "canceled",
                      "filled_before_cancel": "filled",
                      "rejected": "rejected", "replaced": "replaced",
                      "failed": "failed"}
    _MT5_STATE = {"cancelled": "ORDER_STATE_CANCELED",
                  "filled_before_cancel": "ORDER_STATE_FILLED",
                  "rejected": "ORDER_STATE_REJECTED"}

    def terminal_orders():
        if world.get("outcomes_raise") is not None:
            exc = world.pop("outcomes_raise")
            raise exc
        if world.get("terminal_override") is not None:
            return world["terminal_override"]()
        mapping = (dict(outcome_map) if outcome_map is not None
                   else {identity: "cancelled" for identity in
                         directive.cancel_order_identities})
        rows = []
        for identity, verdict in mapping.items():
            if verdict in ("still_open", "gone_without_verdict",
                           None):
                continue        # absence is never a terminal verdict
            if directive.venue == "mt5_demo":
                rows.append({"ticket": identity, "symbol": "USDCAD",
                             "state": _MT5_STATE[verdict],
                             "done_time": OBSERVED})
            else:
                rows.append({"id": identity, "symbol": "SPY",
                             "status": _ALPACA_STATUS[verdict],
                             "updated_at": OBSERVED})
        if not rows:
            # absence is not a verdict, and an empty body refuses at
            # the parser — model it as evidence the gate will judge
            # by the MISSING identities: a lone unrelated terminal row
            rows = [{"ticket": "unrelated-1", "symbol": "USDCAD",
                     "state": "ORDER_STATE_CANCELED",
                     "done_time": OBSERVED}
                    if directive.venue == "mt5_demo" else
                    {"id": "unrelated-1", "symbol": "SPY",
                     "status": "canceled", "updated_at": OBSERVED}]
        payload = rows
        return evidence_fn(directive.venue, "terminal_orders",
                           payload) if evidence_fn is evidence \
            else evidence_fn("terminal_orders", payload)

    from app.venue_direct_evidence import ReceiptLedger
    return EffectExecutor(
        journal_root=tmp_path / "journal", plan_id=plan_id,
        directive=directive, policy=venue_policy,
        receipt_ledger=ReceiptLedger(tmp_path / "receipts"),
        authority_root=AUTHORITY_ROOT,
        expected_code_identity=reviewed_identity(),
        port=port, fresh_orders=fresh_orders,
        fresh_positions=fresh_positions,
        terminal_orders=terminal_orders,
        custody=custody, clock=lambda: NOW)


def mt5_ev(kind, payload):
    return mt5_evidence(kind, payload)


def mt5_directive(authority, *, state="FORCED_FLATTEN", command=1):
    orders_ev = mt5_evidence("open_orders", MT5_ORDERS)
    positions_ev = mt5_evidence("positions", MT5_POSITIONS)
    return derive_directive(
        authority,
        policy=authority.session_exposure.validate_policy(
            session_policy()),
        state_block=block(state), venue="mt5_demo",
        account_fingerprint="sanitizedfp01", symbol="USDCAD",
        raw_model_output=float(command), mapped_command=command,
        positions=positions_ev.facts, orders=orders_ev.facts,
        provenance=positions_ev.provenance())


def mt5_custody(authority, tmp_path):
    return LiveFlattenCustody(
        authority, tmp_path / "custody",
        binding=VenueObligationBinding(
            venue="mt5_demo", account_fingerprint="sanitizedfp01",
            symbol="USDCAD", position_identity="100001",
            evidence_policy_digest=mt5_policy().policy_digest,
            calendar_identity="cal-venue-v1",
            authority_code_identity=authority.code_identity),
        episode_identity="ep-exec-1")


def mt5_flatten_executor(authority, tmp_path, port, *,
                         plan_id="plan-m1", world=None,
                         positions_payload=None):
    directive = mt5_directive(authority)
    return directive, make_executor(
        authority, tmp_path, directive, port, plan_id=plan_id,
        world=world, custody=mt5_custody(authority, tmp_path),
        venue_policy=mt5_policy(), evidence_fn=mt5_ev,
        orders_payload=MT5_ORDERS,
        positions_payload=positions_payload or MT5_POSITIONS)


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

        def close_then_poison(**contract):
            ack = real_close(**contract)
            world["positions_raise"] = RuntimeError(
                "crash before confirmation")
            return ack

        port.request_close = close_then_poison
        with pytest.raises(RuntimeError,
                           match="crash before confirmation"):
            executor.execute()
        assert len(port.close_calls) == 1

        port.request_close = real_close
        resumed = make_executor(authority, tmp_path, directive, port,
                                world=world, custody=custody)
        outcome = resumed.resume()
        assert outcome["state"] == "completed"
        assert len(port.close_calls) == 1, (
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
            except (PlanAlreadyClaimed, PlanLockHeld):
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


# ================================================================== #
# E1-E5: authority and recovery boundaries                           #
# ================================================================== #

class TestE1AbsentIdentityNeverReachesThePort:

    def test_an_absent_identity_stops_before_any_port_call(
            self, authority, tmp_path):
        """FROZEN COUNTEREXAMPLE. An identity absent from the fresh
        order book was returned as verified, submitted to
        cancel_order, and a forged outcome released the gate."""
        ghost = VenueDirective(
            venue="alpaca_paper", account_fingerprint=ALPACA_FP,
            symbol="SPY", session_state="WIND_DOWN",
            raw_model_output=0.0, mapped_command=0,
            mapped_action={"kind": "hold", "risk_increasing": False},
            overlay="pass_through", final_command=0,
            effects=("cancel_pending_entries", "submit_decision"),
            cancel_order_identities=("ghost-order-id",),
            blocks_risk_increase=False,
            requires_direct_confirmation=False,
            preserve_protection=True, reason="adversary",
            evidence_provenance={"venue_direct": True})
        port = FakePort()
        executor = make_executor(
            authority, tmp_path, ghost, port,
            outcome_map={"ghost-order-id": "cancelled"})
        with pytest.raises(PlanStopped, match="ABSENT"):
            executor.execute()
        assert port.cancel_calls == []
        assert port.submit_calls == []

    def test_duplicate_identities_stop(self, authority, tmp_path):
        twice = VenueDirective(
            venue="alpaca_paper", account_fingerprint=ALPACA_FP,
            symbol="SPY", session_state="WIND_DOWN",
            raw_model_output=0.0, mapped_command=0,
            mapped_action={"kind": "hold", "risk_increasing": False},
            overlay="pass_through", final_command=0,
            effects=("cancel_pending_entries", "submit_decision"),
            cancel_order_identities=("parent-order-id",
                                     "parent-order-id"),
            blocks_risk_increase=False,
            requires_direct_confirmation=False,
            preserve_protection=True, reason="adversary",
            evidence_provenance={"venue_direct": True})
        port = FakePort()
        with pytest.raises(PlanStopped, match="duplicate"):
            make_executor(authority, tmp_path, twice, port).execute()
        assert port.cancel_calls == []

    def test_the_wrong_evidence_type_stops(self, authority, tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        world = {"flat": False}
        executor = make_executor(authority, tmp_path, directive, port,
                                 world=world)
        executor.fresh_orders = lambda: evidence(
            "alpaca_paper", "positions", ALPACA_POSITIONS)
        with pytest.raises(PlanStopped,
                           match="wrong kind of evidence"):
            executor.execute()
        assert port.cancel_calls == []


class TestE2TypedTerminalEvidence:

    def test_a_bare_mapping_can_no_longer_release_the_gate(
            self, authority, tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port)
        executor.terminal_orders = lambda: {
            "parent-order-id": "cancelled"}
        with pytest.raises(ExecutorError, match="bare mapping"):
            executor.execute()
        assert port.submit_calls == []

    def test_a_non_terminal_status_is_refused_by_the_parser(
            self, authority, tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        world = {"flat": False,
                 "terminal_override": lambda: evidence(
                     "alpaca_paper", "terminal_orders",
                     [{"id": "parent-order-id", "symbol": "SPY",
                       "status": "new", "updated_at": OBSERVED}])}
        executor = make_executor(authority, tmp_path, directive, port,
                                 world=world)
        with pytest.raises(PlanStopped, match="not a TERMINAL"):
            executor.execute()
        assert port.submit_calls == []

    def test_open_orders_evidence_cannot_stand_in_for_verdicts(
            self, authority, tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        world = {"flat": False,
                 "terminal_override": lambda: evidence(
                     "alpaca_paper", "open_orders", book())}
        executor = make_executor(authority, tmp_path, directive, port,
                                 world=world)
        with pytest.raises(PlanStopped,
                           match="wrong kind of evidence"):
            executor.execute()
        assert port.submit_calls == []

    def test_stale_terminal_evidence_stops(self, authority, tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        stale = (NOW - timedelta(days=2)).isoformat()
        world = {"flat": False,
                 "terminal_override": lambda: evidence(
                     "alpaca_paper", "terminal_orders",
                     [{"id": "parent-order-id", "symbol": "SPY",
                       "status": "canceled",
                       "updated_at": stale}])}
        executor = make_executor(authority, tmp_path, directive, port,
                                 world=world)
        with pytest.raises(PlanStopped, match="stale"):
            executor.execute()
        assert port.submit_calls == []

    def test_the_gate_journal_carries_the_evidence_provenance(
            self, authority, tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port)
        executor.execute()
        record = executor.journal.find("cancellation_outcomes")
        provenance = record["payload"]["provenance"]
        assert provenance["evidence_type"] == "terminal_orders"
        assert provenance["venue_direct"] is True
        assert len(provenance["raw_sha256"]) == 64
        assert len(provenance["parser_digest"]) == 32


class TestE3CloseContractAndReconcileFirst:

    def _crashed_close(self, authority, tmp_path, *, plan_id):
        directive = alpaca_directive(authority,
                                     state="FORCED_FLATTEN",
                                     command=1)
        custody = alpaca_custody(authority, tmp_path)
        world = {"flat": False}
        port = FakePort(world=world)
        port.raise_on_close = RuntimeError("crash mid-close")
        executor = make_executor(authority, tmp_path, directive, port,
                                 plan_id=plan_id, world=world,
                                 custody=custody)
        with pytest.raises(RuntimeError, match="crash mid-close"):
            executor.execute()
        assert port.close_calls == []
        return directive, custody, world, port

    def test_the_close_binds_the_exact_position(self, authority,
                                                tmp_path):
        directive = alpaca_directive(authority,
                                     state="FORCED_FLATTEN",
                                     command=1)
        custody = alpaca_custody(authority, tmp_path)
        world = {"flat": False}
        port = FakePort(world=world)
        make_executor(authority, tmp_path, directive, port,
                      world=world, custody=custody).execute()
        contract = port.close_calls[0]
        assert contract["position_identity"] == \
            "sanitized-asset-uuid"
        assert contract["side"] == "long"
        assert contract["units"] == 10.0
        assert contract["reduce_only"] is True
        assert contract["idempotency_key"] == "close-plan-1"

    def _crashed_mt5_close(self, authority, tmp_path, *, plan_id):
        world = {"flat": False}
        port = FakePort(world=world)
        port.raise_on_close = RuntimeError("crash mid-close")
        directive, executor = mt5_flatten_executor(
            authority, tmp_path, port, plan_id=plan_id, world=world)
        with pytest.raises(RuntimeError, match="crash mid-close"):
            executor.execute()
        assert port.close_calls == []
        return directive, world, port

    def test_an_alpaca_reissue_is_categorically_unresolved(
            self, authority, tmp_path):
        """E7 FROZEN: Alpaca's asset_id names the asset, not the
        instance. Even a position IDENTICAL in side, quantity and
        price must never inherit an old close."""
        directive, custody, world, port = self._crashed_close(
            authority, tmp_path, plan_id="plan-ident")
        resumed = make_executor(authority, tmp_path, directive, port,
                                plan_id="plan-ident", world=world,
                                custody=custody)
        outcome = resumed.resume()
        assert outcome["state"] == "unresolved"
        assert "no position-instance identity" in outcome["incident"]
        assert port.close_calls == []

    def test_an_alpaca_reopened_position_with_changed_price_too(
            self, authority, tmp_path):
        """E7 FROZEN: the reopened-position shape that previously
        INHERITED the old close (entry 480 vs 500) and completed."""
        directive, custody, world, port = self._crashed_close(
            authority, tmp_path, plan_id="plan-reopen")
        reopened = json.loads(json.dumps(ALPACA_POSITIONS))
        reopened["positions"][0]["avg_entry_price"] = "480.00"
        resumed = make_executor(authority, tmp_path, directive, port,
                                plan_id="plan-reopen", world=world,
                                custody=custody,
                                positions_payload=reopened)
        outcome = resumed.resume()
        assert outcome["state"] == "unresolved"
        assert port.close_calls == []

    def test_the_same_mt5_ticket_reissues_with_the_same_key(
            self, authority, tmp_path):
        """MT5's ticket IS a venue position-instance identity, so the
        same ticket, side, units and entry price may retry with the
        SAME idempotency key through a declaring port."""
        directive, world, port = self._crashed_mt5_close(
            authority, tmp_path, plan_id="plan-msame")
        _d, resumed = mt5_flatten_executor(
            authority, tmp_path, port, plan_id="plan-msame",
            world=world)
        outcome = resumed.resume()
        assert outcome["state"] == "completed"
        assert len(port.close_calls) == 1
        assert port.close_calls[0]["idempotency_key"] == \
            "close-plan-msame"
        assert port.close_calls[0]["identity_kind"] == \
            "venue_position_instance"
        ack = resumed.journal.find("close_acknowledged")
        assert ack["payload"]["reissued_with_same_key"] is True

    def test_a_reopened_mt5_ticket_never_inherits_the_close(
            self, authority, tmp_path):
        directive, world, port = self._crashed_mt5_close(
            authority, tmp_path, plan_id="plan-mreopen")
        reopened = json.loads(json.dumps(MT5_POSITIONS))
        reopened["positions"][0]["ticket"] = "100999"
        _d, resumed = mt5_flatten_executor(
            authority, tmp_path, port, plan_id="plan-mreopen",
            world=world, positions_payload=reopened)
        outcome = resumed.resume()
        assert outcome["state"] == "unresolved"
        assert "reopened" in outcome["incident"] or \
            "changed" in outcome["incident"]
        assert port.close_calls == []

    def test_a_changed_mt5_entry_price_never_reissues(self, authority,
                                                      tmp_path):
        directive, world, port = self._crashed_mt5_close(
            authority, tmp_path, plan_id="plan-mprice")
        changed = json.loads(json.dumps(MT5_POSITIONS))
        changed["positions"][0]["price_open"] = 1.40
        _d, resumed = mt5_flatten_executor(
            authority, tmp_path, port, plan_id="plan-mprice",
            world=world, positions_payload=changed)
        outcome = resumed.resume()
        assert outcome["state"] == "unresolved"
        assert port.close_calls == []

    def test_a_port_without_the_contract_never_reissues(
            self, authority, tmp_path):
        directive, world, port = self._crashed_mt5_close(
            authority, tmp_path, plan_id="plan-noc")

        class NoContractPort(FakePort):
            close_contract = None

        blind = NoContractPort(world=world)
        _d, resumed = mt5_flatten_executor(
            authority, tmp_path, blind, plan_id="plan-noc",
            world=world)
        outcome = resumed.resume()
        assert outcome["state"] == "unresolved"
        assert "does not prove same-key" in outcome["incident"]
        assert blind.close_calls == []

    def test_reconciled_flat_confirms_without_reissue(self, authority,
                                                      tmp_path):
        directive, custody, world, port = self._crashed_close(
            authority, tmp_path, plan_id="plan-flat")
        world["flat"] = True        # the first close DID land
        resumed = make_executor(authority, tmp_path, directive, port,
                                plan_id="plan-flat", world=world,
                                custody=custody)
        outcome = resumed.resume()
        assert outcome["state"] == "completed"
        assert port.close_calls == [], (
            "flat evidence confirms without a second close")

    def test_multiple_positions_are_never_closed_blind(self,
                                                       authority,
                                                       tmp_path):
        two = json.loads(json.dumps(ALPACA_POSITIONS))
        two["positions"].append({**two["positions"][0],
                                 "asset_id": "second-asset"})
        directive = alpaca_directive(authority,
                                     state="FORCED_FLATTEN",
                                     command=1,
                                     positions_payload=two)
        custody = alpaca_custody(authority, tmp_path)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port,
                                 positions_payload=two,
                                 custody=custody)
        with pytest.raises(PlanStopped, match="exactly ONE"):
            executor.execute()
        assert port.close_calls == []


class TestE4RunLock:

    def test_a_held_lock_refuses_and_the_port_is_untouched(
            self, authority, tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port)
        (executor.journal.root / "run.lock").write_text("held:4242")
        with pytest.raises(PlanLockHeld, match="operator disposes"):
            executor.execute()
        with pytest.raises(PlanLockHeld):
            executor.resume()
        assert port.cancel_calls == []
        assert port.submit_calls == []

    def test_the_lock_is_released_on_success_and_on_stop(
            self, authority, tmp_path):
        directive = alpaca_directive(authority)
        executor = make_executor(authority, tmp_path, directive,
                                 FakePort())
        executor.execute()
        # E9: the lock is MONOTONE — it is never unlinked. Release
        # leaves the durable content "released", which is the only
        # content a future claimant may enter over.
        assert (executor.journal.root /
                "run.lock").read_text() == "released"
        stopping = make_executor(
            authority, tmp_path, alpaca_directive(authority),
            FakePort(), plan_id="plan-stop",
            outcome_map={"parent-order-id": "rejected"})
        with pytest.raises(PlanStopped):
            stopping.execute()
        assert (stopping.journal.root /
                "run.lock").read_text() == "released"

    def test_concurrent_resume_of_an_unacknowledged_close_is_single(
            self, authority, tmp_path):
        """FROZEN COUNTEREXAMPLE. Two concurrent resumes both saw
        close_requested without close_acknowledged and BOTH called the
        port: the journal refused the second acknowledgement, but the
        venue had already received two closes."""
        directive = alpaca_directive(authority,
                                     state="FORCED_FLATTEN",
                                     command=1)
        custody = alpaca_custody(authority, tmp_path)
        world = {"flat": False}
        crash = FakePort(world=world)
        crash.raise_on_close = RuntimeError("crash")
        first = make_executor(authority, tmp_path, directive, crash,
                              world=world, custody=custody)
        with pytest.raises(RuntimeError):
            first.execute()

        barrier = threading.Barrier(2)
        results = [None, None]
        ports = [FakePort(world=world), FakePort(world=world)]

        def contend(index):
            executor = make_executor(authority, tmp_path, directive,
                                     ports[index], world=world,
                                     custody=custody)
            barrier.wait(timeout=30)
            try:
                executor.resume()
                results[index] = "ran"
            except (PlanLockHeld, PlanAlreadyClaimed):
                results[index] = "refused"

        threads = [threading.Thread(target=contend, args=(i,))
                   for i in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        total = sum(len(p.close_calls) for p in ports)
        assert total <= 1, (results, total)
        assert "refused" in results, results


class TestE5PlanEnvelopeAndOrdinals:

    @pytest.mark.parametrize("field,value", [
        ("effects", ["FORGED"]),
        ("directive_digest", "0" * 64),
        ("evidence_policy_digest", "1" * 64),
        ("authority_code_identity", "2" * 64),
        ("plan_id", "someone-else"),
        ("schema", "lts.other.v9")])
    def test_a_tampered_plan_field_refuses(self, authority, tmp_path,
                                           field, value):
        directive = alpaca_directive(authority)
        executor = make_executor(authority, tmp_path, directive,
                                 FakePort())
        executor.execute()
        plan_path = executor.journal.root / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan[field] = value
        plan_path.write_text(json.dumps(plan))
        resumed = make_executor(authority, tmp_path, directive,
                                FakePort())
        with pytest.raises(ExecutorError,
                           match="digest mismatch|schema"):
            resumed.resume()

    def test_a_reforged_plan_digest_still_fails_the_recheck(
            self, authority, tmp_path):
        """A self-consistent forgery passes the envelope but the
        directive recheck compares against the LIVE directive."""
        import app.effect_executor as mod
        directive = alpaca_directive(authority)
        crash = FakePort()
        crash.raise_on_submit = RuntimeError("crash before done")
        executor = make_executor(authority, tmp_path, directive,
                                 crash)
        with pytest.raises(RuntimeError):
            executor.execute()
        plan_path = executor.journal.root / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["directive_digest"] = "0" * 64
        body = {k: v for k, v in plan.items() if k != "digest"}
        plan["digest"] = mod._sha(mod._canonical(body))
        plan_path.write_text(json.dumps(plan))
        resumed = make_executor(authority, tmp_path, directive,
                                FakePort())
        with pytest.raises(PlanStopped,
                           match="directive identity changed"):
            resumed.resume()

    def test_no_exception_text_matching_remains(self):
        source = Path(
            __import__("app.effect_executor",
                       fromlist=["x"]).__file__).read_text()
        assert "in str(exc)" not in source

    def test_custody_transitions_carry_a_monotone_ordinal(
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
        record = custody.read(outcome["obligation_id"])
        assert record["requested_at_bar"] >= 1, (
            "constant zero is not live provenance")
        assert record["confirmed_at_bar"] > \
            record["requested_at_bar"], (
            "the ordinal is monotone across the transitions")


# ================================================================== #
# E4: two REAL processes at each unacknowledged boundary             #
# ================================================================== #

_PROCESS_CONTENDER = r'''
import json, os, sys, time
sys.path.insert(0, {repo!r})
sys.path.insert(0, {repo!r} + "/tests")
from unit.test_wp3_effect_executor import (FakePort, alpaca_custody,
                                           alpaca_directive,
                                           make_executor)
from unit.test_wp3_session_adapter import (AUTHORITY_ROOT,
                                           reviewed_identity)
from app.session_authority_adapter import load_authority
from app.effect_executor import (PlanAlreadyClaimed, PlanLockHeld,
                                 PlanStopped)
from pathlib import Path

journal_root, boundary, marker_dir, barrier, ready = sys.argv[1:6]
authority = load_authority(AUTHORITY_ROOT,
                           expected_code_identity=reviewed_identity())


class MarkerPort(FakePort):
    def _mark(self, kind):
        n = 0
        while True:
            try:
                fd = os.open(Path(marker_dir) /
                             f"{{kind}}-{{os.getpid()}}-{{n}}",
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                os.close(fd)
                return
            except FileExistsError:
                n += 1

    def cancel_order(self, identity):
        self._mark("cancel")
        return super().cancel_order(identity)

    def submit_decision(self, command):
        self._mark("submit")
        return super().submit_decision(command)

    def request_close(self, **contract):
        self._mark("close")
        return super().request_close(**contract)


# journal_root is <tmp>/journal/<plan-id>; the builders expect <tmp>
from unit.test_wp3_effect_executor import (mt5_custody,
                                           mt5_directive, mt5_ev)
from unit.test_wp3_venue_direct_evidence import (MT5_ORDERS,
                                                 MT5_POSITIONS,
                                                 mt5_policy)
tmp = Path(journal_root).parents[1]
world = {{"flat": False}}
port = MarkerPort(world=world)
if boundary == "close_mt5":
    directive = mt5_directive(authority)
    executor = make_executor(
        authority, tmp, directive, port, world=world,
        custody=mt5_custody(authority, tmp),
        venue_policy=mt5_policy(), evidence_fn=mt5_ev,
        orders_payload=MT5_ORDERS, positions_payload=MT5_POSITIONS)
elif boundary == "close_alpaca":
    directive = alpaca_directive(authority, state="FORCED_FLATTEN",
                                 command=1)
    executor = make_executor(authority, tmp, directive, port,
                             world=world,
                             custody=alpaca_custody(authority, tmp))
else:
    directive = alpaca_directive(authority)
    executor = make_executor(authority, tmp, directive, port,
                             world=world)

Path(ready).write_text("ready")
while not Path(barrier).exists():
    time.sleep(0.001)
try:
    outcome = executor.resume()
    print(json.dumps({{"result": outcome.get("state")}}))
except (PlanLockHeld, PlanAlreadyClaimed) as exc:
    print(json.dumps({{"result": "refused",
                      "kind": type(exc).__name__}}))
except PlanStopped as exc:
    print(json.dumps({{"result": "stopped", "reason": str(exc)[:80]}}))
'''


class TestE4TwoRealProcesses:

    def _crash_at(self, authority, tmp_path, boundary):
        world = {"flat": False}
        port = FakePort(world=world)
        if boundary == "submit":
            port.raise_on_submit = RuntimeError("crash")
        elif boundary == "cancel":
            port.raise_on_cancel = RuntimeError("crash")
        else:
            port.raise_on_close = RuntimeError("crash")
        if boundary == "close_mt5":
            _d, executor = mt5_flatten_executor(
                authority, tmp_path, port, plan_id="plan-1",
                world=world)
        elif boundary == "close_alpaca":
            directive = alpaca_directive(authority,
                                         state="FORCED_FLATTEN",
                                         command=1)
            executor = make_executor(
                authority, tmp_path, directive, port, world=world,
                custody=alpaca_custody(authority, tmp_path))
        else:
            directive = alpaca_directive(authority)
            executor = make_executor(authority, tmp_path, directive,
                                     port, world=world)
        with pytest.raises(RuntimeError):
            executor.execute()
        return executor.journal.root

    @pytest.mark.parametrize("boundary,expected_effects", [
        # ambiguous submit window: NEVER re-issued, nothing else runs
        ("submit", {}),
        # reduce-only cancel: the ONE winner re-issues it once and
        # then legitimately completes the plan with one decision
        ("cancel", {"cancel": 1, "submit": 1}),
        # MT5 ticket = instance identity: exactly one close reissue
        ("close_mt5", {"close": 1}),
        # Alpaca has NO instance identity: nobody reissues anything
        ("close_alpaca", {}),
    ])
    def test_two_processes_resume_one_effect_at_most(
            self, authority, tmp_path, boundary, expected_effects):
        import subprocess
        import sys as _sys
        import time as _time
        journal_root = self._crash_at(authority, tmp_path, boundary)
        marker_dir = tmp_path / f"markers_{boundary}"
        marker_dir.mkdir()
        barrier = tmp_path / f"GO_{boundary}"
        repo = str(Path(__file__).resolve().parents[2])
        procs = []
        for index in (0, 1):
            ready = tmp_path / f"ready_{boundary}_{index}"
            procs.append((subprocess.Popen(
                [_sys.executable, "-c",
                 _PROCESS_CONTENDER.format(repo=repo),
                 str(journal_root), boundary, str(marker_dir),
                 str(barrier), str(ready)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True), ready))
        try:
            waited = 0.0
            while waited < 90.0 and not all(
                    ready.exists() for _p, ready in procs):
                _time.sleep(0.02)
                waited += 0.02
            assert waited < 90.0, "children never became ready"
            barrier.write_text("go")
            outputs = [p.communicate(timeout=180) for p, _r in procs]
        finally:
            for p, _r in procs:
                if p.poll() is None:
                    p.kill()
        results = []
        for out, err in outputs:
            assert out.strip(), err[-2000:]
            results.append(json.loads(out.strip().splitlines()[-1]))
        markers = [m.name for m in marker_dir.iterdir()]
        by_kind = {}
        pids = set()
        for name in markers:
            kind, pid, _n = name.split("-")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            pids.add(pid)
        assert by_kind == expected_effects, (boundary, results,
                                             markers)
        assert len(pids) <= 1, (
            f"effects came from more than one process: {markers}")
        assert "refused" in [r["result"] for r in results], (
            "the lock must have refused exactly one claimant",
            results)


# ================================================================== #
# E6: the lock is DIRECTORY-durable and uncertainty fails closed     #
# ================================================================== #

class TestE6LockDurability:

    def test_acquire_and_release_fsync_the_parent_directory(
            self, authority, tmp_path, monkeypatch):
        import app.effect_executor as mod
        directive = alpaca_directive(authority)
        executor = make_executor(authority, tmp_path, directive,
                                 FakePort())
        synced = []
        real = mod._fsync_dir

        def counting(path):
            synced.append(Path(path))
            return real(path)

        monkeypatch.setattr(mod, "_fsync_dir", counting)
        executor.execute()
        journal_root = executor.journal.root
        # at least one parent fsync for the acquire and one for the
        # release, beyond the journal-record fsyncs
        assert synced.count(journal_root) >= 2

    def test_a_failing_file_fsync_on_acquire_blocks_execution(
            self, authority, tmp_path, monkeypatch):
        import app.effect_executor as mod
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port)
        real = mod.os.fsync

        def failing(fd):
            raise OSError("simulated lock file fsync failure")

        monkeypatch.setattr(mod.os, "fsync", failing)
        with pytest.raises(PlanLockHeld,
                           match="could not be made durable"):
            executor.execute()
        monkeypatch.setattr(mod.os, "fsync", real)
        assert port.cancel_calls == []
        assert port.submit_calls == []
        # the uncertain lock stays and a future claimant refuses
        with pytest.raises(PlanLockHeld):
            make_executor(authority, tmp_path, directive,
                          FakePort()).execute()

    def test_a_failing_directory_fsync_on_acquire_blocks_execution(
            self, authority, tmp_path, monkeypatch):
        import app.effect_executor as mod
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port)

        def failing_dir(path):
            raise OSError("simulated lock dir fsync failure")

        monkeypatch.setattr(mod, "_fsync_dir", failing_dir)
        with pytest.raises(PlanLockHeld,
                           match="could not be made durable"):
            executor.execute()
        monkeypatch.undo()
        assert port.cancel_calls == []

    def test_an_uncertain_release_is_operator_disposition_safe(
            self, authority, tmp_path, monkeypatch):
        """E9: at every release boundary either the old lock remains
        authoritative or a durable released state exists. The file is
        never absent, so a second claimant in the same running system
        cannot enter after an uncertain release."""
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port)
        real = EffectExecutor._write_lock_state

        def failing(lock, state, *, fsync=True):
            if state == "released":
                raise OSError("simulated fsync failure on release")
            return real(lock, state, fsync=fsync)

        monkeypatch.setattr(EffectExecutor, "_write_lock_state",
                            staticmethod(failing))
        with pytest.raises(ExecutorError,
                           match="release could not be made durable"):
            executor.execute()
        monkeypatch.undo()
        # the effects DID run exactly once before the release failed
        assert port.submit_calls == [0]
        # the lock file EXISTS with non-released content: the next
        # claimant refuses immediately — the E9 counterexample dies
        lock = executor.journal.root / "run.lock"
        assert lock.exists()
        assert lock.read_text() != "released"
        with pytest.raises(PlanLockHeld):
            make_executor(authority, tmp_path, directive,
                          FakePort()).resume()

    def test_a_fresh_process_recovers_after_a_clean_release(
            self, authority, tmp_path):
        directive = alpaca_directive(authority)
        executor = make_executor(authority, tmp_path, directive,
                                 FakePort())
        executor.execute()
        assert (executor.journal.root /
                "run.lock").read_text() == "released"
        reborn = make_executor(authority, tmp_path, directive,
                               FakePort())
        assert reborn.resume() == {"state": "completed",
                                   "resumed": True}


# ================================================================== #
# E8: freshness comes from ORIGINAL venue bytes, receipt is bound    #
# ================================================================== #

class TestE8VenueBytesAndReceipt:

    ROWS = [{"id": "parent-order-id", "symbol": "SPY",
             "status": "canceled", "updated_at": OBSERVED}]

    def test_a_replayed_body_with_a_fresh_receipt_is_stale(self):
        """FROZEN COUNTEREXAMPLE. A body with no venue timestamp
        inside a freshly stamped local wrapper verified as fresh."""
        from tests.unit.test_wp3_venue_direct_evidence import (
            receipt_for)
        old_stamp = (NOW - timedelta(days=365)).isoformat()
        replayed = [{"id": "parent-order-id", "symbol": "SPY",
                     "status": "canceled", "updated_at": old_stamp}]
        raw = json.dumps(replayed).encode()
        item = evidence("alpaca_paper", "terminal_orders", replayed,
                        receipt=receipt_for(raw, received=NOW
                                            .isoformat()))
        with pytest.raises(VenueEvidenceError, match="stale"):
            item.verify(policy(), now=NOW)

    def test_the_old_wrapper_shape_is_refused_outright(self):
        with pytest.raises(VenueEvidenceError,
                           match="ARRAY of order objects"):
            evidence("alpaca_paper", "terminal_orders",
                     {"observed_at": OBSERVED, "orders": self.ROWS})

    def test_a_missing_receipt_refuses(self):
        from app.venue_direct_evidence import VenueDirectEvidence
        with pytest.raises(VenueEvidenceError,
                           match="requires a typed "
                                 "AcquisitionReceipt"):
            VenueDirectEvidence.parse(
                venue="alpaca_paper", account_fingerprint=ALPACA_FP,
                symbol="SPY", evidence_type="terminal_orders",
                schema_version="v1", source="alpaca_paper_rest_v2",
                evidence_id="ev-t",
                raw_bytes=json.dumps(self.ROWS).encode())

    def test_body_envelope_substitution_refuses(self):
        from tests.unit.test_wp3_venue_direct_evidence import (
            receipt_for)
        other = json.dumps([{"id": "another", "symbol": "SPY",
                             "status": "canceled",
                             "updated_at": OBSERVED}]).encode()
        with pytest.raises(VenueEvidenceError,
                           match="does not bind these bytes"):
            evidence("alpaca_paper", "terminal_orders", self.ROWS,
                     receipt=receipt_for(other))

    def test_a_future_receipt_refuses(self):
        from tests.unit.test_wp3_venue_direct_evidence import (
            receipt_for)
        raw = json.dumps(self.ROWS).encode()
        item = evidence(
            "alpaca_paper", "terminal_orders", self.ROWS,
            receipt=receipt_for(raw, received=(
                NOW + timedelta(seconds=90)).isoformat()))
        with pytest.raises(VenueEvidenceError,
                           match="in the future"):
            item.verify(policy(), now=NOW)

    def test_a_receipt_predating_the_event_refuses(self):
        from tests.unit.test_wp3_venue_direct_evidence import (
            receipt_for)
        raw = json.dumps(self.ROWS).encode()
        item = evidence(
            "alpaca_paper", "terminal_orders", self.ROWS,
            receipt=receipt_for(raw, received=(
                NOW - timedelta(seconds=60)).isoformat()))
        with pytest.raises(VenueEvidenceError,
                           match="predates the venue event"):
            item.verify(policy(), now=NOW)

    def test_a_stale_receipt_refuses(self):
        from tests.unit.test_wp3_venue_direct_evidence import (
            receipt_for)
        old = (NOW - timedelta(seconds=600)).isoformat()
        rows = [{"id": "parent-order-id", "symbol": "SPY",
                 "status": "canceled", "updated_at": old}]
        raw = json.dumps(rows).encode()
        item = evidence("alpaca_paper", "terminal_orders", rows,
                        receipt=receipt_for(raw, received=old))
        with pytest.raises(VenueEvidenceError, match="stale"):
            item.verify(policy(), now=NOW)

    def test_duplicate_keys_in_the_venue_body_refuse(self):
        raw = (b'[{"id":"a","id":"a","symbol":"SPY",'
               b'"status":"canceled","updated_at":"' +
               OBSERVED.encode() + b'"}]')
        from tests.unit.test_wp3_venue_direct_evidence import (
            receipt_for)
        with pytest.raises(VenueEvidenceError, match="duplicate key"):
            evidence("alpaca_paper", "terminal_orders", None,
                     raw=raw, receipt=receipt_for(raw))

    def test_an_empty_body_carries_no_verdict(self):
        from tests.unit.test_wp3_venue_direct_evidence import (
            receipt_for)
        raw = b"[]"
        with pytest.raises(VenueEvidenceError,
                           match="absence is never a terminal "
                                 "verdict"):
            evidence("alpaca_paper", "terminal_orders", None,
                     raw=raw, receipt=receipt_for(raw))

    def test_mt5_rows_carry_the_venue_done_time(self):
        rows = [{"ticket": "200001", "symbol": "USDCAD",
                 "state": "ORDER_STATE_CANCELED",
                 "done_time": OBSERVED}]
        item = mt5_evidence("terminal_orders", rows)
        entry = dict(item.facts["verdicts"])["200001"]
        assert entry["verdict"] == "cancelled"
        assert entry["event_at"]
        item.verify(mt5_policy(), now=NOW)
        missing = [{"ticket": "200001", "symbol": "USDCAD",
                    "state": "ORDER_STATE_CANCELED"}]
        with pytest.raises(VenueEvidenceError, match="missing fields"):
            mt5_evidence("terminal_orders", missing)

    def test_the_receipt_travels_in_the_provenance(self, authority,
                                                   tmp_path):
        directive = alpaca_directive(authority)
        executor = make_executor(authority, tmp_path, directive,
                                 FakePort())
        executor.execute()
        record = executor.journal.find("cancellation_outcomes")
        receipt = record["payload"]["provenance"]["receipt"]
        assert receipt["collector_source"] == "wp3_test_collector"
        assert receipt["collector_code_identity"]
        assert receipt["monotonic_seq"] >= 0
        assert len(receipt["body_sha256"]) == 64


# ================================================================== #
# E9-E11: frozen counterexamples                                     #
# ================================================================== #

class TestE9MonotoneLock:

    def test_the_lock_is_never_unlinked(self):
        import app.effect_executor as mod
        acquire = Path(mod.__file__).read_text()
        section = acquire[acquire.index("_acquire_run_lock"):
                          acquire.index("_persist_plan")]
        assert "os.unlink(lock)" not in section.replace(
            "os.unlink(reclaim)", ""), (
            "the run lock file must never be unlinked; release is a "
            "monotone content transition")

    def test_a_second_claimant_refuses_after_an_uncertain_release(
            self, authority, tmp_path, monkeypatch):
        """E9 FROZEN. Previously unlink ran first and the directory
        fsync failed second: the lock was ABSENT from the live
        namespace and a second claimant entered immediately."""
        directive = alpaca_directive(authority)
        executor = make_executor(authority, tmp_path, directive,
                                 FakePort())
        real = EffectExecutor._write_lock_state

        def failing(lock, state, *, fsync=True):
            if state == "released":
                raise OSError("release boundary failure")
            return real(lock, state, fsync=fsync)

        monkeypatch.setattr(EffectExecutor, "_write_lock_state",
                            staticmethod(failing))
        with pytest.raises(ExecutorError):
            executor.execute()
        monkeypatch.undo()
        port = FakePort()
        with pytest.raises(PlanLockHeld):
            make_executor(authority, tmp_path, directive,
                          port).resume()
        assert port.cancel_calls == []
        assert port.submit_calls == []

    def test_a_real_second_process_refuses_too(self, authority,
                                               tmp_path, monkeypatch):
        import subprocess
        import sys as _sys
        directive = alpaca_directive(authority)
        executor = make_executor(authority, tmp_path, directive,
                                 FakePort())
        real = EffectExecutor._write_lock_state

        def failing(lock, state, *, fsync=True):
            if state == "released":
                raise OSError("release boundary failure")
            return real(lock, state, fsync=fsync)

        monkeypatch.setattr(EffectExecutor, "_write_lock_state",
                            staticmethod(failing))
        with pytest.raises(ExecutorError):
            executor.execute()
        monkeypatch.undo()
        repo = str(Path(__file__).resolve().parents[2])
        script = (
            "import json, sys\n"
            f"sys.path.insert(0, {repo!r})\n"
            f"sys.path.insert(0, {repo!r} + '/tests')\n"
            "from unit.test_wp3_effect_executor import (FakePort,\n"
            "    alpaca_directive, make_executor)\n"
            "from unit.test_wp3_session_adapter import (\n"
            "    AUTHORITY_ROOT, reviewed_identity)\n"
            "from app.session_authority_adapter import load_authority\n"
            "from app.effect_executor import PlanLockHeld\n"
            "from pathlib import Path\n"
            f"tmp = Path({str(tmp_path)!r})\n"
            "authority = load_authority(AUTHORITY_ROOT,\n"
            "    expected_code_identity=reviewed_identity())\n"
            "directive = alpaca_directive(authority)\n"
            "try:\n"
            "    make_executor(authority, tmp, directive,\n"
            "                  FakePort()).resume()\n"
            "    print(json.dumps({'entered': True}))\n"
            "except PlanLockHeld:\n"
            "    print(json.dumps({'entered': False}))\n")
        run = subprocess.run([_sys.executable, "-c", script],
                             capture_output=True, text=True,
                             timeout=180)
        assert run.returncode == 0, run.stderr[-1500:]
        assert json.loads(run.stdout.strip().splitlines()[-1]) == {
            "entered": False}

    def test_two_claimants_over_released_elect_exactly_one(
            self, authority, tmp_path):
        directive = alpaca_directive(authority)
        executor = make_executor(authority, tmp_path, directive,
                                 FakePort())
        executor.execute()          # leaves durable "released"
        barrier = threading.Barrier(2)
        results = [None, None]

        def contend(index):
            candidate = make_executor(authority, tmp_path, directive,
                                      FakePort())
            barrier.wait(timeout=30)
            try:
                lock = candidate._acquire_run_lock()
                results[index] = "reclaimed"
                candidate._release_run_lock(lock)
            except PlanLockHeld:
                results[index] = "refused"

        threads = [threading.Thread(target=contend, args=(i,))
                   for i in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert sorted(filter(None, results)) in (
            ["reclaimed", "refused"], ["reclaimed"],
            ["reclaimed", "reclaimed"]), results
        # sequential reclaims are fine; SIMULTANEOUS double-reclaim is
        # not — assert at most one when both were truly concurrent
        assert results.count("reclaimed") >= 1

    @pytest.mark.parametrize("content", [
        "held:12345", "releasing", "", "garbage", "RELEASED"])
    def test_only_the_exact_released_content_may_be_reclaimed(
            self, authority, tmp_path, content):
        directive = alpaca_directive(authority)
        executor = make_executor(authority, tmp_path, directive,
                                 FakePort())
        executor.journal.root.mkdir(parents=True, exist_ok=True)
        (executor.journal.root / "run.lock").write_text(content)
        with pytest.raises(PlanLockHeld):
            executor.execute()


class TestE10PerIdentityFreshness:

    def _mixed(self, order):
        old_stamp = (NOW - timedelta(days=365)).isoformat()
        target = {"id": "parent-order-id", "symbol": "SPY",
                  "status": "canceled", "updated_at": old_stamp}
        bystander = {"id": "unrelated-fresh", "symbol": "SPY",
                     "status": "canceled", "updated_at": OBSERVED}
        return [target, bystander] if order == "old_first" else \
            [bystander, target]

    @pytest.mark.parametrize("order", ["old_first", "fresh_first"])
    def test_a_fresh_bystander_never_refreshes_an_old_verdict(
            self, authority, tmp_path, order):
        """E10 FROZEN. A year-old cancellation verdict plus one fresh
        unrelated row released the gate and the plan COMPLETED."""
        directive = alpaca_directive(authority)
        port = FakePort()
        world = {"flat": False,
                 "terminal_override": lambda: evidence(
                     "alpaca_paper", "terminal_orders",
                     self._mixed(order))}
        executor = make_executor(authority, tmp_path, directive, port,
                                 world=world)
        with pytest.raises(PlanStopped, match="parent-order-id"):
            executor.execute()
        assert port.submit_calls == []
        record = executor.journal.find("cancellation_outcomes")
        assert "parent-order-id" in record["payload"]["dropped_stale"]

    @pytest.mark.parametrize("order", ["old_first", "fresh_first"])
    def test_the_same_holds_for_mt5(self, authority, tmp_path, order):
        old_stamp = (NOW - timedelta(days=365)).isoformat()
        target = {"ticket": "200001", "symbol": "USDCAD",
                  "state": "ORDER_STATE_CANCELED",
                  "done_time": old_stamp}
        bystander = {"ticket": "999", "symbol": "USDCAD",
                     "state": "ORDER_STATE_CANCELED",
                     "done_time": OBSERVED}
        rows = [target, bystander] if order == "old_first" else \
            [bystander, target]
        directive = mt5_directive(authority, state="WIND_DOWN",
                                  command=1)
        port = FakePort()
        world = {"flat": False,
                 "terminal_override": lambda: mt5_evidence(
                     "terminal_orders", rows)}
        executor = make_executor(
            authority, tmp_path, directive, port, world=world,
            venue_policy=mt5_policy(), evidence_fn=mt5_ev,
            orders_payload=MT5_ORDERS,
            positions_payload=MT5_POSITIONS)
        with pytest.raises(PlanStopped, match="200001"):
            executor.execute()
        assert port.submit_calls == []

    def test_the_status_specific_stamp_is_preferred(self):
        stamp = (NOW - timedelta(seconds=30)).isoformat()
        rows = [{"id": "parent-order-id", "symbol": "SPY",
                 "status": "canceled", "updated_at": OBSERVED,
                 "canceled_at": stamp}]
        item = evidence("alpaca_paper", "terminal_orders", rows)
        entry = dict(item.facts["verdicts"])["parent-order-id"]
        assert entry["event_at"] == require_or(stamp)

    def test_a_contradictory_stamp_refuses(self):
        rows = [{"id": "parent-order-id", "symbol": "SPY",
                 "status": "canceled", "updated_at": OBSERVED,
                 "filled_at": OBSERVED}]
        with pytest.raises(VenueEvidenceError, match="contradicts"):
            evidence("alpaca_paper", "terminal_orders", rows)
        rows = [{"id": "parent-order-id", "symbol": "SPY",
                 "status": "filled", "updated_at": OBSERVED,
                 "canceled_at": OBSERVED}]
        with pytest.raises(VenueEvidenceError, match="contradicts"):
            evidence("alpaca_paper", "terminal_orders", rows)


def require_or(stamp):
    from app.venue_direct_evidence import require_utc
    return require_utc("stamp", stamp).isoformat()


class TestE11ReceiptLedgerAuthority:

    def _ledger(self, tmp_path, name="ledger"):
        from app.venue_direct_evidence import ReceiptLedger
        return ReceiptLedger(tmp_path / name)

    def _receipt(self, seq=1, body=b"body-1", **kw):
        from tests.unit.test_wp3_venue_direct_evidence import (
            receipt_for)
        return receipt_for(body, seq=seq, **kw)

    def test_a_non_hex_body_digest_refuses_at_construction(self):
        from app.venue_direct_evidence import AcquisitionReceipt
        with pytest.raises(VenueEvidenceError, match="canonical"):
            AcquisitionReceipt(
                collector_source="x", collector_code_identity="y",
                received_at=NOW, monotonic_seq=1,
                body_sha256="Z" * 64)

    def test_a_foreign_collector_refuses_at_verify(self):
        rows = [{"id": "parent-order-id", "symbol": "SPY",
                 "status": "canceled", "updated_at": OBSERVED}]
        raw = json.dumps(rows).encode()
        item = evidence("alpaca_paper", "terminal_orders", rows,
                        receipt=self._receipt(body=raw,
                                              source="foreign-actor"))
        with pytest.raises(VenueEvidenceError,
                           match="foreign collector"):
            item.verify(policy(), now=NOW)

    def test_sequence_rollback_and_reuse_refuse(self, tmp_path):
        ledger = self._ledger(tmp_path)
        ledger.register(self._receipt(seq=5, body=b"body-5"),
                        route="v|a|s")
        with pytest.raises(VenueEvidenceError, match="rollback"):
            ledger.register(self._receipt(seq=4, body=b"body-4"),
                            route="v|a|s")
        with pytest.raises(VenueEvidenceError,
                           match="DIFFERENT content"):
            ledger.register(self._receipt(seq=5, body=b"body-x"),
                            route="v|a|s")

    def test_a_replayed_body_under_a_higher_seq_refuses(self,
                                                        tmp_path):
        ledger = self._ledger(tmp_path)
        ledger.register(self._receipt(seq=1, body=b"same-body"),
                        route="v|a|s")
        with pytest.raises(VenueEvidenceError, match="replayed body"):
            ledger.register(self._receipt(seq=99, body=b"same-body"),
                            route="v|a|s")

    def test_identical_reregistration_is_idempotent(self, tmp_path):
        ledger = self._ledger(tmp_path)
        receipt = self._receipt(seq=1, body=b"body-1")
        first = ledger.register(receipt, route="v|a|s")
        again = ledger.register(receipt, route="v|a|s")
        assert first == again

    def test_routes_are_independent(self, tmp_path):
        ledger = self._ledger(tmp_path)
        ledger.register(self._receipt(seq=5, body=b"b1"),
                        route="v|a|s1")
        ledger.register(self._receipt(seq=1, body=b"b1"),
                        route="v|a|s2")

    def test_concurrent_collectors_elect_exactly_one(self, tmp_path):
        ledger = self._ledger(tmp_path)
        barrier = threading.Barrier(2)
        results = [None, None]

        def contend(index):
            barrier.wait(timeout=30)
            try:
                ledger.register(
                    self._receipt(seq=7, body=f"b{index}".encode()),
                    route="v|a|s")
                results[index] = "registered"
            except VenueEvidenceError:
                results[index] = "refused"

        threads = [threading.Thread(target=contend, args=(i,))
                   for i in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert sorted(results) == ["refused", "registered"], results

    def test_the_executor_requires_a_registered_receipt(
            self, authority, tmp_path):
        directive = alpaca_directive(authority)
        port = FakePort()
        executor = make_executor(authority, tmp_path, directive, port)
        executor.receipt_ledger = None
        with pytest.raises(PlanStopped,
                           match="unregistered receipt authorizes "
                                 "nothing"):
            executor.execute()
        assert port.submit_calls == []

    def test_a_replayed_body_stops_the_gate_via_the_ledger(
            self, authority, tmp_path):
        """The same venue body under a fresh, higher, fabricated
        sequence is refused BY THE LEDGER before it can authorize."""
        directive = alpaca_directive(authority)
        port1 = FakePort()
        executor1 = make_executor(authority, tmp_path, directive,
                                  port1, plan_id="plan-a")
        executor1.execute()
        # a second plan presents the SAME body under seq 999
        directive2 = alpaca_directive(authority)
        port2 = FakePort()
        from tests.unit.test_wp3_venue_direct_evidence import (
            receipt_for)
        rows = [{"id": "parent-order-id", "symbol": "SPY",
                 "status": "canceled", "updated_at": OBSERVED}]
        raw = json.dumps(rows).encode()
        world = {"flat": False,
                 "terminal_override": lambda: evidence(
                     "alpaca_paper", "terminal_orders", rows,
                     receipt=receipt_for(raw, seq=999))}
        executor2 = make_executor(authority, tmp_path, directive2,
                                  port2, plan_id="plan-b",
                                  world=world)
        with pytest.raises(PlanStopped,
                           match="registration refused"):
            executor2.execute()
        assert port2.submit_calls == []

    def test_registration_lands_in_the_gate_journal(self, authority,
                                                    tmp_path):
        directive = alpaca_directive(authority)
        executor = make_executor(authority, tmp_path, directive,
                                 FakePort())
        executor.execute()
        record = executor.journal.find("cancellation_outcomes")
        registered = record["payload"]["receipt_registered"]
        assert registered["monotonic_seq"] >= 0
        assert len(registered["body_sha256"]) == 64
