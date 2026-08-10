# LTS — Live Trading System

LTS is the execution endpoint of this trading stack: a plugin-based Python
trading framework that turns model predictions into venue orders and
observations. It provides a multi-user, multi-portfolio core (FastAPI + SQL
persistence), six plugin families loaded via entry points, per-venue CLIs and
systemd units for paper/demo execution labs, and a model-authority / L1
execution layer that binds hash-verified model artifacts to order intents
using [trading-contracts](https://github.com/harveybc/trading-contracts).

## Status

**ACTIVE — core repository.** Package `lts` version **0.1.0**
([`setup.py`](setup.py)).

**Trading status: simulation and paper/demo venues only.** The wired venues
are OANDA practice, Alpaca paper, IBKR paper, an MT5 bridge and a Capital.com
demo lab. Real-capital trading is **not** enabled anywhere in this
repository, and none of the examples or strategies are financial advice.

## Role and non-responsibilities

**Owns**

- Order execution and venue observation in simulation and paper/demo
  environments (broker plugins, venue CLIs, watchdogs, systemd units).
- The model-authority / L1 execution layer: hash-pinned model loading and
  decision-to-order translation
  ([`app/ibkr_model_authority.py`](app/ibkr_model_authority.py),
  [`app/ibkr_l1_adapter.py`](app/ibkr_l1_adapter.py),
  [`app/ibkr_l1_executor.py`](app/ibkr_l1_executor.py),
  [`app/ibkr_l1_outbox.py`](app/ibkr_l1_outbox.py),
  [`app/demo_execution_service.py`](app/demo_execution_service.py)).
- Multi-user / multi-portfolio accounting, JWT-authenticated REST API and
  SQLite persistence (schema notes in
  [`app/README_db_schema.md`](app/README_db_schema.md)).

**Does not own**

- Model training or optimization (predictor, agent-multi, doin-node).
- Prediction serving — LTS is the primary HTTP client of
  [prediction_provider](https://github.com/harveybc/prediction_provider), not
  its host.
- Contract data shapes — imported from
  [trading-contracts](https://github.com/harveybc/trading-contracts).

## Architecture

Core flow: the pipeline plugin loop runs every `global_latency` interval and,
for each active user → portfolio → asset: the portfolio plugin allocates
capital → the strategy plugin decides → the broker plugin executes or
observes → orders/positions are recorded in the database. The FastAPI core
exposes the REST/UI surface; the L1 venue services run separately under
systemd.

### Console scripts (8, from `setup.py`)

| Command | Module | Purpose |
|---|---|---|
| `lts` | [`app/main.py`](app/main.py) | Generic config-driven entry point |
| `lts-oanda-practice` | [`app/oanda_practice_cli.py`](app/oanda_practice_cli.py) | OANDA practice execution lab |
| `lts-alpaca-paper` | [`app/alpaca_paper_cli.py`](app/alpaca_paper_cli.py) | Alpaca paper execution lab |
| `lts-ibkr-paper` | [`app/ibkr_paper_cli.py`](app/ibkr_paper_cli.py) | IBKR paper observer/lab |
| `lts-multi-venue-shadow` | [`app/multi_venue_shadow_cli.py`](app/multi_venue_shadow_cli.py) | Cross-venue shadow comparison |
| `lts-capital-demo` | [`app/capital_demo_cli.py`](app/capital_demo_cli.py) | Capital.com demo lab |
| `lts-mt5-bridge` | [`app/mt5_bridge_cli.py`](app/mt5_bridge_cli.py) | MT5 bridge control |
| `lts-social-trading-lab` | [`app/social_trading_cli.py`](app/social_trading_cli.py) | Social-trading reality lab |

### Plugin entry-point groups (6, from `setup.py`)

| Group | Directory | Registered plugins |
|---|---|---|
| `plugins_aaa` | [`plugins_aaa/`](plugins_aaa/) | `default_aaa` |
| `plugins_core` | [`plugins_core/`](plugins_core/) | `default_core` |
| `plugins_pipeline` | [`plugins_pipeline/`](plugins_pipeline/) | `default_pipeline` |
| `plugins_strategy` | [`plugins_strategy/`](plugins_strategy/) | `default_strategy`, `prediction_strategy`, `eurusd_mr_strategy`, `usdjpy_tsmom_strategy`, `usdjpy_dual_momentum_strategy` |
| `plugins_broker` | [`plugins_broker/`](plugins_broker/) | `default_broker`, `backtrader_broker`, `backtrader_simulation_broker`, `oanda_broker`*, `alpaca_paper_broker`, `ibkr_paper_broker`, `mt5_bridge_broker`, `capital_demo_broker` |
| `plugins_portfolio` | [`plugins_portfolio/`](plugins_portfolio/) | `default_portfolio` |

\* `oanda_broker` is an OANDA REST-v20 **prototype** and is not compatible
with the OANDA Global Markets MT5 flow used by the current labs; see
[Limitations](#limitations).

Full plugin details: [`REFERENCE_plugins.md`](REFERENCE_plugins.md).

### Relationship to sibling repositories

- [prediction_provider](https://github.com/harveybc/prediction_provider) —
  [`plugins_strategy/prediction_strategy.py`](plugins_strategy/prediction_strategy.py)
  consumes its `/api/v1/predict/entry` and `/api/v1/predict/exit` HTTP
  endpoints; the live integration test boots a sibling checkout of that
  repository.
- [trading-contracts](https://github.com/harveybc/trading-contracts) —
  imported by roughly 21 files (L1 adapters, model runners, demo execution).
- [predictor](https://github.com/harveybc/predictor) and
  [agent-multi](https://github.com/harveybc/agent-multi) — produce the model
  artifacts and champion configurations that the model-authority layer pins
  by hash. LTS itself is not a DOIN network participant; distributed
  optimization lives in [doin-node](https://github.com/harveybc/doin-node).

## Requirements

- Python: no `python_requires` is declared in [`setup.py`](setup.py); the
  repository was verified with **Python 3.12.13**.
- Key dependencies (from `install_requires`): `fastapi`, `uvicorn`,
  `sqlalchemy`, `backtrader`, `pydantic`, `httpx`, `oandapyV20`,
  `ib_async==2.1.0`, `matplotlib`, `schedule`.
- `trading-contracts` is imported but **not declared** in
  `install_requires`; install it alongside LTS (see below).

## Installation

```bash
git clone https://github.com/harveybc/trading-contracts.git
git clone https://github.com/harveybc/lts.git
pip install -e ./trading-contracts
pip install -e ./lts
```

Not re-executed in a clean environment for this document (unverified). Note:
the package installs a generic top-level package named `app`; in an
environment where several sibling repositories are installed editable, the
`lts` console script can resolve a *different* repository's `app` package
(this was observed in a shared environment). Use a dedicated virtual
environment, or run from the repository root with `PYTHONPATH=./` as below.

## Smallest working example

From the repository root:

```bash
PYTHONPATH=./ python -m app.main --help
```

Verified: exits 0 and prints the argument reference (`--load_config`, plugin
selection flags, remote-config options). Repository-owned example
configurations live in [`examples/configs/`](examples/configs/) (paper
execution lab) and [`examples/config/`](examples/config/) (phase inference
configs). Executing a venue lab additionally requires paper/demo credentials
supplied via environment or local config and was not executed for this
document (unverified).

## Configuration

Configuration is JSON merged from defaults, an optional `--load_config`
file, CLI flags and unknown-argument passthrough (see
[`app/config_handler.py`](app/config_handler.py) and
[`app/config_merger.py`](app/config_merger.py)). Venue setup helpers and
preflight scripts are in [`examples/scripts/`](examples/scripts/); systemd
service/timer units for observers, model runners, watchdogs and the
multi-venue shadow are in [`examples/systemd/`](examples/systemd/).

## Tests and validation

```bash
python -m pytest -q --collect-only
```

Observed result: `661 tests collected in 0.78s` (collection clean).
Full-suite execution was not run for this document — some tests boot venue
labs and a sibling `prediction_provider` checkout (see [`tests/`](tests/)).
Deeper operational docs:
[`docs/MULTI_VENUE_PAPER_EXECUTION.md`](docs/MULTI_VENUE_PAPER_EXECUTION.md),
[`docs/OANDA_PRACTICE_EXECUTION_LAB.md`](docs/OANDA_PRACTICE_EXECUTION_LAB.md),
[`docs/SOCIAL_TRADING_REALITY_LAB.md`](docs/SOCIAL_TRADING_REALITY_LAB.md).

## Artifacts, data and outputs

- Runtime state is SQLite (`database_url` in config) plus per-venue journals
  and outbox files created by the L1 layer at the paths each venue config
  declares; systemd units define their own working directories.
- Model artifacts are **inputs**, referenced by hash through the
  model-authority layer; they are produced by predictor/agent-multi, not
  here.
- Reproducibility: decision-to-order flows journal their intents and
  execution reports as trading-contracts models, so a run can be audited
  from its journal plus the pinned artifact hashes.

## Safety, security and credentials

- Venue credentials (OANDA practice, Alpaca paper, IBKR paper, MT5, Capital
  demo) are supplied via environment variables or local, uncommitted config
  files; no credentials belong in this repository.
- The REST API uses JWT authentication with RBAC, account lockout and rate
  limiting (see [`plugins_aaa/`](plugins_aaa/) and
  [`docs/security/`](docs/security/)).
- Venue observers are fail-closed: paper execution requires explicit
  capability/mandate files minted by the tooling in [`tools/`](tools/).
- **Simulation and paper/demo only. Not financial advice.**

## Limitations

- **Committed runtime residue.** The repository currently tracks artifacts
  of past runs (`lts_trading.db`, `lts_security_test.db`, `app.log`,
  `model.bin`, `config_out.json`, `prediction_provider.pid`,
  `live_api_integration_results.json`). Treat them as historical residue,
  not as inputs or documentation.
- **`oanda_broker` prototype.** It is registered as a first-class plugin but
  implements an OANDA REST-v20 prototype that is incompatible with the OANDA
  Global Markets MT5 flow; the maintained OANDA path is the practice lab via
  `lts-oanda-practice` and `mt5_bridge_broker`.
- **Generic `app` package name.** Editable installs of multiple sibling
  repositories that all use a top-level `app` package can shadow each other,
  making the `lts` console script unreliable in shared environments; prefer
  a dedicated venv or `PYTHONPATH=./ python -m app.main`.
- Parts of [`app/cli.py`](app/cli.py) retain argument text inherited from an
  ancestor project; the venue CLIs listed above are the operational entry
  points.
- Some root-level files (`arima_predictor.py`, `predictor.bat`,
  `ls_pred.bat`, `STATUS.md`) are historical and may reference environments
  that no longer exist.

## License

MIT — see [`LICENSE.txt`](LICENSE.txt).
