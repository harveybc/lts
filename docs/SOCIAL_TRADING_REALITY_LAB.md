# Social-Trading Business Reality Lab

Status: provider-neutral accounting and allocation vertical implemented;
external social-platform commissioning pending
Decision date: 2026-08-01

## Purpose

The lab learns how copy, signal, PAMM, MAM and investable-provider products
behave before LTS accepts customer capital or publishes a strategy. It is not
a marketing exercise and does not treat a platform's demo P&L as proof of
alpha.

The first implementation is deliberately broker-neutral and cannot place an
order. It captures the business invariants that every future adapter must
expose:

- investor subscriptions, deposits, withdrawals and equity;
- unitized pooled accounting;
- investor-level high-water marks;
- performance and management fees;
- manager capital and fee balances;
- proportional copy sizing, precision and rejection reasons;
- copied-versus-provider exposure and tracking error;
- native or account-local SL/TP capability;
- platform, account, instrument and jurisdiction limitations.

Machine-readable sources:

```text
examples/configs/social_trading_platform_registry_v1.json
examples/configs/social_trading_accounting_scenario_v1.json
```

Execution and report:

```bash
lts-social-trading-lab registry \
  --registry examples/configs/social_trading_platform_registry_v1.json

lts-social-trading-lab run-scenario \
  --scenario examples/configs/social_trading_accounting_scenario_v1.json

lts-social-trading-lab report \
  --database ~/.local/state/lts/social-trading-lab.sqlite
```

The SQLite views `social_lab_run_olap` and `social_lab_event_olap` preserve
scenario hashes, registry hashes, event outcomes and the final ledger. Every
run records `orders_submitted=0`.

## Platform Decision Matrix

| Platform | Immediate question | Automation | Capital | Protected-order consequence |
| --- | --- | --- | --- | --- |
| MQL5 Signals | Future live-only signal/provider workflow | Platform-managed | Real account only | MT5 build 4150 disabled Signals on demo accounts; no current demo experiment |
| cTrader Open API demo | Custom copier, broad broker capabilities, latency and account-local protection | Official Python API | Demo | LTS must create and reconcile local SL/TP on every copied entry |
| cTrader Copy demo investor | User experience, equity-to-equity sizing, fees, missed trades and reallocation | Platform-managed | Demo investor | Native Copy does not copy provider SL/TP; observation only under our strict rule |
| Darwinex Zero | Provider track record, investable index, risk transformation, capital allocation and performance fees | MT4/MT5/TradingView | Paid virtual membership | Provider policy can retain protected entries; investor exposure is mediated by the DARWIN layer |
| eToro Virtual CopyTrader | Manual copy UX, proportional holdings, copy open trades, pause/stop and reallocation | Manual control | Free virtual | Copy-level loss controls are not per-order SL/TP; no LTS route |
| HFM PAMM | True pooled manager capital, investor allocations, success fees, rollover and withdrawals | Platform-managed | Live manager capital | Deferred until legal review and a platform-specific protection test pass |

Opening another generic demo account has low value unless it contributes a
new asset class, execution contract, social mechanic or independent control.
The registry records the experimental role before any credential or capital is
committed.

## Immediate Commissioning Order

1. Keep the OANDA MT5 demo as a read-only venue-reality stream. Do not create
   another MQL5 identity: MetaQuotes disabled Signals on demo accounts in
   build 4150 and also removed free/private signal creation.
2. Complete the cTrader demo account under the existing cTID. Use cTrader
   Copy as an investor in one eligible free strategy, then register an Open API
   application for the custom protected-copy experiment.
3. Create an eToro account only for its free Virtual Portfolio. Capture the
   copy-open-trades, add/remove funds, pause, stop and reallocation behavior
   manually; do not build an execution adapter.
4. Review Darwinex Zero country eligibility, current membership price and
   terms. Subscribe only after the owner approves the recurring cost. If
   approved, run the same frozen protected strategy and compare the platform's
   risk/index transformation with our source account.
5. Do not fund a PAMM manager yet. First reproduce its allocation, HWM,
   performance fee, rollover and withdrawal rules in the local ledger. A live
   PAMM pilot requires legal/account review and a separate capital limit.

Credentials never enter this registry, Git, chat, portable OLAP or the DOIN
chain. External account creation and terms acceptance remain owner actions.

## Owner Walkthrough State, 2026-08-01

- cTrader: cTID and the automatically provisioned Spotware demo are active in
  cross-broker cTrader Web (`EUR 1,000`, leverage `1:100`). This account is
  sufficient for Open API preflight. Copy catalogue visibility and account
  type confirmation remain pending; no broker onboarding is required now.
- eToro: account and Virtual Portfolio opened. Asset inventory and copy-control
  walkthrough remain pending; no real deposit or automated adapter is needed.
- Darwinex Zero: on hold. Registration requires selecting a paid product and
  charging the first recurring subscription.
- OANDA MT5: demo and read-only bridge are operational. MQL5 Signals is
  intentionally unavailable on demo accounts, independently of MQL5 Community
  email identity.

## Business-Reality Feedback Loop

```text
paper/social platform facts
        |
        v
normalized execution + investor OLAP
        |
        v
gap and counterfactual analysis
        |
        v
simulator/fitness/genome correction
        |
        v
DOIN optimization and frozen artifacts
        |
        v
shadow -> protected canary -> social demo
```

Each observed defect becomes a named variable or invariant when possible:

| Live fact | Research/optimization feedback |
| --- | --- |
| copied volume rounded or rejected | lot-step allocator, missed-exposure penalty, minimum useful capital |
| unavailable instrument | venue/asset eligibility mask and cash fallback |
| provider/subscriber price divergence | slippage and tracking-error distribution |
| lower subscriber leverage | margin headroom feature and rejection model |
| deposits/withdrawals trigger reallocation | flow-aware portfolio transition cost |
| delayed copy close | tail-loss and protection-gap penalty |
| fee crystallization | after-fee RAP and investor-specific HWM state |
| provider risk transformation | source-to-investor beta/tracking model |
| rollover forces partial closes | liquidity and withdrawal stress scenario |

No observation mutates an active DOIN chain. It first becomes a versioned
dataset/config change, passes deterministic tests, and enters a new campaign
or curriculum stage.

## Required OLAP Facts

External social commissioning extends the current ledger with:

- platform, legal entity, account environment and experiment role;
- provider strategy and subscriber/copy identity hashes;
- manager and investor capital, units, NAV and free margin;
- deposits, withdrawals, subscriptions, pauses and stops;
- provider intent time, provider execution, copy receipt and subscriber fill;
- raw and rounded volume, exposure ratio and rejection reason;
- provider/subscriber prices, spread, slippage and tracking error;
- source and subscriber SL/TP state at entry and throughout the lifecycle;
- gross P&L, all fee types, after-fee P&L and high-water mark;
- platform risk transformation, leverage and margin state;
- strategy availability/ranking state and provider revenue;
- exact terms/config/code/terminal versions and evidence timestamps.

Personal data, raw account identifiers and credentials stay outside portable
OLAP. Stable pseudonymous hashes are sufficient for analysis.

## Acceptance Gates

### Social observation eligible

- demo/virtual or explicitly approved live account;
- current official terms captured in the registry;
- no hidden order authority in a read-only adapter;
- account/strategy identity pseudonymized;
- cash flows and fees reconcile with the platform UI.

### Protected social execution eligible

- every copied risk-increasing entry has broker-side SL and TP in the
  subscriber account;
- volume and leverage differences are known before submission;
- duplicate, stale and missing signals fail closed;
- source close, subscriber close and emergency local close reconcile;
- maximum tracking error, daily loss and subscriber exposure are capped;
- a minimum-size protected demo canary passes.

### Provider/business pilot eligible

- protected execution and seven-day social shadow pass;
- after-fee investor metrics and drawdown are published honestly;
- legal, tax, suitability, marketing and country requirements are reviewed;
- fee, support, disclosure, incident and strategy-retirement policies exist;
- real capital and public publication require separate owner approval.

## Official References

- cTrader Open API: https://help.ctrader.com/open-api/
- cTrader Copy FAQ: https://help.ctrader.com/ctrader-copy/faq/
- cTrader Copy investing: https://help.ctrader.com/ctrader-copy/investing-in-strategies/
- MQL5 Signals rules: https://www.mql5.com/en/signals/rules
- MQL5 provider agreement: https://www.mql5.com/en/signals/terms/provider
- MT5 build 4150 release: https://www.mql5.com/en/forum/459335
- Darwinex Zero overview: https://www.darwinexzero.com/docs/what-is-darwinex-zero
- Darwinex Zero assets: https://www.darwinexzero.com/assets
- eToro CopyTrader: https://www.etoro.com/en-us/copytrader/
- HFM PAMM program: https://pamm.hfm.com/int/en/pamm-accounts/program
- HFM manager FAQ: https://pamm.hfm.com/int/en/fund-managers/fund-managers-faqs
