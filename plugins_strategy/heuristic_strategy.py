"""
Heuristic Strategy Plugin for LTS

Logic copied from heuristic-strategy/app/plugins/plugin_long_short_predictions.py.
Computes entry signals (long/short/hold) with TP/SL from daily predictions,
and exit signals using variant-based early close logic from hourly+daily predictions.
"""

from typing import Dict, Any, List, Optional

# Default parameters matching the heuristic-strategy plugin
DEFAULT_PARAMS = {
    'pip_cost': 0.00001,
    'rel_volume': 0.02,
    'min_order_volume': 10000,
    'max_order_volume': 1000000,
    'leverage': 100,
    'profit_threshold': 5,
    'min_drawdown_pips': 10,
    'tp_multiplier': 0.9,
    'sl_multiplier': 2.0,
    'lower_rr_threshold': 0.5,
    'upper_rr_threshold': 2.0,
    'max_trades_per_5days': 3,
    'exit_variant': 'E',
    'spread_pips': 2.0,
    'commission_per_lot': 7.0,
    'slippage_pips': 1.0,
    'swap_per_lot_per_day': 10.0,
}


def compute_signal(
    current_price: float,
    daily_predictions: List[float],
    hourly_predictions: List[float] = None,
    config: Dict[str, Any] = None,
    balance: float = 10000.0,
) -> Dict[str, Any]:
    """
    Compute a trading signal from predictions.
    
    Returns dict: {action, tp, sl, volume, reason, rr}
    action is 'buy', 'sell', or 'hold'.
    """
    cfg = {**DEFAULT_PARAMS, **(config or {})}
    pip_cost = cfg['pip_cost']
    profit_threshold = cfg['profit_threshold']
    min_drawdown_pips = cfg['min_drawdown_pips']
    tp_multiplier = cfg['tp_multiplier']
    sl_multiplier = cfg['sl_multiplier']
    lower_rr = cfg['lower_rr_threshold']
    upper_rr = cfg['upper_rr_threshold']

    if not daily_predictions or all(p is None for p in daily_predictions):
        return {"action": "hold", "reason": "no daily predictions", "tp": 0, "sl": 0, "volume": 0}

    daily_preds = [p for p in daily_predictions if p is not None]
    if not daily_preds:
        return {"action": "hold", "reason": "empty daily predictions", "tp": 0, "sl": 0, "volume": 0}

    # --- Long entry conditions ---
    ideal_profit_pips_buy = (max(daily_preds) - current_price) / pip_cost
    ideal_drawdown_pips_buy = max(
        (current_price - min(daily_preds)) / pip_cost,
        min_drawdown_pips
    )
    rr_buy = ideal_profit_pips_buy / ideal_drawdown_pips_buy if ideal_drawdown_pips_buy > 0 else 0
    tp_buy = current_price + tp_multiplier * ideal_profit_pips_buy * pip_cost
    sl_buy = current_price - sl_multiplier * ideal_drawdown_pips_buy * pip_cost

    # --- Short entry conditions ---
    ideal_profit_pips_sell = (current_price - min(daily_preds)) / pip_cost
    ideal_drawdown_pips_sell = max(
        (max(daily_preds) - current_price) / pip_cost,
        min_drawdown_pips
    )
    rr_sell = ideal_profit_pips_sell / ideal_drawdown_pips_sell if ideal_drawdown_pips_sell > 0 else 0
    tp_sell = current_price - tp_multiplier * ideal_profit_pips_sell * pip_cost
    sl_sell = current_price + sl_multiplier * ideal_drawdown_pips_sell * pip_cost

    long_signal = ideal_profit_pips_buy >= profit_threshold
    short_signal = ideal_profit_pips_sell >= profit_threshold

    if long_signal and (rr_buy >= rr_sell):
        signal = 'buy'
        chosen_tp = tp_buy
        chosen_sl = sl_buy
        chosen_rr = rr_buy
    elif short_signal and (rr_sell > rr_buy):
        signal = 'sell'
        chosen_tp = tp_sell
        chosen_sl = sl_sell
        chosen_rr = rr_sell
    else:
        return {"action": "hold", "reason": "no signal meets threshold", "tp": 0, "sl": 0, "volume": 0}

    # Compute volume (size) based on RR
    volume = _compute_size(chosen_rr, cfg, balance)
    if volume <= 0:
        return {"action": "hold", "reason": "computed volume <= 0", "tp": 0, "sl": 0, "volume": 0}

    return {
        "action": signal,
        "tp": chosen_tp,
        "sl": chosen_sl,
        "volume": volume,
        "rr": chosen_rr,
        "entry_price": current_price,
        "reason": f"{signal} signal: profit_pips={'%.1f' % (ideal_profit_pips_buy if signal=='buy' else ideal_profit_pips_sell)}, RR={'%.2f' % chosen_rr}",
    }


def should_early_close(
    direction: str,
    exit_variant: str,
    hourly_predictions: List[float],
    daily_predictions: List[float],
    sl: float,
    entry_price: float = None,
) -> bool:
    """
    Check if an open position should be closed early based on exit variant.
    Copied from heuristic-strategy _should_early_close_long/_should_early_close_short.
    """
    v = exit_variant
    preds_h = hourly_predictions or []
    preds_d = daily_predictions or []

    if direction == 'long':
        if v == 'A':
            all_p = preds_h + preds_d
            return bool(all_p) and min(all_p) < sl
        elif v == 'B':
            return bool(preds_d) and min(preds_d) < sl
        elif v == 'C':
            return bool(preds_h) and min(preds_h) < sl
        elif v == 'D':
            h_trig = bool(preds_h) and min(preds_h) < sl
            d_trig = bool(preds_d) and min(preds_d) < sl
            return h_trig and d_trig
        elif v == 'E':
            if preds_h and preds_d:
                return 0.6 * min(preds_h) + 0.4 * min(preds_d) < sl
            elif preds_h:
                return min(preds_h) < sl
            elif preds_d:
                return min(preds_d) < sl
        elif v == 'F':
            buf = 0.5 * abs(sl - entry_price) if entry_price else 0
            h_trig = bool(preds_h) and min(preds_h) < (sl - buf)
            d_trig = bool(preds_d) and min(preds_d) < sl
            return h_trig or d_trig
        elif v == 'G':
            return False
    elif direction == 'short':
        if v == 'A':
            all_p = preds_h + preds_d
            return bool(all_p) and max(all_p) > sl
        elif v == 'B':
            return bool(preds_d) and max(preds_d) > sl
        elif v == 'C':
            return bool(preds_h) and max(preds_h) > sl
        elif v == 'D':
            h_trig = bool(preds_h) and max(preds_h) > sl
            d_trig = bool(preds_d) and max(preds_d) > sl
            return h_trig and d_trig
        elif v == 'E':
            if preds_h and preds_d:
                return 0.6 * max(preds_h) + 0.4 * max(preds_d) > sl
            elif preds_h:
                return max(preds_h) > sl
            elif preds_d:
                return max(preds_d) > sl
        elif v == 'F':
            buf = 0.5 * abs(sl - entry_price) if entry_price else 0
            h_trig = bool(preds_h) and max(preds_h) > (sl + buf)
            d_trig = bool(preds_d) and max(preds_d) > sl
            return h_trig or d_trig
        elif v == 'G':
            return False
    return False


def _compute_size(rr: float, cfg: Dict[str, Any], balance: float) -> float:
    """Compute order size based on RR ratio, copied from heuristic-strategy."""
    min_vol = cfg['min_order_volume']
    max_vol = cfg['max_order_volume']
    lower_rr = cfg['lower_rr_threshold']
    upper_rr = cfg['upper_rr_threshold']
    rel_volume = cfg['rel_volume']
    leverage = cfg['leverage']

    if rr >= upper_rr:
        size = max_vol
    elif rr <= lower_rr:
        size = min_vol
    else:
        size = min_vol + ((rr - lower_rr) / (upper_rr - lower_rr)) * (max_vol - min_vol)

    max_from_cash = balance * rel_volume * leverage
    return min(size, max_from_cash)
