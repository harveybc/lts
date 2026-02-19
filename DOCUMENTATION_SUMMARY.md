# LTS — Documentation Summary

## Documentation Structure

| Document | Purpose | Status |
|---|---|---|
| **README.md** | System overview, architecture, installation, configuration, API reference | ✅ Complete |
| **REFERENCE_plugins.md** | Plugin interface specs, all available plugins, parameters, custom plugin guide | ✅ Complete |
| **COMPLETION_SUMMARY.md** | Project status, completed features, known limitations | ✅ Complete |
| **DOCUMENTATION_SUMMARY.md** | This file — documentation index | ✅ Complete |

### Design Documents

| Document | Level | Content |
|---|---|---|
| **design_acceptance.md** | User requirements | User stories, acceptance criteria for traders/developers/operators |
| **design_system.md** | System architecture | Non-functional requirements, security, database schema, plugin architecture |
| **design_integration.md** | Component integration | Plugin interactions, database integration, API integration, prediction provider |
| **design_unit.md** | Individual components | Plugin methods, database models, API endpoints, validation, error handling |

### Test Documents

| Document | Content |
|---|---|
| **test_plan.md** | Master test strategy covering all 4 levels |
| **tests/plan_unit.md** | Unit test plan |
| **tests/plan_integration.md** | Integration test plan |
| **tests/plan_system.md** | System test plan |
| **tests/plan_acceptance.md** | Acceptance test plan |

### Example Configurations

| File | Description |
|---|---|
| **examples/configs/config_simulation.json** | Backtesting with BacktraderSimulationBroker (realistic forex costs) |
| **examples/configs/config_live_oanda.json** | Live/practice trading with OANDA + prediction strategy |
| **examples/configs/config_prediction_provider.json** | Prediction provider integration (API + CSV test modes) |

## Implementation Reference

### Core Application (`app/`)

| File | Purpose |
|---|---|
| `main.py` | Entry point — config loading, plugin init, pipeline startup |
| `web.py` | FastAPI app — JWT auth, RBAC, rate limiting, all REST endpoints |
| `database.py` | SQLAlchemy ORM models (10 tables), session management |
| `prediction_client.py` | Client for prediction_provider (API + CSV test mode) |
| `config.py` | DEFAULT_VALUES dictionary (~80 parameters) |
| `cli.py` | argparse CLI argument definitions |
| `plugin_base.py` | Base classes for all 6 plugin types |
| `plugin_loader.py` | Dynamic plugin loading via importlib entry points |
| `config_handler.py` | Config file load/save, remote config, checksum |
| `config_merger.py` | Multi-source config merge (plugin_params < defaults < file < CLI) |
| `init_db.py` | Database initialization + default admin + sample data |

### Plugins

| Plugin | File | Class |
|---|---|---|
| AAA | `plugins_aaa/default_aaa.py` | `DefaultAAA` |
| Core | `plugins_core/default_core.py` | `CorePlugin` |
| Pipeline | `plugins_pipeline/default_pipeline.py` | `PipelinePlugin` |
| Strategy (dummy) | `plugins_strategy/default_strategy.py` | `DefaultStrategy` |
| Strategy (ML) | `plugins_strategy/prediction_strategy.py` | `PredictionBasedStrategy` |
| Broker (demo) | `plugins_broker/default_broker.py` | `DefaultBroker` |
| Broker (simulation) | `plugins_broker/backtrader_simulation_broker.py` | `BacktraderSimulationBroker` |
| Broker (backtrader) | `plugins_broker/backtrader_broker.py` | `BacktraderBroker` |
| Broker (OANDA) | `plugins_broker/oanda_broker.py` | `OandaBroker` |
| Portfolio | `plugins_portfolio/default_portfolio.py` | `DefaultPortfolio` |

---

*Last Updated: 2025-02*
