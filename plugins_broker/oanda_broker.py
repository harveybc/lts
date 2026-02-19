"""
OANDA Live Broker Plugin for LTS (Live Trading System)

Uses OANDA v20 REST API via oandapyV20 for live/practice trading.
"""

import time
import logging
from typing import Dict, Any, Optional, List
from app.plugin_base import PluginBase

logger = logging.getLogger(__name__)

# Lazy imports to avoid hard dependency at module level
_oanda_imported = False
_oandapyV20 = None
_orders_mod = None
_trades_mod = None
_pricing_mod = None
_accounts_mod = None


def _ensure_oanda():
    global _oanda_imported, _oandapyV20, _orders_mod, _trades_mod, _pricing_mod, _accounts_mod
    if _oanda_imported:
        return
    import oandapyV20
    import oandapyV20.endpoints.orders as orders_ep
    import oandapyV20.endpoints.trades as trades_ep
    import oandapyV20.endpoints.pricing as pricing_ep
    import oandapyV20.endpoints.accounts as accounts_ep
    _oandapyV20 = oandapyV20
    _orders_mod = orders_ep
    _trades_mod = trades_ep
    _pricing_mod = pricing_ep
    _accounts_mod = accounts_ep
    _oanda_imported = True


def _ok(data=None):
    r = {"success": True}
    if data:
        r.update(data)
    return r


def _err(msg):
    return {"success": False, "error": str(msg)}


class OandaBroker(PluginBase):
    """
    OANDA v20 REST API broker plugin.

    Provides market order execution with TP/SL, trade management,
    account queries, and pricing via oandapyV20.
    """

    plugin_params = {
        "account_id": "",
        "access_token": "",
        "environment": "practice",   # 'practice' or 'live'
        "instrument": "EUR_USD",
        "max_retries": 3,
        "retry_backoff": 1.0,        # seconds, doubles each retry
    }

    plugin_debug_vars = ["account_id", "environment", "instrument"]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            _ensure_oanda()
            self._client = _oandapyV20.API(
                access_token=self.params["access_token"],
                environment=self.params["environment"],
            )
        return self._client

    def _request(self, endpoint, retries=None):
        """Execute an oandapyV20 endpoint with retry + backoff."""
        client = self._get_client()
        max_retries = retries if retries is not None else self.params["max_retries"]
        backoff = self.params["retry_backoff"]
        last_err = None
        for attempt in range(max_retries):
            try:
                client.request(endpoint)
                return endpoint.response
            except Exception as e:
                last_err = e
                logger.warning("OANDA request failed (attempt %d/%d): %s", attempt + 1, max_retries, e)
                if attempt < max_retries - 1:
                    time.sleep(backoff * (2 ** attempt))
        logger.error("OANDA request failed after %d attempts: %s", max_retries, last_err)
        raise last_err  # type: ignore[misc]

    @property
    def _aid(self):
        return self.params["account_id"]

    # ------------------------------------------------------------------
    # Public interface (matches BrokerPluginBase + extended methods)
    # ------------------------------------------------------------------

    def open_order(self, instrument: str, direction: str, volume: float,
                   tp: Optional[float] = None, sl: Optional[float] = None) -> Dict[str, Any]:
        """
        Open a market order with optional TP and SL.

        Args:
            instrument: e.g. 'EUR_USD'
            direction: 'buy' or 'sell'
            volume: units (positive). Negative sent automatically for sell.
            tp: take-profit price
            sl: stop-loss price

        Returns:
            dict with 'success', 'order_id', 'trade_id', etc.
        """
        try:
            _ensure_oanda()
            units = str(int(volume)) if direction == "buy" else str(-int(volume))
            order_body: Dict[str, Any] = {
                "type": "MARKET",
                "instrument": instrument,
                "units": units,
            }
            if tp is not None:
                order_body["takeProfitOnFill"] = {"price": f"{tp:.5f}"}
            if sl is not None:
                order_body["stopLossOnFill"] = {"price": f"{sl:.5f}"}

            data = {"order": order_body}
            ep = _orders_mod.OrderCreate(accountID=self._aid, data=data)
            resp = self._request(ep)

            # Extract IDs from response
            otf = resp.get("orderFillTransaction", {})
            order_id = otf.get("orderID") or resp.get("orderCreateTransaction", {}).get("id")
            trade_ids = otf.get("tradeOpened", {}).get("tradeID") if otf.get("tradeOpened") else None

            return _ok({
                "order_id": order_id,
                "trade_id": trade_ids,
                "fill_price": otf.get("price"),
                "response": resp,
            })
        except Exception as e:
            logger.error("open_order failed: %s", e)
            return _err(e)

    def close_order(self, order_id: str) -> Dict[str, Any]:
        """Close a trade by its trade ID."""
        try:
            _ensure_oanda()
            ep = _trades_mod.TradeClose(accountID=self._aid, tradeID=str(order_id))
            resp = self._request(ep)
            return _ok({"response": resp})
        except Exception as e:
            logger.error("close_order failed: %s", e)
            return _err(e)

    def modify_order(self, order_id: str, tp: Optional[float] = None,
                     sl: Optional[float] = None) -> Dict[str, Any]:
        """Modify TP/SL on an existing trade."""
        try:
            _ensure_oanda()
            data: Dict[str, Any] = {}
            if tp is not None:
                data["takeProfit"] = {"price": f"{tp:.5f}"}
            if sl is not None:
                data["stopLoss"] = {"price": f"{sl:.5f}"}
            if not data:
                return _err("No modifications specified")
            ep = _trades_mod.TradeCRCDO(accountID=self._aid, tradeID=str(order_id), data=data)
            resp = self._request(ep)
            return _ok({"response": resp})
        except Exception as e:
            logger.error("modify_order failed: %s", e)
            return _err(e)

    def get_open_trades(self) -> List[Dict[str, Any]]:
        """Return list of open trades."""
        try:
            _ensure_oanda()
            ep = _trades_mod.OpenTrades(accountID=self._aid)
            resp = self._request(ep)
            trades = resp.get("trades", [])
            return [
                {
                    "trade_id": t.get("id"),
                    "instrument": t.get("instrument"),
                    "direction": "buy" if float(t.get("currentUnits", 0)) > 0 else "sell",
                    "units": abs(float(t.get("currentUnits", 0))),
                    "open_price": float(t.get("price", 0)),
                    "unrealized_pnl": float(t.get("unrealizedPL", 0)),
                    "open_time": t.get("openTime"),
                }
                for t in trades
            ]
        except Exception as e:
            logger.error("get_open_trades failed: %s", e)
            return []

    def get_account_summary(self) -> Dict[str, Any]:
        """Return account balance, equity, margin, unrealized P&L."""
        try:
            _ensure_oanda()
            ep = _accounts_mod.AccountSummary(accountID=self._aid)
            resp = self._request(ep)
            acct = resp.get("account", {})
            return {
                "success": True,
                "balance": float(acct.get("balance", 0)),
                "equity": float(acct.get("NAV", 0)),
                "margin": float(acct.get("marginUsed", 0)),
                "unrealized_pnl": float(acct.get("unrealizedPL", 0)),
                "currency": acct.get("currency", "USD"),
            }
        except Exception as e:
            logger.error("get_account_summary failed: %s", e)
            return _err(e)

    def get_trade_history(self, count: int = 50) -> List[Dict[str, Any]]:
        """Return closed trades."""
        try:
            _ensure_oanda()
            params = {"state": "CLOSED", "count": count}
            ep = _trades_mod.TradesList(accountID=self._aid, params=params)
            resp = self._request(ep)
            trades = resp.get("trades", [])
            return [
                {
                    "trade_id": t.get("id"),
                    "instrument": t.get("instrument"),
                    "direction": "buy" if float(t.get("initialUnits", 0)) > 0 else "sell",
                    "units": abs(float(t.get("initialUnits", 0))),
                    "open_price": float(t.get("price", 0)),
                    "close_price": float(t.get("averageClosePrice", 0)),
                    "pnl": float(t.get("realizedPL", 0)),
                    "open_time": t.get("openTime"),
                    "close_time": t.get("closeTime"),
                    "close_reason": t.get("closingTransactionIDs", []),
                }
                for t in trades
            ]
        except Exception as e:
            logger.error("get_trade_history failed: %s", e)
            return []

    def get_current_price(self, instrument: str) -> Dict[str, Any]:
        """Get latest bid/ask for an instrument."""
        try:
            _ensure_oanda()
            params = {"instruments": instrument}
            ep = _pricing_mod.PricingInfo(accountID=self._aid, params=params)
            resp = self._request(ep)
            prices = resp.get("prices", [])
            if not prices:
                return _err("No pricing data returned")
            p = prices[0]
            bid = float(p["bids"][0]["price"]) if p.get("bids") else 0.0
            ask = float(p["asks"][0]["price"]) if p.get("asks") else 0.0
            return {
                "success": True,
                "bid": bid,
                "ask": ask,
                "spread": round(ask - bid, 6),
                "instrument": instrument,
                "time": p.get("time"),
            }
        except Exception as e:
            logger.error("get_current_price failed: %s", e)
            return _err(e)

    # ------------------------------------------------------------------
    # BrokerPluginBase compatibility aliases
    # ------------------------------------------------------------------

    def execute_order(self, action: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """Compatibility wrapper matching DefaultBroker.execute_order interface."""
        try:
            if action == "open":
                instrument = parameters.get("symbol", self.params["instrument"])
                direction = parameters.get("side", "buy")
                volume = float(parameters.get("quantity", 1))
                tp = parameters.get("take_profit")
                sl = parameters.get("stop_loss")
                return self.open_order(instrument, direction, volume, tp, sl)
            elif action == "close":
                order_id = parameters.get("broker_order_id") or parameters.get("position_id", "")
                return self.close_order(str(order_id))
            else:
                return _err(f"Unknown action: {action}")
        except Exception as e:
            return _err(e)

    # Alias for BrokerPluginBase.get_open_orders
    def get_open_orders(self) -> list:
        return self.get_open_trades()
