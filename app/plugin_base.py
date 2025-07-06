"""
Plugin base classes for encoder/decoder and AAA plugins.
Defines required interface, params, and debug info structure.
"""
from typing import Dict, Any

class PluginBase:
    plugin_params: Dict[str, Any] = {}
    plugin_debug_vars: list = []

    def __init__(self, config: Dict[str, Any] = None):
        self.params = self.plugin_params.copy()
        if config:
            self.params.update(config)
        self.debug_info = {var: None for var in self.plugin_debug_vars}

    def set_params(self, params: Dict[str, Any]):
        for k, v in params.items():
            if k in self.plugin_params:
                self.params[k] = v

    def get_debug_info(self) -> Dict[str, Any]:
        return self.debug_info.copy()

    def add_debug_info(self, key: str, value: Any):
        if key in self.plugin_debug_vars:
            self.debug_info[key] = value

    def run(self, *args, **kwargs):
        raise NotImplementedError("Plugin must implement run()")

class EncoderPluginBase(PluginBase):
    pass

class DecoderPluginBase(PluginBase):
    pass

class AAABase(PluginBase):
    def register(self, user: str, email: str, password: str, role: str) -> bool:
        raise NotImplementedError
    def login(self, user: str, password: str) -> bool:
        raise NotImplementedError
    def assign_role(self, user: str, role: str) -> bool:
        raise NotImplementedError
    def audit(self, user: str, action: str) -> None:
        raise NotImplementedError
    def session(self, user: str) -> str:
        raise NotImplementedError
