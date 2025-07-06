"""
Default Strategy Plugin for LTS (Live Trading System)

This is a dummy strategy plugin for testing purposes that alternates between
opening and closing orders based on the current position status.
"""

from app.plugin_base import BasePlugin
from app.database import SessionLocal, Order, Position
import random

class StrategyPlugin(BasePlugin):
    plugin_params = {
        "position_size": 0.02,
        "max_risk_per_trade": 0.02,
        "prediction_threshold": 0.6,
        "stop_loss_pips": 80,
        "take_profit_pips": 100,
        "order_type": "market"
    }
    
    plugin_debug_vars = ["position_size", "max_risk_per_trade", "prediction_threshold"]
    
    def __init__(self, config=None):
        super().__init__(config)
        self.order_counter = 0
    
    def process(self, asset, market_data=None, predictions=None):
        """
        Process asset data and return trading action.
        
        This dummy strategy alternates between opening and closing orders:
        - If no open position exists, it returns an "open" action
        - If an open position exists, it returns a "close" action
        - If no action is needed, it returns "none"
        
        Args:
            asset: Asset object from database
            market_data: Market data (not used in dummy strategy)
            predictions: Predictions data (not used in dummy strategy)
            
        Returns:
            dict: Trading action with parameters for broker
        """
        try:
            # Check if there's an open position for this asset
            db = SessionLocal()
            open_position = db.query(Position).filter(
                Position.asset_id == asset.id,
                Position.status == "open"
            ).first()
            
            if open_position:
                # There's an open position, return close action
                db.close()
                return {
                    "action": "close",
                    "parameters": {
                        "position_id": open_position.id,
                        "broker_position_id": open_position.broker_position_id
                    }
                }
            else:
                # No open position, check if we should open one
                # For testing, alternate between buy and sell
                side = "buy" if self.order_counter % 2 == 0 else "sell"
                self.order_counter += 1
                
                # Generate dummy market price (for testing)
                if asset.symbol == "EUR/USD":
                    base_price = 1.0950
                elif asset.symbol == "GBP/USD":
                    base_price = 1.2650
                else:
                    base_price = 1.0000
                
                # Add some randomness to simulate market movement
                current_price = base_price + random.uniform(-0.0050, 0.0050)
                
                # Calculate stop loss and take profit
                if side == "buy":
                    stop_loss = current_price - (self.params["stop_loss_pips"] * 0.0001)
                    take_profit = current_price + (self.params["take_profit_pips"] * 0.0001)
                else:
                    stop_loss = current_price + (self.params["stop_loss_pips"] * 0.0001)
                    take_profit = current_price - (self.params["take_profit_pips"] * 0.0001)
                
                db.close()
                return {
                    "action": "open",
                    "parameters": {
                        "order_type": self.params["order_type"],
                        "side": side,
                        "quantity": self.params["position_size"],
                        "price": current_price,
                        "stop_loss": stop_loss,
                        "take_profit": take_profit,
                        "symbol": asset.symbol
                    }
                }
                
        except Exception as e:
            print(f"Error in strategy processing: {e}")
            return {
                "action": "none",
                "parameters": {},
                "error": str(e)
            }
    
    def get_market_data(self, symbol):
        """
        Get market data for a symbol (dummy implementation)
        """
        # In a real strategy, this would fetch actual market data
        return {
            "symbol": symbol,
            "price": 1.0950 + random.uniform(-0.01, 0.01),
            "timestamp": "2025-01-01T00:00:00Z"
        }
    
    def analyze_predictions(self, predictions):
        """
        Analyze predictions to determine trading signal (dummy implementation)
        """
        # In a real strategy, this would analyze actual predictions
        return {
            "signal": "buy" if random.random() > 0.5 else "sell",
            "confidence": random.uniform(0.5, 1.0)
        }
