"""
Tests for Web UI templates, heartbeat, and heuristic strategy.
"""
import pytest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient


# ---- Template rendering tests ----

class TestTemplateRendering:
    """Test that all HTML pages return 200."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from app.web import app
        self.client = TestClient(app)

    def test_login_page(self):
        r = self.client.get("/login")
        assert r.status_code == 200
        assert "Sign in" in r.text

    def test_register_page(self):
        r = self.client.get("/register")
        assert r.status_code == 200
        assert "Register" in r.text

    def test_dashboard_page(self):
        r = self.client.get("/dashboard")
        assert r.status_code == 200
        assert "Dashboard" in r.text

    def test_root_redirects_to_dashboard(self):
        r = self.client.get("/")
        assert r.status_code == 200

    def test_portfolios_page(self):
        r = self.client.get("/portfolios")
        assert r.status_code == 200
        assert "Portfolios" in r.text

    def test_trades_page(self):
        r = self.client.get("/trades")
        assert r.status_code == 200
        assert "Trade" in r.text

    def test_analytics_page(self):
        r = self.client.get("/analytics")
        assert r.status_code == 200
        assert "Analytics" in r.text

    def test_portfolio_detail_page(self):
        r = self.client.get("/portfolios/1")
        assert r.status_code == 200
        assert "Portfolio" in r.text

    def test_asset_detail_page(self):
        r = self.client.get("/assets/1")
        assert r.status_code == 200
        assert "Asset" in r.text

    def test_admin_users_page(self):
        r = self.client.get("/admin/users")
        assert r.status_code == 200
        assert "User" in r.text

    def test_admin_system_page(self):
        r = self.client.get("/admin/system")
        assert r.status_code == 200
        assert "System" in r.text


# ---- Heuristic Strategy tests ----

class TestHeuristicStrategy:
    """Test the heuristic strategy signal computation."""

    def test_hold_no_predictions(self):
        from plugins_strategy.heuristic_strategy import compute_signal
        result = compute_signal(current_price=1.10, daily_predictions=[], hourly_predictions=[])
        assert result["action"] == "hold"

    def test_buy_signal(self):
        from plugins_strategy.heuristic_strategy import compute_signal
        # Daily predictions show price going up significantly
        current = 1.10000
        daily_preds = [1.10100, 1.10200, 1.10300, 1.10400, 1.10500, 1.10600]
        result = compute_signal(current_price=current, daily_predictions=daily_preds)
        # With max pred 1.10600, profit_pips = (1.10600-1.10000)/0.00001 = 600 >> threshold 5
        assert result["action"] == "buy"
        assert result["tp"] > current
        assert result["sl"] < current
        assert result["volume"] > 0

    def test_sell_signal(self):
        from plugins_strategy.heuristic_strategy import compute_signal
        current = 1.10000
        daily_preds = [1.09900, 1.09800, 1.09700, 1.09600, 1.09500, 1.09400]
        result = compute_signal(current_price=current, daily_predictions=daily_preds)
        assert result["action"] == "sell"
        assert result["tp"] < current
        assert result["sl"] > current

    def test_early_close_long_variant_e(self):
        from plugins_strategy.heuristic_strategy import should_early_close
        # Weighted min below SL should trigger close
        result = should_early_close(
            direction='long',
            exit_variant='E',
            hourly_predictions=[1.090, 1.089, 1.088],
            daily_predictions=[1.091, 1.089, 1.087],
            sl=1.095,
            entry_price=1.100,
        )
        assert result is True

    def test_early_close_long_variant_g_never(self):
        from plugins_strategy.heuristic_strategy import should_early_close
        result = should_early_close(
            direction='long', exit_variant='G',
            hourly_predictions=[0.5], daily_predictions=[0.5],
            sl=1.0, entry_price=1.1,
        )
        assert result is False

    def test_early_close_short_variant_e(self):
        from plugins_strategy.heuristic_strategy import should_early_close
        result = should_early_close(
            direction='short', exit_variant='E',
            hourly_predictions=[1.11, 1.12, 1.13],
            daily_predictions=[1.11, 1.12, 1.14],
            sl=1.105, entry_price=1.100,
        )
        assert result is True

    def test_compute_size_scaling(self):
        from plugins_strategy.heuristic_strategy import _compute_size, DEFAULT_PARAMS
        # Low RR -> min volume
        size_low = _compute_size(0.3, DEFAULT_PARAMS, 10000)
        assert size_low >= DEFAULT_PARAMS['min_order_volume']
        # High RR -> higher volume
        size_high = _compute_size(3.0, DEFAULT_PARAMS, 10000)
        assert size_high >= size_low


# ---- Heartbeat tests ----

class TestHeartbeat:
    """Test heartbeat execution with mocked prediction client and broker."""

    @pytest.mark.asyncio
    async def test_heartbeat_cycle_no_portfolios(self):
        """Heartbeat should complete even with no active portfolios."""
        from app.heartbeat import run_heartbeat_cycle
        from app.database import Database

        db = Database(':memory:')
        await db.initialize()

        config = {"prediction_provider_url": "http://localhost:9999", "csv_test_mode": False}
        result = await run_heartbeat_cycle(config, db)
        assert result["portfolios_processed"] == 0
        assert isinstance(result["errors"], list)

    @pytest.mark.asyncio
    async def test_heartbeat_cycle_with_portfolio(self):
        """Heartbeat processes portfolio with mocked predictions."""
        from app.heartbeat import run_heartbeat_cycle
        from app.database import Database, User, Portfolio, Asset

        db = Database(':memory:')
        await db.initialize()

        # Create test data
        async with db.get_session() as session:
            user = User(username="test", email="t@t.com", password_hash="x", role="user", is_active=True)
            session.add(user)
            await session.flush()
            pf = Portfolio(user_id=user.id, name="Test PF", is_active=True, total_capital=10000)
            session.add(pf)
            await session.flush()
            asset = Asset(portfolio_id=pf.id, symbol="EURUSD", is_active=True, allocated_capital=5000)
            session.add(asset)

        config = {"csv_test_mode": False, "prediction_provider_url": "http://localhost:9999"}

        # Mock prediction client
        mock_predictions = {
            'status': 'success',
            'predictions': {
                'short_term': [1.10, 1.101, 1.102, 1.103, 1.104, 1.105],
                'long_term': [1.10, 1.105, 1.11, 1.115, 1.12, 1.125],
            },
            'uncertainties': {'short_term': [0.001]*6, 'long_term': [0.002]*6},
            'historical_context': {'data': [{'CLOSE': 1.10}], 'timestamps': [], 'count': 1}
        }

        with patch('app.heartbeat.PredictionProviderClient') as MockClient:
            instance = MockClient.return_value
            instance.get_predictions = AsyncMock(return_value=mock_predictions)
            result = await run_heartbeat_cycle(config, db)

        assert result["portfolios_processed"] == 1


# ---- Auth + API integration test ----

class TestAuthIntegration:
    """Test login, register, and authenticated API calls."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from app.web import app
        from app.database import create_tables
        create_tables()
        self.client = TestClient(app)

    def test_register_and_login(self):
        import uuid
        uid = uuid.uuid4().hex[:8]
        # Register
        r = self.client.post("/api/auth/register", json={
            "username": f"webui_{uid}",
            "email": f"webui_{uid}@example.com",
            "password": "TestPass1"
        })
        assert r.status_code == 200

        # Login
        r = self.client.post("/api/auth/login", json={
            "username": f"webui_{uid}",
            "password": "TestPass1"
        })
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data

        # Access protected endpoint
        token = data["access_token"]
        r = self.client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["username"] == f"webui_{uid}"

    def test_create_portfolio_authenticated(self):
        import uuid
        uid = uuid.uuid4().hex[:8]
        # Register + login
        self.client.post("/api/auth/register", json={
            "username": f"webpf_{uid}", "email": f"webpf_{uid}@example.com", "password": "TestPass2"
        })
        r = self.client.post("/api/auth/login", json={"username": f"webpf_{uid}", "password": "TestPass2"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Create portfolio
        r = self.client.post("/api/portfolios", json={"name": "My Portfolio", "total_capital": 5000}, headers=headers)
        assert r.status_code == 200
        assert r.json()["name"] == "My Portfolio"

        # List portfolios
        r = self.client.get("/api/portfolios", headers=headers)
        assert r.status_code == 200
        assert len(r.json()) >= 1
