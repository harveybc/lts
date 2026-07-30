"""Fail-closed read-only Alpaca Paper broker adapter."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from app.alpaca_paper_lab import AlpacaPaperClient
from app.plugin_base import PluginBase


def _disabled() -> Dict[str, Any]:
    return {
        "success": False,
        "error": "Alpaca Paper order submission is disabled pending native SL+TP verification",
    }


class AlpacaPaperBroker(PluginBase):
    """Exposes reconciliation reads while all order mutations fail closed."""

    plugin_params = {
        "api_key_env": "ALPACA_PAPER_API_KEY_ID",
        "api_secret_env": "ALPACA_PAPER_API_SECRET_KEY",
        "timeout_seconds": 20.0,
        "read_only": True,
    }

    plugin_debug_vars = ["read_only"]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._client: Optional[AlpacaPaperClient] = None

    def _get_client(self) -> AlpacaPaperClient:
        if self._client is None:
            key = os.environ.get(self.params["api_key_env"], "")
            secret = os.environ.get(self.params["api_secret_env"], "")
            self._client = AlpacaPaperClient(
                key,
                secret,
                timeout_seconds=float(self.params["timeout_seconds"]),
            )
        return self._client

    def open_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _disabled()

    def modify_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _disabled()

    def close_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _disabled()

    def execute_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return _disabled()

    def get_open_orders(self) -> list[Dict[str, Any]]:
        return self._get_client().open_orders()

    def get_positions(self) -> list[Dict[str, Any]]:
        return self._get_client().positions()

    def get_account_summary(self) -> Dict[str, Any]:
        account = self._get_client().account()
        return {
            "success": True,
            "status": account.get("status"),
            "currency": account.get("currency"),
            "equity": float(account.get("equity", 0.0)),
            "cash": float(account.get("cash", 0.0)),
            "buying_power": float(account.get("buying_power", 0.0)),
            "account_blocked": bool(account.get("account_blocked")),
            "trading_blocked": bool(account.get("trading_blocked")),
        }

    def get_current_price(self, symbol: str) -> Dict[str, Any]:
        quote = self._get_client().latest_crypto_quotes([symbol]).get(symbol)
        if not quote:
            return {"success": False, "error": f"No Alpaca quote for {symbol}"}
        bid = float(quote["bp"])
        ask = float(quote["ap"])
        return {
            "success": True,
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "spread": ask - bid,
            "time": quote.get("t"),
        }
