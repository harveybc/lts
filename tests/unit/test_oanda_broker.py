"""Unit tests for OandaBroker plugin — all OANDA API calls are mocked."""

import sys, os, unittest
from unittest.mock import patch, MagicMock

# Ensure lts root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from plugins_broker.oanda_broker import OandaBroker


def _broker(**kw):
    cfg = {"account_id": "101-001-1234", "access_token": "tok", "environment": "practice"}
    cfg.update(kw)
    return OandaBroker(config=cfg)


class TestOandaBrokerInit(unittest.TestCase):
    def test_defaults(self):
        b = _broker()
        self.assertEqual(b.params["environment"], "practice")
        self.assertEqual(b.params["account_id"], "101-001-1234")

    def test_set_params(self):
        b = _broker()
        b.set_params(instrument="GBP_USD")
        self.assertEqual(b.params["instrument"], "GBP_USD")

    def test_debug_info(self):
        b = _broker()
        info = b.get_debug_info()
        self.assertIn("account_id", info)


class TestOpenOrder(unittest.TestCase):
    @patch("plugins_broker.oanda_broker._orders_mod")
    @patch("plugins_broker.oanda_broker._oandapyV20")
    def test_open_order_buy(self, mock_api_mod, mock_orders):
        b = _broker()
        # Mock client
        mock_client = MagicMock()
        mock_api_mod.API.return_value = mock_client
        ep_instance = MagicMock()
        ep_instance.response = {
            "orderFillTransaction": {"orderID": "42", "price": "1.10500",
                                     "tradeOpened": {"tradeID": "99"}},
        }
        mock_orders.OrderCreate.return_value = ep_instance
        # Force imports flag
        import plugins_broker.oanda_broker as mod
        mod._oanda_imported = True

        result = b.open_order("EUR_USD", "buy", 10000, tp=1.11, sl=1.09)
        self.assertTrue(result["success"])
        self.assertEqual(result["order_id"], "42")
        self.assertEqual(result["trade_id"], "99")

    @patch("plugins_broker.oanda_broker._orders_mod")
    @patch("plugins_broker.oanda_broker._oandapyV20")
    def test_open_order_failure(self, mock_api_mod, mock_orders):
        b = _broker()
        mock_client = MagicMock()
        mock_api_mod.API.return_value = mock_client
        mock_client.request.side_effect = Exception("timeout")
        ep_instance = MagicMock()
        mock_orders.OrderCreate.return_value = ep_instance
        import plugins_broker.oanda_broker as mod
        mod._oanda_imported = True
        b.params["max_retries"] = 1

        result = b.open_order("EUR_USD", "buy", 10000)
        self.assertFalse(result["success"])


class TestCloseOrder(unittest.TestCase):
    @patch("plugins_broker.oanda_broker._trades_mod")
    @patch("plugins_broker.oanda_broker._oandapyV20")
    def test_close(self, mock_api_mod, mock_trades):
        b = _broker()
        mock_client = MagicMock()
        mock_api_mod.API.return_value = mock_client
        ep = MagicMock()
        ep.response = {"orderFillTransaction": {"id": "100"}}
        mock_trades.TradeClose.return_value = ep
        import plugins_broker.oanda_broker as mod
        mod._oanda_imported = True

        result = b.close_order("99")
        self.assertTrue(result["success"])


class TestModifyOrder(unittest.TestCase):
    @patch("plugins_broker.oanda_broker._trades_mod")
    @patch("plugins_broker.oanda_broker._oandapyV20")
    def test_modify(self, mock_api_mod, mock_trades):
        b = _broker()
        mock_client = MagicMock()
        mock_api_mod.API.return_value = mock_client
        ep = MagicMock()
        ep.response = {}
        mock_trades.TradeCRCDO.return_value = ep
        import plugins_broker.oanda_broker as mod
        mod._oanda_imported = True

        result = b.modify_order("99", tp=1.12, sl=1.08)
        self.assertTrue(result["success"])

    def test_modify_no_params(self):
        b = _broker()
        import plugins_broker.oanda_broker as mod
        mod._oanda_imported = True
        result = b.modify_order("99")
        self.assertFalse(result["success"])


class TestGetOpenTrades(unittest.TestCase):
    @patch("plugins_broker.oanda_broker._trades_mod")
    @patch("plugins_broker.oanda_broker._oandapyV20")
    def test_open_trades(self, mock_api_mod, mock_trades):
        b = _broker()
        mock_client = MagicMock()
        mock_api_mod.API.return_value = mock_client
        ep = MagicMock()
        ep.response = {"trades": [
            {"id": "1", "instrument": "EUR_USD", "currentUnits": "10000",
             "price": "1.1050", "unrealizedPL": "25.00", "openTime": "2024-01-01T00:00:00Z"},
        ]}
        mock_trades.OpenTrades.return_value = ep
        import plugins_broker.oanda_broker as mod
        mod._oanda_imported = True

        trades = b.get_open_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["direction"], "buy")


class TestAccountSummary(unittest.TestCase):
    @patch("plugins_broker.oanda_broker._accounts_mod")
    @patch("plugins_broker.oanda_broker._oandapyV20")
    def test_summary(self, mock_api_mod, mock_accts):
        b = _broker()
        mock_client = MagicMock()
        mock_api_mod.API.return_value = mock_client
        ep = MagicMock()
        ep.response = {"account": {
            "balance": "10000.00", "NAV": "10025.00",
            "marginUsed": "500.00", "unrealizedPL": "25.00", "currency": "USD",
        }}
        mock_accts.AccountSummary.return_value = ep
        import plugins_broker.oanda_broker as mod
        mod._oanda_imported = True

        s = b.get_account_summary()
        self.assertTrue(s["success"])
        self.assertEqual(s["balance"], 10000.0)
        self.assertEqual(s["equity"], 10025.0)


class TestGetTradeHistory(unittest.TestCase):
    @patch("plugins_broker.oanda_broker._trades_mod")
    @patch("plugins_broker.oanda_broker._oandapyV20")
    def test_history(self, mock_api_mod, mock_trades):
        b = _broker()
        mock_client = MagicMock()
        mock_api_mod.API.return_value = mock_client
        ep = MagicMock()
        ep.response = {"trades": [
            {"id": "5", "instrument": "EUR_USD", "initialUnits": "-10000",
             "price": "1.1100", "averageClosePrice": "1.1050",
             "realizedPL": "50.00", "openTime": "t1", "closeTime": "t2",
             "closingTransactionIDs": ["10"]},
        ]}
        mock_trades.TradesList.return_value = ep
        import plugins_broker.oanda_broker as mod
        mod._oanda_imported = True

        history = b.get_trade_history(count=10)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["direction"], "sell")
        self.assertEqual(history[0]["pnl"], 50.0)


class TestGetCurrentPrice(unittest.TestCase):
    @patch("plugins_broker.oanda_broker._pricing_mod")
    @patch("plugins_broker.oanda_broker._oandapyV20")
    def test_price(self, mock_api_mod, mock_pricing):
        b = _broker()
        mock_client = MagicMock()
        mock_api_mod.API.return_value = mock_client
        ep = MagicMock()
        ep.response = {"prices": [{
            "bids": [{"price": "1.10000"}],
            "asks": [{"price": "1.10020"}],
            "time": "2024-01-01T00:00:00Z",
        }]}
        mock_pricing.PricingInfo.return_value = ep
        import plugins_broker.oanda_broker as mod
        mod._oanda_imported = True

        p = b.get_current_price("EUR_USD")
        self.assertTrue(p["success"])
        self.assertAlmostEqual(p["bid"], 1.10000)
        self.assertAlmostEqual(p["ask"], 1.10020)
        self.assertAlmostEqual(p["spread"], 0.00020, places=5)


class TestExecuteOrderCompat(unittest.TestCase):
    @patch("plugins_broker.oanda_broker._orders_mod")
    @patch("plugins_broker.oanda_broker._oandapyV20")
    def test_execute_open(self, mock_api_mod, mock_orders):
        b = _broker()
        mock_client = MagicMock()
        mock_api_mod.API.return_value = mock_client
        ep = MagicMock()
        ep.response = {"orderFillTransaction": {"orderID": "1", "price": "1.1",
                                                 "tradeOpened": {"tradeID": "2"}}}
        mock_orders.OrderCreate.return_value = ep
        import plugins_broker.oanda_broker as mod
        mod._oanda_imported = True

        result = b.execute_order("open", {"symbol": "EUR_USD", "side": "buy", "quantity": 1000})
        self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
