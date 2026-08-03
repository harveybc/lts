"""Real, Paper-only TWS implementation of the narrow IBKR L1 protocol."""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from app.ibkr_l1_adapter import IB_ASYNC_VERSION, L1ExecutionError, L1Profile
from app.ibkr_l1_broker import IbkrClientProtocol, order_fact


class IbAsyncTwsClient(IbkrClientProtocol):
    """One persistent local TWS Paper session.

    The constructor is the only connection point. It accepts only the strict
    ``L1Profile`` (loopback, port 7497, Paper account fingerprint) and refuses
    multiple accounts so every effect has one unambiguous owner.
    """

    def __init__(
        self,
        profile: L1Profile,
        *,
        timeout_seconds: float = 15.0,
        settle_seconds: float = 0.20,
    ) -> None:
        if profile.host != "127.0.0.1" or profile.port != 7497:
            raise L1ExecutionError("IBKR L1 may connect only to local TWS Paper")
        try:
            import ib_async
            from ib_async import IB, StartupFetch
        except ImportError as exc:
            raise L1ExecutionError(
                f"ib_async=={IB_ASYNC_VERSION} is required"
            ) from exc
        if ib_async.__version__ != IB_ASYNC_VERSION:
            raise L1ExecutionError(
                f"expected ib_async {IB_ASYNC_VERSION}, found {ib_async.__version__}"
            )
        self.profile = profile
        self.settle_seconds = float(settle_seconds)
        self.ib = IB()
        try:
            # TWS installations can disable the completed-orders startup
            # request. Fetch positions, account facts and executions here;
            # this client requests open orders explicitly after connection.
            self.ib.connect(
                profile.host,
                profile.port,
                clientId=profile.client_id,
                timeout=float(timeout_seconds),
                readonly=False,
                raiseSyncErrors=True,
                fetchFields=(
                    StartupFetch.POSITIONS
                    | StartupFetch.ACCOUNT_UPDATES
                    | StartupFetch.EXECUTIONS
                ),
            )
        except Exception:
            self.close()
            raise
        accounts = list(self.ib.managedAccounts())
        if len(accounts) != 1 or not accounts[0].upper().startswith("DU"):
            self.close()
            raise L1ExecutionError(
                "TWS L1 requires exactly one connected IBKR Paper DU account"
            )
        self._account = str(accounts[0])
        observed = hashlib.sha256(self._account.encode()).hexdigest()[:16]
        if observed != profile.account_fingerprint:
            self.close()
            raise L1ExecutionError(
                "TWS Paper account fingerprint does not match the L1 profile"
            )

    def close(self) -> None:
        if getattr(self, "ib", None) is not None and self.ib.isConnected():
            self.ib.disconnect()

    def __enter__(self) -> "IbAsyncTwsClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def connected_account(self) -> Optional[str]:
        if not self.ib.isConnected():
            return None
        return self._account

    def _qualify(self, contract: Any) -> Any:
        if int(getattr(contract, "conId", 0) or 0) > 0:
            return contract
        matches = self.ib.qualifyContracts(contract)
        if len(matches) != 1:
            raise L1ExecutionError("TWS did not resolve exactly one contract")
        return matches[0]

    @staticmethod
    def _trade_fact(trade: Any) -> dict[str, Any]:
        status = str(getattr(trade.orderStatus, "status", "") or "Unknown")
        fact = order_fact(trade.contract, trade.order, status)
        fact["filled"] = float(getattr(trade.orderStatus, "filled", 0.0) or 0.0)
        fact["remaining"] = float(
            getattr(trade.orderStatus, "remaining", fact["totalQuantity"])
        )
        return fact

    def _known_trades(self) -> list[Any]:
        self.ib.sleep(0)
        by_id: dict[int, Any] = {}
        for trade in self.ib.trades():
            by_id[int(trade.order.orderId)] = trade
        for trade in self.ib.reqAllOpenOrders():
            by_id[int(trade.order.orderId)] = trade
        self.ib.sleep(self.settle_seconds)
        return list(by_id.values())

    def place_order(self, contract: Any, order: Any) -> dict[str, Any]:
        if not self.ib.isConnected():
            raise ConnectionError("TWS Paper session is disconnected")
        if str(getattr(order, "account", "")) != self._account:
            raise L1ExecutionError("order account differs from connected Paper account")
        qualified = self._qualify(contract)
        trade = self.ib.placeOrder(qualified, order)
        self.ib.sleep(self.settle_seconds)
        return self._trade_fact(trade)

    def cancel_order(self, order_id: int) -> dict[str, Any]:
        match = next(
            (
                trade for trade in self._known_trades()
                if int(trade.order.orderId) == int(order_id)
            ),
            None,
        )
        if match is None:
            return {"orderId": int(order_id), "status": "Unknown"}
        trade = self.ib.cancelOrder(match.order)
        self.ib.sleep(self.settle_seconds)
        status = "Unknown" if trade is None else str(trade.orderStatus.status)
        return {"orderId": int(order_id), "status": status}

    def open_order_facts(self) -> list[dict[str, Any]]:
        return [self._trade_fact(trade) for trade in self._known_trades()]

    def position_facts(self) -> list[dict[str, Any]]:
        self.ib.sleep(0)
        return [
            {
                "account": str(position.account),
                "symbol": str(position.contract.symbol),
                "currency": str(position.contract.currency),
                "secType": str(position.contract.secType),
                "conId": int(position.contract.conId or 0),
                "units": float(position.position),
                "averageCost": float(position.avgCost),
            }
            for position in self.ib.positions(self._account)
        ]

    def next_order_id(self) -> int:
        return self.reserve_order_ids(1)

    def reserve_order_ids(self, count: int) -> int:
        if count < 1:
            raise ValueError("count must be positive")
        ids = [int(self.ib.client.getReqId()) for _ in range(count)]
        if ids != list(range(ids[0], ids[0] + count)):
            raise L1ExecutionError("TWS did not reserve a contiguous order-id block")
        return ids[0]

    def account_balance(self) -> dict[str, float]:
        values = {
            item.tag: float(item.value)
            for item in self.ib.accountSummary(self._account)
            if item.currency in ("USD", "BASE", "")
            and item.tag in ("NetLiquidation", "TotalCashValue", "AvailableFunds")
        }
        return {
            "equity": values.get("NetLiquidation", 0.0),
            "cash": values.get("TotalCashValue", 0.0),
            "available_funds": values.get("AvailableFunds", 0.0),
        }
