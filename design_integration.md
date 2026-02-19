# LTS — Integration-Level Design Document

## 1. Integration Points

### 1.1 Plugin ↔ Plugin Communication

The Pipeline Plugin orchestrates inter-plugin communication:

```
main.py → loads all 6 plugins → passes to PipelinePlugin.start()
  PipelinePlugin.start() → CorePlugin.start() (starts FastAPI)
  PipelinePlugin.run() → for each portfolio:
    PortfolioPlugin.allocate(portfolio, assets) → capital allocation
    for each asset:
      StrategyPlugin.decide(asset_data, market_data) → trading decision
      BrokerPlugin.execute_order(action, parameters) → order execution
      Database ← record Order, Position
```

Plugins receive references to each other via `pipeline.set_plugins(plugins)`.

### 1.2 Config Merger Integration

`app/config_merger.py` performs 2-pass merge:

**Pass 1** (before plugin loading): `DEFAULT_VALUES < file_config < CLI_args`  
**Pass 2** (after plugins loaded): Adds each plugin's `plugin_params` as lowest priority

### 1.3 Database Integration

All components access the database via SQLAlchemy ORM models in `app/database.py`:
- **Pipeline Plugin**: reads Portfolio, Asset; writes Order, Statistics
- **AAA Plugin**: reads/writes User, Session, AuditLog
- **Core Plugin (API)**: CRUD on all tables via FastAPI endpoints
- **Strategy Plugin**: reads Position (to check open positions)
- **Broker Plugin**: (default_broker) reads Order, Position for simulation

Session management:
- Sync: `SyncSessionLocal` / `get_db()` generator for FastAPI dependencies
- Async: `Database` class with `AsyncSession` for async operations

### 1.4 Prediction Provider Integration

`app/prediction_client.py` ↔ external `prediction_provider` service:

**API Mode**:
1. POST to `{base_url}/api/v1/predict` with symbol, prediction_type, model config
2. Poll `{base_url}/api/v1/predictions/{id}` until status=completed
3. Extract predictions array + compute uncertainties

**CSV Test Mode**:
1. Load CSV with OHLC + DATE_TIME columns
2. For each prediction horizon, read future CLOSE prices
3. Return perfect predictions with near-zero uncertainty

The `PredictionBasedStrategy` consumes these predictions to generate trading signals.

### 1.5 Broker Integration Patterns

All brokers implement `execute_order(action, parameters)` for compatibility:

| Broker | Integration |
|---|---|
| `default_broker` | In-memory simulation, no external calls |
| `backtrader_simulation_broker` | Reads CSV files, simulates with costs |
| `backtrader_broker` | Within backtrader cerebro; fetches predictions from CSV or API |
| `oanda_broker` | REST API calls via oandapyV20; retry with exponential backoff |

### 1.6 Web API ↔ Database Integration

FastAPI endpoints use `Depends(get_db)` for session injection:
```python
@app.get("/api/portfolios")
async def get_portfolios(current_user: User = Depends(get_current_user),
                         db: DBSession = Depends(get_db)):
    portfolios = db.query(Portfolio).filter(Portfolio.user_id == current_user.id).all()
```

Authentication flow:
1. Client sends `POST /api/auth/login` with credentials
2. Server verifies bcrypt hash, creates JWT tokens
3. Client includes `Authorization: Bearer <token>` on subsequent requests
4. `get_current_user` dependency decodes JWT, queries User from DB

## 2. Integration Requirements

| ID | Requirement | Source |
|---|---|---|
| INT-01 | Plugin loading must fail gracefully with clear error messages | `main.py`, `plugin_loader.py` |
| INT-02 | Config merge must preserve plugin-specific params through 2-pass merge | `config_merger.py` |
| INT-03 | Database sessions must be properly opened/closed (no leaked connections) | `database.py` `get_db()` |
| INT-04 | Pipeline must isolate per-asset failures (one asset error ≠ system crash) | `default_pipeline.py` |
| INT-05 | Prediction client must handle API timeouts and retries | `prediction_client.py` |
| INT-06 | OANDA broker must retry with exponential backoff on API errors | `oanda_broker.py` |
| INT-07 | JWT tokens must be validated on every protected endpoint | `web.py` `get_current_user` |
| INT-08 | Audit logs must be written for all state-changing operations | `default_aaa.py`, `default_core.py` |
| INT-09 | Backtrader broker must support runtime prediction source switching | `backtrader_broker.py` |

## 3. Data Flow

### Order Lifecycle
```
Strategy.decide() → {action: "open", parameters: {side, quantity, price, sl, tp}}
  → Broker.execute_order("open", parameters)
    → Returns {success, order_id, entry_price}
  → Pipeline writes Order to DB (status="open")
  → Pipeline writes Position to DB (is_open=True)

Later: Strategy.decide() → {action: "close", parameters: {position_id}}
  → Broker.execute_order("close", parameters)
    → Returns {success, close_price, pnl}
  → Pipeline updates Order (status="filled")
  → Pipeline updates Position (is_open=False, realized_pnl)
```

### Prediction Flow
```
PredictionBasedStrategy.generate_signal()
  → PredictionProviderClient.get_predictions(symbol, datetime, types)
    → [API mode] POST /api/v1/predict → poll → predictions[]
    → [CSV mode] Read future CLOSE prices from CSV
  → _analyze_predictions(short_term, long_term, uncertainties)
    → Calculate weighted trend signal
    → Check confidence threshold, uncertainty, trend alignment
  → Return TradingSignal(action, confidence, quantity, reasoning)
```

---

*Status: Reflects as-built system (2025-02)*
