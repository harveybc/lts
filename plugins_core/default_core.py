"""
Default Core Plugin for LTS (Live Trading System)

This plugin is responsible for:
1. Starting the FastAPI server
2. Running the main trading loop
3. Coordinating all other plugins
"""

from app.plugin_base import BasePlugin
from app.database import SessionLocal, User, Portfolio, Asset, Order, Position, AuditLog
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
import threading
import time
import asyncio
import uvicorn
import schedule
import json

# Create the FastAPI app instance that can be imported by main.py
app = FastAPI(title="LTS API", description="Live Trading System API")

# Security
security = HTTPBearer()

class CorePlugin(BasePlugin):
    plugin_params = {
        "global_latency": 5,  # Minutes between main loop executions
        "api_host": "0.0.0.0",
        "api_port": 8000,
        "api_debug": False,
        "max_parallel_portfolios": 10,
        "log_level": "INFO"
    }
    
    plugin_debug_vars = ["global_latency", "api_host", "api_port", "max_parallel_portfolios"]
    
    def __init__(self, config=None):
        super().__init__(config)
        self.app = app  # Use the global app instance
        self.server = None
        self.running = False
        self.plugins = {}
        self.setup_routes()
    
    def set_plugins(self, plugins):
        """Set references to all loaded plugins"""
        self.plugins = plugins
    
    def setup_routes(self):
        """Setup API routes"""
        
        @self.app.get("/")
        async def root():
            return {"message": "LTS API is running", "version": "1.0.0"}
        
        @self.app.get("/health")
        async def health_check():
            return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}
        
        @self.app.get("/api/portfolios")
        async def get_portfolios():
            """Get all portfolios"""
            db = SessionLocal()
            try:
                portfolios = db.query(Portfolio).all()
                return [self._portfolio_to_dict(p) for p in portfolios]
            finally:
                db.close()
        
        @self.app.get("/api/assets/{portfolio_id}")
        async def get_assets(portfolio_id: int):
            """Get assets for a portfolio"""
            db = SessionLocal()
            try:
                assets = db.query(Asset).filter(Asset.portfolio_id == portfolio_id).all()
                return [self._asset_to_dict(a) for a in assets]
            finally:
                db.close()
        
        @self.app.get("/api/orders")
        async def get_orders():
            """Get all orders"""
            db = SessionLocal()
            try:
                orders = db.query(Order).all()
                return [self._order_to_dict(o) for o in orders]
            finally:
                db.close()
        
        @self.app.get("/api/positions")
        async def get_positions():
            """Get all positions"""
            db = SessionLocal()
            try:
                positions = db.query(Position).all()
                return [self._position_to_dict(p) for p in positions]
            finally:
                db.close()
    
    def _portfolio_to_dict(self, portfolio):
        """Convert portfolio to dict for API response"""
        return {
            "id": portfolio.id,
            "user_id": portfolio.user_id,
            "name": portfolio.name,
            "description": portfolio.description,
            "is_active": portfolio.is_active,
            "portfolio_plugin": portfolio.portfolio_plugin,
            "total_capital": portfolio.total_capital,
            "last_execution": portfolio.last_execution.isoformat() if portfolio.last_execution else None,
            "portfolio_latency": portfolio.portfolio_latency,
            "created_at": portfolio.created_at.isoformat(),
            "updated_at": portfolio.updated_at.isoformat()
        }
    
    def _asset_to_dict(self, asset):
        """Convert asset to dict for API response"""
        return {
            "id": asset.id,
            "portfolio_id": asset.portfolio_id,
            "symbol": asset.symbol,
            "name": asset.name,
            "strategy_plugin": asset.strategy_plugin,
            "broker_plugin": asset.broker_plugin,
            "pipeline_plugin": asset.pipeline_plugin,
            "allocated_capital": asset.allocated_capital,
            "is_active": asset.is_active,
            "created_at": asset.created_at.isoformat(),
            "updated_at": asset.updated_at.isoformat()
        }
    
    def _order_to_dict(self, order):
        """Convert order to dict for API response"""
        return {
            "id": order.id,
            "asset_id": order.asset_id,
            "order_type": order.order_type,
            "side": order.side,
            "quantity": order.quantity,
            "price": order.price,
            "status": order.status,
            "broker_order_id": order.broker_order_id,
            "created_at": order.created_at.isoformat(),
            "filled_at": order.filled_at.isoformat() if order.filled_at else None
        }
    
    def _position_to_dict(self, position):
        """Convert position to dict for API response"""
        return {
            "id": position.id,
            "asset_id": position.asset_id,
            "side": position.side,
            "quantity": position.quantity,
            "entry_price": position.entry_price,
            "current_price": position.current_price,
            "unrealized_pnl": position.unrealized_pnl,
            "status": position.status,
            "opened_at": position.opened_at.isoformat(),
            "closed_at": position.closed_at.isoformat() if position.closed_at else None
        }
    
    def start(self):
        """Start the core plugin - API server and trading loop"""
        print("Starting Core Plugin...")
        self.running = True
        
        # Schedule the trading loop
        schedule.every(self.params["global_latency"]).minutes.do(self.execute_trading_loop)
        
        # Start the scheduler in a separate thread
        scheduler_thread = threading.Thread(target=self._run_scheduler, daemon=True)
        scheduler_thread.start()
        
        # Start the FastAPI server
        print(f"Starting API server on {self.params['api_host']}:{self.params['api_port']}")
        try:
            uvicorn.run(
                self.app,
                host=self.params["api_host"],
                port=self.params["api_port"],
                log_level=self.params["log_level"].lower()
            )
        except Exception as e:
            print(f"Error starting API server: {e}")
            self.running = False
    
    def _run_scheduler(self):
        """Run the scheduler in a separate thread"""
        while self.running:
            schedule.run_pending()
            time.sleep(1)
    
    def stop(self):
        """Stop the core plugin"""
        print("Stopping Core Plugin...")
        self.running = False
        if self.server:
            self.server.should_exit = True
    
    def execute_trading_loop(self):
        """Execute the main trading loop"""
        print(f"Executing trading loop at {datetime.now(timezone.utc)}")
        
        try:
            db = SessionLocal()
            
            # Get all active portfolios
            portfolios = db.query(Portfolio).filter(Portfolio.is_active == True).all()
            
            for portfolio in portfolios:
                self._process_portfolio(portfolio, db)
            
            db.close()
            
        except Exception as e:
            print(f"Error in trading loop: {e}")
            if 'db' in locals():
                db.close()
    
    def _process_portfolio(self, portfolio, db):
        """Process a single portfolio"""
        try:
            # Check if enough time has passed since last execution
            if portfolio.last_execution:
                minutes_since_last = (datetime.now(timezone.utc) - portfolio.last_execution).total_seconds() / 60
                if minutes_since_last < portfolio.portfolio_latency:
                    print(f"Skipping portfolio {portfolio.name} - not enough time elapsed")
                    return
            
            print(f"Processing portfolio: {portfolio.name}")
            
            # Get portfolio plugin
            portfolio_plugin = self.plugins.get('portfolio')
            if not portfolio_plugin:
                print("Portfolio plugin not found")
                return
            
            # Get active assets for this portfolio
            assets = db.query(Asset).filter(
                Asset.portfolio_id == portfolio.id,
                Asset.is_active == True
            ).all()
            
            if not assets:
                print(f"No active assets in portfolio {portfolio.name}")
                return
            
            # Allocate capital using portfolio plugin
            try:
                # Parse portfolio config
                portfolio_config = json.loads(portfolio.portfolio_config) if portfolio.portfolio_config else {}
                
                # Set portfolio plugin parameters
                portfolio_plugin.set_params(**portfolio_config)
                
                # Allocate capital
                allocation = portfolio_plugin.allocate_capital(portfolio, assets)
                
                # Update asset allocations
                for asset in assets:
                    if asset.id in allocation:
                        asset.allocated_capital = allocation[asset.id]
                        db.commit()
                
            except Exception as e:
                print(f"Error in portfolio allocation: {e}")
            
            # Process each asset
            for asset in assets:
                self._process_asset(asset, db)
            
            # Update portfolio last execution time
            portfolio.last_execution = datetime.now(timezone.utc)
            db.commit()
            
        except Exception as e:
            print(f"Error processing portfolio {portfolio.name}: {e}")
    
    def _process_asset(self, asset, db):
        """Process a single asset"""
        try:
            print(f"Processing asset: {asset.symbol}")
            
            # Get strategy plugin
            strategy_plugin = self.plugins.get('strategy')
            if not strategy_plugin:
                print("Strategy plugin not found")
                return
            
            # Get broker plugin
            broker_plugin = self.plugins.get('broker')
            if not broker_plugin:
                print("Broker plugin not found")
                return
            
            # Parse asset configs
            strategy_config = json.loads(asset.strategy_config) if asset.strategy_config else {}
            broker_config = json.loads(asset.broker_config) if asset.broker_config else {}
            
            # Set plugin parameters
            strategy_plugin.set_params(**strategy_config)
            broker_plugin.set_params(**broker_config)
            
            # Get market data and predictions (dummy for now)
            market_data = {"price": 1.0950, "timestamp": datetime.now(timezone.utc).isoformat()}
            predictions = {"signal": "buy", "confidence": 0.7}
            
            # Process with strategy plugin
            strategy_result = strategy_plugin.process(asset, market_data, predictions)
            
            if strategy_result.get("action") == "none":
                print(f"No action needed for {asset.symbol}")
                return
            
            # Execute with broker plugin
            broker_result = broker_plugin.execute(
                strategy_result["action"],
                strategy_result["parameters"]
            )
            
            # Save order and position to database
            self._save_trading_result(asset, strategy_result, broker_result, db)
            
        except Exception as e:
            print(f"Error processing asset {asset.symbol}: {e}")
    
    def _save_trading_result(self, asset, strategy_result, broker_result, db):
        """Save trading result to database"""
        try:
            action = strategy_result["action"]
            parameters = strategy_result["parameters"]
            
            if action == "open" and broker_result.get("success"):
                # Create order record
                order = Order(
                    asset_id=asset.id,
                    order_type=parameters.get("order_type", "market"),
                    side=parameters.get("side"),
                    quantity=parameters.get("quantity"),
                    price=broker_result.get("execution_price"),
                    stop_loss=parameters.get("stop_loss"),
                    take_profit=parameters.get("take_profit"),
                    status="filled",
                    broker_order_id=broker_result.get("broker_order_id"),
                    broker_response=json.dumps(broker_result["broker_response"]),
                    created_at=datetime.now(timezone.utc),
                    filled_at=datetime.now(timezone.utc)
                )
                db.add(order)
                db.commit()
                
                # Create position record
                position = Position(
                    asset_id=asset.id,
                    order_id=order.id,
                    side=parameters.get("side"),
                    quantity=parameters.get("quantity"),
                    entry_price=broker_result.get("execution_price"),
                    current_price=broker_result.get("execution_price"),
                    unrealized_pnl=0.0,
                    realized_pnl=0.0,
                    status="open",
                    broker_position_id=broker_result.get("broker_position_id"),
                    opened_at=datetime.now(timezone.utc)
                )
                db.add(position)
                db.commit()
                
                print(f"Opened {parameters.get('side')} position for {asset.symbol}")
                
            elif action == "close" and broker_result.get("success"):
                # Update position to closed
                position_id = parameters.get("position_id")
                if position_id:
                    position = db.query(Position).filter(Position.id == position_id).first()
                    if position:
                        position.status = "closed"
                        position.current_price = broker_result.get("close_price")
                        position.closed_at = datetime.now(timezone.utc)
                        # Calculate realized P&L (simplified)
                        if position.side == "buy":
                            position.realized_pnl = (broker_result.get("close_price") - position.entry_price) * position.quantity
                        else:
                            position.realized_pnl = (position.entry_price - broker_result.get("close_price")) * position.quantity
                        db.commit()
                        
                        print(f"Closed position for {asset.symbol} with P&L: {position.realized_pnl}")
            
            elif not broker_result.get("success"):
                print(f"Broker execution failed for {asset.symbol}: {broker_result.get('error_message')}")
                
        except Exception as e:
            print(f"Error saving trading result: {e}")
            db.rollback()
