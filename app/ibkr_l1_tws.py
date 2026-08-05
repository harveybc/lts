"""Real, Paper-only TWS implementation of the narrow IBKR L1 protocol."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone
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
        # Finding 100: record connectivity transitions (1100 lost, 1101
        # lost+reset, 1102 restored) so reconciliation can mark the
        # TWS-local order/position caches suspect after a blip.
        self._connectivity_events: list[tuple[int, str]] = []

        def _on_error(reqId, errorCode, errorString, *_rest):
            if int(errorCode) in (1100, 1101, 1102):
                self._connectivity_events.append((
                    int(errorCode),
                    datetime.now(timezone.utc).isoformat(),
                ))

        self.ib.errorEvent += _on_error

    def connectivity_events(self) -> list[tuple[int, str]]:
        """(code, iso_utc) for 1100/1101/1102 seen this session."""
        return list(self._connectivity_events)

    def execution_units_for_order(self, order_id: int) -> Optional[float]:
        """Signed filled units for one order from SERVER-SIDE execution
        reports — authoritative over open-order/position caches after a
        connectivity blip (finding 100). None when no execution report
        exists for the order."""
        total = 0.0
        seen = False
        for fill in self.ib.fills():
            execution = getattr(fill, "execution", None)
            if execution is None or int(getattr(execution, "orderId", -1)) \
                    != int(order_id):
                continue
            seen = True
            side = str(getattr(execution, "side", "")).upper()
            sign = 1.0 if side.startswith("B") else -1.0
            total += sign * float(getattr(execution, "shares", 0.0))
        return total if seen else None

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

    @staticmethod
    def _completed_execution_facts(
        completed_orders: list[Any], fills: list[Any]
    ) -> list[dict[str, Any]]:
        """Join completed orders to executions without inventing restart facts.

        TWS reports a completed order with ``orderId=0`` after reconnect, while
        its execution retains both the original order id and the same permanent
        id.  Only that direct ``permId`` match is sufficient to reconstruct the
        filled parent used by protection and cumulative-fill reconciliation.
        """
        orders_by_perm_id = {
            int(trade.order.permId): trade
            for trade in completed_orders
            if int(getattr(trade.order, "permId", 0) or 0) > 0
        }
        executions_by_order: dict[int, list[Any]] = {}
        for fill in fills:
            execution = fill.execution
            perm_id = int(getattr(execution, "permId", 0) or 0)
            order_id = int(getattr(execution, "orderId", 0) or 0)
            if perm_id in orders_by_perm_id and order_id > 0:
                executions_by_order.setdefault(order_id, []).append(fill)

        facts = []
        for order_id, matched_fills in executions_by_order.items():
            perm_ids = {int(fill.execution.permId) for fill in matched_fills}
            if len(perm_ids) != 1:
                continue
            completed = orders_by_perm_id[next(iter(perm_ids))]
            cumulative = max(float(fill.execution.cumQty) for fill in matched_fills)
            filled_quantity = float(
                getattr(completed.order, "filledQuantity", 0.0) or 0.0
            )
            total = max(cumulative, filled_quantity)
            fact = order_fact(
                completed.contract,
                completed.order,
                str(getattr(completed.orderStatus, "status", "") or "Unknown"),
            )
            fact.update({
                "orderId": order_id,
                "totalQuantity": total,
                "filled": cumulative,
                "remaining": max(0.0, total - cumulative),
            })
            facts.append(fact)
        return facts

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
        facts = {
            int(fact["orderId"]): fact
            for fact in (self._trade_fact(trade) for trade in self._known_trades())
        }
        completed = self.ib.reqCompletedOrders(apiOnly=False)
        self.ib.sleep(self.settle_seconds)
        for fact in self._completed_execution_facts(completed, list(self.ib.fills())):
            facts.setdefault(int(fact["orderId"]), fact)
        return list(facts.values())

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

    def qualified_forex_fact(self, instrument: str) -> dict[str, Any]:
        from ib_async import Forex
        from app.ibkr_l1_broker import forex_pair

        contract = self._qualify(Forex(forex_pair(instrument)))
        return {
            "contract": contract,
            "conId": int(contract.conId),
            "symbol": str(contract.symbol),
            "currency": str(contract.currency),
            "secType": str(contract.secType),
            "exchange": str(contract.exchange),
        }

    def current_quote(self, instrument: str) -> dict[str, Any]:
        fact = self.qualified_forex_fact(instrument)
        tickers = self.ib.reqTickers(fact["contract"])
        if len(tickers) != 1:
            raise L1ExecutionError("TWS did not return exactly one FX quote")
        ticker = tickers[0]
        bid, ask = float(ticker.bid), float(ticker.ask)
        if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask < bid:
            raise L1ExecutionError("TWS FX quote is unavailable or invalid")
        observed = ticker.time
        if not isinstance(observed, datetime):
            observed = datetime.now(timezone.utc)
        elif observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return {
            **{key: fact[key] for key in ("conId", "symbol", "currency", "secType")},
            "bid": bid,
            "ask": ask,
            "observed_at": observed.astimezone(timezone.utc),
        }

    def historical_closed_bars(
        self, instrument: str, *, timeframe: str = "4h", count: int = 60,
    ) -> list[dict[str, Any]]:
        if timeframe != "4h" or not 51 <= count <= 180:
            raise L1ExecutionError("continuous Paper runner supports 51-180 H4 bars")
        fact = self.qualified_forex_fact(instrument)
        bars = self.ib.reqHistoricalData(
            fact["contract"], endDateTime="", durationStr="30 D",
            barSizeSetting="4 hours", whatToShow="MIDPOINT", useRTH=False,
            formatDate=2, keepUpToDate=False,
        )
        now = datetime.now(timezone.utc)
        result = []
        for bar in bars:
            observed = bar.date
            if isinstance(observed, str):
                observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            if not isinstance(observed, datetime):
                continue
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            observed = observed.astimezone(timezone.utc)
            if observed + timedelta(hours=4) > now:
                continue
            result.append({
                "time": observed.isoformat(), "open": float(bar.open),
                "high": float(bar.high), "low": float(bar.low),
                "close": float(bar.close),
                "volume": max(0.0, float(bar.volume)), "complete": True,
            })
        result.sort(key=lambda item: item["time"])
        if len(result) < 51:
            raise L1ExecutionError("TWS returned fewer than 51 completed H4 bars")
        return result[-count:]
