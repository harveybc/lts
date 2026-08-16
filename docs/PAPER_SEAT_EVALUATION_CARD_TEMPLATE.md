# Paper/Demo Seat Evaluation Card

Use one immutable card per seated model and evaluation window. This is an
integration and business-reality record, not evidence of live profitability.
Unknown facts stay `unavailable`; zero must come from direct venue evidence.

## Identity

| Field | Value |
| --- | --- |
| Card ID / generated UTC | `<id>` / `<timestamp>` |
| Venue / environment | `<venue>` / `paper-or-demo` |
| Account | `<redacted fingerprint>` |
| Instrument / timeframe | `<symbol>` / `<bar horizon>` |
| Model ID / artifact SHA-256 | `<id>` / `<sha256>` |
| Observation-contract SHA-256 | `<sha256>` |
| Strategy-config SHA-256 | `<sha256>` |
| Code revision | `<repo>@<commit>` |
| Window start / end UTC | `<timestamp>` / `<timestamp>` |

## Evaluation Contract

- Historical role used for the frozen artifact: `<train/validation/test role>`.
- Paper window is forward-only and selected before its outcomes were known:
  `<yes/no plus evidence>`.
- Cost model used by the paired simulation: commission `<value and unit>`,
  spread `<value and unit>`, slippage `<value and unit>`.
- Signal comparison key: `(artifact_sha256, instrument, due_bar_utc)`; never
  join simulation to venue effects by collection clock alone.
- Every risk-increasing order requires native SL and TP at submission:
  `<direct evidence reference>`.

## Same-Window Results

Always name the horizon and unit. Do not place a weekly value beside an annual
value without also reporting both on the same scale.

| Metric | Paired simulation | Paper/demo | Residual |
| --- | ---: | ---: | ---: |
| Due bars expected / matched | `<count>` | `<count>` | `<count>` |
| Signals long / short / hold | `<counts>` | `<counts>` | `<counts>` |
| Orders submitted / filled / rejected | `<counts>` | `<counts>` | `<counts>` |
| Closed trades | `<count>` | `<count>` | `<count>` |
| Return over this exact window (%) | `<value>` | `<value>` | `<paper-sim>` |
| Maximum drawdown over this window (%) | `<value>` | `<value>` | `<paper-sim>` |
| Realized costs over this window (account currency) | `<value>` | `<value>` | `<paper-sim>` |
| Median decision-to-ack latency (ms) | `not-applicable` | `<value>` | `not-applicable` |
| Missing/stale decision bars | `<count>` | `<count>` | `<count>` |

If a risk-adjusted metric is included, state its complete formula, input
horizon and annualization convention next to the value. Raw return, drawdown,
trade count and costs remain mandatory.

## Safety and Operations

| Check | Direct result |
| --- | --- |
| SL and TP present on every opened exposure | `<pass/refuse/unavailable>` |
| Maximum size respected | `<pass/refuse/unavailable>` |
| Foreign orders/positions excluded | `<pass/refuse/unavailable>` |
| Holds / recovery actions / reconnects | `<counts and evidence>` |
| Runner availability during window (%) | `<value>` |
| Broker or terminal outage time (minutes) | `<value>` |
| Duplicate effect attempts / duplicate fills | `<counts>` |
| Final direct open orders / positions | `<counts>` |

## Disposition

- What the window demonstrates: `<integration or divergence claim only>`.
- What it does not demonstrate: `real-capital profitability, population-level
  superiority, or performance outside the named window`.
- Simulation correction proposed from observed residuals: `<none or bounded
  proposal>`.
- Promotion/succession eligibility: `<eligible/refused/unavailable>` with the
  executable predicate and evidence reference; a green runner alone is never
  eligibility.
- Next fixed window or owner action: `<action>`.

