# LTS — System-Level Design Document

## 1. System Architecture

The LTS is a **monolithic Python application** with a plugin-based architecture:

- **Entry point**: `app/main.py` loads config, initializes all 6 plugin types, starts pipeline
- **Web layer**: FastAPI (`app/web.py`) with JWT auth, RBAC, rate limiting
- **Core plugin**: `plugins_core/default_core.py` registers all API routes
- **Pipeline plugin**: `plugins_pipeline/default_pipeline.py` runs the main trading loop
- **Database**: SQLite via SQLAlchemy ORM (`app/database.py`) — 10 tables
- **Plugin loading**: `app/plugin_loader.py` uses `importlib.metadata.entry_points`
- **Config**: `app/config_merger.py` merges defaults < file < CLI

## 2. Technology Stack

| Component | Technology |
|---|---|
| Language | Python 3.12+ |
| Web Framework | FastAPI + Uvicorn |
| Database | SQLite + SQLAlchemy ORM |
| Authentication | bcrypt + python-jose (JWT) |
| Web UI | AdminLTE (Jinja2 templates) |
| Backtesting | backtrader framework |
| Live Broker | oandapyV20 (OANDA v20 REST API) |
| HTTP Client | httpx (async), requests (sync) |
| Testing | pytest, hypothesis, httpx (TestClient) |

## 3. Plugin System

6 plugin types, dynamically loaded via Python entry points:

| Type | Base Class | Directory | Purpose |
|---|---|---|---|
| AAA | `AAAPluginBase` | `plugins_aaa/` | Auth, authz, audit |
| Core | `CorePluginBase` | `plugins_core/` | API server, routes |
| Pipeline | `PipelinePluginBase` | `plugins_pipeline/` | Trading loop |
| Strategy | `StrategyPluginBase` | `plugins_strategy/` | Trading decisions |
| Broker | `BrokerPluginBase` | `plugins_broker/` | Order execution |
| Portfolio | `PortfolioPluginBase` | `plugins_portfolio/` | Capital allocation |

## 4. Database Design

Portfolio-centric schema with 10 tables. Key relationships:
- User (1) → (N) Portfolio (1) → (N) Asset
- Asset (1) → (N) Order, Position
- User (1) → (N) AuditLog, BillingRecord, Session

Each Asset stores its own `strategy_plugin`, `broker_plugin`, `pipeline_plugin` names and JSON config columns, enabling per-asset customization.

## 5. Security Architecture

- **Transport**: HTTPS recommended (HSTS header set)
- **Authentication**: JWT tokens (HS256), bcrypt password storage
- **Authorization**: 2-role RBAC (admin/user) enforced at API layer
- **Audit**: All actions logged in `audit_logs` table
- **Input validation**: Pydantic models + custom SQL injection pattern checks
- **Rate limiting**: In-memory per-IP (60/min)
- **Session management**: Token-based with configurable expiry

## 6. Trading Execution Flow

1. Pipeline plugin wakes every `global_latency` minutes
2. Queries all active portfolios
3. For each portfolio (if latency elapsed):
   - Portfolio plugin allocates capital
   - For each active asset:
     - Strategy plugin decides (buy/sell/hold)
     - Broker plugin executes order
     - Order/Position recorded in database
4. Execution statistics recorded

## 7. External Integration

- **Prediction Provider**: `app/prediction_client.py` — HTTP client for ML predictions (short-term transformer, long-term CNN)
- **OANDA API**: `plugins_broker/oanda_broker.py` — live forex trading via oandapyV20
- **Google OAuth**: `plugins_aaa/default_aaa.py` — OAuth 2.0 login

## 8. Non-Functional Requirements

| Requirement | Implementation |
|---|---|
| Concurrent portfolios | Sequential processing (configurable `max_concurrent_portfolios`) |
| Error isolation | Per-asset try/catch in pipeline; failed assets don't crash system |
| Observability | Audit logs, statistics table, plugin debug info export |
| Configuration | Multi-source merge with validation |
| Cross-platform | Python-based, runs on Linux and Windows |
| Quiet mode | `LTS_QUIET=1` suppresses all non-error output |

---

*Status: Reflects as-built system (2025-02)*
