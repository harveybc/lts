"""
Plugin base classes for LTS (Live Trading System) plugins.
Defines the exact required interface, params, and debug info structure
that all plugins must implement.
"""
from typing import Dict, Any

class PluginBase:
    """
    Base class for all LTS plugins.
    All plugins must inherit from this class and follow the exact structure.
    """
    # Plugin-specific parameters - must be overridden in each plugin
    plugin_params: Dict[str, Any] = {}
    
    # Plugin-specific debug variables - must be overridden in each plugin
    plugin_debug_vars: list = []

    def __init__(self, config: Dict[str, Any] = None):
        """
        Initialize plugin with parameters.
        This method structure must be exactly the same in all plugins.
        """
        self.params = self.plugin_params.copy()
        if config:
            self.set_params(**config)

    def set_params(self, **kwargs):
        """
        Update plugin parameters with global configuration.
        This method structure must be exactly the same in all plugins.
        """
        for key, value in kwargs.items():
            self.params[key] = value

    def get_debug_info(self):
        """
        Return debug information for relevant plugin parameters.
        This method structure must be exactly the same in all plugins.
        """
        return {var: self.params.get(var) for var in self.plugin_debug_vars}

    def add_debug_info(self, debug_info):
        """
        Add debug information to the provided dictionary.
        This method structure must be exactly the same in all plugins.
        """
        debug_info.update(self.get_debug_info())

class AAAPluginBase(PluginBase):
    """Base class for AAA (Authentication, Authorization, Accounting) plugins"""
    def register(self, username: str, email: str, password: str, role: str) -> bool:
        raise NotImplementedError("AAA plugin must implement register()")
    
    def login(self, username: str, password: str) -> dict:
        raise NotImplementedError("AAA plugin must implement login()")
    
    def assign_role(self, username: str, role: str) -> bool:
        raise NotImplementedError("AAA plugin must implement assign_role()")
    
    def audit(self, user_id: int, action: str, details: str = None) -> None:
        raise NotImplementedError("AAA plugin must implement audit()")
    
    def create_session(self, user_id: int) -> str:
        raise NotImplementedError("AAA plugin must implement create_session()")

class CorePluginBase(PluginBase):
    """Base class for Core plugins"""
    def start(self):
        raise NotImplementedError("Core plugin must implement start()")
    
    def stop(self):
        raise NotImplementedError("Core plugin must implement stop()")
    
    def set_plugins(self, plugins: dict):
        raise NotImplementedError("Core plugin must implement set_plugins()")

class PipelinePluginBase(PluginBase):
    """Base class for Pipeline plugins"""
    def run(self, portfolio_id: int, assets: list) -> dict:
        raise NotImplementedError("Pipeline plugin must implement run()")
    
    def start(self, plugins: dict):
        raise NotImplementedError("Pipeline plugin must implement start()")

class StrategyPluginBase(PluginBase):
    """Base class for Strategy plugins"""
    def decide(self, asset_data: dict, market_data: dict) -> dict:
        raise NotImplementedError("Strategy plugin must implement decide()")

class BrokerPluginBase(PluginBase):
    """Base class for Broker plugins"""
    def open_order(self, order_params: dict) -> dict:
        raise NotImplementedError("Broker plugin must implement open_order()")
    
    def modify_order(self, order_id: str, new_params: dict) -> dict:
        raise NotImplementedError("Broker plugin must implement modify_order()")
    
    def close_order(self, order_id: str) -> dict:
        raise NotImplementedError("Broker plugin must implement close_order()")
    
    def get_open_orders(self) -> list:
        raise NotImplementedError("Broker plugin must implement get_open_orders()")

class PortfolioPluginBase(PluginBase):
    """Base class for Portfolio plugins"""
    def allocate(self, portfolio_id: int, assets: list) -> dict:
        raise NotImplementedError("Portfolio plugin must implement allocate()")
    
    def update(self, portfolio_id: int, operation_result: dict) -> None:
        raise NotImplementedError("Portfolio plugin must implement update()")
