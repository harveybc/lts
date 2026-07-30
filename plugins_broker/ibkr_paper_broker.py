"""Fail-closed read-only IBKR TWS Paper broker adapter."""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.ibkr_paper_lab import IbkrPaperLabConfig, IbkrTwsPaperClient
from app.plugin_base import PluginBase


def _disabled() -> Dict[str, Any]:
    return {
        "success": False,
        "error": "IBKR Paper order submission is disabled pending protected bracket canaries",
    }


class IbkrPaperBroker(PluginBase):
    """Provides TWS Paper reads while every mutation fails closed."""

    plugin_params = {
        "host": "127.0.0.1",
        "port": 7497,
        "client_id": 8,
        "timeout_seconds": 15.0,
        "read_only": True,
    }

    plugin_debug_vars = ["host", "port", "client_id", "read_only"]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)

    def _client(self) -> IbkrTwsPaperClient:
        return IbkrTwsPaperClient(
            str(self.params["host"]),
            int(self.params["port"]),
            int(self.params["client_id"]),
            timeout_seconds=float(self.params["timeout_seconds"]),
        )

    def open_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _disabled()

    def modify_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _disabled()

    def close_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _disabled()

    def execute_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _disabled()

    def get_open_orders(self) -> list[Dict[str, Any]]:
        raise RuntimeError("Use the IBKR Paper preflight snapshot with declared contracts")
