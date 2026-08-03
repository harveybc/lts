"""Adversarial tests for the IBKR Paper L1 venue binding (post-063/064/067).

Sockets are booby-trapped module-wide. The former activation-phrase
authorization and the lying ``submit_bracket`` no longer exist: submission
lives only in ``BracketExecutor`` (see ``test_ibkr_l1_effects.py``) and
authority only in owner capabilities (see ``test_ibkr_l1_capability.py``).
"""
import json
import socket
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from trading_contracts import OrderIntentV2, ProtectiveBracket, RiskEnvelope

from app.ibkr_l1_adapter import (
    IbkrPaperL1Sink,
    L1AuthorizationError,
    L1ExecutionError,
    L1Profile,
    build_bracket,
    verify_bracket_acknowledgement,
)

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
CAP_HASH = "sha256:" + "c" * 64


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted without authorization")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


def _profile_payload(**overrides):
    payload = {
        "schema_version": "lts.ibkr.paper.l1.profile.v2",
        "venue": "ibkr_paper",
        "environment": "paper",
        "host": "127.0.0.1",
        "port": 7497,
        "client_id": 77,
        "account_fingerprint_algorithm": "account_id_sha256_16",
        "account_fingerprint": "c0ff137a3cc1a363",
        "instrument": "EUR.USD",
        "asset_id": "fx:EUR/USD",
        "max_orders_this_activation": 2,
        "quantity_ceiling": 20000.0,
        "stop_distance_price_max": 0.0020,
        "take_profit_distance_price_max": 0.0040,
        "max_spread_price": 0.0003,
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not None}


def _profile(tmp_path, **overrides):
    path = tmp_path / "l1_profile.json"
    path.write_text(json.dumps(_profile_payload(**overrides)))
    return L1Profile.load(path)


def _intent(instrument="EUR.USD", asset="fx:EUR/USD", units=20000.0,
            sl=1.0850, tp=1.0910):
    return OrderIntentV2(
        object_id="oi2-l1-1", as_of=NOW,
        producer={"name": "lts.demo_execution_service", "version": "0.2.0"},
        trace_id="t-l1", account_ref="c0ff137a3cc1a363", asset_id=asset,
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


# ── strict profile v2: finding 067 ──

def test_profile_v2_loads_and_hashes(tmp_path):
    profile = _profile(tmp_path)
    assert profile.venue == "ibkr_paper" and profile.port == 7497
    assert profile.account_fingerprint_algorithm == "account_id_sha256_16"
    assert len(profile.profile_hash) == 64


def test_profile_rejects_wrong_schema_version(tmp_path):
    with pytest.raises(L1AuthorizationError, match="schema"):
        _profile(tmp_path, schema_version="lts.ibkr.paper.l1.profile.v1")


def test_profile_rejects_live_environment(tmp_path):
    with pytest.raises(L1AuthorizationError, match="paper"):
        _profile(tmp_path, environment="live")


def test_profile_rejects_arbitrary_venue(tmp_path):
    with pytest.raises(L1AuthorizationError, match="venue"):
        _profile(tmp_path, venue="anything")


def test_profile_rejects_non_loopback_host(tmp_path):
    with pytest.raises(L1AuthorizationError, match="loopback"):
        _profile(tmp_path, host="0.0.0.0")


def test_profile_rejects_non_paper_port(tmp_path):
    with pytest.raises(L1AuthorizationError, match="7497"):
        _profile(tmp_path, port=7496)


@pytest.mark.parametrize("client_id", [0, -1, 1000])
def test_profile_rejects_unbounded_client_id(tmp_path, client_id):
    with pytest.raises(L1AuthorizationError, match="client_id"):
        _profile(tmp_path, client_id=client_id)


@pytest.mark.parametrize("budget", [0, 3, -1])
def test_profile_rejects_order_budget_outside_one_or_two(tmp_path, budget):
    with pytest.raises(L1AuthorizationError, match="1 or 2"):
        _profile(tmp_path, max_orders_this_activation=budget)


@pytest.mark.parametrize("key,value", [
    ("quantity_ceiling", 0.0),
    ("quantity_ceiling", -1.0),
    ("quantity_ceiling", float("inf")),
    ("stop_distance_price_max", -2.0),
    ("take_profit_distance_price_max", 0.0),
    ("max_spread_price", -4.0),
])
def test_profile_rejects_non_positive_or_non_finite_limits(tmp_path, key, value):
    with pytest.raises(L1AuthorizationError, match=key):
        _profile(tmp_path, **{key: value})


def test_profile_rejects_sanity_ceiling_breach(tmp_path):
    with pytest.raises(L1AuthorizationError, match="sanity ceiling"):
        _profile(tmp_path, quantity_ceiling=2_000_000.0)


def test_profile_rejects_wrong_fingerprint_algorithm(tmp_path):
    with pytest.raises(L1AuthorizationError, match="algorithm"):
        _profile(tmp_path, account_fingerprint_algorithm="account_set_sha256_16")


@pytest.mark.parametrize("fp", ["", "xyz", "C0FF137A3CC1A363", "c0ff137a3cc1a3"])
def test_profile_rejects_malformed_fingerprint(tmp_path, fp):
    with pytest.raises(L1AuthorizationError, match="fingerprint"):
        _profile(tmp_path, account_fingerprint=fp)


@pytest.mark.parametrize("instrument", ["EURUSD", "EUR/USD", "eur.usd", "E1.USD"])
def test_profile_rejects_malformed_instrument(tmp_path, instrument):
    with pytest.raises(L1AuthorizationError, match="instrument"):
        _profile(tmp_path, instrument=instrument)


def test_profile_rejects_asset_instrument_mismatch(tmp_path):
    with pytest.raises(L1AuthorizationError, match="asset_id"):
        _profile(tmp_path, asset_id="fx:USD/CAD")


def test_profile_rejects_unknown_keys(tmp_path):
    with pytest.raises(L1AuthorizationError, match="unknown keys"):
        _profile(tmp_path, activation_phrase="ANY PHRASE AT ALL")


def test_profile_rejects_missing_keys(tmp_path):
    payload = _profile_payload()
    del payload["max_spread_price"]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(L1AuthorizationError, match="missing"):
        L1Profile.load(path)


def test_edited_profile_changes_hash(tmp_path):
    first = _profile(tmp_path)
    second = _profile(tmp_path, quantity_ceiling=25000.0)
    assert first.profile_hash != second.profile_hash


def test_repository_phrase_authorization_no_longer_exists():
    import app.ibkr_l1_adapter as adapter
    assert not hasattr(adapter, "L1Authorization")
    assert not hasattr(IbkrPaperL1Sink, "submit_bracket")


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


# ── the serialize sink: same service contract as L0 ──

def test_sink_interface_matches_l0_shape(tmp_path):
    from app.demo_execution_service import ZeroNetworkSink
    l0 = ZeroNetworkSink().serialize(_intent())
    l1 = IbkrPaperL1Sink(_profile(tmp_path)).serialize(_intent())
    assert set(l0) == set(l1)                          # same service contract
    assert l1["bracket"]["transmit_rule"] == "parent_and_children_atomic"
    assert l1["adapter"].endswith("l1.v2")


def test_sink_holds_no_write_capable_connection_path(tmp_path):
    sink = IbkrPaperL1Sink(_profile(tmp_path))
    assert not hasattr(sink, "connect")            # only connect_readonly exists
    assert hasattr(sink, "connect_readonly")
