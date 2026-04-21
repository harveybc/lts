#!/usr/bin/env python3
"""
USD/JPY Dual Momentum Strategy Plugin for LTS.

Phase 6.B: Ported from Phase 5.5 script-level `run_dual_momentum()`.
Strategy: Dual Momentum (Antonacci 2014).
  - Absolute momentum: long only if 12-month return > 0
  - Relative momentum: long only the best FX pair (cross-asset comparison)
  - Rebalance: monthly on first trading day
  - Single-asset usage: long if own 12m return > 0 AND own 12m return is best among FX peers
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


class UsdJpyDualMomentumStrategy(PluginBase):
    """USD/JPY Dual Momentum — long if absolute + relative momentum positive."""

    plugin_params = {
        "lookback_months": 12,
        "min_history_days": 252,
        "instrument": "USD_JPY",
        "peer_assets": ["EUR/USD", "GBP/USD", "AUD/USD"],
    }

    def __init__(self, config=None):
        super().__init__(config)
        self._price_history = []
        self._date_history = []
        self._peer_histories = {}  # asset -> [prices]
        self._position = 0  # 0=flat, 1=long
        self._current_month = None

    def _compute_12m_return(self, prices):
        """Compute log return over trailing ~252 trading days."""
        n = len(prices)
        if n < self.params["min_history_days"]:
            return None
        lookback_bars = min(252, n - 1)
        ret = np.log(prices[-1] + 1e-12) - np.log(prices[-1 - lookback_bars] + 1e-12)
        return ret

    def generate_signal(self, asset, market_data=None, predictions=None):
        """
        Generate trading signal for USD/JPY Dual Momentum strategy.

        Rebalances monthly:
          - Computes 12-month return for this asset and all peers
          - Long if own return > 0 AND own return is highest among peers
          - Flat otherwise
        """
        if market_data is None:
            return {"action": "none", "parameters": {}}

        # Extract current price and date
        current_price = None
        current_date = None
        if isinstance(market_data, dict):
            current_price = market_data.get("close") or market_data.get("price")
            current_date = market_data.get("date") or market_data.get("datetime")
            # Extract peer prices if available
            peer_prices = market_data.get("peer_prices", {})
            for peer_asset, peer_price in peer_prices.items():
                if peer_asset not in self._peer_histories:
                    self._peer_histories[peer_asset] = []
                self._peer_histories[peer_asset].append(float(peer_price))
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

        # Check if we're at a new month boundary
        month_key = None
        if current_date is not None:
            month_key = (current_date.year, current_date.month)

        if month_key is None:
            if len(self._price_history) % 21 != 0:
                return {"action": "none", "parameters": {}}
        elif month_key == self._current_month:
            return {"action": "none", "parameters": {}}

        # New month — rebalance
        self._current_month = month_key

        # Absolute momentum: own 12m return
        own_ret = self._compute_12m_return(self._price_history)
        if own_ret is None:
            return {"action": "none", "parameters": {}}

        # Relative momentum: compare to peers
        all_rets = {"USD/JPY": own_ret}
        for peer_asset, peer_prices_list in self._peer_histories.items():
            peer_ret = self._compute_12m_return(peer_prices_list)
            if peer_ret is not None:
                all_rets[peer_asset] = peer_ret

        best_asset = max(all_rets, key=all_rets.get)
        should_be_long = (own_ret > 0) and (best_asset == "USD/JPY")

        if should_be_long:
            if self._position == 1:
                # Already long — hold
                return {"action": "none", "parameters": {}}
            # Enter long
            self._position = 1
            logger.info(f"DUAL_MOM ENTRY {asset}: BUY, 12m_ret={own_ret:.4f}, "
                       f"best_asset={best_asset}, price={current_price:.5f}")
            return {
                "action": "open",
                "parameters": {
                    "side": "buy",
                    "instrument": self.params["instrument"],
                    "order_type": "MARKET",
                    "ret_12m": round(own_ret, 6),
                    "best_asset": best_asset,
                }
            }
        else:
            if self._position == 0:
                return {"action": "none", "parameters": {}}
            # Close long
            self._position = 0
            reason = "abs_mom_negative" if own_ret <= 0 else f"rel_mom_best={best_asset}"
            logger.info(f"DUAL_MOM EXIT {asset}: reason={reason}, 12m_ret={own_ret:.4f}")
            return {
                "action": "close",
                "parameters": {
                    "reason": reason,
                    "ret_12m": round(own_ret, 6),
                    "best_asset": best_asset,
                }
            }
