# Cost and sizing sheet: ETH 4h MT5 demo route, EURUSD paper route, paired harness

Written for front E, owner order §3 row E. Read-only: every value is copied from an
EXISTING config, risk block or code path. **No limit was added and no config was
changed.** Hosts are named by role. No account identifier is reproduced; the
fingerprint fields are omitted on purpose. Each SHA-256 is of the file as read on
2026-10-01.

Legend: **[PLACEHOLDER]** means the value is a placeholder, an example, a disabled
template, or a default that no deployed config sets. **[DIVERGES]** means the deployed
value differs from the repository template.

## 1. ETH 4h, MT5 demo route (the route a modular ETH model would enter in shadow)

| Item | Value | Source (sha256) |
|---|---|---|
| Environment / tier | `demo`; `execution_tier: demo_research_canary` (linear model today) | worker_b `~/.config/lts/mt5_eth_model_runner_v1.json` (`6fdf6876…f960`), the deployed runner |
| Symbol / timeframe | `ETHUSD`, `4h` | same |
| Stop rule | `strategy.stop_fraction = 0.01` (1% of the mid reference price, rounded to the symbol's digits) | same; code `app/mt5_model_runner.py` (`33b8cb8b…`) |
| Take-profit rule | `strategy.take_profit_fraction = 0.02` | same |
| Reference price | mid = (bid + ask) / 2 from the latest MT5 snapshot. The spread is observed and is NOT added as a cost. | `app/mt5_model_runner.py` |
| Risk at stop | `service.risk_fraction_at_stop = 2e-05` of equity | deployed config |
| Overshoot allowance | `service.max_overshoot_ratio = 0.5` (a venue minimum may exceed the risk target by at most 50%) | deployed config |
| Gross notional cap | `service.gross_notional_fraction_max = 0.003` of equity | deployed config |
| Margin cap | `service.margin_fraction_max = 0.003` of equity; `margin_rate = 1.0` in the capability snapshot | deployed config; `app/mt5_model_runner.py::_capability` |
| Daily loss budget | `service.daily_loss_budget_fraction = 0.0002` **[DIVERGES]**: the repository template `examples/configs/mt5_eth_model_runner_v1.json` (`149f5592…`) has `0.00008` | deployed vs template |
| Concurrent positions | `service.max_concurrent_positions = 1` | deployed config |
| Signal age | `service.signal_max_age_seconds = 28800` | deployed config |
| Position size | `units = min(E·2e-05 / stop_distance, E·day_left / stop_distance, E·0.003 / P, E·0.003 / (P·1.0))`, rounded **down** to `volume_step`. A venue minimum is allowed only if it breaches no cap and the implied risk is ≤ 1.5 × 2e-05; otherwise the order is skipped. | `app/demo_execution_service.py::plan_units` (`32e38e97…`) |
| Volume ceiling | bridge `max_volume = 0.01` lots per command | worker_b `~/.config/lts/mt5_execution_bridge.json` (`b5ef4279…`); identical to the template `examples/configs/mt5_execution_bridge_demo_v2.json` (`b5ef4279…`) |
| Command budget | bridge `max_open_commands_per_day = 4` | same |
| Commission per side | **not modelled in LTS sizing.** The broker's actual charges appear only in the fills recorded by the bridge. | `app/mt5_model_runner.py`; no cost field in any ETH config |
| Swap / financing | **not modelled.** No swap field exists in the ETH route; realised swap appears only in broker fills. | same |
| Slippage | not modelled; market fills are recorded as filled | same |
| Dormant modular shadow config | `examples/configs/mt5_eth_4h_modular_shadow_DORMANT.json` (`a8358c7c…`) reuses the template's service block **verbatim**. That means daily loss budget `0.00008` **[DIVERGES from the deployed 0.0002]** and an account placeholder **[PLACEHOLDER]**. The shadow tier never sizes or queues anything. | lts `db9650f` |

## 2. EURUSD paper route (IBKR L1)

| Item | Value | Source (sha256) |
|---|---|---|
| Environment / instrument | `paper`, `EUR.USD` | `examples/configs/ibkr_l1_canary_profile_v2.json` (`d2ec083a…`) |
| Quantity ceiling | `quantity_ceiling = 20000.0` (base-currency units) | same |
| Orders per activation | `max_orders_this_activation = 2` | same |
| Stop distance cap | `stop_distance_price_max = 0.002` | same |
| Take-profit distance cap | `take_profit_distance_price_max = 0.004` | same |
| Spread gate | `max_spread_price = 0.0003` (the outbox refuses wider quotes); code default `0.01` | same; `app/ibkr_l1_adapter.py`, `app/ibkr_l1_outbox.py` |
| Service risk block | `risk_fraction_at_stop 0.005`, `max_overshoot_ratio 0.25`, `gross_notional_fraction_max 0.1`, `margin_fraction_max 0.1`, `daily_loss_budget_fraction 0.02`, `max_concurrent_positions 3`, `signal_max_age_seconds 300` **[PLACEHOLDER]**: from `ibkr_l1_runner.example.json` with `enabled: false` and `owner_issuer_allowlist: ['owner-1']`, an example rather than a deployed block | `examples/configs/ibkr_l1_runner.example.json` (`074981b4…`) |
| Commission / swap | **not modelled** in the L1 sizing path; the realised values come from broker reports | code |
| Deployed EURUSD model runner | **none found.** The deployed IBKR model runner is USDCAD (`ibkr_usdcad_model_runner_v1.json`), not EURUSD. | `examples/configs/` listing |

## 3. Paired harness (heuristic-strategy `app/paired_backtest.py`, `daaac1f3…` @ `64ab2f9`)

Every field below is lane G's own specification (`rl_temporal/reconciliation.py::reconcile_episode`, agent-multi `02db0701`). None is taken from a route above.

| Field | Value |
|---|---|
| `commission` | 0.001 of notional **per side** (backtrader percentage commission) |
| `slippage` / `spread` | 0.0 / not modelled |
| `financing_enabled` | false |
| `fill` | the decision after bar t closes fills at bar t+1 OPEN |
| `initial_cash` | 10,000 |
| `position_units` | 1.0 asset unit per entry, no leverage declared |
| Stop / take profit | heuristic rule in price fractions: TP = price ± 0.9 × predicted move, SL = price ∓ 2.0 × max(predicted adverse move, 0.25% of price); exits on the close only |
| Entry threshold | predicted favourable move ≥ 0.5% of price (`HeuristicParams`, frozen) |

## 4. Gaps this sheet makes explicit (none is acted on here)

1. **Sizing parity.** The harness (and lane G's RL episodes) trade **1 ETH unit on 10,000
   cash**. That is roughly the full equity in notional at 2024 prices. The demo route
   caps gross notional at **0.3% of equity** and volume at **0.01 lot**. Harness returns,
   drawdowns and turnover are therefore **not** what the demo route would realise; they
   are a paired comparison only.
2. **Cost parity.** The harness charges 0.001 per side and no spread or swap. The demo
   route models none of these in sizing; the broker's real spread, commission and swap
   appear only in fills. A cost-calibrated replay would need those fills and is not done
   here.
3. **Template drift.** The deployed ETH daily loss budget (0.0002) differs from the
   template (0.00008). The dormant shadow config inherited the template value. It never
   sizes, but the drift is recorded.
4. **EURUSD.** No deployed EURUSD model route exists. The only EURUSD risk block is a
   disabled example.

Satoshi, successor technical lead, 2026-10-01 (UTC).
