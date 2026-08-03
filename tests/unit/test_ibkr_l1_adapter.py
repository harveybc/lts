"""Adversarial tests for the IBKR Paper L1 adapter (auditor's mandatory list).

Sockets are booby-trapped module-wide: every test proves the adapter reaches
no network unless a valid authorization exists AND dry_run is disabled — and
no test here ever disables it.
"""
import json
import socket
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from trading_contracts import OrderIntentV2, ProtectiveBracket, RiskEnvelope

from app.ibkr_l1_adapter import (
    IbkrPaperL1Sink,
    L1Authorization,
    L1AuthorizationError,
    L1ExecutionError,
    L1Profile,
    build_bracket,
    verify_bracket_acknowledgement,
)

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
PHRASE = "ACTIVATE L1 CANARY IBKR PAPER NOW"
CAP_HASH = "sha256:" + "c" * 64


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted without authorization")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


class FakeLedger:
    def __init__(self):
        self.tokens = set()
        self.journal = []

    def activation_token_seen(self, token):
        return token in self.tokens

    def burn_activation_token(self, token, **kw):
        self.tokens.add(token)

    def journal_submission(self, **kw):
        self.journal.append(kw)


def _profile_payload(**overrides):
    payload = {
        "venue": "ibkr_paper",
        "account_fingerprint": "86aa086401855219",
        "environment": "paper",
        "host": "127.0.0.1",
        "port": 7497,
        "client_id": 77,
        "instrument": "EUR.USD",
        "asset_id": "fx:EUR/USD",
        "activation_phrase": PHRASE,
        "max_orders_this_activation": 2,
        "quantity": 20000.0,
        "stop_distance_price": 0.0020,
        "take_profit_distance_price": 0.0040,
        "max_spread_price": 0.0003,
    }
    payload.update(overrides)
    return payload


def _profile(tmp_path, **overrides):
    path = tmp_path / "l1_profile.json"
    path.write_text(json.dumps(_profile_payload(**overrides)))
    return L1Profile.load(path)


def _auth(profile, phrase=PHRASE, token="tok-1", issued_at=None):
    # the validity window is wall-clock by design (a stale authorization must
    # expire in reality, not in fixture time), so fresh auths use real now
    return L1Authorization(
        profile=profile, supplied_phrase=phrase, token=token,
        issued_at=issued_at or datetime.now(timezone.utc),
    )


def _intent(instrument="EUR.USD", asset="fx:EUR/USD", units=20000.0,
            sl=1.0850, tp=1.0910):
    return OrderIntentV2(
        object_id="oi2-l1-1", as_of=NOW,
        producer={"name": "lts.demo_execution_service", "version": "0.2.0"},
        trace_id="t-l1", account_ref="86aa086401855219", asset_id=asset,
        venue="ibkr_paper", instrument=instrument,
        intent_class="risk_increasing", order_type="market",
        delta_units=units,
        protection=ProtectiveBracket(stop_loss_price=sl, take_profit_price=tp),
        risk=RiskEnvelope(risk_fraction_at_stop=0.005,
                          gross_notional_fraction=0.05, margin_fraction=0.02,
                          daily_loss_budget_fraction=0.02,
                          reservation_id="rsv-l1"),
        capability_snapshot_hash=CAP_HASH, idempotency_key="idem-l1-1",
    )


# ── profile and authorization: the gate ──

def test_profile_rejects_live_environment(tmp_path):
    with pytest.raises(L1AuthorizationError, match="paper"):
        _profile(tmp_path, environment="live")


def test_profile_rejects_non_paper_port(tmp_path):
    with pytest.raises(L1AuthorizationError, match="7497"):
        _profile(tmp_path, port=7496)


def test_profile_rejects_order_budget_above_two(tmp_path):
    with pytest.raises(L1AuthorizationError, match="at most 2"):
        _profile(tmp_path, max_orders_this_activation=5)


def test_wrong_phrase_refuses_and_opens_no_socket(tmp_path):
    profile = _profile(tmp_path)
    with pytest.raises(L1AuthorizationError, match="phrase"):
        IbkrPaperL1Sink(_auth(profile, phrase="activate l1 canary ibkr paper now"),
                        ledger=FakeLedger())


def test_missing_token_refuses(tmp_path):
    profile = _profile(tmp_path)
    with pytest.raises(L1AuthorizationError, match="token"):
        IbkrPaperL1Sink(_auth(profile, token=""), ledger=FakeLedger())


def test_duplicate_activation_refuses_second_use(tmp_path):
    profile = _profile(tmp_path)
    ledger = FakeLedger()
    IbkrPaperL1Sink(_auth(profile), ledger=ledger)
    with pytest.raises(L1AuthorizationError, match="single-use"):
        IbkrPaperL1Sink(_auth(profile), ledger=ledger)


def test_expired_authorization_refuses(tmp_path):
    profile = _profile(tmp_path)
    stale = _auth(profile,
                  issued_at=datetime.now(timezone.utc) - timedelta(hours=2))
    with pytest.raises(L1AuthorizationError, match="validity window"):
        IbkrPaperL1Sink(stale, ledger=FakeLedger())


def test_edited_profile_changes_hash(tmp_path):
    first = _profile(tmp_path)
    second = _profile(tmp_path, quantity=25000.0)
    assert first.profile_hash != second.profile_hash


# ── bracket construction: the protection contract in venue terms ──

def _plan(intent=None, **kw):
    args = dict(parent_order_id=1000, account="DU1234567",
                price_decimals=5, quantity_decimals=0)
    args.update(kw)
    return build_bracket(intent or _intent(), **args)


def test_official_transmission_order_and_flags():
    plan = _plan()
    ordered = plan.transmission_order()
    assert [o["orderId"] for o in ordered] == [1000, 1001, 1002]
    assert ordered[0]["transmit"] is False       # parent
    assert ordered[1]["transmit"] is False       # take-profit child
    assert ordered[2]["transmit"] is True        # stop-loss child transmits all
    assert ordered[1]["parentId"] == 1000
    assert ordered[2]["parentId"] == 1000


def test_long_children_are_sell_side():
    plan = _plan()
    assert plan.parent["action"] == "BUY"
    assert plan.take_profit["action"] == "SELL"
    assert plan.stop_loss["action"] == "SELL"
    assert plan.take_profit["orderType"] == "LMT"
    assert plan.stop_loss["orderType"] == "STP"


def test_short_children_are_buy_side():
    plan = _plan(_intent(units=-20000.0, sl=1.0910, tp=1.0850))
    assert plan.parent["action"] == "SELL"
    assert plan.take_profit["action"] == "BUY"
    assert plan.stop_loss["action"] == "BUY"


def test_wrong_side_geometry_is_unconstructable_at_the_contract():
    with pytest.raises(ValidationError):
        _intent(sl=1.0910, tp=1.0850)            # long with inverted bracket
    with pytest.raises(ValidationError):
        _intent(units=-20000.0, sl=1.0850, tp=1.0910)


def test_rounding_that_destroys_geometry_rejects():
    tight = _intent(sl=1.08501, tp=1.08502)
    with pytest.raises(L1ExecutionError, match="geometry"):
        _plan(tight, price_decimals=2)           # both round to 1.09


def test_quantity_rounding_to_zero_rejects():
    tiny = _intent(units=0.4)
    with pytest.raises(L1ExecutionError, match="rounded to zero"):
        _plan(tiny, quantity_decimals=0)


def test_unprotected_intent_cannot_build_a_bracket():
    close_only = OrderIntentV2(
        object_id="oi2-close", as_of=NOW,
        producer={"name": "t", "version": "0"}, trace_id="t",
        account_ref="fp", asset_id="fx:EUR/USD", venue="ibkr_paper",
        instrument="EUR.USD", intent_class="risk_reducing",
        reduce_action="flatten",
        reduce_target_order_intent_id="oi2-l1-1",
        order_type="market", delta_units=-20000.0, idempotency_key="k",
    )
    with pytest.raises(L1ExecutionError, match="protected"):
        _plan(close_only)


# ── acknowledgement verification: the hard post-submit condition ──

def _ack(plan, drop=None, mutate=None):
    orders = []
    for name, spec in (("parent", plan.parent), ("take_profit", plan.take_profit),
                       ("stop_loss", plan.stop_loss)):
        if name == drop:
            continue
        entry = {
            "orderId": spec["orderId"], "action": spec["action"],
            "totalQuantity": spec["totalQuantity"], "status": "Submitted",
        }
        if "parentId" in spec:
            entry["parentId"] = spec["parentId"]
        if mutate and name in mutate:
            entry.update(mutate[name])
        orders.append(entry)
    return orders


def test_full_acknowledgement_is_protected():
    plan = _plan()
    verdict = verify_bracket_acknowledgement(plan=plan, open_orders=_ack(plan))
    assert verdict["protected"] is True


@pytest.mark.parametrize("missing", ["parent", "take_profit", "stop_loss"])
def test_any_missing_leg_demands_cancel_flatten_and_hold(missing):
    plan = _plan()
    verdict = verify_bracket_acknowledgement(
        plan=plan, open_orders=_ack(plan, drop=missing))
    assert verdict["protected"] is False
    assert verdict["required_action"] == "cancel_flatten_and_global_hold"


def test_child_without_parent_link_is_not_protected():
    plan = _plan()
    verdict = verify_bracket_acknowledgement(
        plan=plan, open_orders=_ack(plan, mutate={"stop_loss": {"parentId": 999}}))
    assert verdict["protected"] is False


def test_quantity_mismatch_is_not_protected():
    plan = _plan()
    verdict = verify_bracket_acknowledgement(
        plan=plan, open_orders=_ack(plan, mutate={"take_profit": {"totalQuantity": 10000.0}}))
    assert verdict["protected"] is False


def test_wrong_side_child_is_not_protected():
    plan = _plan()
    verdict = verify_bracket_acknowledgement(
        plan=plan, open_orders=_ack(plan, mutate={"stop_loss": {"action": "BUY"}}))
    assert verdict["protected"] is False


def test_empty_broker_evidence_is_never_read_as_success():
    plan = _plan()
    verdict = verify_bracket_acknowledgement(plan=plan, open_orders=[])
    assert verdict["protected"] is False
    assert verdict["required_action"] == "cancel_flatten_and_global_hold"


def test_child_acknowledged_before_parent_is_not_protected():
    plan = _plan()
    verdict = verify_bracket_acknowledgement(
        plan=plan, open_orders=_ack(plan, drop="parent"))
    assert verdict["protected"] is False
    assert verdict["legs"]["stop_loss"]["acknowledged"] is True


# ── submission gating: identity, budget, journal, dry-run ──

def _sink(tmp_path, ledger=None, **profile_kw):
    profile = _profile(tmp_path, **profile_kw)
    return IbkrPaperL1Sink(_auth(profile), ledger=ledger or FakeLedger())


def test_wrong_venue_intent_refuses(tmp_path):
    sink = _sink(tmp_path)
    intent = _intent()
    bad = intent.model_copy(update={"venue": "alpaca_paper"})
    with pytest.raises(L1ExecutionError, match="venue"):
        sink.submit_bracket(bad, _plan())


def test_wrong_instrument_intent_refuses(tmp_path):
    sink = _sink(tmp_path)
    with pytest.raises(L1ExecutionError, match="instrument"):
        sink.submit_bracket(_intent(instrument="USD.CAD"), _plan())


def test_wrong_asset_intent_refuses(tmp_path):
    sink = _sink(tmp_path)
    bad = _intent(asset="fx:USD/CAD")
    with pytest.raises(L1ExecutionError, match="asset"):
        sink.submit_bracket(bad, _plan())


def test_order_budget_is_enforced(tmp_path):
    sink = _sink(tmp_path, max_orders_this_activation=1)
    sink.submissions = 1
    with pytest.raises(L1AuthorizationError, match="budget exhausted"):
        sink.submit_bracket(_intent(), _plan())


def test_dry_run_journals_but_never_submits(tmp_path):
    ledger = FakeLedger()
    sink = _sink(tmp_path, ledger=ledger)
    result = sink.submit_bracket(_intent(), _plan())
    assert result["submitted"] is False and result["reason"] == "dry_run"
    assert sink.network_submissions == 0
    assert len(ledger.journal) == 1                    # journaled before effect
    assert ledger.journal[0]["order_ids"] == [1000, 1001, 1002]


def test_live_mode_without_connection_refuses(tmp_path):
    sink = _sink(tmp_path)
    sink.dry_run = False
    with pytest.raises(L1ExecutionError, match="not connected"):
        sink.submit_bracket(_intent(), _plan())
    assert sink.network_submissions == 0


def test_sink_interface_matches_l0_shape(tmp_path):
    from app.demo_execution_service import ZeroNetworkSink
    l0 = ZeroNetworkSink().serialize(_intent())
    l1 = _sink(tmp_path).serialize(_intent())
    assert set(l0) == set(l1)                          # same service contract
    assert l1["bracket"]["transmit_rule"] == "parent_and_children_atomic"
    assert l1["adapter"].endswith("l1.v1")
