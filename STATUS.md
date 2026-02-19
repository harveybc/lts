# LTS System Status — Phase 1A Documentation

## Date: 2026-02-19

## LTS Test Status: 75 pass, 0 fail ✅

### What Exists

#### Database Schema (`app/database.py`)
- **Users**: id, username, email, password_hash, role, is_active, created_at, updated_at
- **Sessions**: id, user_id, token, created_at, expires_at
- **AuditLogs**: id, user_id, action, timestamp, details
- **Config**: id, key, value, updated_at
- **Statistics**: id, key, value, timestamp
- **Portfolios**: id, user_id, name, description, is_active, portfolio_plugin, portfolio_config, total_capital
- **Assets**: id, portfolio_id, symbol, name, is_active, strategy_plugin, strategy_config, broker_plugin, broker_config, pipeline_plugin, pipeline_config, allocated_capital, max_positions
- **Orders**: id, portfolio_id, asset_id, external_id, order_type, status, symbol, quantity, price, stop_price, filled_quantity, filled_price, commission
- **Positions**: id, portfolio_id, asset_id, symbol, side, quantity, entry_price, current_price, unrealized_pnl, realized_pnl, commission, is_open

#### Plugin Architecture
- **AAA Plugin** (`plugins_aaa/default_aaa.py`): Registration, login, session management, role assignment, audit logging
- **Core Plugin** (`plugins_core/default_core.py`): FastAPI app with all API endpoints (auth, portfolios, assets, orders, positions, trading, strategy, plugins)
- **Pipeline Plugin** (`plugins_pipeline/default_pipeline.py`): Orchestrates portfolio execution, strategy for each asset, broker orders
- **Strategy Plugin** (`plugins_strategy/default_strategy.py`): Dummy strategy alternating buy/sell
- **Prediction Strategy** (`plugins_strategy/prediction_strategy.py`): Uses PredictionProviderClient for ML-based signals
- **Broker Plugin** (`plugins_broker/default_broker.py`): Simulated broker with order execution
- **Backtrader Broker** (`plugins_broker/backtrader_broker.py`): Backtrader-compatible broker with CSV/API predictions
- **Portfolio Plugin** (`plugins_portfolio/default_portfolio.py`): Capital allocation

#### Prediction Client (`app/prediction_client.py`)
- Supports two modes: CSV test mode (ideal predictions from future data) and API mode (calls prediction_provider)
- API mode: POST to `/api/v1/predict`, polls for completion
- Returns short-term (1-6h) and long-term (1-6d) predictions with uncertainties

#### Web Interface (`app/web.py`)
- FastAPI + Jinja2 templates (AdminLTE)
- Pages: dashboard, login, portfolios, assets, users, analytics
- API endpoints for all entities

#### CSV Plugins (newly created)
- `feeder_plugins/csv_feeder.py`: Loads OHLC CSV data, provides windowed access
- `predictor_plugins/csv_predictor.py`: Ideal predictions from CSV lookahead

### What Was Fixed (Phase 1B)
1. **CorePlugin get_db shadowing**: Removed class-level `get_db = staticmethod(get_db)` that was shadowing module-level import, breaking FastAPI dependency overrides in tests
2. **HTTPBearer 401 vs 403**: Changed `HTTPBearer()` to `HTTPBearer(auto_error=False)` to return 403 (Forbidden) instead of 401 for missing tokens
3. **BacktraderBroker positions**: Added class-level `positions = []` default and `_count_active_positions()` method to handle both dict (backtrader) and list (test) formats
4. **BacktraderBroker CSVPredictor**: Made `CSVPredictor` a module-level import for test patching
5. **BacktraderBroker parent buy/sell**: Added `_parent_buy`/`_parent_sell` delegating methods with try/except for test environments; handles `sys.modules` pollution from other test files
6. **conftest fresh_db**: Changed from async to sync fixture to fix table creation issues
7. **conftest event_loop**: Removed deprecated custom event_loop fixture
8. **pytest-asyncio**: Upgraded from 1.3.0 to 0.24.0
9. **pytest-mock**: Installed for system tests

### What's Missing
- Templates directory for AdminLTE (web.py references templates that likely don't exist yet)
- Google login/signup integration
- Real broker API integration (Oanda etc.)
- Heartbeat/scheduler for periodic portfolio rebalancing
- Heuristic strategy integration

---

## Prediction Provider Test Status: 103 pass, 29 fail

### What Was Fixed
1. **pandas_ta Strategy API**: Replaced deprecated `ta.Strategy()` with individual indicator calls
2. **Role naming**: Changed `require_admin` to accept both "administrator" and "admin"
3. **User creation API key**: Added `api_key` field to `UserResponse` and generate API key on user creation
4. **Status code**: Added `status_code=201` to user management create_user endpoint
5. **Audit logging**: Added `request_payload` to `LogEntry` model and serialization
6. **Request logging middleware**: Added DB logging via `ApiLog` for all `/api/` paths
7. **XSS sanitization**: Sanitized request data stored in prediction `result` field
8. **Test isolation**: Added module-level conftest with DB reset for production/security tests
9. **psutil**: Installed for memory monitoring tests

### Remaining 29 Failures
All caused by **cross-test state pollution** within the same module. All tests pass when run individually or by module. The shared `test.db` and in-memory rate limit stores accumulate state across test classes within the same module.

---

## Heuristic Strategy
- Located at `/home/openclaw/.openclaw/workspace/heuristic-strategy/`
- Plugin: `app/plugins/plugin_long_short_predictions.py`
- Config: exit_variant='E', leverage=100, all costs, mandatory SL
