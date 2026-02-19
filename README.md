# LTS — Live Trading System

## Overview

LTS is a **multi-user, multi-portfolio live trading system** built in Python. It provides a modular, plugin-based architecture for automated trading with support for both backtesting (simulation) and live execution through broker APIs.

**Key capabilities:**

- **Multi-user, multi-portfolio** — each user owns multiple portfolios, each portfolio holds multiple assets with independent strategy/broker/pipeline configurations.
- **Plugin architecture** — six plugin types (AAA, Core, Pipeline, Strategy, Broker, Portfolio) loaded dynamically via Python entry points.
- **Broker plugins** — simulated backtesting via `BacktraderSimulationBroker` with realistic costs, and live trading via `OandaBroker` (OANDA v20 REST API).
- **Prediction-based strategy** — `PredictionBasedStrategy` integrates with an external `prediction_provider` service for ML-based short-term (1–6h transformer) and long-term (1–6d CNN) predictions.
- **FastAPI + AdminLTE web interface** — secure REST API with JWT authentication, RBAC (admin/user), rate limiting, and CORS.
- **Google OAuth 2.0** — users can authenticate via Google in addition to username/password.
- **SQLite via SQLAlchemy ORM** — full schema for users, sessions, audit logs, billing, portfolios, assets, orders, and positions.
- **Heartbeat/core loop** — the pipeline plugin executes trading logic on a configurable `global_latency` interval, processing all active portfolios and assets each cycle.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     FastAPI + AdminLTE UI                     │
│         (JWT auth, RBAC, rate limiting, CORS, security)       │
└────────────────────────┬────────────────────────────────────┘
                         │ REST API
┌────────────────────────┴────────────────────────────────────┐
│                      Core Plugin                              │
│     Registers API routes, manages lifecycle, coordinates      │
│     all plugins via Pipeline Plugin                           │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────┴────────────────────────────────────┐
│                    Pipeline Plugin                             │
│     Main trading loop (every global_latency minutes):         │
│     For each active user → portfolio → asset:                 │
│       1. Portfolio Plugin → allocate capital                   │
│       2. Strategy Plugin → decide (buy/sell/hold)             │
│       3. Broker Plugin → execute order                        │
│       4. Record order/position in database                    │
└─────────────┬───────────────┬──────────────┬────────────────┘
              │               │              │
     ┌────────┴──┐    ┌──────┴─────┐   ┌───┴──────────┐
     │ Strategy   │    │   Broker    │   │  Portfolio    │
     │ Plugins    │    │   Plugins   │   │  Plugins      │
     └────────────┘    └────────────┘   └──────────────┘
              │
     ┌────────┴──────────────┐
     │  Prediction Provider   │
     │  (external service)    │
     │  Short-term: 1h Transf │
     │  Long-term: 1d CNN     │
     └───────────────────────┘
```

### Plugin Types

| Plugin Type | Directory | Available Plugins | Purpose |
|---|---|---|---|
| **AAA** | `plugins_aaa/` | `default_aaa` | Authentication (bcrypt + JWT), authorization (admin/user RBAC), audit logging, Google OAuth |
| **Core** | `plugins_core/` | `default_core` | FastAPI server, API route registration, plugin coordination |
| **Pipeline** | `plugins_pipeline/` | `default_pipeline` | Main trading loop, portfolio/asset execution orchestration |
| **Strategy** | `plugins_strategy/` | `default_strategy`, `prediction_strategy` | Trading decision logic; prediction-based uses `prediction_provider` |
| **Broker** | `plugins_broker/` | `default_broker`, `backtrader_broker`, `backtrader_simulation_broker`, `oanda_broker` | Order execution — simulation or live |
| **Portfolio** | `plugins_portfolio/` | `default_portfolio` | Capital allocation across assets |

---

## Database Schema

All persistent data is stored in **SQLite** via **SQLAlchemy ORM**. The schema is defined in `app/database.py`.

### Tables

| Table | Description | Key Columns |
|---|---|---|
| **users** | User accounts with AAA | `id`, `username`, `email`, `password_hash`, `role` (admin/user), `is_active`, `failed_login_attempts`, `locked_until` |
| **sessions** | User sessions | `id`, `user_id` (FK), `token`, `created_at`, `expires_at` |
| **audit_logs** | Audit trail for all actions | `id`, `user_id` (FK), `action`, `timestamp`, `details`, `ip_address` |
| **billing_records** | Per-trade/subscription billing | `id`, `user_id` (FK), `action_type`, `reference_id`, `amount`, `currency`, `description` |
| **config** | System key-value configuration | `id`, `key` (unique), `value` (text/JSON), `updated_at` |
| **statistics** | System metrics and analytics | `id`, `key`, `value` (float), `timestamp` |
| **portfolios** | User portfolios | `id`, `user_id` (FK), `name`, `is_active`, `portfolio_plugin`, `portfolio_config` (JSON), `total_capital` |
| **assets** | Instruments within portfolios | `id`, `portfolio_id` (FK), `symbol`, `strategy_plugin`, `strategy_config` (JSON), `broker_plugin`, `broker_config` (JSON), `pipeline_plugin`, `pipeline_config` (JSON), `allocated_capital`, `max_positions` |
| **orders** | Trading orders | `id`, `portfolio_id` (FK), `asset_id` (FK), `user_id` (FK), `external_id`, `order_type`, `status`, `symbol`, `quantity`, `price`, `stop_price`, `filled_quantity`, `filled_price`, `commission` |
| **positions** | Open/closed positions | `id`, `portfolio_id` (FK), `asset_id` (FK), `symbol`, `side` (long/short), `quantity`, `entry_price`, `current_price`, `unrealized_pnl`, `realized_pnl`, `is_open` |

### Relationships

- `User` → has many `Session`, `AuditLog`, `Portfolio`, `Order`, `BillingRecord`
- `Portfolio` → has many `Asset`, `Order`, `Position`; belongs to `User`
- `Asset` → has many `Order`, `Position`; belongs to `Portfolio`

---

## AAA System (Authentication, Authorization, Accounting)

The system uses a **2-role model**: `admin` and `user`.

### Authentication
- **Username/password**: bcrypt-hashed passwords, JWT access tokens (30 min) + refresh tokens (7 days)
- **Google OAuth 2.0**: Users can authenticate via Google; accounts are auto-created on first OAuth login
- **Account lockout**: After 5 failed attempts, account is locked for 30 minutes
- **Password complexity**: minimum 8 characters, requires uppercase letter and digit

### Authorization
- **Admin**: full access to all users, portfolios, audit logs, billing, system config
- **User**: access to own portfolios, assets, orders, positions, billing

### Accounting
- All user actions are logged in the `audit_logs` table
- Billing records tracked per trade/subscription in `billing_records`

---

## Broker Plugins

### `default_broker` — Simulated Demo Broker
A dummy broker that simulates order execution with configurable spread and randomized success rates (95% fills). Used for testing.

### `backtrader_simulation_broker` — Realistic Forex Simulation
Standalone simulation broker (no running backtrader cerebro required). Features:
- **Realistic cost modeling**: spread (2 pips default), commission ($7/lot round-turn), slippage (1 pip), overnight swap ($10/lot/day)
- **CSV data loading**: reads OHLC CSV files for backtesting
- **TP/SL checking**: automatic take-profit and stop-loss execution per bar
- **Full simulation loop**: `run_simulation(strategy_fn)` iterates all bars

**Key parameters**: `initial_cash=10000`, `leverage=100`, `spread_pips=2`, `commission_per_lot=7`, `slippage_pips=1`, `swap_per_lot_day=10`, `pip_value=0.0001`, `lot_size=100000`

### `backtrader_broker` — Backtrader Framework Integration
Extends `backtrader.brokers.BackBroker` for use within a backtrader cerebro. Supports:
- CSV predictions (ideal/perfect) via `CSVPredictor` from `prediction_provider`
- API predictions (real ML) via HTTP POST to `prediction_provider`
- Runtime switching between prediction sources

### `oanda_broker` — OANDA v20 Live Trading
Production live/practice broker using `oandapyV20`:
- Market orders with TP/SL
- Trade management (close, modify TP/SL)
- Account summary (balance, equity, margin)
- Pricing (bid/ask/spread)
- Trade history
- Retry with exponential backoff (3 retries default)

**Key parameters**: `account_id`, `access_token`, `environment` (practice/live), `instrument`, `max_retries=3`

---

## Strategy Plugins

### `default_strategy` — Dummy Alternating Strategy
Alternates between buy/sell orders for testing. Not for production use.

### `prediction_strategy` — ML Prediction-Based Strategy
Uses the `PredictionProviderClient` to get predictions from an external `prediction_provider` service:

- **Short-term model**: 1h Transformer (predictions for 1–6 hours ahead)
- **Long-term model**: 1d CNN (predictions for 1–6 days ahead)

Decision logic:
1. Calculate weighted trend signal: `short_term_weight * short_trend + long_term_weight * long_trend`
2. Compute confidence from trend strength and prediction uncertainty
3. Require trend alignment (short-term and long-term agree on direction)
4. Apply confidence threshold (default 0.7) and uncertainty threshold (default 0.05)
5. Size position proportional to confidence, respecting max position limits

**Key parameters**: `short_term_weight=0.6`, `long_term_weight=0.4`, `confidence_threshold=0.7`, `uncertainty_threshold=0.05`, `position_size_base=0.02`, `trend_alignment_required=True`

---

## Prediction Provider Integration

LTS integrates with an external `prediction_provider` service via `app/prediction_client.py`:

- **API mode**: Makes HTTP requests to `prediction_provider` at `prediction_provider_url` (default `http://localhost:8000`). Posts prediction requests, polls for completion, extracts predictions and uncertainties.
- **CSV test mode**: When `csv_test_mode=True`, reads future prices from a CSV file to generate perfect predictions (for backtesting evaluation).

Configuration in `app/config.py`:
```python
"prediction_provider_url": "http://localhost:8000",
"prediction_provider_timeout": 300,
"prediction_provider_retries": 3,
"short_term_model": {"predictor_plugin": "transformer", "window_size": 144, "interval": "1h", ...},
"long_term_model": {"predictor_plugin": "cnn", "window_size": 256, "interval": "1d", ...},
```

---

## Core Loop / Heartbeat

The **Pipeline Plugin** drives the main trading loop:

1. **Start**: `main.py` loads all plugins, calls `pipeline_plugin.start()` which starts the Core Plugin (FastAPI server)
2. **Execution cycle** (every `global_latency` minutes, default 5):
   - Query all active portfolios across all users
   - For each portfolio, check if `portfolio_latency` has elapsed since last execution
   - Run portfolio plugin to allocate capital
   - For each active asset in the portfolio:
     - Strategy plugin makes trading decision
     - Broker plugin executes order if action is buy/sell
     - Order and position records stored in database
   - Record execution statistics
3. **Web interface**: Runs concurrently via FastAPI on `api_port` (default 8000)

---

## Installation

### Prerequisites
- Python 3.12+
- pip
- Git

### Steps

```bash
# 1. Clone the repository
git clone git@github.com:harveybc/lts.git
cd lts

# 2. Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install LTS package (registers plugins via entry points)
pip install -e .

# 5. Initialize the database
python app/init_db.py
# Creates lts_trading.db with schema, default admin user (admin/admin123),
# default config entries, and sample portfolio with EUR/USD and GBP/USD assets

# 6. Start the system
python app/main.py --load_config input_config.json
# Or start just the web interface:
python app/web.py
```

### First Login
1. Navigate to `http://localhost:8000`
2. Login with default admin: username `admin`, password `admin123`
3. **Change the admin password immediately in production**

---

## Quick Start Guide

### 1. Configure a Simulation Backtest

Create a config file `my_config.json`:
```json
{
    "broker_plugin": "backtrader_simulation_broker",
    "strategy_plugin": "default_strategy",
    "global_latency": 1
}
```

Run:
```bash
python app/main.py --load_config my_config.json
```

### 2. Configure Live Trading with OANDA

```json
{
    "broker_plugin": "oanda_broker",
    "strategy_plugin": "prediction_strategy",
    "prediction_provider_url": "http://localhost:8000",
    "global_latency": 60
}
```

Asset-level broker config (stored in `assets.broker_config` JSON column):
```json
{
    "account_id": "101-001-12345678-001",
    "access_token": "your-oanda-api-token",
    "environment": "practice",
    "instrument": "EUR_USD"
}
```

### 3. Use Prediction Provider

Start `prediction_provider` on port 8000, then configure LTS:
```json
{
    "prediction_provider_url": "http://localhost:8000",
    "csv_test_mode": false,
    "short_term_model": {
        "predictor_plugin": "transformer",
        "window_size": 144,
        "interval": "1h",
        "prediction_horizon": 6
    },
    "long_term_model": {
        "predictor_plugin": "cnn",
        "window_size": 256,
        "interval": "1d",
        "prediction_horizon": 6
    }
}
```

---

## Configuration Reference

Configuration is merged from multiple sources with this precedence (highest wins):
1. CLI arguments / unknown args
2. Config file (`--load_config`)
3. Default values (`app/config.py`)
4. Plugin default parameters

### Key Configuration Parameters

| Parameter | Default | Description |
|---|---|---|
| `load_config` | `None` | Path to JSON config file |
| `quiet_mode` | `False` | Suppress output (or set `LTS_QUIET=1`) |
| `global_latency` | `5` | Minutes between main loop executions |
| `api_host` | `0.0.0.0` | FastAPI server host |
| `api_port` | `8000` | FastAPI server port |
| `aaa_plugin` | `default_aaa` | AAA plugin name |
| `core_plugin` | `default_core` | Core plugin name |
| `pipeline_plugin` | `default_pipeline` | Pipeline plugin name |
| `strategy_plugin` | `default_strategy` | Strategy plugin name |
| `broker_plugin` | `default_broker` | Broker plugin name |
| `portfolio_plugin` | `default_portfolio` | Portfolio plugin name |
| `prediction_provider_url` | `http://localhost:8000` | Prediction provider API URL |
| `prediction_provider_timeout` | `300` | Prediction request timeout (seconds) |
| `csv_test_mode` | `False` | Use CSV data for perfect predictions |
| `database_url` | `sqlite:///./lts_trading.db` | Database connection string |
| `max_login_attempts` | `5` | Failed logins before lockout |
| `session_timeout` | `3600` | Session timeout (seconds) |
| `max_drawdown` | `0.20` | Maximum portfolio drawdown limit |
| `max_daily_loss` | `0.05` | Maximum daily loss limit |

See `app/config.py` for the complete list of ~80 configuration parameters.

---

## API Endpoint Reference

All protected endpoints require `Authorization: Bearer <jwt_token>` header.

### Authentication (Public)

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/auth/login` | Login with username/password, returns JWT tokens |
| POST | `/api/auth/register` | Register new user |
| POST | `/api/auth/refresh` | Refresh access token using refresh token |

### User Endpoints

| Method | Endpoint | Description | Role |
|---|---|---|---|
| GET | `/api/users/me` | Get current user profile | any |
| GET | `/api/users` | List all users | admin |

### Portfolio Management

| Method | Endpoint | Description | Role |
|---|---|---|---|
| GET | `/api/portfolios` | List portfolios (own or all for admin) | any |
| POST | `/api/portfolios` | Create portfolio | any |
| GET | `/api/portfolios/{id}` | Get portfolio details | any |
| PUT | `/api/portfolios/{id}` | Update portfolio | any |
| PATCH | `/api/portfolios/{id}/activate` | Activate portfolio | any |
| PATCH | `/api/portfolios/{id}/deactivate` | Deactivate portfolio | any |

### Asset Management

| Method | Endpoint | Description |
|---|---|---|
| GET | `/portfolios/{id}/assets` | List assets in portfolio |
| POST | `/portfolios/{id}/assets` | Add asset to portfolio |
| PATCH | `/assets/{id}/strategy` | Update asset strategy config |
| PATCH | `/assets/{id}/broker` | Update asset broker config |
| PATCH | `/assets/{id}/pipeline` | Update asset pipeline config |
| PATCH | `/assets/{id}/allocation` | Update asset capital allocation |
| PATCH | `/assets/{id}/activate` | Activate asset |
| PATCH | `/assets/{id}/deactivate` | Deactivate asset |

### Trading & Orders

| Method | Endpoint | Description |
|---|---|---|
| POST | `/trading/execute` | Execute trade for asset |
| GET | `/api/orders` | List orders |
| GET | `/assets/{id}/orders` | Get orders for asset |
| GET | `/api/positions` | List positions |
| GET | `/assets/{id}/positions` | Get positions for asset |

### Strategy & Predictions

| Method | Endpoint | Description |
|---|---|---|
| POST | `/predictions/test` | Test prediction provider integration |
| POST | `/strategy/signal` | Get trading signal from strategy |
| POST | `/strategy/backtest` | Backtest strategy on historical data |
| GET | `/strategy/config` | Get strategy configuration |
| PUT | `/strategy/config` | Update strategy configuration |

### System

| Method | Endpoint | Description | Role |
|---|---|---|---|
| GET | `/health` | Health check | public |
| GET | `/api/audit-logs` | Get audit logs | admin |
| GET | `/api/config` | Get system config | admin |
| GET | `/api/billing` | Get billing records | admin |
| GET | `/api/billing/me` | Get own billing records | any |
| GET | `/api/statistics` | Get system statistics | any |
| GET | `/plugins/list` | List available plugins | any |

---

## Testing

The project uses BDD/TDD methodology with four test levels:

```bash
# Run all tests
pytest

# Run by level
pytest tests/unit/
pytest tests/integration/
pytest tests/system/
pytest tests/acceptance/

# With coverage
pytest --cov=app --cov=plugins_aaa --cov=plugins_broker --cov=plugins_core \
       --cov=plugins_pipeline --cov=plugins_portfolio --cov=plugins_strategy
```

---

## Project Structure

```
lts/
├── app/                          # Core application
│   ├── main.py                   # Entry point — config loading, plugin init, startup
│   ├── web.py                    # FastAPI app with JWT auth, RBAC, all API endpoints
│   ├── database.py               # SQLAlchemy ORM models (10 tables)
│   ├── prediction_client.py      # Client for prediction_provider (API + CSV test mode)
│   ├── config.py                 # DEFAULT_VALUES (~80 params)
│   ├── cli.py                    # argparse CLI argument parsing
│   ├── plugin_base.py            # Base classes for all 6 plugin types
│   ├── plugin_loader.py          # Dynamic plugin loading via entry points
│   ├── config_handler.py         # Config file load/save, remote config, checksum
│   ├── config_merger.py          # Multi-source config merge (defaults < file < CLI)
│   ├── init_db.py                # DB initialization + default admin + sample data
│   └── utils/                    # Utilities (concurrency, data_transformer, error_handler)
├── plugins_aaa/                  # AAA plugins
│   └── default_aaa.py            # bcrypt + JWT + Google OAuth + audit logging
├── plugins_core/                 # Core plugins
│   └── default_core.py           # FastAPI route registration, API endpoints
├── plugins_pipeline/             # Pipeline plugins
│   └── default_pipeline.py       # Main trading loop orchestration
├── plugins_strategy/             # Strategy plugins
│   ├── default_strategy.py       # Dummy alternating strategy (testing)
│   └── prediction_strategy.py    # ML prediction-based strategy
├── plugins_broker/               # Broker plugins
│   ├── default_broker.py         # Simulated demo broker
│   ├── backtrader_broker.py      # Backtrader framework integration
│   ├── backtrader_simulation_broker.py  # Standalone simulation with full costs
│   └── oanda_broker.py           # OANDA v20 live/practice broker
├── plugins_portfolio/            # Portfolio plugins
│   └── default_portfolio.py      # Basic portfolio (stub)
├── tests/                        # Test suite (BDD/TDD)
│   ├── unit/                     # Unit tests
│   ├── integration/              # Integration tests
│   ├── system/                   # System tests
│   └── acceptance/               # Acceptance tests
├── examples/                     # Example configs and data
├── templates/                    # AdminLTE Jinja2 templates
├── setup.py                      # Package setup with plugin entry points
├── requirements.txt              # Python dependencies
├── pyproject.toml                # Modern Python project config
└── input_config.json             # Example input configuration
```

---

## License

MIT License. See `LICENSE.txt`.
