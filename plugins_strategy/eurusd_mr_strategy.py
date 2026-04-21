#!/usr/bin/env python3
"""
EUR/USD Daily Mean-Reversion Strategy Plugin for LTS.

Phase 5 Q3: Operational deployment via OANDA demo account.
Strategy: z-score mean reversion on EUR/USD daily close.
"""
import numpy as np
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

try:
    from app.plugin_base import PluginBase
except ImportError:
    class PluginBase:
        """Fallback if LTS not installed."""
        plugin_params = {}
        def __init__(self, config=None):
            self.params = dict(self.plugin_params)
            if config:
                self.params.update(config)


class EurUsdMrStrategy(PluginBase):
    """EUR/USD daily mean-reversion strategy."""

    plugin_params = {
        "lookback": 20,
        "z_entry": 1.5,
        "z_exit": 0.5,
        "max_holding_bars": 30,
        "sl_atr_multiple": 3.0,
        "atr_lookback": 20,
        "risk_per_trade_pct": 0.005,
        "max_leverage": 2.0,
        "instrument": "EUR_USD",
    }

    def __init__(self, config=None):
        super().__init__(config)
        self._price_history = []
        self._position = 0  # 0=flat, 1=long, -1=short
        self._entry_bar = 0
        self._entry_price = 0.0
        self._bars_since_entry = 0

    def _compute_z_score(self, prices):
        """Compute z-score of latest price vs rolling window."""
        if len(prices) < self.params["lookback"]:
            return 0.0
        window = prices[-self.params["lookback"]:]
        mean_p = np.mean(window)
        std_p = np.std(window)
        if std_p < 1e-12:
            return 0.0
        return (prices[-1] - mean_p) / std_p

    def _compute_atr_pct(self, prices):
        """ATR as percentage of price."""
        if len(prices) < self.params["atr_lookback"] + 1:
            return 0.01
        arr = np.array(prices[-self.params["atr_lookback"]-1:], dtype=float)
        returns = np.diff(np.log(arr + 1e-12))
        return float(np.std(returns))

    def generate_signal(self, asset, market_data=None, predictions=None):
        """
        Generate trading signal for EUR/USD MR strategy.

        Returns:
            dict: {action, parameters}
        """
        if market_data is None:
            return {"action": "none", "parameters": {}}

        # Extract current price
        current_price = None
        if isinstance(market_data, dict):
            current_price = market_data.get("close") or market_data.get("price")
        elif isinstance(market_data, (list, np.ndarray)):
            current_price = float(market_data[-1])

        if current_price is None:
            return {"action": "none", "parameters": {}}

        self._price_history.append(float(current_price))

        # Need enough history
        if len(self._price_history) < self.params["lookback"]:
            return {"action": "none", "parameters": {}}

        z = self._compute_z_score(self._price_history)
        atr_pct = self._compute_atr_pct(self._price_history)

        # If in position, check exit
        if self._position != 0:
            self._bars_since_entry += 1
            current_pnl = self._position * (current_price - self._entry_price) / self._entry_price
            sl_level = self.params["sl_atr_multiple"] * atr_pct

            should_exit = False
            exit_reason = ""

            if abs(z) < self.params["z_exit"]:
                should_exit = True
                exit_reason = "z_exit_reached"
            elif current_pnl < -sl_level:
                should_exit = True
                exit_reason = "stop_loss"
            elif current_pnl > 2.0 * atr_pct:
                should_exit = True
                exit_reason = "take_profit"
            elif self._bars_since_entry >= self.params["max_holding_bars"]:
                should_exit = True
                exit_reason = "max_hold_reached"

            if should_exit:
                logger.info(f"EXIT {asset}: reason={exit_reason}, pnl={current_pnl:.4f}, "
                           f"bars={self._bars_since_entry}, z={z:.2f}")
                self._position = 0
                self._bars_since_entry = 0
                return {
                    "action": "close",
                    "parameters": {
                        "reason": exit_reason,
                        "pnl_pct": round(current_pnl * 100, 4),
                    }
                }
            return {"action": "none", "parameters": {}}

        # Not in position — check entry
        if z > self.params["z_entry"]:
            # Overextended upward → sell (mean reversion)
            direction = "sell"
            self._position = -1
        elif z < -self.params["z_entry"]:
            # Overextended downward → buy
            direction = "buy"
            self._position = 1
        else:
            return {"action": "none", "parameters": {}}

        self._entry_price = current_price
        self._entry_bar = len(self._price_history)
        self._bars_since_entry = 0

        # Position sizing
        sl_distance = self.params["sl_atr_multiple"] * atr_pct * current_price
        tp_price = current_price - self._position * abs(z - np.sign(z) * self.params["z_exit"]) * np.std(self._price_history[-self.params["lookback"]:])
        sl_price = current_price + self._position * sl_distance

        # Ensure SL/TP are on correct side
        if direction == "buy":
            sl_price = current_price - abs(sl_distance)
            tp_price = max(tp_price, current_price + abs(sl_distance) * 0.5)
        else:
            sl_price = current_price + abs(sl_distance)
            tp_price = min(tp_price, current_price - abs(sl_distance) * 0.5)

        logger.info(f"ENTRY {asset}: {direction}, z={z:.2f}, price={current_price:.5f}, "
                   f"sl={sl_price:.5f}, tp={tp_price:.5f}")

        return {
            "action": "open",
            "parameters": {
                "side": direction,
                "instrument": self.params["instrument"],
                "stop_loss": round(sl_price, 5),
                "take_profit": round(tp_price, 5),
                "order_type": "MARKET",
                "z_score": round(z, 3),
                "atr_pct": round(atr_pct, 6),
            }
        }
