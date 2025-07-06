# Live Trading System (LTS)

## Overview

LTS (Live Trading System) is a secure, modular, and extensible framework for automated trading, featuring:
- **FastAPI** for a modern, async, and secure API layer.
- **AdminLTE-based web interface** for admin and user dashboards, fully integrated with the API.
- **Plugin-based AAA (Authentication, Authorization, Accounting)** for flexible, secure user management.
- **Plugin-based core loop**: The core plugin runs the main trading loop and starts the API server.
- **SQLite database via SQLAlchemy** for all AAA, configuration, statistics, and app state, with a fully documented schema.
- **Full BDD/TDD methodology**: All endpoints and behaviors are covered by unit, integration, system, and acceptance tests.

## Architecture

- **API Layer**: Built with FastAPI, providing secure, async endpoints for all system operations. All endpoints require authentication and authorization, enforced by AAA plugins.
- **AdminLTE UI**: The web dashboard is built on AdminLTE, communicating with the FastAPI backend via secure API calls. All admin/user actions are performed through the API.
- **Plugin System**: All major system components (AAA, core loop, pipelines, strategies, brokers, portfolio managers) are implemented as plugins. Plugins are loaded dynamically and can be extended or replaced without modifying the core.
- **Security**: AAA plugins provide authentication (login, registration, password reset), authorization (role-based access), and accounting (audit logging). All API endpoints are protected.
- **Core Plugin**: The core plugin is responsible for running the main trading loop and starting the FastAPI server. It coordinates all other plugins and system components.
- **Database**: All AAA, configuration, statistics, and app state are stored in a SQLite database, accessed exclusively via SQLAlchemy ORM. The schema is fully documented below.

## Database Schema (SQLAlchemy ORM)

### User Table
| Column         | Type        | Description                       |
|---------------|------------|-----------------------------------|
| id            | Integer PK | Unique user ID                    |
| username      | String     | Unique username                   |
| email         | String     | Unique email                      |
| password_hash | String     | Hashed password                   |
| role          | String     | User role (admin, user, etc.)     |
| is_active     | Boolean    | Account active flag               |
| created_at    | DateTime   | Account creation timestamp        |

### Session Table
| Column         | Type        | Description                       |
|---------------|------------|-----------------------------------|
| id            | Integer PK | Unique session ID                 |
| user_id       | Integer FK | Linked user                       |
| token         | String     | Session token (JWT or random)     |
| created_at    | DateTime   | Session creation timestamp        |
| expires_at    | DateTime   | Session expiration                |

### AuditLog Table
| Column         | Type        | Description                       |
|---------------|------------|-----------------------------------|
| id            | Integer PK | Unique log entry                  |
| user_id       | Integer FK | Linked user                       |
| action        | String     | Action performed                  |
| timestamp     | DateTime   | When action occurred              |
| details       | String     | Additional context                |

### Config Table
| Column         | Type        | Description                       |
|---------------|------------|-----------------------------------|
| id            | Integer PK | Unique config entry               |
| key           | String     | Config key                        |
| value         | String     | Config value (JSON/text)          |
| updated_at    | DateTime   | Last update timestamp             |

### Statistics Table
| Column         | Type        | Description                       |
|---------------|------------|-----------------------------------|
| id            | Integer PK | Unique stat entry                 |
| key           | String     | Stat key                          |
| value         | Float      | Stat value                        |
| timestamp     | DateTime   | When stat was recorded            |

### Portfolio Table
| Column              | Type        | Description                       |
|--------------------|------------|-----------------------------------|
| id                 | Integer PK | Unique portfolio ID               |
| user_id            | Integer FK | Owner user ID                     |
| name               | String     | Portfolio name                    |
| description        | Text       | Portfolio description             |
| is_active          | Boolean    | Portfolio active flag             |
| portfolio_plugin   | String     | Portfolio plugin name             |
| portfolio_config   | Text       | Portfolio plugin JSON config     |
| total_capital      | Float      | Total capital allocated           |
| last_execution     | DateTime   | Last execution timestamp          |
| portfolio_latency  | Integer    | Minutes between executions        |
| created_at         | DateTime   | Portfolio creation timestamp      |
| updated_at         | DateTime   | Last update timestamp             |

### Asset Table
| Column              | Type        | Description                       |
|--------------------|------------|-----------------------------------|
| id                 | Integer PK | Unique asset ID                   |
| portfolio_id       | Integer FK | Parent portfolio ID               |
| symbol             | String     | Asset symbol (e.g., EUR/USD)      |
| name               | String     | Asset name                        |
| strategy_plugin    | String     | Strategy plugin name              |
| strategy_config    | Text       | Strategy plugin JSON config      |
| broker_plugin      | String     | Broker plugin name                |
| broker_config      | Text       | Broker plugin JSON config        |
| pipeline_plugin    | String     | Pipeline plugin name              |
| pipeline_config    | Text       | Pipeline plugin JSON config      |
| allocated_capital  | Float      | Capital allocated to this asset   |
| is_active          | Boolean    | Asset active flag                 |
| created_at         | DateTime   | Asset creation timestamp          |
| updated_at         | DateTime   | Last update timestamp             |

### Order Table
| Column              | Type        | Description                       |
|--------------------|------------|-----------------------------------|
| id                 | Integer PK | Unique order ID                   |
| asset_id           | Integer FK | Related asset ID                  |
| order_type         | String     | Order type (market, limit, etc.)  |
| side               | String     | Order side (buy, sell)            |
| quantity           | Float      | Order quantity                    |
| price              | Float      | Order price                       |
| stop_loss          | Float      | Stop loss price                   |
| take_profit        | Float      | Take profit price                 |
| status             | String     | Order status (pending, filled, etc.) |
| broker_order_id    | String     | Broker's order ID                 |
| broker_response    | Text       | Full broker response JSON         |
| created_at         | DateTime   | Order creation timestamp          |
| updated_at         | DateTime   | Last update timestamp             |
| filled_at          | DateTime   | Order fill timestamp              |

### Position Table
| Column              | Type        | Description                       |
|--------------------|------------|-----------------------------------|
| id                 | Integer PK | Unique position ID                |
| asset_id           | Integer FK | Related asset ID                  |
| order_id           | Integer FK | Opening order ID                  |
| side               | String     | Position side (long, short)       |
| quantity           | Float      | Position quantity                 |
| entry_price        | Float      | Entry price                       |
| current_price      | Float      | Current market price              |
| unrealized_pnl     | Float      | Unrealized P&L                    |
| realized_pnl       | Float      | Realized P&L                      |
| status             | String     | Position status (open, closed)    |
| broker_position_id | String     | Broker's position ID              |
| opened_at          | DateTime   | Position opening timestamp        |
| closed_at          | DateTime   | Position closing timestamp        |
| updated_at         | DateTime   | Last update timestamp             |

## Trading System Flow

The LTS follows this execution flow:

1. **Main Loop**: Core plugin executes every `global_latency` minutes
2. **Portfolio Processing**: For each active portfolio of each user:
   - Check if `portfolio_latency` minutes have passed since last execution
   - Portfolio plugin allocates capital among assets
   - For each active asset in the portfolio:
     - Strategy plugin processes predictions and market data
     - Strategy returns order action (open/close) and parameters
     - Broker plugin executes the order with the broker API
     - Results are stored in Order and Position tables
3. **Web Interface**: AdminLTE dashboard shows portfolios, assets, and trading activity
4. **API Layer**: All actions go through authenticated FastAPI endpoints
5. **Database**: All state, orders, positions, and audit logs are stored in SQLite

## Features

- **AdminLTE-based dashboard**: Modern, responsive UI for managing portfolios, assets, users, and system analytics.
- **Secure API**: All actions (including UI) go through authenticated, authorized FastAPI endpoints.
- **Plugin-based AAA**: Authentication, authorization, and audit logging are fully pluggable and configurable.
- **Plugin-based core loop**: The main trading loop and API server are managed by a core plugin, allowing for custom execution logic.
- **Extensible plugin system**: Pipelines, strategies, brokers, portfolio managers, and AAA can all be extended or replaced.
- **Unified configuration**: All config is merged from defaults, files, CLI, and remote sources, and passed to every plugin.
- **Full BDD/TDD**: All endpoints and behaviors are covered by tests at every level.
- **SQLite/SQLAlchemy**: All persistent data is stored in a SQLite database, accessed via SQLAlchemy ORM only.

## Technology Stack

- **FastAPI** (API layer)
- **AdminLTE** (web UI)
- **SQLAlchemy** (ORM, SQLite backend)
- **Python 3.12+**
- **pytest, hypothesis, pydocstyle** (testing)
- **httpx** (API testing)
- **pandas, numpy, etc.** (data handling)
- **All dependencies listed in requirements.txt**

## Running the System

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
2. **Initialize the database**:
   ```bash
   python app/init_db.py
   ```
3. **Configure your system**:
   - Edit `input_config.json` or use CLI parameters to define plugins, AAA, and core loop.
4. **Run the system**:
   ```bash
   python app/main.py --load_config input_config.json [other CLI options]
   ```
5. **Start the web dashboard**:
   ```bash
   python app/web.py
   ```
6. **Develop new plugins**:
   - Implement the required interface as described in `REFERENCE_plugins.md` and register via `setup.py`.

## Testing

- **Unit tests**: `pytest tests/unit/`
- **Integration tests**: `pytest tests/integration/` (includes real FastAPI endpoint tests)
- **System tests**: `pytest tests/system/`
- **Acceptance tests**: `pytest tests/acceptance/`
- **All endpoints and behaviors are covered by real, executable tests.**
- **Database tests**: All database models, migrations, and queries are covered by tests at every level.

## Project Structure

The LTS project follows a modular, plugin-based architecture with clear separation of concerns:

```
lts/
├── app/                                    # Core application modules
│   ├── __init__.py                        # Package initialization
│   ├── cli.py                             # Command-line interface and argument parsing
│   ├── config.py                          # Default configuration values and constants
│   ├── config_handler.py                  # Configuration file loading and validation
│   ├── config_merger.py                   # Multi-source configuration merging logic
│   ├── database.py                        # SQLAlchemy models and database operations
│   ├── init_db.py                         # Database initialization and schema creation
│   ├── main.py                            # Main application entry point and orchestration
│   ├── plugin_base.py                     # Base classes for all plugin types
│   ├── plugin_loader.py                   # Dynamic plugin loading and management
│   ├── web.py                             # FastAPI endpoints and web interface
│   └── README_db_schema.md                # Database schema documentation
│
├── plugins_aaa/                           # Authentication, Authorization, Accounting plugins
│   └── default_aaa.py                     # Default AAA plugin implementation
│
├── plugins_broker/                        # Broker execution plugins
│   ├── __init__.py                        # Package initialization
│   └── default_broker.py                  # Default broker plugin implementation
│
├── plugins_core/                          # Core system orchestration plugins
│   └── default_core.py                    # Default core plugin with FastAPI server
│
├── plugins_pipeline/                      # Data pipeline plugins
│   ├── __init__.py                        # Package initialization
│   └── default_pipeline.py                # Default pipeline plugin implementation
│
├── plugins_portfolio/                     # Portfolio management plugins
│   ├── __init__.py                        # Package initialization
│   └── default_portfolio.py               # Default portfolio plugin implementation
│
├── plugins_strategy/                      # Trading strategy plugins
│   ├── __init__.py                        # Package initialization
│   └── default_strategy.py                # Default strategy plugin implementation
│
├── tests/                                 # Test suite (BDD/TDD methodology)
│   ├── __init__.py                        # Package initialization
│   ├── conftest.py                        # pytest configuration and fixtures
│   ├── plan_acceptance.md                 # Acceptance test plan and requirements
│   ├── plan_integration.md                # Integration test plan and requirements
│   ├── plan_system.md                     # System test plan and requirements
│   ├── plan_unit.md                       # Unit test plan and requirements
│   ├── acceptance/                        # Acceptance tests (end-to-end user scenarios)
│   │   └── test_acceptance.py             # User story validation tests
│   ├── integration/                       # Integration tests (cross-component)
│   │   └── test_integration.py            # Component integration tests
│   ├── system/                            # System tests (full system validation)
│   │   └── test_system.py                 # System-wide behavior tests
│   └── unit/                              # Unit tests (isolated component tests)
│       └── test_unit.py                   # Individual component tests
│
├── examples/                              # Example configurations and data
│   ├── config/                            # Example configuration files
│   ├── data/                              # Example data files
│   ├── results/                           # Example results and outputs
│   └── scripts/                           # Example execution scripts
│
├── design_acceptance.md                   # Acceptance-level design and requirements
├── design_integration.md                  # Integration-level design and requirements
├── design_system.md                       # System-level design and requirements
├── design_unit.md                         # Unit-level design and requirements
├── test_plan.md                           # Master test plan and strategy
├── DOCUMENTATION_SUMMARY.md               # Complete documentation overview
├── REFERENCE_plugins.md                   # Plugin development reference
├── acceptance_requirements_and_test_plan.md # Legacy acceptance requirements
│
├── arima_predictor.py                     # ARIMA prediction utility
├── concatenate_csv.py                     # CSV file concatenation utility
├── show_schema.py                         # Database schema display utility
├── config_out.json                        # Example output configuration
├── input_config.json                      # Example input configuration
├── model.bin                              # Example model file
├── test.csv                               # Example test data
│
├── predictor.sh                           # Linux execution script
├── predictor.bat                          # Windows execution script
├── ls_pred.bat                            # Windows prediction script
├── set_env.sh                             # Linux environment setup
├── set_env.bat                            # Windows environment setup
│
├── requirements.txt                       # Python dependencies
├── setup.py                               # Package setup and installation
├── pyproject.toml                         # Modern Python project configuration
├── pytest.ini                             # pytest configuration
├── LICENSE.txt                            # MIT License
├── README.md                              # This file
├── prompt.txt                             # Development prompt/context
└── start_prompt.md                        # Development startup guide
```

### Core Application Files (`app/`)

- **`main.py`**: Application entry point. Orchestrates plugin loading, configuration merging, and system startup.
- **`cli.py`**: Command-line interface using argparse. Handles all CLI arguments and options.
- **`config.py`**: Default configuration values and system constants.
- **`config_handler.py`**: Configuration file loading with validation and error handling.
- **`config_merger.py`**: Multi-pass configuration merging from CLI, files, and environment variables.
- **`database.py`**: SQLAlchemy ORM models for all database tables (users, portfolios, assets, orders, positions, etc.).
- **`init_db.py`**: Database initialization script. Creates schema and initial data.
- **`plugin_base.py`**: Base classes defining interfaces for all plugin types.
- **`plugin_loader.py`**: Dynamic plugin loading system with validation and error handling.
- **`web.py`**: FastAPI application with all API endpoints and AdminLTE web interface.

### Plugin System

The LTS uses a plugin-based architecture where each major component is implemented as a plugin:

- **AAA Plugins** (`plugins_aaa/`): Authentication, Authorization, and Accounting
- **Core Plugins** (`plugins_core/`): Main system orchestration and FastAPI server management
- **Pipeline Plugins** (`plugins_pipeline/`): Data processing and execution coordination
- **Strategy Plugins** (`plugins_strategy/`): Trading decision logic and signal generation
- **Broker Plugins** (`plugins_broker/`): Order execution and market interaction
- **Portfolio Plugins** (`plugins_portfolio/`): Portfolio management and capital allocation

Each plugin type has a standardized interface defined in `plugin_base.py` and documented in `REFERENCE_plugins.md`.

### Test Suite (`tests/`)

The project follows BDD/TDD methodology with comprehensive test coverage:

- **Acceptance Tests**: End-to-end user scenarios validating business requirements
- **System Tests**: Full system validation including performance and security
- **Integration Tests**: Cross-component integration and data flow validation
- **Unit Tests**: Isolated component testing with mocking

Each test level has its own test plan document defining requirements, test cases, and traceability.

### Design Documentation

- **`design_acceptance.md`**: User stories and acceptance criteria
- **`design_system.md`**: System architecture and non-functional requirements
- **`design_integration.md`**: Component integration and interface specifications
- **`design_unit.md`**: Individual component specifications and unit requirements
- **`test_plan.md`**: Master test plan and testing strategy

### Utility Files

- **`arima_predictor.py`**: ARIMA time series prediction utility
- **`concatenate_csv.py`**: CSV file manipulation utility
- **`show_schema.py`**: Database schema visualization tool
- **Configuration Files**: Example configurations for different deployment scenarios
- **Scripts**: Platform-specific execution and environment setup scripts

### Configuration Files

- **`requirements.txt`**: Python package dependencies
- **`setup.py`**: Package installation and plugin registration
- **`pyproject.toml`**: Modern Python project configuration
- **`pytest.ini`**: Test framework configuration
- **`LICENSE.txt`**: MIT License terms

## Directory Structure

## User Stories

- As a user/admin, I want to log in securely and access my dashboard (AdminLTE UI).
- As a user/admin, I want to manage portfolios, assets, and users via the web UI and API.
- As a developer, I want to implement and test new plugins for AAA, core loop, pipelines, strategies, brokers, and portfolio managers.
- As an operator, I want all AAA, config, and statistics to be stored in a secure, auditable database (SQLite/SQLAlchemy).
- As a tester, I want all database models and queries to be covered by tests at every level.

## Security & Compliance

- All API endpoints require authentication and authorization.
- AAA plugins enforce role-based access and audit logging.
- All actions are traceable and auditable.
- The system is designed for robust, ethical, and maintainable operation.

## Extending the System

- See `REFERENCE_plugins.md` for detailed plugin interfaces and extension points.
- All plugins must be registered and configured via the unified config system.
- The core plugin manages the main loop and API server; custom core plugins can be developed for advanced use cases.

---

For full details, see the design documents for each level (acceptance, system, integration, unit) and the plugin reference.