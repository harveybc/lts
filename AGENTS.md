# AGENTS.md — lts

Guidance for coding agents working in this repository. Follows the
[agents.md](https://agents.md) convention.

> **Read this before running anything.** This repository contains live
> paper/demo trading code, and runners driven by it may be executing right now
> on the machine you are on. The quickstart below is deliberately, entirely
> offline. Nothing in it opens a socket, reads a credential or reaches a
> broker. Do not substitute a "more realistic" command for it.

## Project overview

`lts` is a plugin-based Python trading framework that turns model predictions
into venue orders and observations. It provides a multi-user, multi-portfolio
core (FastAPI plus SQL persistence), six plugin families loaded through
setuptools entry points, per-venue CLIs and systemd units for paper/demo
execution labs, and a model-authority / L1 execution layer that binds
hash-verified model artifacts to order intents using
[trading-contracts](https://github.com/harveybc/trading-contracts).

It does **not** train or optimise models — artifacts arrive from `predictor`
and `agent-multi` and are pinned here by hash. It does **not** serve
predictions; it is an HTTP *client* of `prediction_provider`. It does **not**
define contract data shapes; those are imported from `trading-contracts`. It
does **not** trade real capital: the wired venues are practice, paper and demo
only, and nothing here is financial advice.

## Agent quickstart (install → run → show the user results)

### 1. Environment

```bash
git clone https://github.com/harveybc/trading-contracts.git
git clone https://github.com/harveybc/lts.git
pip install -e ./trading-contracts
pip install -e ./lts
```

Not re-executed in a clean environment for this document — treat the install
block as unverified. Two packaging facts matter:

- The package installs a generic top-level package named `app`. In an
  environment where several sibling repositories are installed editable, the
  `lts` console script can resolve a *different* repository's `app` package.
  Use a dedicated virtual environment, or always run from the repository root
  with `PYTHONPATH=./ python -m app.main`.
- `plugins_aaa/` and `plugins_core/` have no `__init__.py`, so
  `find_packages()` omits them and a non-editable install ships an incomplete
  plugin set. Install editable.

### 2. Smoke test (offline)

```bash
cd /path/to/lts
PYTHONPATH=./ python -m app.main --help    # exits 0
python -m pytest -q                        # 701 passed in ~14 s
python -m pytest -q tests/unit             # 569 passed in ~4 s
```

All three verified on Python 3.12.13. The suite is hermetic: it left the
working tree byte-clean (`git status --porcelain` empty afterwards), and no
test in `tests/` references `~/.local/state`, `Path.home()` or `expanduser`.

Two cautions:

- **Never run `pytest .` or `pytest` from the repository root without a
  path.** `pytest.ini` sets `testpaths = tests` precisely because several
  root-level `test_*.py` files (`test_lts_endpoints.py`,
  `test_live_api_integration.py`, `test_prediction_provider_integration.py`,
  `test_backtrader_broker.py`) hit live endpoints and are deliberately
  excluded. A bare `pytest .` would collect them.
- There is **no marker for offline versus live tests.** `pytest.ini` declares
  `acceptance, system, integration, unit, slow, security, performance, smoke`,
  but `tests/conftest.py` auto-applies them from file paths in
  `pytest_collection_modifyitems` — they are path proxies, not safety labels.
  `-m "not live"` is meaningless here. Select by path.

### 3. Representative safe run — the rolling evidence report against fixtures

`tools/rolling_evidence_report.py` is the read-only reporting tool. It opens
every ledger with `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`, issues
only `SELECT`s, and imports nothing that can reach a network
(`argparse, json, os, sqlite3, datetime, pathlib`). All ledger paths come from
the config, so it can be pointed at fixtures.

Build a throwaway fixture ledger in a scratch directory — never at a live
path:

```bash
cd /path/to/lts
export LTS_FIXTURE_DIR="$(mktemp -d)"

PYTHONPATH=./ python - "$LTS_FIXTURE_DIR" <<'PY'
import json, sys
from pathlib import Path
from app.ibkr_l1_journal import L1ExecutionOlap

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
ledger_path = out / "fixture-execution.sqlite"

ledger = L1ExecutionOlap(ledger_path)
for hour, outcome in ((0, "would_be_order"), (4, "hold"), (8, "rejected")):
    ledger.record_due_bar_decision({
        "venue": "fixture_venue", "account_fingerprint": "fixture",
        "asset_id": "fx:EUR/USD", "instrument": "EUR.USD", "timeframe": "4h",
        "bar_close": f"2026-08-05T{hour:02d}:00:00+00:00",
        "decided_at": f"2026-08-05T{hour:02d}:00:05+00:00",
        "input_sha256": "a" * 64, "config_sha256": "b" * 64,
        "model_id": "fixture-model", "artifact_sha256": "c" * 64,
        "action": "short", "outcome": outcome,
        "decision_id": f"fixture:{hour}",
    })
ledger.create_effect("fixture-effect", "k1", "bracket_entry", [])
for state in ("submitted_pending_ack", "acknowledged", "terminal_flat"):
    ledger.advance_effect("fixture-effect", state)
ledger.close()

(out / "rolling_evidence_report_fixture.json").write_text(json.dumps({
    "schema": "lts.rolling_evidence_config.v1",
    "version": "1.0.0",
    "incident_ledger": str(out / "absent-incidents.sqlite"),
    "venues": [{"venue": "fixture_venue", "timeframe": "4h",
                "execution_ledger": str(ledger_path)}],
}, indent=2))
print("fixture ledger:", ledger_path)
PY
```

Then run the report against it, pinned to a fixed `--as-of` so the output is
reproducible:

```bash
PYTHONPATH=./ python tools/rolling_evidence_report.py \
  --config "$LTS_FIXTURE_DIR/rolling_evidence_report_fixture.json" \
  --as-of 2026-08-05T12:00:00+00:00 \
  --output "$LTS_FIXTURE_DIR/rolling_report.json"
```

Verified output: schema `lts.rolling_evidence_report.v1`, with `24h` and `7d`
windows for `fixture_venue`, `available: true`, `delivered_bars: 3`,
`by_outcome: {hold: 1, rejected: 1, would_be_order: 1}`,
`expected_bars_upper_bound` 6 and 42 respectively, and
`incidents: "unavailable"` for the deliberately-absent incident ledger. That
last value is the point of the tool: a fact it cannot read is reported as
unavailable, never invented.

The config must carry `"schema": "lts.rolling_evidence_config.v1"` or the tool
exits with `config schema mismatch`.

The two report tools are also covered by unit tests that build their own
fixtures — a faster way to confirm both work:

```bash
python -m pytest -q tests/unit/test_rolling_evidence_report.py \
                     tests/unit/test_live_sim_replay.py   # 6 passed
```

### 4. Optional second read-only tool

`tools/live_sim_replay.py` performs a deterministic live-versus-simulation
replay (order C2) with the same `mode=ro` ledger access and no network
imports. Verified `--help`:

```
usage: live_sim_replay.py [-h] --config CONFIG --venue VENUE --since SINCE
                          --until UNTIL [--output OUTPUT]
```

Its config (`examples/configs/live_sim_replay_v1.json`, schema
`lts.live_sim_replay_config.v1`) takes ledger paths from the config too, so it
is fixture-pointable the same way. Running it against real fixtures needs a
richer seeded ledger than the snippet above; `tests/unit/test_live_sim_replay.py`
shows the required shape.

### 5. Connecting to a broker is NOT part of this quickstart

Every venue path — `lts-oanda-practice`, `lts-alpaca-paper`, `lts-ibkr-paper`,
`lts-capital-demo`, `lts-mt5-bridge`, `lts-multi-venue-shadow`, the
`app/*_lab.py` modules and the `app/*_model_runner.py` runners — requires the
owner's own paper/demo credentials, supplied through environment variables or
local uncommitted config, plus a minted capability or mandate. None of that is
in this repository and none of it is part of this recipe. An agent must not
attempt it, must not mint a capability, and must not ask the user for
credentials in order to "complete" the quickstart. The quickstart is complete
when the tests pass and the fixture report is produced.

### 6. Final message to the user

Report exactly this:

> Everything ran offline. No broker was contacted and no live state was read
> or written.
>
> - **Test suite:** `701 passed` in about 14 seconds
>   (`python -m pytest -q` from the repository root). The unit subset alone is
>   `569 passed` (`python -m pytest -q tests/unit`). The working tree was
>   byte-clean afterwards.
> - **Report tool output:** the rolling evidence report was generated against a
>   throwaway fixture ledger and written to
>   `$LTS_FIXTURE_DIR/rolling_report.json` (the scratch directory printed
>   during the run). The fixture ledger itself is
>   `$LTS_FIXTURE_DIR/fixture-execution.sqlite`.
> - Neither path is under `~/.local/state/lts/`, where the real ledgers live —
>   those were never opened.
>
> **One thing to inspect first** — open the generated report and compare the
> two windows:
>
> ```bash
> python -m json.tool "$LTS_FIXTURE_DIR/rolling_report.json" | less
> ```
>
> Look at `windows["24h"]["fixture_venue"]` against
> `windows["7d"]["fixture_venue"]`. `delivered_bars` is 3 in both, while
> `expected_bars_upper_bound` goes 6 → 42. That gap is the coverage question
> the tool exists to answer: how many 4-hour bars *should* have produced a
> decision versus how many actually did. Then note
> `incidents: "unavailable"` — the incident ledger was deliberately absent, and
> the tool reports that rather than substituting a zero. Re-running with the
> same `--as-of` reproduces the file byte for byte.

## Build, test and lint commands

```bash
# install (editable; see caveats above)
pip install -e ./trading-contracts
pip install -e ./lts

# tests
python -m pytest -q                    # 701 passed, ~14 s
python -m pytest -q tests/unit         # 569 passed, ~4 s
python -m pytest -q --collect-only     # 701 collected, no errors
python run_tests.py unit               # wrapper; levels unit|integration|system|acceptance|all

# CI subset (.github/workflows/tier-a.yml)
python -m pip install --require-hashes -r requirements-ci.txt
python -m pytest --confcutdir=tests/unit -q \
  tests/unit/test_multi_venue_shadow.py \
  tests/unit/test_paper_execution_watchdog.py \
  tests/unit/test_portfolio_invariants.py

# run (offline)
PYTHONPATH=./ python -m app.main --help
```

No linter is configured in this repository — there is no `ruff`, `flake8`,
`black` or pre-commit configuration. `pyproject.toml` contains only the build
backend.

`app/main.py --help` prints argument text inherited from an ancestor project
(`--x_train_file`, `-e EPOCHS`, and a description that says "Predictor: A tool
for timeseries prediction with plugin support"). That text does not describe
LTS. Do not treat it as a feature list.

## Layout

| Path | Contents |
|---|---|
| `app/` | Runtime core (~47 modules): generic entry (`main.py`, `cli.py`, `config*.py`, `plugin_loader.py`, `database.py`, `web.py`), the eight venue CLIs, the six `*_lab.py` venue labs, the IBKR L1 stack (`ibkr_l1_{adapter,broker,capability,executor,journal,outbox,recovery,resume,runner,tws}.py`), the model runners, and `demo_execution_{service,runner}.py`. |
| `plugins_aaa/` | Authentication / authorisation / accounting plugin. |
| `plugins_core/` | FastAPI core and API-server plugin. |
| `plugins_pipeline/` | The trading-loop orchestrator. |
| `plugins_strategy/` | Five registered strategies plus an unregistered `heuristic_strategy.py`. |
| `plugins_broker/` | Eight registered brokers, including the offline `backtrader_simulation_broker` and the prototype `oanda_broker`. |
| `plugins_portfolio/` | Capital allocation. |
| `feeder_plugins/`, `predictor_plugins/` | Installed top-level packages with no entry-point group; imported directly. |
| `tools/` | Operator tools: capability/mandate minting, watchdogs, monitors, the two read-only report tools, replay, inventory. Mixed offline and online — see *Do not touch*. |
| `examples/configs/` | Venue and tool JSON configs. |
| `examples/config/` | Phase inference configs. |
| `examples/data/phase_3/` | CSV fixtures. |
| `examples/scripts/`, `examples/systemd/` | Venue setup and preflight scripts; systemd service and timer units. |
| `docs/` | Operational docs for the multi-venue paper execution, the OANDA practice lab, the social-trading lab, plus `docs/security/`. |
| `mt5/` | MQL5 expert advisors deployed to the MT5 host; not run from here. |
| `tests/` | 701 tests plus `conftest.py`, four `plan_*.md` test-plan documents and `tests/data/`. |

## Conventions and constraints

- **Plugin architecture.** Six entry-point groups declared in `setup.py`:
  `plugins_aaa`, `plugins_core`, `plugins_pipeline`, `plugins_strategy`,
  `plugins_broker`, `plugins_portfolio`. Every plugin subclasses `PluginBase`
  (`app/plugin_base.py`) and declares `plugin_params` (defaults) and
  `plugin_debug_vars`. Selection is by config key (`broker_plugin`,
  `strategy_plugin`, …). Interface details: `REFERENCE_plugins.md`.
- **Config-driven JSON with versioned schemas.** Merge order is defaults <
  `--load_config` file < CLI flags < unknown-argument passthrough
  (`app/config_handler.py`, `app/config_merger.py`; defaults in
  `app/config.py`). Every tool and runner config carries a `"schema"` string
  and refuses to run on mismatch rather than guessing.
- **Refusals are string reason codes plus durable journalled facts** — not a
  typed enum in this repository. Runners attach `reason_codes: list[str]` with
  namespaced prefixes (`model:…`, `input:…`); the field itself is defined in
  the sibling `trading-contracts` package. `app/ibkr_l1_outbox.py` journals
  durable refusal facts (`consumer_refusal`, `fill_sync_refusal`,
  `flatten_refusal`) as `terminal_rejected` effects.
- **Fail-closed gates.** `app/ibkr_l1_capability.py` returns "the exactly-one
  valid, unconsumed capability, fail-closed"; the outbox refuses rather than
  proceeding on ambiguity. `tests/unit/test_alpaca_paper_lab.py` contains
  `test_broker_adapter_is_fail_closed_for_every_mutation`. When a gate refuses,
  the fix is to establish the missing fact, never to relax the gate.
- **Evidence, not "sealed evidence".** The repository has no `sealed`
  vocabulary. What it does have is hash-pinned artifacts, an explicit
  `capability_evidence` distinction between `synthetic_fixture` and
  `live_observed`, journalled broker facts, and JSON evidence files carrying a
  `schema` and `generated_at`. Reports are reproducible from persisted facts:
  identical inputs and the same `--as-of` produce an identical file.
- **Read-only means `mode=ro`.** Every tool that is safe to run against live
  ledgers opens SQLite with `file:<path>?mode=ro`. If you write a new tool
  that reads a ledger, do the same.
- **Historical documents.** `COMPLETION_SUMMARY.md`, `DOCUMENTATION_SUMMARY.md`
  and `STATUS.md` are stale (`STATUS.md` still claims a 75-test suite against
  today's 701). `start_prompt.md` and `prompt.txt` are raw LLM prompts, and
  `prompt.txt` describes an entirely different project. `README.md`,
  `REFERENCE_plugins.md` and this file are the authoritative entry points.

## Do not touch

This section is the most important one in this repository.

**Live trading actions — never, under any circumstances:**

- **Do not submit, modify or cancel a broker order.** Not on paper, not on
  demo, not "just to test".
- **Do not clear, release or override a fail-closed hold.** A halt is a row in
  the ledger's `service_state`; clearing one requires a signed owner resume
  capability and a human decision. `tools/ibkr_resume_after_reconciliation.py`
  is not yours to run.
- **Do not mint, sign, copy, move or delete anything in the owner's capability
  stores** — `~/.config/lts/ibkr-resume-capabilities/` and `~/.lts/`. The
  minting tools are TTY-interactive by design and refuse non-interactive input;
  that refusal is the human-authentication boundary, not an obstacle to work
  around. Do not read `~/.config/lts/*.env` or any mandate file.
- **Do not start, stop, restart, enable or disable any `lts-*` systemd unit,
  and do not kill any running process.** Model runners and timer-driven
  observers are live. If a runner looks wrong, report it; do not act on it.

**Live state — read-only at most, and only with `mode=ro`:**

- The entire `~/.local/state/lts/` tree is live: execution ledgers, venue lab
  databases (some with WAL open), the shadow and monitor ledgers, heartbeats,
  watchdog state and lock files. Never write there, never delete there, never
  open a database without `mode=ro`, and never run a tool that would.
- `~/.local/state/lts/evidence/` is mode 0700 and contains files explicitly
  named private. Do not read them, do not copy them, and never quote them in a
  file committed to this repository.

**This repository is PUBLIC. Never write into any file here:**

- Account identifiers, account fingerprints, broker credentials, API keys or
  tokens.
- Private IP addresses, hostnames or fleet topology. Use `<your-host>`
  placeholders.
- Live stop-loss / take-profit levels, live order evidence, or anything copied
  out of `~/.local/state/lts/evidence/`.

**Do not run these tools** — they are not offline:
`tools/paper_execution_watchdog.py` (opens a TCP socket to the broker gateway
and posts over HTTP), `tools/tws_continuity_monitor.py`,
`tools/fetch_alpaca_closed_bars.py`, `tools/hermes_live_trading_context.py`
(reads live lab databases), and `tools/controller_inventory.py` (shells out
over `ssh` and queries systemd). Every `app/*_lab.py` module contacts a broker.

**Repository hygiene:**

- Running anything from the repository root with default config rewrites
  `./lts_trading.db` and `./app.log` — `app/config.py` defaults
  `database_url` to `sqlite:///./lts_trading.db` and `app/main.py` logs to
  `app.log`. Both root `.db` files are empty husks from a past test run and are
  gitignored, but do not add to the residue. Run experiments from a scratch
  directory or with an explicit config.
- `examples/configs/config_simulation.json` selects the genuinely offline
  `backtrader_simulation_broker`, but it also sets
  `database_url: sqlite:///./lts_trading.db` and `enable_api: true` on port
  8000. Copy it and change both before running it.
- Do not modify committed fixtures under `examples/data/` or `tests/data/`.
- Do not touch sibling repositories. `agent-multi` runs GPU training workers
  and `prediction_provider` serves this stack; changes there are out of scope.
