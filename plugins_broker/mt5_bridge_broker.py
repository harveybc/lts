"""Fail-closed MT5 demo bridge broker during read-only observation."""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.plugin_base import PluginBase


def _disabled() -> Dict[str, Any]:
    return {
        "success": False,
        "error": (
            "MT5 order submission is disabled during read-only capability "
            "observation and before protected SL+TP canaries"
        ),
    }


class Mt5BridgeBroker(PluginBase):
    """Declares the MT5 venue while all mutations remain disabled."""

    plugin_params = {
        "bridge_url": "http://100.110.215.85:8766",
        "environment": "demo",
        "read_only": True,
        "require_stop_loss": True,
        "require_take_profit": True,
    }
    plugin_debug_vars = [
        "bridge_url",
        "environment",
        "read_only",
        "require_stop_loss",
        "require_take_profit",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        if self.params.get("environment") != "demo":
            raise ValueError("The initial MT5 bridge broker is demo-only")
        if self.params.get("read_only") is not True:
            raise ValueError("Protected MT5 canaries are not enabled")

    def open_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _disabled()

    def modify_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _disabled()

    def close_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _disabled()

    def execute_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _disabled()

    def get_open_orders(self) -> list[Dict[str, Any]]:
        raise RuntimeError("Use the authenticated MT5 bridge snapshot report")
