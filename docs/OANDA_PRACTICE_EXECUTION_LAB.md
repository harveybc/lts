# OANDA Practice Execution Lab

This laboratory measures the broker contract before LTS is allowed to submit
portfolio orders. It is independent of model optimization and is restricted in
code to `https://api-fxpractice.oanda.com`.

## Safety Contract

- Credentials come only from `OANDA_PRACTICE_ACCOUNT_ID` and
  `OANDA_PRACTICE_TOKEN`.
- Account IDs and authorization material are not written to the OLAP.
- The default configuration is read-only.
- An order requires both `orders.enabled=true` and the exact CLI confirmation.
- Every risk-increasing order must attach both stop-loss and take-profit.
- The Practice lab has no live API override.

## Asset Set

The tracked selection is generated from the Project3 OLAP evidence:

- `EUR_USD`: execution control;
- `USD_CAD` 4h: primary long-horizon model shadow;
- `NZD_USD` 1h: primary short-horizon model shadow;
- `GBP_JPY` 1h: activity and diversification shadow;
- `USD_JPY` 1h: activity alternate;
- `EUR_JPY` 4h: historical positive comparator.

OANDA account availability remains authoritative. The preflight records missing
instruments rather than silently replacing them.

## Local Preparation

Create a Practice account and token in OANDA, then place the secrets in a local
file that is never committed:

```bash
install -m 700 -d ~/.config/lts
install -m 700 -d ~/.local/state/lts
install -m 600 /dev/null ~/.config/lts/oanda-practice.env
```

The file contains:

```bash
OANDA_PRACTICE_ACCOUNT_ID=...
OANDA_PRACTICE_TOKEN=...
```

Load it without printing it:

```bash
set -a
source ~/.config/lts/oanda-practice.env
set +a
```

## Phase 0: Preflight

```bash
cd /home/harveybc/Documents/GitHub/lts
conda run -n trading-stack python -m app.oanda_practice_cli \
  --config examples/configs/oanda_practice_execution_lab_v1.json \
  preflight
```

Acceptance requires successful account access, prices, and recorded capability
rows for every intended instrument. A missing instrument must be resolved by an
explicit selection change.

## Phase 1: 24-Hour Read-Only Observation

```bash
conda run -n trading-stack python -m app.oanda_practice_cli \
  --config examples/configs/oanda_practice_execution_lab_v1.json \
  observe --hours 24
```

Report:

```bash
conda run -n trading-stack python -m app.oanda_practice_cli \
  --config examples/configs/oanda_practice_execution_lab_v1.json \
  report
```

Day 1 evaluates instrument availability, quote coverage, p50/p95 spread,
request/session failures, and transaction reconciliation. Profit is not a
meaningful day-one gate because no strategy order is submitted.

Optional user service:

```bash
install -m 700 -d ~/.config/systemd/user
install -m 644 examples/systemd/lts-oanda-practice-observer.service \
  ~/.config/systemd/user/lts-oanda-practice-observer.service
systemctl --user daemon-reload
systemctl --user enable --now lts-oanda-practice-observer.service
```

## Phase 2: Protected Practice Canary

Only after reviewing Phase 1:

1. Copy the config locally and set `orders.enabled` to `true`.
2. Use the minimum account-supported units.
3. Submit one buy and one sell canary during liquid market hours.

```bash
conda run -n trading-stack python -m app.oanda_practice_cli \
  --config ~/.config/lts/oanda-practice-orders.json \
  protected-canary \
  --instrument EUR_USD \
  --side buy \
  --units 1 \
  --stop-distance-pips 10 \
  --reward-risk-ratio 2 \
  --confirmation ENABLE_PROTECTED_OANDA_PRACTICE_ORDERS
```

Acceptance requires order acknowledgement and attached SL/TP. Any rejection or
orphaned protection blocks the next phase.

## Phase 3: Seven-Day Portfolio Shadow

Run read-only observation continuously while the selected model artifacts emit
shadow decisions. Canary execution remains separately gated. The weekly report
adds actual spread cost, implementation shortfall, financing, protected-order
acceptance, execution-adjusted weekly return/RAP, and backtest-to-Practice
drift.

One week is an execution-calibration sample, not sufficient evidence to promote
or reject alpha. It identifies incorrect assumptions in data timing, sizing,
broker semantics, costs, and reconciliation before longer Practice operation.

## OLAP

Default database:

```text
~/.local/state/lts/oanda-practice-lab.sqlite
```

Analytical views:

- `practice_price_summary_olap`
- `practice_execution_summary_olap`

Raw facts include sessions, instrument capabilities, account snapshots, quotes,
broker transactions, order intents, and execution reports. Transaction IDs make
the collector restart-safe and idempotent.
