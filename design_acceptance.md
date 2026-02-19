# LTS — Acceptance-Level Design Document

## 1. User Stories and Requirements

### 1.1 Trader (User Role)

| ID | Story | Implemented |
|---|---|---|
| R1 | As a trader, I can register with username/email/password and log in securely via JWT | ✅ `web.py` `/api/auth/register`, `/api/auth/login` |
| R2 | As a trader, I can log in via Google OAuth 2.0 | ✅ `default_aaa.py` `google_oauth_login()` |
| R3 | As a trader, I can view my portfolios and their status | ✅ `GET /api/portfolios` filtered by user |
| R4 | As a trader, I can create, activate, and deactivate portfolios | ✅ `POST /api/portfolios`, `PATCH .../activate`, `PATCH .../deactivate` |
| R5 | As a trader, I can add assets to a portfolio with independent strategy/broker/pipeline configs | ✅ `POST /portfolios/{id}/assets`, `PATCH /assets/{id}/strategy` etc. |
| R6 | As a trader, I can view my orders and positions | ✅ `GET /api/orders`, `GET /api/positions` |
| R7 | As a trader, I can view my billing records | ✅ `GET /api/billing/me` |
| R8 | As a trader, I can manually trigger a trade execution for an asset | ✅ `POST /trading/execute` |
| R9 | As a trader, I can configure strategy parameters per asset | ✅ `PATCH /assets/{id}/strategy`, stored in `assets.strategy_config` JSON |
| R10 | As a trader, I can use prediction-based strategies with ML model predictions | ✅ `prediction_strategy` plugin + `PredictionProviderClient` |

### 1.2 Admin

| ID | Story | Implemented |
|---|---|---|
| R11 | As an admin, I can view all users and manage roles | ✅ `GET /api/users` (admin only) |
| R12 | As an admin, I can view all portfolios across all users | ✅ `GET /api/portfolios` returns all for admin |
| R13 | As an admin, I can view audit logs of all actions | ✅ `GET /api/audit-logs` (admin only) |
| R14 | As an admin, I can view system configuration | ✅ `GET /api/config` (admin only) |
| R15 | As an admin, I can view billing records for all users | ✅ `GET /api/billing` (admin only) |

### 1.3 Developer

| ID | Story | Implemented |
|---|---|---|
| R16 | As a developer, I can create a custom plugin by inheriting from the appropriate base class and registering in setup.py | ✅ `plugin_base.py`, `setup.py` entry points |
| R17 | As a developer, I can override config parameters via CLI or config file | ✅ `config_merger.py` multi-source merge |
| R18 | As a developer, I can access debug info from all plugins | ✅ `get_debug_info()`, `/plugins/{name}/debug` |
| R19 | As a developer, I can use the simulation broker to backtest strategies with realistic costs | ✅ `backtrader_simulation_broker` |
| R20 | As a developer, I can switch between CSV (ideal) and API (real) prediction sources | ✅ `backtrader_broker.switch_prediction_source()` |

### 1.4 Operator

| ID | Story | Implemented |
|---|---|---|
| R21 | As an operator, I can initialize the database with default data | ✅ `python app/init_db.py` |
| R22 | As an operator, I can start the system with a config file | ✅ `python app/main.py --load_config config.json` |
| R23 | As an operator, the system handles plugin failures without crashing | ✅ `main.py` catches plugin load errors, `pipeline` catches per-asset errors |
| R24 | As an operator, all actions are audit-logged for compliance | ✅ `AuditLog` table, `audit()` calls throughout |

## 2. Security Requirements

- All API endpoints require JWT authentication (except `/health`, `/api/auth/login`, `/api/auth/register`)
- RBAC enforced: admin-only endpoints check role
- bcrypt password hashing with complexity requirements
- Account lockout after 5 failed attempts (30 min)
- Rate limiting: 60 requests/minute per IP
- Security headers: X-Content-Type-Options, X-Frame-Options, HSTS, CSP, Referrer-Policy
- Request size limit: 1MB
- Input validation and SQL injection prevention via SQLAlchemy ORM
- CORS restricted to configured origins

## 3. Database Requirements

All persistent data stored in SQLite via SQLAlchemy ORM. See README.md for full schema.

## 4. Design Constraints

- All plugins must inherit from `PluginBase` and implement the exact `plugin_params` / `plugin_debug_vars` / `set_params` / `get_debug_info` / `add_debug_info` structure
- Config merge order: plugin_params < DEFAULT_VALUES < file_config < CLI_args
- System must not crash on individual plugin or asset errors

---

*Status: Reflects as-built system (2025-02)*
