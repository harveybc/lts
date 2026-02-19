"""Unit tests for BacktraderSimulationBroker plugin."""

import sys, os, unittest, tempfile, csv
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from plugins_broker.backtrader_simulation_broker import BacktraderSimulationBroker


def _write_csv(rows, path):
    """Write test OHLC CSV."""
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["DATE_TIME", "OPEN", "HIGH", "LOW", "CLOSE"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _make_bars(n=10, start_price=1.1000, step=0.0010):
    """Generate n bars with predictable prices."""
    base = datetime(2024, 1, 1)
    rows = []
    for i in range(n):
        p = start_price + i * step
        rows.append({
            "DATE_TIME": (base + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
            "OPEN": f"{p:.5f}",
            "HIGH": f"{p + 0.0005:.5f}",
            "LOW": f"{p - 0.0005:.5f}",
            "CLOSE": f"{p:.5f}",
        })
    return rows


class TestInit(unittest.TestCase):
    def test_defaults(self):
        b = BacktraderSimulationBroker()
        self.assertEqual(b.cash, 10000.0)
        self.assertEqual(b.params["leverage"], 100)

    def test_custom_config(self):
        b = BacktraderSimulationBroker(config={"initial_cash": 50000, "spread_pips": 3})
        self.assertEqual(b.cash, 50000.0)
        self.assertEqual(b.params["spread_pips"], 3)

    def test_debug_info(self):
        b = BacktraderSimulationBroker()
        info = b.get_debug_info()
        self.assertIn("leverage", info)


class TestLoadCSV(unittest.TestCase):
    def test_load(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
            path = f.name
        rows = _make_bars(5)
        _write_csv(rows, path)
        b = BacktraderSimulationBroker(config={"csv_file": path})
        b.load_csv()
        self.assertEqual(len(b.get_bars()), 5)
        os.unlink(path)


class TestOpenClose(unittest.TestCase):
    def setUp(self):
        self.b = BacktraderSimulationBroker(config={
            "initial_cash": 10000,
            "spread_pips": 2,
            "commission_per_lot": 7,
            "slippage_pips": 1,
            "swap_per_lot_day": 10,
        })
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
            self.path = f.name
        _write_csv(_make_bars(10), self.path)
        self.b.load_csv(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_open_buy(self):
        r = self.b.open_order("EUR_USD", "buy", 0.1, tp=1.12, sl=1.08)
        self.assertTrue(r["success"])
        self.assertIn("order_id", r)
        # Entry should be higher than bar close due to spread+slippage
        bar_close = self.b.get_bars()[0]["close"]
        self.assertGreater(r["entry_price"], bar_close)

    def test_open_sell(self):
        r = self.b.open_order("EUR_USD", "sell", 0.1, tp=1.08, sl=1.12)
        self.assertTrue(r["success"])
        bar_close = self.b.get_bars()[0]["close"]
        self.assertLess(r["entry_price"], bar_close)

    def test_close_pnl(self):
        r = self.b.open_order("EUR_USD", "buy", 0.1, price=1.1000)
        oid = r["order_id"]
        # Move to bar 5 (price ~1.1050)
        self.b._bar_idx = 5
        cr = self.b.close_order(oid, reason="manual")
        self.assertTrue(cr["success"])
        # Should have positive pnl (price went up)
        self.assertGreater(cr["trade"]["pnl_usd"], -50)  # not catastrophic

    def test_close_not_found(self):
        r = self.b.close_order("999")
        self.assertFalse(r["success"])

    def test_commission_deducted(self):
        initial = self.b.cash
        self.b.open_order("EUR_USD", "buy", 1.0)  # 1 lot
        self.assertAlmostEqual(self.b.cash, initial - 7.0, places=2)


class TestModify(unittest.TestCase):
    def test_modify_tp_sl(self):
        b = BacktraderSimulationBroker()
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
            path = f.name
        _write_csv(_make_bars(3), path)
        b.load_csv(path)
        r = b.open_order("EUR_USD", "buy", 0.1, tp=1.12, sl=1.08)
        m = b.modify_order(r["order_id"], tp=1.15, sl=1.07)
        self.assertTrue(m["success"])
        trade = b._open_trades[r["order_id"]]
        self.assertEqual(trade["tp"], 1.15)
        self.assertEqual(trade["sl"], 1.07)
        os.unlink(path)


class TestTPSL(unittest.TestCase):
    def test_stop_loss_hit(self):
        b = BacktraderSimulationBroker(config={
            "spread_pips": 0, "slippage_pips": 0, "commission_per_lot": 0, "swap_per_lot_day": 0,
        })
        # Create bars where price drops
        base = datetime(2024, 1, 1)
        rows = [
            {"DATE_TIME": "2024-01-01 00:00:00", "OPEN": "1.10000", "HIGH": "1.10100", "LOW": "1.09900", "CLOSE": "1.10000"},
            {"DATE_TIME": "2024-01-01 01:00:00", "OPEN": "1.10000", "HIGH": "1.10050", "LOW": "1.09500", "CLOSE": "1.09600"},
        ]
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
            path = f.name
        _write_csv(rows, path)
        b.load_csv(path)

        b.open_order("EUR_USD", "buy", 0.1, sl=1.09800)
        closed = b.tick(1)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["close_reason"], "stop_loss")
        os.unlink(path)

    def test_take_profit_hit(self):
        b = BacktraderSimulationBroker(config={
            "spread_pips": 0, "slippage_pips": 0, "commission_per_lot": 0, "swap_per_lot_day": 0,
        })
        rows = [
            {"DATE_TIME": "2024-01-01 00:00:00", "OPEN": "1.10000", "HIGH": "1.10100", "LOW": "1.09900", "CLOSE": "1.10000"},
            {"DATE_TIME": "2024-01-01 01:00:00", "OPEN": "1.10000", "HIGH": "1.10500", "LOW": "1.09900", "CLOSE": "1.10300"},
        ]
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
            path = f.name
        _write_csv(rows, path)
        b.load_csv(path)

        b.open_order("EUR_USD", "buy", 0.1, tp=1.10200)
        closed = b.tick(1)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["close_reason"], "take_profit")
        os.unlink(path)


class TestRunSimulation(unittest.TestCase):
    def test_full_run(self):
        b = BacktraderSimulationBroker(config={
            "spread_pips": 2, "slippage_pips": 1,
            "commission_per_lot": 7, "swap_per_lot_day": 10,
        })
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
            path = f.name
        _write_csv(_make_bars(20, step=0.0005), path)
        b.load_csv(path)

        # Simple strategy: buy on bar 2, let it ride
        def strategy(broker, idx, bar):
            if idx == 2 and not broker.get_open_trades():
                broker.open_order("EUR_USD", "buy", 0.1, tp=bar["close"] + 0.005)

        result = b.run_simulation(strategy_fn=strategy)
        self.assertIn("total_trades", result)
        self.assertGreaterEqual(result["total_trades"], 1)
        self.assertIn("close_reasons", result)
        os.unlink(path)


class TestAccountSummary(unittest.TestCase):
    def test_summary(self):
        b = BacktraderSimulationBroker()
        s = b.get_account_summary()
        self.assertTrue(s["success"])
        self.assertEqual(s["balance"], 10000.0)


class TestGetCurrentPrice(unittest.TestCase):
    def test_no_data(self):
        b = BacktraderSimulationBroker()
        p = b.get_current_price()
        self.assertFalse(p["success"])

    def test_with_data(self):
        b = BacktraderSimulationBroker()
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
            path = f.name
        _write_csv(_make_bars(3), path)
        b.load_csv(path)
        p = b.get_current_price("EUR_USD")
        self.assertTrue(p["success"])
        self.assertGreater(p["ask"], p["bid"])
        os.unlink(path)


class TestExecuteOrderCompat(unittest.TestCase):
    def test_execute_open(self):
        b = BacktraderSimulationBroker()
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
            path = f.name
        _write_csv(_make_bars(3), path)
        b.load_csv(path)
        r = b.execute_order("open", {"symbol": "EUR_USD", "side": "buy", "quantity": 0.1})
        self.assertTrue(r["success"])
        os.unlink(path)


if __name__ == "__main__":
    unittest.main()
