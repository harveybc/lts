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

## Directory Structure

- `app/`: Core application, plugin interfaces, API, and web modules.
- `plugins_*`: Default plugin implementations for each type.
- `examples/`: Example configs, data, and scripts.
- `tests/`: Acceptance, system, integration, and unit tests (BDD/TDD methodology).
- `README.md`: This file.
- `REFERENCE_plugins.md`: Detailed plugin interface and requirements.

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