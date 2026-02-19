# LTS — Unit-Level Design Document

## 1. Core Modules

### `app/main.py` — Entry Point
- `setup_logging(level)` → configures logging
- `main()` → orchestrates: parse args → load config → merge → load plugins → start pipeline
- Quiet mode: filters print output when `LTS_QUIET=1`

### `app/cli.py` — CLI Argument Parser
- `parse_args()` → returns `(args, unknown_args)` via argparse
- Supports `--load_config`, `--save_config`, `--quiet_mode`, plus plugin-specific unknown args
- **Note**: Contains some legacy arguments from predictor project; LTS primarily uses `--load_config`

### `app/config.py` — Default Values
- `DEFAULT_VALUES` dict with ~80 parameters covering: system, core, AAA, pipeline, strategy, broker, prediction provider, models, portfolio, database, web, trading, risk, logging, performance, security

### `app/config_handler.py` — Config File Management
- `ConfigHandler` class: loads from file/remote, merges CLI, calculates SHA256 checksum
- `load_config(file_path)` → dict from JSON file
- `save_config(config, path)` → writes non-default values to JSON
- `remote_load_config(url)` / `remote_save_config(config, url, user, pass)`
- `remote_log(config, debug_info, url, user, pass)` → posts config+debug to API

### `app/config_merger.py` — Multi-Source Merge
- `process_unknown_args(unknown_args)` → dict from `['--key', 'value', ...]`
- `convert_type(value)` → tries int, then float, then str
- `merge_config(defaults, plugin_params1, plugin_params2, file_config, cli_args, unknown_args)` → merged dict
  - Order: plugin_params < defaults < file_config < CLI args

### `app/plugin_base.py` — Plugin Base Classes
- `PluginBase` — base with `plugin_params`, `plugin_debug_vars`, `set_params()`, `get_debug_info()`, `add_debug_info()`, `initialize()`, `configure()`, `shutdown()`
- `AAAPluginBase(PluginBase)` — adds `register()`, `login()`, `assign_role()`, `audit()`, `create_session()`
- `CorePluginBase(PluginBase)` — adds `start()`, `stop()`, `set_plugins()`
- `PipelinePluginBase(PluginBase)` — adds `run()`, `start()`
- `StrategyPluginBase(PluginBase)` — adds `decide()`
- `BrokerPluginBase(PluginBase)` — adds `open_order()`, `modify_order()`, `close_order()`, `get_open_orders()`
- `PortfolioPluginBase(PluginBase)` — adds `allocate()`, `update()`

### `app/plugin_loader.py` — Dynamic Plugin Loading
- `load_plugin(plugin_group, plugin_name)` → `(plugin_class, required_params)` via `importlib.metadata.entry_points`
- `get_plugin_params(group, name)` → list of param keys
- `get_available_plugins(group=None)` → dict of group → plugin names

### `app/database.py` — SQLAlchemy ORM Models
10 model classes: `User`, `Session`, `AuditLog`, `BillingRecord`, `Config`, `Statistics`, `Portfolio`, `Asset`, `Order`, `Position`
- `Database` class for async operations
- `create_tables()`, `init_db()`, `get_db()` utilities
- Sync: `SyncSessionLocal`, `get_db_session()`, `db_session()` context manager

### `app/prediction_client.py` — Prediction Provider Client
- `PredictionProviderClient(config)`
  - `get_predictions(symbol, datetime_str, prediction_types)` → async
  - `_get_csv_predictions()` → reads future CLOSE from DataFrame
  - `_get_api_predictions()` → POST to API, poll for completion
  - `_make_prediction_request()` → single API request with retries
  - `get_model_info()` → model configuration summary

### `app/init_db.py` — Database Initialization
- `main()` → calls `init_db()`, `create_default_admin()`, `create_default_config()`, `create_sample_portfolios()`
- Default admin: username=`admin`, password=`admin123` (SHA256 hash)
- Sample portfolio: "Sample Portfolio" with EUR/USD and GBP/USD assets

### `app/web.py` — FastAPI Application
- JWT utilities: `hash_password()`, `verify_password()`, `create_access_token()`, `decode_token()`
- Dependencies: `get_current_user()`, `require_role(*roles)`, `check_rate_limit()`
- Middleware: security headers, request size limiting
- Pydantic models: `LoginRequest`, `RegisterRequest`, `TokenResponse`, `PortfolioCreate`, `UserResponse`
- ~20 API endpoints across auth, users, portfolios, orders, positions, statistics, billing

## 2. Plugin Modules

### `plugins_aaa/default_aaa.py` — DefaultAAA
- 16 plugin_params (session timeout, password rules, JWT config, Google OAuth)
- Password: bcrypt hash/verify, complexity validation
- JWT: create/decode/validate access and refresh tokens
- Lockout: in-memory tracking of failed attempts per username
- Google OAuth: `google_oauth_login(id_token_data)` — creates user if needed
- Methods: `register()`, `login()`, `assign_role()`, `audit()`, `create_session()`, `validate_session()`, `validate_jwt()`, `refresh_access_token()`

### `plugins_core/default_core.py` — CorePlugin
- `__init__()` → creates APIRouter, registers ~40 routes
- `initialize(plugins, config, database, get_db)` → stores references
- Routes cover: auth, portfolios, assets (CRUD + plugin config), orders, positions, audit, debug, strategy/prediction endpoints
- `create_app()` factory function

### `plugins_pipeline/default_pipeline.py` — PipelinePlugin
- 8 plugin_params (latency, concurrency, timeout, retry)
- `start(plugins)` → stores plugins, starts Core
- `run()` → iterates active portfolios → `_execute_portfolio()` → `_execute_asset()`
- `_should_execute_portfolio()` → checks latency elapsed
- `_record_statistics()` → writes to Statistics table

### `plugins_strategy/default_strategy.py` — DefaultStrategy
- 6 plugin_params (position_size, risk, threshold, SL/TP pips)
- `generate_signal(asset, market_data, predictions)` → checks open positions, alternates buy/sell

### `plugins_strategy/prediction_strategy.py` — PredictionBasedStrategy
- 6 plugin_params (weights, thresholds, position size, trend alignment)
- `generate_signal()` → async, calls PredictionProviderClient
- `_analyze_predictions()` → weighted trend, confidence, risk management
- `backtest_signal()` → for historical evaluation
- Returns `TradingSignal` dataclass

### `plugins_broker/default_broker.py` — DefaultBroker
- 6 plugin_params (API URL, key, account, timeout, spread, delay)
- `execute_order(action, params)` → dispatches to `_execute_open_order` / `_execute_close_order`
- Simulates with randomized success rates

### `plugins_broker/backtrader_simulation_broker.py` — BacktraderSimulationBroker
- 16 plugin_params (cash, leverage, costs, CSV columns)
- `load_csv(path)` → reads OHLC data
- `open_order()` → applies spread + slippage + commission
- `close_order()` → calculates P&L with swap costs
- `tick(bar_index)` → advances simulation, checks TP/SL
- `run_simulation(strategy_fn)` → full backtest loop
- `_summary()` → performance report

### `plugins_broker/backtrader_broker.py` — BacktraderBroker
- Extends `backtrader.brokers.BackBroker`
- 12 plugin_params (cash, commission, prediction source, CSV/API config)
- Overrides `buy()` / `sell()` to log prediction usage
- `get_predictions(timestamp)` → CSV or API
- `switch_prediction_source(new_source)` → runtime switch
- `get_performance_metrics()` → returns, trades, predictions

### `plugins_broker/oanda_broker.py` — OandaBroker
- 6 plugin_params (account, token, environment, instrument, retries, backoff)
- `_get_client()` → lazy oandapyV20 API client
- `_request(endpoint)` → retry with exponential backoff
- `open_order()`, `close_order()`, `modify_order()` → via oandapyV20 endpoints
- `get_open_trades()`, `get_account_summary()`, `get_trade_history()`, `get_current_price()`

### `plugins_portfolio/default_portfolio.py` — DefaultPortfolio
- No params. Stub implementation: `rebalance()` and `get_allocations()` are no-ops.

---

*Status: Reflects as-built system (2025-02)*
