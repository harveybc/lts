#!/usr/bin/env python3
"""
USD/JPY TSMOM Strategy Plugin for LTS.

Phase 6.B: Ported from Phase 5.5 script-level `run_tsmom()`.
Strategy: Time-Series Momentum (Moskowitz et al. 2012).
  - Signal: sign of trailing 12-month return
  - Sizing: inverse trailing 60-day volatility, target 10% annualized vol
  - Rebalance: monthly on first trading day
  - Leverage cap: 3x
"""
import numpy as np
import logging
from datetime import datetime

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


class UsdJpyTsmomStrategy(PluginBase):
    """USD/JPY TSMOM strategy — monthly rebalance, inverse-vol sized."""

    plugin_params = {
        "lookback_months": 12,
        "target_vol": 0.10,
        "max_leverage": 3.0,
        "min_vol": 0.01,
        "min_history_days": 252,
        "instrument": "USD_JPY",
    }

    def __init__(self, config=None):
        super().__init__(config)
        self._price_history = []
        self._date_history = []
        self._position = 0.0  # continuous sizing (not just -1/0/1)
        self._current_month = None
        self._last_signal = 0.0
        self._pending_entry = None  # stores (direction, size, ret_12m) after reversal close

    def _compute_12m_return(self):
        """Compute log return over trailing ~252 trading days."""
        prices = self._price_history
        n = len(prices)
        if n < self.params["min_history_days"]:
            return None
        # Use ~252 trading days as 12-month proxy
        lookback_bars = min(252, n - 1)
        ret = np.log(prices[-1] + 1e-12) - np.log(prices[-1 - lookback_bars] + 1e-12)
        return ret

    def _compute_inverse_vol_size(self):
        """Compute position size as target_vol / realized_vol, capped."""
        prices = self._price_history
        if len(prices) < 62:
            return 1.0
        log_rets = np.diff(np.log(np.array(prices[-61:]) + 1e-12))
        vol = np.std(log_rets) * np.sqrt(252)
        if vol < self.params["min_vol"]:
            vol = self.params["min_vol"]
        size = self.params["target_vol"] / vol
        return min(size, self.params["max_leverage"])

    def generate_signal(self, asset, market_data=None, predictions=None):
        """
        Generate trading signal for USD/JPY TSMOM strategy.

        Rebalances monthly: computes 12-month return sign × inverse-vol size.
        Between rebalances, holds the same position.
        """
        if market_data is None:
            return {"action": "none", "parameters": {}}

        # Extract current price and date
        current_price = None
        current_date = None
        if isinstance(market_data, dict):
            current_price = market_data.get("close") or market_data.get("price")
            current_date = market_data.get("date") or market_data.get("datetime")
        elif isinstance(market_data, (list, np.ndarray)):
            current_price = float(market_data[-1])

        if current_price is None:
            return {"action": "none", "parameters": {}}

        self._price_history.append(float(current_price))
        if current_date is not None:
            if isinstance(current_date, str):
                current_date = datetime.fromisoformat(current_date)
            self._date_history.append(current_date)

        # Need enough history
        if len(self._price_history) < self.params["min_history_days"]:
            return {"action": "none", "parameters": {}}

        # Execute pending entry from a previous reversal close
        if self._pending_entry is not None:
            direction, size, ret_12m = self._pending_entry
            self._pending_entry = None
            new_position = size if direction == "buy" else -size
            self._position = new_position
            self._last_signal = new_position
            logger.info(f"TSMOM ENTRY (pending) {asset}: {direction}, size={size:.2f}, "
                       f"12m_ret={ret_12m:.4f}, price={current_price:.5f}")
            return {
                "action": "open",
                "parameters": {
                    "side": direction,
                    "instrument": self.params["instrument"],
                    "order_type": "MARKET",
                    "ret_12m": round(ret_12m, 6),
                    "vol_size": round(size, 4),
                }
            }

        # Check if we're at a new month boundary (rebalance point)
        month_key = None
        if current_date is not None:
            month_key = (current_date.year, current_date.month)
        elif len(self._date_history) > 0:
            month_key = (self._date_history[-1].year, self._date_history[-1].month)

        if month_key is None:
            # Fallback: rebalance every ~21 bars
            if len(self._price_history) % 21 != 0:
                return {"action": "none", "parameters": {}}
        elif month_key == self._current_month:
            # Same month — hold position
            return {"action": "none", "parameters": {}}

        # New month — rebalance
        self._current_month = month_key

        ret_12m = self._compute_12m_return()
        if ret_12m is None:
            return {"action": "none", "parameters": {}}

        signal = np.sign(ret_12m)
        size = self._compute_inverse_vol_size()
        new_position = signal * size

        # Determine action
        if abs(new_position) < 0.01:
            # Flat
            if abs(self._position) > 0.01:
                old_pos = self._position
                self._position = 0.0
                self._last_signal = 0.0
                logger.info(f"TSMOM FLAT {asset}: 12m_ret={ret_12m:.4f}")
                return {
                    "action": "close",
                    "parameters": {
                        "reason": "tsmom_flat",
                        "ret_12m": round(ret_12m, 6),
                    }
                }
            return {"action": "none", "parameters": {}}

        direction = "buy" if new_position > 0 else "sell"

        # Check if direction changed or if we need to rebalance size
        if (self._position > 0 and new_position > 0) or (self._position < 0 and new_position < 0):
            # Same direction — size adjustment only (skip for simplicity in LTS)
            self._position = new_position
            self._last_signal = new_position
            return {"action": "none", "parameters": {}}

        # Direction change or new entry
        if abs(self._position) > 0.01:
            # Close old position first, store pending entry for next bar
            self._position = 0.0
            self._pending_entry = (direction, size, ret_12m)
            logger.info(f"TSMOM REVERSE {asset}: new_dir={direction}, 12m_ret={ret_12m:.4f}")
            return {
                "action": "close",
                "parameters": {
                    "reason": "tsmom_reverse",
                    "new_direction": direction,
                    "ret_12m": round(ret_12m, 6),
                }
            }

        # New entry
        self._position = new_position
        self._last_signal = new_position
        logger.info(f"TSMOM ENTRY {asset}: {direction}, size={size:.2f}, "
                   f"12m_ret={ret_12m:.4f}, price={current_price:.5f}")

        return {
            "action": "open",
            "parameters": {
                "side": direction,
                "instrument": self.params["instrument"],
                "order_type": "MARKET",
                "ret_12m": round(ret_12m, 6),
                "vol_size": round(size, 4),
            }
        }
