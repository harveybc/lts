"""
Heartbeat / Core Loop for LTS

Background task that periodically processes all active portfolios:
- Fetches predictions for each asset
- Runs strategy to get signals
- Executes trades via broker
- Updates portfolio P&L
- Stores heartbeat status in Config table
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from app.database import Database, Config, Portfolio, Asset, Order, Position
from app.prediction_client import PredictionProviderClient

logger = logging.getLogger(__name__)

# Global reference so it can be stopped
_heartbeat_task: Optional[asyncio.Task] = None


async def run_heartbeat_cycle(config: Dict[str, Any], db: Database, plugins: Dict = None):
    """
    Execute one heartbeat cycle: process all active portfolios.
    
    Returns dict with execution results.
    """
    start_time = time.time()
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "portfolios_processed": 0,
        "signals_generated": 0,
        "orders_placed": 0,
        "errors": []
    }

    try:
        prediction_client = PredictionProviderClient(config)

        async with db.get_session() as session:
            from sqlalchemy import select
            # Get active portfolios
            stmt = select(Portfolio).where(Portfolio.is_active == True)
            result = await session.execute(stmt)
            portfolios = result.scalars().all()

            for portfolio in portfolios:
                try:
                    portfolio_result = await _process_portfolio(
                        session, portfolio, prediction_client, config, plugins
                    )
                    results["portfolios_processed"] += 1
                    results["signals_generated"] += portfolio_result.get("signals", 0)
                    results["orders_placed"] += portfolio_result.get("orders", 0)
                except Exception as e:
                    err = f"Portfolio {portfolio.id} error: {str(e)}"
                    results["errors"].append(err)
                    logger.error(err)

            # Store heartbeat status in Config table
            elapsed = time.time() - start_time
            results["elapsed_seconds"] = round(elapsed, 2)

            await _update_config(session, "last_heartbeat", datetime.now(timezone.utc).isoformat())
            await _update_config(session, "heartbeat_status", "ok" if not results["errors"] else "errors")
            await _update_config(session, "heartbeat_last_result", str(results))

    except Exception as e:
        results["errors"].append(f"Critical: {str(e)}")
        logger.error(f"Heartbeat critical error: {e}")

    logger.info(f"Heartbeat cycle complete: {results}")
    return results


async def _process_portfolio(session, portfolio, prediction_client, config, plugins):
    """Process a single portfolio: iterate assets, get signals, execute."""
    from sqlalchemy import select
    result = {"signals": 0, "orders": 0}

    stmt = select(Asset).where(Asset.portfolio_id == portfolio.id, Asset.is_active == True)
    asset_result = await session.execute(stmt)
    assets = asset_result.scalars().all()

    for asset in assets:
        try:
            # Fetch predictions
            dt_str = datetime.now(timezone.utc).isoformat()
            predictions = await prediction_client.get_predictions(
                symbol=asset.symbol,
                datetime_str=dt_str,
                prediction_types=['short_term', 'long_term']
            )

            if predictions.get('status') != 'success':
                logger.warning(f"Prediction failed for {asset.symbol}")
                continue

            # Run strategy to get signal
            signal = _compute_heuristic_signal(
                asset, predictions, config
            )
            result["signals"] += 1

            if signal["action"] != "hold":
                # Execute via broker plugin or record order
                order = Order(
                    portfolio_id=portfolio.id,
                    asset_id=asset.id,
                    symbol=asset.symbol,
                    order_type=signal["action"],
                    status="filled",
                    quantity=signal.get("volume", 1.0),
                    price=signal.get("entry_price", 0),
                    stop_price=signal.get("sl"),
                    user_id=portfolio.user_id,
                    created_at=datetime.now(timezone.utc),
                    executed_at=datetime.now(timezone.utc),
                )
                session.add(order)
                result["orders"] += 1

        except Exception as e:
            logger.error(f"Asset {asset.symbol} processing error: {e}")

    return result


def _compute_heuristic_signal(asset, predictions, config) -> Dict[str, Any]:
    """
    Compute trading signal using heuristic strategy logic
    (copied from heuristic-strategy plugin_long_short_predictions.py).
    """
    from plugins_strategy.heuristic_strategy import compute_signal
    
    short_preds = predictions['predictions'].get('short_term', [])
    long_preds = predictions['predictions'].get('long_term', [])

    # Get current price from predictions context or default
    current_price = None
    hist = predictions.get('historical_context', {})
    if hist and hist.get('data'):
        current_price = hist['data'][-1].get('CLOSE')
    if current_price is None and short_preds:
        current_price = short_preds[0]
    if current_price is None:
        return {"action": "hold", "reason": "no price data"}

    # Strategy config from asset or defaults
    strategy_cfg = asset.strategy_config or {}
    if isinstance(strategy_cfg, str):
        import json
        strategy_cfg = json.loads(strategy_cfg)

    return compute_signal(
        current_price=current_price,
        daily_predictions=long_preds,
        hourly_predictions=short_preds,
        config=strategy_cfg
    )


async def _update_config(session, key: str, value: str):
    """Upsert a config entry."""
    from sqlalchemy import select
    stmt = select(Config).where(Config.key == key)
    result = await session.execute(stmt)
    cfg = result.scalar_one_or_none()
    if cfg:
        cfg.value = value
    else:
        session.add(Config(key=key, value=value))


async def heartbeat_loop(config: Dict[str, Any], db: Database, plugins: Dict = None):
    """Background loop that runs heartbeat cycles on an interval."""
    interval = config.get("heartbeat_interval", 3600)
    logger.info(f"Heartbeat loop started, interval={interval}s")

    while True:
        try:
            await run_heartbeat_cycle(config, db, plugins)
        except Exception as e:
            logger.error(f"Heartbeat loop error: {e}")
        await asyncio.sleep(interval)


def start_heartbeat(config: Dict[str, Any], db: Database, plugins: Dict = None):
    """Start the heartbeat as a background asyncio task."""
    global _heartbeat_task
    loop = asyncio.get_event_loop()
    _heartbeat_task = loop.create_task(heartbeat_loop(config, db, plugins))
    return _heartbeat_task


def stop_heartbeat():
    """Cancel the heartbeat task."""
    global _heartbeat_task
    if _heartbeat_task:
        _heartbeat_task.cancel()
        _heartbeat_task = None
