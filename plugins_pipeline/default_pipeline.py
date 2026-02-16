"""
Default Pipeline plugin for LTS.
Orchestrates the execution of all plugins and manages the main trading loop.
"""
import os as _os
_QUIET = _os.environ.get('LTS_QUIET', '0') == '1'

from app.plugin_base import PipelinePluginBase
from app.database import SessionLocal, User, Portfolio, Asset, Order, Statistics
from datetime import datetime, timezone, timedelta
import time
import json

class PipelinePlugin(PipelinePluginBase):
    """Default Pipeline plugin implementation"""
    
    # Plugin-specific parameters
    plugin_params = {
        "global_latency": 5,  # Minutes between executions
        "max_concurrent_portfolios": 10,
        "execution_timeout": 300,  # Seconds
        "error_retry_count": 3,
        "error_retry_delay": 60,  # Seconds
        "statistics_enabled": True,
        "debug_mode": False,
        "log_level": "INFO"
    }
    
    # Debug variables
    plugin_debug_vars = ["global_latency", "max_concurrent_portfolios", "execution_timeout", "error_retry_count"]

    def __init__(self, config=None):
        """Initialize Pipeline plugin with configuration"""
        super().__init__(config)
        self.db = SessionLocal()
        self.plugins = {}
        self.running = False

    def set_params(self, **kwargs):
        """Update parameters with global configuration"""
        super().set_params(**kwargs)

    def get_debug_info(self):
        """Return debug information"""
        return super().get_debug_info()

    def add_debug_info(self, debug_info):
        """Add debug information to dictionary"""
        super().add_debug_info(debug_info)

    def start(self, plugins: dict):
        """Start the pipeline with all loaded plugins"""
        self.plugins = plugins
        
        # Start the core plugin first
        core_plugin = self.plugins.get('core')
        if core_plugin:
            if not _QUIET: print("Pipeline: Starting Core Plugin...")
            core_plugin.set_plugins(self.plugins)
            core_plugin.start()
        else:
            if not _QUIET: print("Pipeline: Error - Core plugin not found")
            
    def run(self, portfolio_id: int = None, assets: list = None) -> dict:
        """Execute trading logic for portfolios"""
        try:
            execution_results = {
                "timestamp": datetime.now(timezone.utc),
                "portfolios_executed": 0,
                "total_orders": 0,
                "errors": []
            }
            
            # Get all active portfolios for all users
            portfolios = self.db.query(Portfolio).filter(Portfolio.is_active == True).all()
            
            if portfolio_id:
                # Filter by specific portfolio
                portfolios = [p for p in portfolios if p.id == portfolio_id]
            
            for portfolio in portfolios:
                try:
                    # Check if portfolio should be executed based on latency
                    if self._should_execute_portfolio(portfolio):
                        result = self._execute_portfolio(portfolio)
                        execution_results["portfolios_executed"] += 1
                        execution_results["total_orders"] += result.get("orders_created", 0)
                        
                        # Update portfolio last execution time
                        portfolio.last_execution = datetime.now(timezone.utc)
                        self.db.commit()
                        
                except Exception as e:
                    error_msg = f"Error executing portfolio {portfolio.id}: {str(e)}"
                    execution_results["errors"].append(error_msg)
                    if not _QUIET: print(f"Pipeline: {error_msg}")
            
            # Record statistics
            if self.params["statistics_enabled"]:
                self._record_statistics(execution_results)
            
            return execution_results
            
        except Exception as e:
            if not _QUIET: print(f"Pipeline: Critical error in run(): {str(e)}")
            return {"error": str(e), "timestamp": datetime.now(timezone.utc)}

    def _should_execute_portfolio(self, portfolio: Portfolio) -> bool:
        """Check if portfolio should be executed based on latency settings"""
        if not portfolio.last_execution:
            return True
            
        # Check portfolio-specific latency
        portfolio_latency = portfolio.portfolio_latency_minutes or self.params["global_latency"]
        time_since_last = datetime.now(timezone.utc) - portfolio.last_execution
        
        return time_since_last >= timedelta(minutes=portfolio_latency)

    def _execute_portfolio(self, portfolio: Portfolio) -> dict:
        """Execute trading logic for a specific portfolio"""
        try:
            result = {
                "portfolio_id": portfolio.id,
                "orders_created": 0,
                "assets_processed": 0,
                "errors": []
            }
            
            # Get portfolio plugin
            portfolio_plugin = self.plugins.get('portfolio')
            if not portfolio_plugin:
                result["errors"].append("Portfolio plugin not available")
                return result
            
            # Get assets for this portfolio
            assets = self.db.query(Asset).filter(Asset.portfolio_id == portfolio.id).all()
            
            # First, run portfolio allocation
            allocation_result = portfolio_plugin.allocate(portfolio.id, assets)
            
            # Then execute strategy for each asset
            for asset in assets:
                try:
                    asset_result = self._execute_asset(asset, portfolio)
                    result["assets_processed"] += 1
                    result["orders_created"] += asset_result.get("orders_created", 0)
                    
                except Exception as e:
                    error_msg = f"Error executing asset {asset.id}: {str(e)}"
                    result["errors"].append(error_msg)
                    if not _QUIET: print(f"Pipeline: {error_msg}")
            
            return result
            
        except Exception as e:
            if not _QUIET: print(f"Pipeline: Error in _execute_portfolio(): {str(e)}")
            return {"error": str(e), "portfolio_id": portfolio.id}

    def _execute_asset(self, asset: Asset, portfolio: Portfolio) -> dict:
        """Execute trading logic for a specific asset"""
        try:
            result = {
                "asset_id": asset.id,
                "orders_created": 0,
                "action": "none"
            }
            
            # Get strategy plugin
            strategy_plugin = self.plugins.get('strategy')
            if not strategy_plugin:
                result["error"] = "Strategy plugin not available"
                return result
            
            # Get broker plugin
            broker_plugin = self.plugins.get('broker')
            if not broker_plugin:
                result["error"] = "Broker plugin not available"
                return result
            
            # Prepare asset data
            asset_data = {
                "symbol": asset.symbol,
                "allocated_capital": asset.allocated_capital,
                "strategy_config": json.loads(asset.strategy_config or "{}"),
                "broker_config": json.loads(asset.broker_config or "{}"),
                "pipeline_config": json.loads(asset.pipeline_config or "{}")
            }
            
            # Get current market data (dummy for now)
            market_data = {
                "timestamp": datetime.now(timezone.utc),
                "price": 1.0,  # Dummy data
                "volume": 1000,
                "bid": 0.99,
                "ask": 1.01
            }
            
            # Make trading decision
            decision = strategy_plugin.decide(asset_data, market_data)
            
            # Execute decision if needed
            if decision.get("action") != "none":
                # Check if we have open orders for this asset
                open_orders = self.db.query(Order).filter(
                    Order.asset_id == asset.id,
                    Order.status == "open"
                ).all()
                
                # For now, we only allow one order per asset
                if not open_orders and decision.get("action") in ["buy", "sell"]:
                    order_params = {
                        "symbol": asset.symbol,
                        "action": decision["action"],
                        "quantity": decision.get("quantity", 1),
                        "price": decision.get("price", market_data["price"]),
                        "type": decision.get("type", "market")
                    }
                    
                    # Execute order through broker
                    broker_result = broker_plugin.open_order(order_params)
                    
                    if broker_result.get("success"):
                        # Record order in database
                        order = Order(
                            asset_id=asset.id,
                            portfolio_id=portfolio.id,
                            symbol=asset.symbol,
                            action=decision["action"],
                            quantity=decision.get("quantity", 1),
                            price=decision.get("price", market_data["price"]),
                            order_type=decision.get("type", "market"),
                            status="open",
                            broker_order_id=broker_result.get("order_id"),
                            created_at=datetime.now(timezone.utc)
                        )
                        
                        self.db.add(order)
                        self.db.commit()
                        
                        result["orders_created"] = 1
                        result["action"] = decision["action"]
            
            return result
            
        except Exception as e:
            if not _QUIET: print(f"Pipeline: Error in _execute_asset(): {str(e)}")
            return {"error": str(e), "asset_id": asset.id}

    def _record_statistics(self, execution_results: dict):
        """Record execution statistics"""
        try:
            stats = [
                ("portfolios_executed", execution_results["portfolios_executed"]),
                ("total_orders", execution_results["total_orders"]),
                ("errors_count", len(execution_results["errors"]))
            ]
            
            for key, value in stats:
                stat = Statistics(
                    key=key,
                    value=float(value),
                    timestamp=datetime.now(timezone.utc)
                )
                self.db.add(stat)
            
            self.db.commit()
            
        except Exception as e:
            if not _QUIET: print(f"Pipeline: Error recording statistics: {str(e)}")
