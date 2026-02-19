# LTS Plugin Reference

This document provides the complete interface specification and usage guide for all LTS plugin types.

---

## Plugin Base Structure

All plugins inherit from `PluginBase` (defined in `app/plugin_base.py`) and **must** follow this exact structure:

```python
from app.plugin_base import PluginBase

class MyPlugin(PluginBase):
    plugin_params = {
        "param1": "default_value",
        "param2": 42,
    }
    plugin_debug_vars = ["param1", "param2"]

    def __init__(self, config=None):
        super().__init__(config)
        # Plugin-specific initialization

    def set_params(self, **kwargs):
        super().set_params(**kwargs)

    def get_debug_info(self):
        return super().get_debug_info()

    def add_debug_info(self, debug_info):
        super().add_debug_info(debug_info)
```

### Required Attributes

| Attribute | Type | Description |
|---|---|---|
| `plugin_params` | `dict` | Default parameter values. Keys become configurable parameters. |
| `plugin_debug_vars` | `list[str]` | Parameter names to include in debug info export. |

### Required Methods

| Method | Description |
|---|---|
| `__init__(self, config=None)` | Initialize with optional config dict. Must call `super().__init__(config)`. |
| `set_params(self, **kwargs)` | Update parameters. Must call `super().set_params(**kwargs)`. |
| `get_debug_info(self)` | Return dict of debug variable values. |
| `add_debug_info(self, debug_info)` | Merge debug info into provided dict. |

### Plugin Registration

Plugins are registered via entry points in `setup.py`:

```python
entry_points={
    'plugins_broker': [
        'my_broker=plugins_broker.my_broker:MyBrokerPlugin',
    ],
}
```

After adding an entry point, reinstall: `pip install -e .`

### Plugin Loading

Plugins are loaded dynamically by `app/plugin_loader.py`:

```python
from app.plugin_loader import load_plugin
plugin_class, required_params = load_plugin('plugins_broker', 'oanda_broker')
instance = plugin_class(config)
```

### Configuration

Each plugin receives configuration through:
1. `plugin_params` — built-in defaults (lowest priority)
2. File config — loaded via `--load_config`
3. CLI arguments — highest priority

The `config_merger.py` handles the merge: `plugin_params < DEFAULT_VALUES < file_config < CLI_args`.

---

## 1. AAA Plugins

**Base class**: `AAAPluginBase` (inherits `PluginBase`)

### Interface

```python
class AAAPluginBase(PluginBase):
    def register(self, username: str, email: str, password: str, role: str) -> bool: ...
    def login(self, username: str, password: str) -> dict: ...
    def assign_role(self, username: str, role: str) -> bool: ...
    def audit(self, user_id: int, action: str, details: str = None) -> None: ...
    def create_session(self, user_id: int) -> str: ...
```

### Available Plugin: `default_aaa`

**File**: `plugins_aaa/default_aaa.py`  
**Class**: `DefaultAAA`

**Parameters**:
| Parameter | Default | Description |
|---|---|---|
| `session_timeout_hours` | `24` | Session lifetime in hours |
| `password_min_length` | `8` | Minimum password length |
| `max_login_attempts` | `5` | Failed attempts before lockout |
| `lockout_duration_minutes` | `30` | Lockout duration |
| `audit_enabled` | `True` | Enable audit logging |
| `default_role` | `"user"` | Role for new registrations |
| `admin_role` | `"admin"` | Admin role name |
| `token_length` | `32` | Session token length |
| `jwt_secret` | env `LTS_JWT_SECRET` | JWT signing secret |
| `jwt_algorithm` | `"HS256"` | JWT algorithm |
| `jwt_access_expire_minutes` | `30` | Access token lifetime |
| `jwt_refresh_expire_days` | `7` | Refresh token lifetime |
| `password_require_uppercase` | `True` | Require uppercase in password |
| `password_require_digit` | `True` | Require digit in password |
| `google_client_id` | env `LTS_GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `google_client_secret` | env `LTS_GOOGLE_CLIENT_SECRET` | Google OAuth client secret |

**Key methods**:
- `register(username, email, password, role)` → `bool` — Creates user with bcrypt-hashed password
- `login(username, password, ip=None)` → `dict` — Returns `{success, access_token, refresh_token, session_token, user_id, role}`
- `google_oauth_login(id_token_data)` → `dict` — OAuth login, auto-creates user if needed
- `validate_jwt(token)` → `dict` — Validates JWT, returns `{valid, user_id, username, role}`
- `refresh_access_token(refresh_token)` → `dict` — Returns new access token
- `authorize_user(user_roles, required_role)` → `bool`
- `audit_action(user_id, action, details)` — Logs to `audit_logs` table

---

## 2. Core Plugins

**Base class**: `CorePluginBase` (inherits `PluginBase`)

### Interface

```python
class CorePluginBase(PluginBase):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def set_plugins(self, plugins: dict) -> None: ...
```

### Available Plugin: `default_core`

**File**: `plugins_core/default_core.py`  
**Class**: `CorePlugin`

Responsibilities:
- Creates FastAPI application with all API routes
- Manages plugin references
- Provides REST endpoints for portfolios, assets, orders, users, strategies, predictions
- Adds security middleware (headers, request size limits)
- Failure simulation endpoints for testing

**Key methods**:
- `initialize(plugins, config, database, get_db)` — Set up plugin references and database
- `create_app()` — Factory function returning configured FastAPI app

---

## 3. Pipeline Plugins

**Base class**: `PipelinePluginBase` (inherits `PluginBase`)

### Interface

```python
class PipelinePluginBase(PluginBase):
    def run(self, portfolio_id: int, assets: list) -> dict: ...
    def start(self, plugins: dict) -> None: ...
```

### Available Plugin: `default_pipeline`

**File**: `plugins_pipeline/default_pipeline.py`  
**Class**: `PipelinePlugin`

**Parameters**:
| Parameter | Default | Description |
|---|---|---|
| `global_latency` | `5` | Minutes between execution cycles |
| `max_concurrent_portfolios` | `10` | Max portfolios per cycle |
| `execution_timeout` | `300` | Seconds timeout per execution |
| `error_retry_count` | `3` | Retries on error |
| `error_retry_delay` | `60` | Seconds between retries |
| `statistics_enabled` | `True` | Record execution statistics |

**Execution flow**:
1. `start(plugins)` — Stores plugin references, starts Core Plugin
2. `run()` — Iterates active portfolios, checks latency, executes each
3. `_execute_portfolio(portfolio)` — Runs portfolio allocation, then each asset
4. `_execute_asset(asset, portfolio)` — Strategy decides → Broker executes → DB records

---

## 4. Strategy Plugins

**Base class**: `StrategyPluginBase` (inherits `PluginBase`)

### Interface

```python
class StrategyPluginBase(PluginBase):
    def decide(self, asset_data: dict, market_data: dict) -> dict: ...
```

### Available Plugin: `default_strategy`

**File**: `plugins_strategy/default_strategy.py`  
**Class**: `DefaultStrategy`

A dummy strategy that alternates buy/sell orders. Used for testing.

**Parameters**:
| Parameter | Default | Description |
|---|---|---|
| `position_size` | `0.02` | Position size (fraction of capital) |
| `max_risk_per_trade` | `0.02` | Max risk per trade |
| `prediction_threshold` | `0.6` | Signal confidence threshold |
| `stop_loss_pips` | `80` | Stop loss in pips |
| `take_profit_pips` | `100` | Take profit in pips |
| `order_type` | `"market"` | Order type |

**Returns**: `{"action": "open"|"close"|"none", "parameters": {...}}`

### Available Plugin: `prediction_strategy`

**File**: `plugins_strategy/prediction_strategy.py`  
**Class**: `PredictionBasedStrategy`

ML prediction-based strategy using `PredictionProviderClient`.

**Parameters**:
| Parameter | Default | Description |
|---|---|---|
| `short_term_weight` | `0.6` | Weight for short-term (1-6h) predictions |
| `long_term_weight` | `0.4` | Weight for long-term (1-6d) predictions |
| `confidence_threshold` | `0.7` | Minimum confidence to trade |
| `uncertainty_threshold` | `0.05` | Maximum allowed prediction uncertainty |
| `position_size_base` | `0.02` | Base position size |
| `trend_alignment_required` | `True` | Require short+long term trend agreement |

**Key methods**:
- `async generate_signal(symbol, current_price, historical_data, portfolio_context)` → `TradingSignal`
- `async backtest_signal(symbol, datetime_str, historical_data)` → `TradingSignal`
- `get_strategy_parameters()` → `dict`
- `update_parameters(new_params)` — Runtime parameter update

**TradingSignal dataclass**:
```python
@dataclass
class TradingSignal:
    action: str          # 'buy', 'sell', 'hold'
    confidence: float    # 0.0 to 1.0
    quantity: float
    reasoning: str
    timestamp: datetime
    short_term_predictions: List[float]
    long_term_predictions: List[float]
    uncertainties: Dict[str, List[float]]
```

---

## 5. Broker Plugins

**Base class**: `BrokerPluginBase` (inherits `PluginBase`)

### Interface

```python
class BrokerPluginBase(PluginBase):
    def open_order(self, order_params: dict) -> dict: ...
    def modify_order(self, order_id: str, new_params: dict) -> dict: ...
    def close_order(self, order_id: str) -> dict: ...
    def get_open_orders(self) -> list: ...
```

All broker plugins also implement a compatibility method:
```python
def execute_order(self, action: str, parameters: dict) -> dict:
    # action: "open" or "close"
    # Delegates to open_order/close_order
```

### Available Plugin: `default_broker`

**File**: `plugins_broker/default_broker.py`  
**Class**: `DefaultBroker`

Simulated broker for testing. 95% success rate on opens, 98% on closes.

**Parameters**:
| Parameter | Default | Description |
|---|---|---|
| `broker_api_url` | `"https://api.oanda.com/v3"` | (unused in simulation) |
| `api_key` | `"dummy_api_key"` | (unused) |
| `account_id` | `"dummy_account"` | Account identifier |
| `timeout` | `30` | Request timeout |
| `spread` | `0.0002` | Bid-ask spread |
| `execution_delay` | `0.1` | Simulated delay |

### Available Plugin: `backtrader_simulation_broker`

**File**: `plugins_broker/backtrader_simulation_broker.py`  
**Class**: `BacktraderSimulationBroker`

Standalone simulation with realistic forex costs. **Does not require backtrader cerebro**.

**Parameters**:
| Parameter | Default | Description |
|---|---|---|
| `initial_cash` | `10000.0` | Starting capital |
| `leverage` | `100` | Leverage ratio |
| `spread_pips` | `2.0` | Spread in pips |
| `commission_per_lot` | `7.0` | USD round-turn commission per lot |
| `slippage_pips` | `1.0` | Slippage in pips |
| `swap_per_lot_day` | `10.0` | Overnight swap USD/lot/day |
| `pip_value` | `0.0001` | Pip value (EUR/USD style) |
| `lot_size` | `100000` | Standard lot size |
| `instrument` | `"EUR_USD"` | Default instrument |
| `csv_file` | `""` | Path to OHLC CSV |
| `datetime_column` | `"DATE_TIME"` | CSV datetime column name |
| `datetime_format` | `"%Y-%m-%d %H:%M:%S"` | Datetime parse format |
| `open_col` / `high_col` / `low_col` / `close_col` | `"OPEN"` etc. | CSV column names |

**Key methods**:
- `load_csv(path)` — Load OHLC data
- `open_order(instrument, direction, volume, tp, sl, price, timestamp)` → `dict`
- `close_order(order_id, price, reason, timestamp)` → `dict`
- `tick(bar_index)` — Advance one bar, check TP/SL
- `run_simulation(strategy_fn)` — Full backtest loop, returns performance summary
- `get_account_summary()` → `{balance, equity, margin, unrealized_pnl}`
- `get_trade_history(count)` → list of closed trades

### Available Plugin: `backtrader_broker`

**File**: `plugins_broker/backtrader_broker.py`  
**Class**: `BacktraderBroker`

Extends `backtrader.brokers.BackBroker`. Requires backtrader cerebro environment.

**Parameters**:
| Parameter | Default | Description |
|---|---|---|
| `initial_cash` | `10000.0` | Starting capital |
| `commission` | `0.001` | Commission rate |
| `prediction_source` | `"csv"` | `"csv"` or `"api"` |
| `csv_file` | `"examples/data/phase_3/base_d6.csv"` | CSV for ideal predictions |
| `api_url` | `"http://localhost:8001"` | Prediction API URL |
| `prediction_horizons` | `["1h", "1d"]` | Prediction horizons |
| `symbol` | `"EURUSD"` | Trading symbol |
| `slippage` | `0.0001` | Slippage value |

**Key methods**:
- `get_predictions(timestamp, symbol)` → predictions from CSV or API
- `switch_prediction_source(new_source, config_updates)` — Runtime source switch
- `get_performance_metrics()` → full performance report
- `get_broker_info()` → broker state

### Available Plugin: `oanda_broker`

**File**: `plugins_broker/oanda_broker.py`  
**Class**: `OandaBroker`

Live/practice broker via OANDA v20 REST API (`oandapyV20`).

**Parameters**:
| Parameter | Default | Description |
|---|---|---|
| `account_id` | `""` | OANDA account ID |
| `access_token` | `""` | OANDA API token |
| `environment` | `"practice"` | `"practice"` or `"live"` |
| `instrument` | `"EUR_USD"` | Default instrument |
| `max_retries` | `3` | Request retry count |
| `retry_backoff` | `1.0` | Initial backoff seconds (doubles) |

**Key methods**:
- `open_order(instrument, direction, volume, tp, sl)` → `{success, order_id, trade_id, fill_price}`
- `close_order(order_id)` → `{success, response}`
- `modify_order(order_id, tp, sl)` → `{success, response}`
- `get_open_trades()` → list of open trades
- `get_account_summary()` → `{balance, equity, margin, unrealized_pnl, currency}`
- `get_trade_history(count)` → list of closed trades
- `get_current_price(instrument)` → `{bid, ask, spread, instrument, time}`

---

## 6. Portfolio Plugins

**Base class**: `PortfolioPluginBase` (inherits `PluginBase`)

### Interface

```python
class PortfolioPluginBase(PluginBase):
    def allocate(self, portfolio_id: int, assets: list) -> dict: ...
    def update(self, portfolio_id: int, operation_result: dict) -> None: ...
```

### Available Plugin: `default_portfolio`

**File**: `plugins_portfolio/default_portfolio.py`  
**Class**: `DefaultPortfolio`

Minimal implementation (stub). Methods `rebalance()` and `get_allocations()` return empty results.

**Parameters**: None defined (uses base defaults).

---

## Creating a Custom Plugin

### Step 1: Create the Plugin File

Create `plugins_broker/my_broker.py`:

```python
from app.plugin_base import BrokerPluginBase

class MyBroker(BrokerPluginBase):
    plugin_params = {
        "api_url": "https://my-broker.com/api",
        "api_key": "",
        "timeout": 30,
    }
    plugin_debug_vars = ["api_url", "timeout"]

    def __init__(self, config=None):
        super().__init__(config)

    def open_order(self, order_params):
        # Implement order execution
        return {"success": True, "order_id": "123"}

    def close_order(self, order_id):
        return {"success": True}

    def modify_order(self, order_id, new_params):
        return {"success": True}

    def get_open_orders(self):
        return []
```

### Step 2: Register in `setup.py`

```python
'plugins_broker': [
    'default_broker=plugins_broker.default_broker:DefaultBroker',
    'my_broker=plugins_broker.my_broker:MyBroker',
],
```

### Step 3: Reinstall and Configure

```bash
pip install -e .
```

Then use in config:
```json
{
    "broker_plugin": "my_broker"
}
```

Or per-asset in the `assets.broker_config` JSON column.

---

## Error Handling

All plugins must:
- Handle exceptions gracefully without crashing the main system
- Log errors (use `logging` or print with `_QUIET` check)
- Return structured error responses (e.g., `{"success": False, "error": "message"}`)
- Validate required configuration in `__init__`
