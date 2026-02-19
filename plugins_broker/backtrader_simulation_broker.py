"""
Backtrader Simulation Broker Plugin for LTS (Live Trading System)

Standalone simulation broker that does NOT require a running backtrader cerebro.
Accepts CSV data and simulates trading with realistic costs (spread, commission,
slippage, swap). Designed for backtesting heuristic-strategy with full cost modeling.
"""

import csv
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from app.plugin_base import PluginBase

logger = logging.getLogger(__name__)


class BacktraderSimulationBroker(PluginBase):
    """
    Simulation broker for backtesting with realistic forex costs.

    Costs modeled:
        - spread: bid/ask spread in pips (applied on entry)
        - commission: per-lot round-turn commission in USD
        - slippage: execution slippage in pips (applied on entry)
        - swap: overnight swap cost per lot per day in USD
    """

    plugin_params = {
        "initial_cash": 10000.0,
        "leverage": 100,
        "spread_pips": 2.0,
        "commission_per_lot": 7.0,      # USD round-turn
        "slippage_pips": 1.0,
        "swap_per_lot_day": 10.0,       # USD per lot per day
        "pip_value": 0.0001,            # for EUR/USD style pairs
        "lot_size": 100000,             # standard lot
        "instrument": "EUR_USD",
        "csv_file": "",                 # path to OHLC CSV
        "datetime_column": "DATE_TIME",
        "datetime_format": "%Y-%m-%d %H:%M:%S",
        "open_col": "OPEN",
        "high_col": "HIGH",
        "low_col": "LOW",
        "close_col": "CLOSE",
    }

    plugin_debug_vars = [
        "initial_cash", "leverage", "spread_pips", "commission_per_lot",
        "slippage_pips", "swap_per_lot_day", "instrument",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        # State
        self.cash = self.params["initial_cash"]
        self.equity = self.cash
        self._trades: List[Dict[str, Any]] = []          # closed
        self._open_trades: Dict[str, Dict[str, Any]] = {}  # id -> trade
        self._next_id = 1
        self._bars: List[Dict[str, Any]] = []
        self._bar_idx = 0

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_csv(self, path: Optional[str] = None):
        """Load OHLC CSV data for backtesting."""
        path = path or self.params["csv_file"]
        if not path:
            raise ValueError("No csv_file specified")
        dt_col = self.params["datetime_column"]
        dt_fmt = self.params["datetime_format"]
        o, h, l, c = (self.params["open_col"], self.params["high_col"],
                       self.params["low_col"], self.params["close_col"])
        bars = []
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    bars.append({
                        "datetime": datetime.strptime(row[dt_col].strip(), dt_fmt),
                        "open": float(row[o]),
                        "high": float(row[h]),
                        "low": float(row[l]),
                        "close": float(row[c]),
                    })
                except (KeyError, ValueError) as e:
                    logger.warning("Skipping row: %s", e)
        self._bars = bars
        self._bar_idx = 0
        logger.info("Loaded %d bars from %s", len(bars), path)

    def get_bars(self):
        return self._bars

    # ------------------------------------------------------------------
    # Cost helpers
    # ------------------------------------------------------------------

    def _pip(self):
        return self.params["pip_value"]

    def _spread_cost(self, volume_lots: float) -> float:
        """Spread cost in price terms (applied to entry)."""
        return self.params["spread_pips"] * self._pip()

    def _slippage_cost(self) -> float:
        return self.params["slippage_pips"] * self._pip()

    def _commission(self, volume_lots: float) -> float:
        return self.params["commission_per_lot"] * volume_lots

    def _swap_cost(self, volume_lots: float, days: float) -> float:
        return self.params["swap_per_lot_day"] * volume_lots * days

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def open_order(self, instrument: str, direction: str, volume: float,
                   tp: Optional[float] = None, sl: Optional[float] = None,
                   price: Optional[float] = None, timestamp: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Open a simulated market order.

        Args:
            instrument: pair name
            direction: 'buy' or 'sell'
            volume: in lots (e.g. 0.1 = mini lot)
            tp: take-profit price
            sl: stop-loss price
            price: execution price (if None, uses current bar close)
            timestamp: order time

        Returns:
            dict with success, order_id
        """
        if price is None:
            if self._bars and self._bar_idx < len(self._bars):
                price = self._bars[self._bar_idx]["close"]
            else:
                return {"success": False, "error": "No price available"}

        # Apply spread + slippage to entry
        slip = self._spread_cost(volume) + self._slippage_cost()
        if direction == "buy":
            entry_price = price + slip
        else:
            entry_price = price - slip

        comm = self._commission(volume)
        self.cash -= comm  # commission on open

        trade_id = str(self._next_id)
        self._next_id += 1

        trade = {
            "trade_id": trade_id,
            "instrument": instrument,
            "direction": direction,
            "volume": volume,
            "entry_price": entry_price,
            "raw_entry_price": price,
            "tp": tp,
            "sl": sl,
            "open_time": timestamp or (self._bars[self._bar_idx]["datetime"] if self._bars and self._bar_idx < len(self._bars) else datetime.now()),
            "commission": comm,
            "swap": 0.0,
            "close_reason": None,
        }
        self._open_trades[trade_id] = trade
        logger.debug("Opened trade %s: %s %s @ %.5f (raw %.5f), comm=%.2f",
                      trade_id, direction, volume, entry_price, price, comm)
        return {"success": True, "order_id": trade_id, "entry_price": entry_price}

    def close_order(self, order_id: str, price: Optional[float] = None,
                    reason: str = "manual", timestamp: Optional[datetime] = None) -> Dict[str, Any]:
        """Close an open trade."""
        trade = self._open_trades.pop(str(order_id), None)
        if trade is None:
            return {"success": False, "error": f"Trade {order_id} not found"}

        if price is None:
            if self._bars and self._bar_idx < len(self._bars):
                price = self._bars[self._bar_idx]["close"]
            else:
                return {"success": False, "error": "No price available"}

        # Calculate P&L
        if trade["direction"] == "buy":
            pnl_pips = (price - trade["entry_price"]) / self._pip()
        else:
            pnl_pips = (trade["entry_price"] - price) / self._pip()

        units = trade["volume"] * self.params["lot_size"]
        pnl_usd = pnl_pips * self._pip() * units

        # Swap cost based on days held
        close_time = timestamp or (self._bars[self._bar_idx]["datetime"] if self._bars and self._bar_idx < len(self._bars) else datetime.now())
        days_held = max((close_time - trade["open_time"]).total_seconds() / 86400.0, 0)
        swap = self._swap_cost(trade["volume"], days_held)
        trade["swap"] = swap

        net_pnl = pnl_usd - swap
        self.cash += net_pnl

        trade.update({
            "close_price": price,
            "close_time": close_time,
            "close_reason": reason,
            "pnl_pips": pnl_pips,
            "pnl_usd": pnl_usd,
            "swap": swap,
            "net_pnl": net_pnl,
        })
        self._trades.append(trade)
        self._update_equity()

        logger.debug("Closed trade %s: reason=%s, pnl=%.2f, swap=%.2f, net=%.2f",
                      order_id, reason, pnl_usd, swap, net_pnl)
        return {"success": True, "trade": trade}

    def modify_order(self, order_id: str, tp: Optional[float] = None,
                     sl: Optional[float] = None) -> Dict[str, Any]:
        """Modify TP/SL on an open trade."""
        trade = self._open_trades.get(str(order_id))
        if trade is None:
            return {"success": False, "error": f"Trade {order_id} not found"}
        if tp is not None:
            trade["tp"] = tp
        if sl is not None:
            trade["sl"] = sl
        return {"success": True}

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_open_trades(self) -> List[Dict[str, Any]]:
        return list(self._open_trades.values())

    def get_account_summary(self) -> Dict[str, Any]:
        self._update_equity()
        return {
            "success": True,
            "balance": self.cash,
            "equity": self.equity,
            "margin": 0.0,
            "unrealized_pnl": self.equity - self.cash,
        }

    def get_trade_history(self, count: int = 50) -> List[Dict[str, Any]]:
        return self._trades[-count:]

    def get_current_price(self, instrument: Optional[str] = None) -> Dict[str, Any]:
        if not self._bars or self._bar_idx >= len(self._bars):
            return {"success": False, "error": "No data"}
        bar = self._bars[self._bar_idx]
        half_spread = self.params["spread_pips"] * self._pip() / 2
        mid = bar["close"]
        return {
            "success": True,
            "bid": mid - half_spread,
            "ask": mid + half_spread,
            "spread": self.params["spread_pips"] * self._pip(),
            "instrument": instrument or self.params["instrument"],
        }

    # ------------------------------------------------------------------
    # Simulation tick
    # ------------------------------------------------------------------

    def tick(self, bar_index: Optional[int] = None):
        """
        Advance to the given bar (or next bar) and check TP/SL.

        Call this in a loop to drive the simulation forward.
        Returns list of trades closed by TP/SL this tick.
        """
        if bar_index is not None:
            self._bar_idx = bar_index
        else:
            self._bar_idx += 1

        if self._bar_idx >= len(self._bars):
            return []

        bar = self._bars[self._bar_idx]
        closed = []

        for tid in list(self._open_trades.keys()):
            trade = self._open_trades[tid]
            direction = trade["direction"]
            tp = trade.get("tp")
            sl = trade.get("sl")

            # Check SL
            if sl is not None:
                if direction == "buy" and bar["low"] <= sl:
                    r = self.close_order(tid, price=sl, reason="stop_loss", timestamp=bar["datetime"])
                    if r["success"]:
                        closed.append(r["trade"])
                    continue
                elif direction == "sell" and bar["high"] >= sl:
                    r = self.close_order(tid, price=sl, reason="stop_loss", timestamp=bar["datetime"])
                    if r["success"]:
                        closed.append(r["trade"])
                    continue

            # Check TP
            if tp is not None:
                if direction == "buy" and bar["high"] >= tp:
                    r = self.close_order(tid, price=tp, reason="take_profit", timestamp=bar["datetime"])
                    if r["success"]:
                        closed.append(r["trade"])
                    continue
                elif direction == "sell" and bar["low"] <= tp:
                    r = self.close_order(tid, price=tp, reason="take_profit", timestamp=bar["datetime"])
                    if r["success"]:
                        closed.append(r["trade"])
                    continue

        self._update_equity()
        return closed

    def run_simulation(self, strategy_fn=None):
        """
        Run full simulation over loaded bars.

        Args:
            strategy_fn: callable(broker, bar_index, bar) -> None
                Called each bar to let the strategy open/close orders.

        Returns:
            dict with performance summary and trade history.
        """
        if not self._bars:
            raise ValueError("No data loaded. Call load_csv() first.")

        self.cash = self.params["initial_cash"]
        self._trades = []
        self._open_trades = {}
        self._bar_idx = 0

        for i in range(len(self._bars)):
            self._bar_idx = i
            bar = self._bars[i]

            # Let strategy act
            if strategy_fn:
                strategy_fn(self, i, bar)

            # Check TP/SL
            self.tick(i)

        # Force-close remaining
        for tid in list(self._open_trades.keys()):
            self.close_order(tid, reason="end_of_data", timestamp=self._bars[-1]["datetime"])

        return self._summary()

    # ------------------------------------------------------------------
    # BrokerPluginBase compatibility
    # ------------------------------------------------------------------

    def execute_order(self, action: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        if action == "open":
            return self.open_order(
                instrument=parameters.get("symbol", self.params["instrument"]),
                direction=parameters.get("side", "buy"),
                volume=float(parameters.get("quantity", 0.1)),
                tp=parameters.get("take_profit"),
                sl=parameters.get("stop_loss"),
                price=parameters.get("price"),
            )
        elif action == "close":
            oid = parameters.get("broker_order_id") or parameters.get("order_id", "")
            return self.close_order(str(oid), reason=parameters.get("reason", "manual"))
        return {"success": False, "error": f"Unknown action: {action}"}

    def get_open_orders(self) -> list:
        return self.get_open_trades()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _update_equity(self):
        unrealized = 0.0
        if self._bars and self._bar_idx < len(self._bars):
            price = self._bars[self._bar_idx]["close"]
            for t in self._open_trades.values():
                units = t["volume"] * self.params["lot_size"]
                if t["direction"] == "buy":
                    unrealized += (price - t["entry_price"]) * units
                else:
                    unrealized += (t["entry_price"] - price) * units
        self.equity = self.cash + unrealized

    def _summary(self) -> Dict[str, Any]:
        initial = self.params["initial_cash"]
        total_pnl = sum(t.get("net_pnl", 0) for t in self._trades)
        total_comm = sum(t.get("commission", 0) for t in self._trades)
        total_swap = sum(t.get("swap", 0) for t in self._trades)
        winners = [t for t in self._trades if t.get("net_pnl", 0) > 0]
        losers = [t for t in self._trades if t.get("net_pnl", 0) <= 0]
        return {
            "initial_cash": initial,
            "final_cash": self.cash,
            "total_return_pct": ((self.cash - initial) / initial) * 100,
            "total_trades": len(self._trades),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": len(winners) / len(self._trades) if self._trades else 0,
            "total_pnl": total_pnl,
            "total_commission": total_comm,
            "total_swap": total_swap,
            "trades": self._trades,
            "close_reasons": {
                reason: len([t for t in self._trades if t.get("close_reason") == reason])
                for reason in set(t.get("close_reason", "unknown") for t in self._trades)
            },
        }
