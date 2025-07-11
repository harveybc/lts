"""
Default Broker Plugin for LTS (Live Trading System)

This is a dummy broker plugin for testing purposes that simulates
broker responses without making actual API calls.
"""

from app.plugin_base import PluginBase
from app.database import SyncSessionLocal as SessionLocal, Order, Position
import random
import uuid
from datetime import datetime, timezone

class DefaultBroker(PluginBase):
    plugin_params = {
        "broker_api_url": "https://api.oanda.com/v3",
        "api_key": "dummy_api_key",
        "account_id": "dummy_account",
        "timeout": 30,
        "spread": 0.0002,
        "execution_delay": 0.1
    }
    
    plugin_debug_vars = ["api_key", "account_id", "timeout", "spread"]
    
    def __init__(self, config=None):
        super().__init__(config)
        self.order_id_counter = 1000
        self.position_id_counter = 2000
    
    def execute_order(self, action, parameters):
        """
        Execute trading action with the broker (dummy implementation)
        
        Args:
            action: "open" or "close"
            parameters: Action parameters from strategy
            
        Returns:
            dict: Broker response simulation
        """
        try:
            if action == "open":
                return self._execute_open_order(parameters)
            elif action == "close":
                return self._execute_close_order(parameters)
            else:
                return {
                    "success": False,
                    "broker_order_id": None,
                    "broker_response": {},
                    "error_message": f"Unknown action: {action}"
                }
                
        except Exception as e:
            return {
                "success": False,
                "broker_order_id": None,
                "broker_response": {},
                "error_message": str(e)
            }
    
    def _execute_open_order(self, parameters):
        """
        Simulate opening an order with the broker
        """
        # Generate dummy broker order ID
        broker_order_id = f"ORDER_{self.order_id_counter}"
        self.order_id_counter += 1
        
        # Generate dummy broker position ID
        broker_position_id = f"POS_{self.position_id_counter}"
        self.position_id_counter += 1
        
        # Simulate execution price with spread
        execution_price = parameters.get("price", 1.0950)
        if parameters.get("side") == "buy":
            execution_price += self.params["spread"] / 2
        else:
            execution_price -= self.params["spread"] / 2
        
        # Add some randomness to simulate market movement
        execution_price += random.uniform(-0.0001, 0.0001)
        
        # Simulate broker response
        broker_response = {
            "orderId": broker_order_id,
            "positionId": broker_position_id,
            "symbol": parameters.get("symbol"),
            "side": parameters.get("side"),
            "quantity": parameters.get("quantity"),
            "executionPrice": execution_price,
            "stopLoss": parameters.get("stop_loss"),
            "takeProfit": parameters.get("take_profit"),
            "status": "filled",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "commission": 0.0,
            "spread": self.params["spread"]
        }
        
        # Simulate 95% success rate
        if random.random() < 0.95:
            return {
                "success": True,
                "broker_order_id": broker_order_id,
                "broker_position_id": broker_position_id,
                "execution_price": execution_price,
                "broker_response": broker_response,
                "error_message": None
            }
        else:
            return {
                "success": False,
                "broker_order_id": None,
                "broker_position_id": None,
                "broker_response": {},
                "error_message": "Simulated broker rejection"
            }
    
    def _execute_close_order(self, parameters):
        """
        Simulate closing a position with the broker
        """
        # Generate dummy close order ID
        close_order_id = f"CLOSE_{self.order_id_counter}"
        self.order_id_counter += 1
        
        # Get position info
        position_id = parameters.get("position_id")
        broker_position_id = parameters.get("broker_position_id")
        
        # Simulate close price
        close_price = 1.0950 + random.uniform(-0.01, 0.01)
        
        # Simulate broker response
        broker_response = {
            "closeOrderId": close_order_id,
            "positionId": broker_position_id,
            "closePrice": close_price,
            "status": "closed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pnl": random.uniform(-50, 100),  # Simulate P&L
            "commission": 0.0
        }
        
        # Simulate 98% success rate for closing
        if random.random() < 0.98:
            return {
                "success": True,
                "broker_order_id": close_order_id,
                "close_price": close_price,
                "broker_response": broker_response,
                "error_message": None
            }
        else:
            return {
                "success": False,
                "broker_order_id": None,
                "broker_response": {},
                "error_message": "Simulated broker close rejection"
            }
    
    def get_account_info(self):
        """
        Get account information (dummy implementation)
        """
        return {
            "account_id": self.params["account_id"],
            "balance": 10000.0,
            "equity": 10000.0,
            "margin_used": 0.0,
            "margin_free": 10000.0,
            "currency": "USD"
        }
    
    def get_open_positions(self):
        """
        Get open positions (dummy implementation)
        """
        # In a real broker plugin, this would query the broker API
        # For testing, we'll return empty list
        return []
    
    def get_market_data(self, symbol):
        """
        Get market data for a symbol (dummy implementation)
        """
        base_prices = {
            "EUR/USD": 1.0950,
            "GBP/USD": 1.2650,
            "USD/JPY": 148.50
        }
        
        base_price = base_prices.get(symbol, 1.0000)
        current_price = base_price + random.uniform(-0.01, 0.01)
        
        return {
            "symbol": symbol,
            "bid": current_price - self.params["spread"] / 2,
            "ask": current_price + self.params["spread"] / 2,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
