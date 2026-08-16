# LTS — Live Trading System

LTS is a plugin-based Python trading framework that turns model predictions
into venue orders and observations. It provides a multi-user, multi-portfolio
core (FastAPI + SQL persistence), six plugin families loaded via entry points,
per-venue CLIs and systemd units for paper/demo execution labs, and a
model-authority / L1 execution layer that binds hash-verified model artifacts
to order intents using
[trading-contracts](https://github.com/harveybc/trading-contracts). It is the
execution end of this stack; model training and prediction serving live in
sibling repositories.

## Status

**ACTIVE — core repository.** Package `lts` version **0.1.0**
([`setup.py`](setup.py)).

**Trading status: simulation and paper/demo venues only.** The wired venues
are OANDA practice, Alpaca paper, IBKR paper, an MT5 bridge and a Capital.com
demo lab. Real-capital trading is **not** enabled anywhere in this
repository, and none of the examples or strategies are financial advice.

## Run this with an AI agent

Paste this into Claude Code, Cursor, Codex, GitHub Copilot or any coding agent
with shell access:

> Read `AGENTS.md` in this repository and follow the **Agent quickstart**
> section end to end: set up the environment, run the smoke test, execute the
> example offline fixture report, then tell me the exact URL or file paths
> where I can see the results and one query I should try first.

`AGENTS.md` is the [agents.md](https://agents.md) convention, read natively by
most coding agents. Its quickstart is strictly offline — connecting to a
broker needs your own credentials and is deliberately excluded.

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

Verified: exits 0. Note that the help text itself is inherited from an
ancestor project and does not describe LTS (see [Limitations](#limitations)).

Repository-owned example configurations live in
[`examples/configs/`](examples/configs/) (paper execution lab) and
[`examples/config/`](examples/config/) (phase inference configs). Executing a
venue lab additionally requires paper/demo credentials supplied via
environment or local config and was not executed for this document
(unverified).

The offline paths are the test suite (below), the read-only report tools in
[`tools/`](tools/) pointed at fixture ledgers, and the
`backtrader_simulation_broker` plugin, whose only imports are `csv`,
`logging`, `datetime`, `typing` and `app.plugin_base`.

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
python -m pytest -q                 # full suite
python -m pytest -q tests/unit      # unit subset
python run_tests.py unit            # wrapper: unit|integration|system|acceptance|all
```

Observed on Python 3.12.13: `701 passed in 13.92s` for the full suite and
`569 passed in 3.78s` for `tests/unit`, with a clean working tree afterwards.
`pytest.ini` sets `testpaths = tests`, which deliberately excludes the
root-level `test_*.py` files that hit live endpoints — do not run `pytest .`
from the repository root. The declared markers are auto-applied from file
paths by `tests/conftest.py`; there is no offline/live marker, so select tests
by path.

An agent-executable version of this recipe, including a fixture-only run of
the read-only reporting tool, is in [`AGENTS.md`](AGENTS.md).

Deeper operational docs:
[`docs/MULTI_VENUE_PAPER_EXECUTION.md`](docs/MULTI_VENUE_PAPER_EXECUTION.md),
[`docs/OANDA_PRACTICE_EXECUTION_LAB.md`](docs/OANDA_PRACTICE_EXECUTION_LAB.md),
[`docs/SOCIAL_TRADING_REALITY_LAB.md`](docs/SOCIAL_TRADING_REALITY_LAB.md).
Record each seated artifact/window with the
[`paper/demo seat evaluation card`](docs/PAPER_SEAT_EVALUATION_CARD_TEMPLATE.md)
so simulation-versus-venue claims carry exact identities, horizons, costs and
direct safety facts.

## Artifacts, data and outputs

- Runtime state is SQLite (`database_url` in config) plus per-venue journals
  and outbox files created by the L1 layer at the paths each venue config
  declares; systemd units define their own working directories.
- Model artifacts are **inputs**, referenced by hash through the
  model-authority layer; they are produced by predictor/agent-multi, not
  here.
- Reproducibility: decision-to-order flows journal their intents and
  execution reports as trading-contracts models. Two read-only tools consume
  those journals — [`tools/rolling_evidence_report.py`](tools/rolling_evidence_report.py)
  (24-hour / 7-day coverage and lifecycle counts) and
  [`tools/live_sim_replay.py`](tools/live_sim_replay.py) (live-versus-simulation
  replay). Both open every ledger with `mode=ro`, take all paths from their
  config so they can be pointed at fixtures, and report an unreadable source
  as `unavailable` rather than substituting a value.

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

- **Committed runtime residue.** Four artifacts of past runs are tracked:
  `model.bin`, `config_out.json`, `prediction_provider.pid` and
  `live_api_integration_results.json`. Treat them as historical residue, not
  as inputs or documentation. `lts_trading.db`, `lts_security_test.db` and
  `app.log` also appear in the working tree but are gitignored — they are
  rewritten by any default-config run from the repository root, because
  `app/config.py` defaults `database_url` to `sqlite:///./lts_trading.db`
  and `app/main.py` logs to `app.log`.
- **Incomplete packaging of two plugin directories.** `plugins_aaa/` and
  `plugins_core/` have no `__init__.py`, so `find_packages()` omits them and a
  non-editable install ships without them. Install editable.
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
- Several root-level files are historical and may reference environments that
  no longer exist: `arima_predictor.py`, `predictor.bat`, `ls_pred.bat`,
  `STATUS.md` (its test counts predate the current suite),
  `COMPLETION_SUMMARY.md`, `DOCUMENTATION_SUMMARY.md`, and the raw prompt
  files `start_prompt.md` and `prompt.txt` — the latter describes a different
  project entirely.

## License

MIT — see [`LICENSE.txt`](LICENSE.txt).
