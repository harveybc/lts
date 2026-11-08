# Forward pool capability matrix

Date: 2026-11-08. Discovery base: `lts` `12bce5f2c4fe2ab0e6a7433d7208d9e05a9cffaa`.
Assignment: `agent-multi` `e6f787a107c6b0eeac1117ea3188c6f1f8a4d32a`.
This inventory did not open a broker session, read a live ledger, or change a unit.

## Reuse and refusal

| Component | Decision |
| --- | --- |
| `app/live_model_selection.py` | Hash-pinned pointer pattern only. Its linear loader is not the categorical policy. |
| `app/live_sac_selection.py` | Refused. Continuous SAC thresholds are not mapped onto NOP/BUY/SELL. |
| `app/project3_sac_observation.py` | Refused. Rolling z-score and agent state are not the selected70 recipe. |
| `app/model_position_control.py` | Reused. Opposite signal closes; same direction holds; no one-step reversal. The integer `command_for` must agree or the step stops with `CONTRACT_DISAGREEMENT`. |
| `app/ibkr_l1_journal.py` | Pattern reused for pending, unknown and acknowledged effects. The pool journal is not a second operational database and does not write `DemoExecutionOlap`. |
| `app/live_flatten_custody.py` | Not integrated. This delivery does not take custody of a venue position. |
| `app/multi_venue_shadow.py` | Not treated as proof of independent policy accounts. The pool keeps one virtual ledger per candidate. |
| `tools/rolling_evidence_report.py`, `tools/live_sim_replay.py` | Left unchanged. Divergence records are content-addressed JSON beside the isolated state directory. |

## Venue gaps

| Route | EURUSD contract | State |
| --- | --- | --- |
| MT5 | Shorting, leverage 1:50, mandatory SL/TP and finalized hourly bars were not re-verified in this delivery. | Disabled. |
| Alpaca | The existing crypto/equity paper path is not this FX contract. | Disabled. Crypto is not substituted. |
| IBKR paper | An audited paper path exists. This delivery does not attach it and does not mint a capability. | Disabled until the owner capability procedure. |

## Contract resolutions

These are explicit. They are not silent edits of the checkpoint.

| Topic | Exported text | Resolution used here |
| --- | --- | --- |
| Activity | Recipe text `orders_per_week >= 2.0` | Assignment requires strictly more than two. Exactly 2.0 is ineligible. |
| Reversal | Recipe evaluation says `CLOSE_AND_OPEN` | Absolute interface and the epoch-0 tape have zero reversals. Command 3 closes and does not reopen on that bar. |
| Levels | Recipe evaluation says `clip` | Absolute preparation returns clipped false and refuses a bad level. Deployment refuses. |
| Daily cap | `max_entries_per_day` is null | Not reintroduced. |
| Tie | Native `argmax` | First maximum wins. A BUY/SELL tie above NOP selects BUY. |
| Command 3 | Codec `NOP_BUY_SELL_CLOSE_OPPOSITE_V1` | Network outputs are 0/1/2. Command 3 is the controller close. |

## Integration corrections

Review `0447d6ab` reproduced F01–F04. The production registration uses the 70 recipe columns. A missing network call is `INFERENCE_ABSENT`. An entry without a bound planner is `PLAN_ABSENT`. Route effects, the unknown halt and close history reload from the isolated state file. A transport that dies after the journal is restored as `effect_unknown`. Executor cash is not copied from a shadow ledger. A position opened on a completed bar is not stopped by that bar's earlier high or low.

The native `entry_plan` / `propose` calculation is not executed inside this service. The bound planner is an adapter slot. Full TRAIN and VALIDATION fill replay is not claimed. `SHADOW_OBSERVING` stays false.

## Selected70 feed plugin

`app/forward_pool/selected70_feed.py` is the consumer plugin. It reads the 70 store columns, applies a consumer `DIFF` only when the recipe asks for one, z-scores with the fixed TRAIN constants, and builds the 13-by-70 stride-2 window. The pinned recipe has an empty consumer operation on every column: `LOG`, `ASINH` and `DIFF` in the column names were already applied in the store. Learned columns stay with their producers: `activity` for `act.p_hit`, `kalman` for `kf.lc.level_minus_close`, and `gmm` for the four `rg.gmm` columns. Layer-1 and layer-2 pools publish the rest. A price-only MT5 bar is not a substitute for those columns, and a missing value is not filled with zero.

`tools/forward_pool_selected70_feed.py` compared every window with the production `FrozenRecipeInputs` loader. The maximum absolute difference was 0 on 30859 rows (30234 valid) after 257 leading non-finite store rows were dropped. It then loaded checkpoint `079bef0ea6f3b9327772f4204e5d5dbbd4be69a5ab58c32b424aa3cb7c686404` and called greedy `surfaces` on CPU. The last finalized bar closes at `2024-12-31T21:00:00Z`. That call is `HISTORICAL_REPLAY`. There is no current-hour row, so `SHADOW_OBSERVING` stays false and no order was submitted.

## Not claimed

- `CODE_VERIFIED` is true only after the named acceptance tests and the feed unit tests pass.
- `SHADOW_OBSERVING` is false. The attached store ends at `2024-12-31T21:00:00Z`.
- `PAPER_DEMO_EXECUTING` is false.
- The published TRAIN 11.51453% and VALIDATION 13.80849% figures stay offline references on the original artifact. This delivery does not replay the 748-order tape through `AbsoluteBarEnv`.
- Real capital is rejected at construction.
