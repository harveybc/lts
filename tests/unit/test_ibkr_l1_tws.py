from types import SimpleNamespace

from app.ibkr_l1_adapter import BracketPlan
from app.ibkr_l1_recovery import verify_bracket_exact
from app.ibkr_l1_tws import IbAsyncTwsClient


def _contract():
    return SimpleNamespace(
        secType="CASH", symbol="USD", currency="CAD",
        exchange="IDEALPRO", conId=15016062,
    )


def _completed(*, perm_id=1193220731, status="Filled"):
    return SimpleNamespace(
        contract=_contract(),
        order=SimpleNamespace(
            orderId=0, parentId=0, permId=perm_id, action="SELL",
            orderType="MKT", totalQuantity=0.0, filledQuantity=20000.0,
            lmtPrice=0.0, auxPrice=0.0, tif="DAY", transmit=True,
            account="DUR378700",
        ),
        orderStatus=SimpleNamespace(status=status),
    )


def _fill(*, perm_id=1193220731, order_id=7, cumulative=20000.0):
    return SimpleNamespace(
        contract=_contract(),
        execution=SimpleNamespace(
            permId=perm_id, orderId=order_id, cumQty=cumulative,
        ),
    )


def test_completed_parent_is_reconstructed_only_from_matching_execution():
    facts = IbAsyncTwsClient._completed_execution_facts(
        [_completed()], [_fill()]
    )
    assert facts == [{
        "orderId": 7, "parentId": 0, "action": "SELL", "orderType": "MKT",
        "totalQuantity": 20000.0, "filled": 20000.0, "remaining": 0.0,
        "lmtPrice": 0.0, "auxPrice": 0.0, "tif": "DAY", "transmit": True,
        "account": "DUR378700", "status": "Filled",
        "contract": {
            "secType": "CASH", "symbol": "USD", "currency": "CAD",
            "exchange": "IDEALPRO", "conId": 15016062,
        },
    }]


def test_completed_order_without_matching_execution_produces_no_fact():
    assert IbAsyncTwsClient._completed_execution_facts(
        [_completed()], [_fill(perm_id=999, order_id=99)]
    ) == []


def test_reconstructed_parent_and_open_children_prove_exact_protection():
    parent = IbAsyncTwsClient._completed_execution_facts(
        [_completed()], [_fill()]
    )[0]
    contract = parent["contract"]
    take = {
        **parent, "orderId": 8, "parentId": 7, "action": "BUY",
        "orderType": "LMT", "filled": 0.0, "remaining": 20000.0,
        "lmtPrice": 1.40035, "tif": "GTC", "status": "Submitted",
        "contract": contract,
    }
    stop = {
        **parent, "orderId": 9, "parentId": 7, "action": "BUY",
        "orderType": "STP", "filled": 0.0, "remaining": 20000.0,
        "auxPrice": 1.40667, "tif": "GTC", "status": "PreSubmitted",
        "contract": contract,
    }
    plan = BracketPlan(
        parent={
            "orderId": 7, "parentId": 0, "action": "SELL", "orderType": "MKT",
            "totalQuantity": 20000.0, "account": "DUR378700", "tif": "DAY",
            "transmit": False,
        },
        take_profit={
            "orderId": 8, "parentId": 7, "action": "BUY", "orderType": "LMT",
            "totalQuantity": 20000.0, "account": "DUR378700", "tif": "GTC",
            "transmit": False, "lmtPrice": 1.40035,
        },
        stop_loss={
            "orderId": 9, "parentId": 7, "action": "BUY", "orderType": "STP",
            "totalQuantity": 20000.0, "account": "DUR378700", "tif": "GTC",
            "transmit": True, "auxPrice": 1.40667,
        },
    )
    verdict = verify_bracket_exact(
        plan=plan, open_orders=[parent, take, stop], instrument="USD.CAD",
        expected_con_id=15016062,
    )
    assert verdict["protected"] is True
    assert verdict["failures"] == []
