# M05: modular paper adapter for the existing LTS routes

Satoshi, successor technical lead. Written 2026-09-30.
Orders: lane M05 of `predictor/docs/handoffs/SATOSHI_MODULAR_OPTIMIZATION_2026_09_30.md`
(predictor `dc72170e`, §3 and the last paragraph of §6) and plan
`docs/tres_temas_entrevista/program_v3/MODULAR_STACK_WORK_PLAN_2026_09_30.md`, acceptance row MS15.

## Summary

1. **Recorded inputs replay through the adapter end to end: `REPLAY_PASS`.** The recorded
   Alpaca IEX SPY 1d closed-bar file (1,511 bars) went through the feature side, a
   hash-pinned modular model and the action side at 12 as-of points. The window the model
   receives is bit-identical to predictor's float32 tensor on all 12 points. This holds
   even though the features were computed by two independent implementations. The two
   outputs differ from predictor's by at most 1.5e-7 (forecast) and 3.9e-7 (bottleneck).
   A second pass reproduces every digest. Rewriting every bar after the as-of point
   leaves the observation unchanged. Rewriting one bar inside the window changes it.
2. **The paper smoke ran read-only: `SMOKE_PASS_SHADOW_ONLY`.** It made one HTTP GET, to
   `/v2/stocks/SPY/bars`, through the Paper observer's GET-only client. That client's
   session refuses every other verb before anything leaves the host. The smoke called no
   account, position or order endpoint and wrote no ledger. `orders_submitted: 0` and
   `execution_authorized: false`.
3. **Boundaries stayed compatible.** In an installed isolated environment, the named
   offline route tests passed 30/30 on `main` and 30/30 again with this branch, and the
   prediction_provider mechanics suite passed 19/19. The same branch cherry-picked onto
   the live checkout's commit also passes (see Suites). New regressions prove that
   nothing modular can enter the linear runner, and that the modular policy refuses a
   linear observation.
4. **Not a promotion.** The candidate is a local smoke fit. Its validation skill over the
   zero forecast is 0.5%, which is no skill, and all 12 replayed actions are `hold`.
   Forecast error is not trading evidence. Promotion waits for M04/M06 model evidence.
   Real-money deployment is not part of this lane.

## What was missing and what now exists

The Alpaca SPY 1d runner selects only `prediction_provider.live_linear_manifest.v1`
through `SelectedLinearPolicy`, and calls `policy.predict(build_closed_bar_features(...))`.
No runner had a way to consume a modular Keras model (`99184e2`, whose preflight is
cherry-picked here as `0e63a5a`). This branch adds that path as a file contract, per the
coupling survey: model archive, contract JSON and bars CSV. It never imports predictor's
packages. `lts` has its own top-level `predictor_plugins`, and installing predictor here
would shadow it.

| File | Role |
|---|---|
| `app/modular_inference_adapter.py` | Contract validation; feature side (`normalize_bars`, `causal_align`, `build_observation`); action side (`map_action`); inference side (`ModularPolicy`) |
| `tools/modular_paper_replay.py` | Recorded-input replay with golden parity, determinism, a two-sided causality probe and a no-execution check; writes a receipt |
| `tools/modular_paper_smoke.py` | Gated read-only smoke: contract → matching replay receipt → fresh observer ledger (opened `mode=ro`) → one GET through a guarded session → shadow inference |
| `tools/candidate_paper_preflight.py` | A `family: modular` handoff that carries a verified `inference_contract` now reaches `paper_path: shadow_inference_only` (exit 2, never runner-compatible). Without a contract the old refusal is unchanged. The tool now resolves `app` from its own checkout |
| `tests/unit/test_modular_inference_adapter.py`, `tests/unit/test_candidate_paper_preflight.py` | Behaviour tests (below) |

Predictor side (branch `satoshi/m05-paper-adapter-20260930` off `556c5f3e`):
`tools/export_modular_paper_candidate.py` fits and exports the candidate described next.

## The versioned contract, `lts.modular_inference_contract.v1`

- **Engine identity.** The contract gives a path and SHA-256 for
  `predictor_plugins/modular_temporal.py` and records the source commit. The adapter
  loads exactly that file under a unique module name. There is no fallback. The
  archive's weight hash is re-derived after loading.
- **Time contract.** It declares the session grid, the nominal `sample_hours`, `window`,
  `bar_close_offset_hours` (a bar enters only once its close is at or before `as_of`)
  and `max_gap_hours`. It also declares one native-grid `bars` stream. Every other
  stream must declare `alignment: asof` with a staleness bound, and it enters only
  through `causal_align`. That transform admits no sample whose `available_at` is later
  than the grid right edge, and refuses stale or missing data rather than filling it.
  An undeclared or differently aligned stream refuses. A test shows that a stream of
  equal length shifted one minute into the future is refused, not lined up: equal
  shapes are not accepted as aligned times. `window`, the ordered feature names and
  `sample_hours` must equal the model's own configuration.
- **Features.** `lts.modular_features.closed_bars.v1` defines `log_return`,
  `range_fraction`, `log_volume_z(window)` and `asof_field(stream, field)`. The scaler
  must be marked `train_partition_only`.
- **Outputs.** The forecast head is `(horizons, targets)` with its de-scaling factor.
  The encoder bottleneck is `rank: 3`, shape `[output_steps, output_channels]` = `[6, 8]`.
  This is MS15: the representation is exported and hashed in every inference. The policy
  adapter that would consume it, with its reward and early-stop validation, is a
  separate owned integration and does not exist here.
- **Action.** `lts.modular_action.v1` is a forecast dead-band (long/short/hold). It is a
  declared mapping, not tuned for profit.
- **Evidence.** `research_validated`, `live_inference_eligible` and
  `live_execution_eligible` must be declared. Any `execution_authorized` value other than
  `false`, anywhere in the contract, refuses. Every adapter output carries
  `execution_authorized: False` and `tier: shadow_inference_only`.

M01 pushed an untested interface preview, `feedfd00`, with `save_bundle`/`load_bundle`
(canonical config + weight hash + output parity). This contract carries the same three
bindings, so it should converge on that API. When M01's tested tip lands: re-export,
re-run golden parity, and record both tips in the contract. Until then everything is
built against `556c5f3e`.

## Measured candidate (local smoke, not promoted)

| Item | Value |
|---|---|
| Model | `modular-spy-1d-local-smoke-v1`; engine defaults; 3 features, one branch each; window 24; bottleneck (6, 8); horizon 1 |
| Data | Read-only snapshot of the recorded `alpaca_iex_spy_1d.csv` (sha256 `ea36281f…`, 2020-07-27 to 2026-07-31) |
| Split | Chronological: 1,027 train / 220 validation (2024-10-24 to 2025-09-11) / 221 test windows **untouched** |
| Fit | MAE loss, Adam 1e-3, early stop patience 6, best-weight restore; 7 epochs run, best epoch 1 |
| Validation MAE (log return) | model 0.007595 · zero forecast 0.007632 · persistence 0.011559 · skill vs zero 0.0048 |
| Contract sha256 | `60a55af072180631b755efe6627407b77df70b59e3e03c72906eb2d3042f9bff` |
| Archive sha256 | `0048df865ba0e6e6…` |

Best epoch 1 and a skill of 0.5% mean the fit has learned essentially nothing beyond
the zero forecast. This candidate is plumbing for the adapter. It says nothing about the
modular architecture, and nothing about trading.

## Evidence (outside every repository)

`$HOME/Documents/GitHub/.runtime/m05-paper-adapter-20260930/`: `recorded/` (read-only
bar snapshot), `candidate-v1/` (read-only; contract, archive, metrics, golden,
provenance, handoff), `replay/replay_receipt.json` (sha256 `6f039431…`, `REPLAY_PASS`),
`replay/replay_receipt_attempt1_REPLAY_FAIL_float64_vs_float32.json`,
`preflight_alpaca_spy.json`, and `smoke-1/` (`smoke_record.json`, `inference.json`,
`observed_closed_bars.csv`: 103 bars to 2026-09-29).

The first replay failed on purpose. It compared the adapter's float64 window with the
float32 tensor predictor had actually fed the model: a difference of 5.7e-8 against a
1e-9 tolerance. The failed receipt is kept. The check now compares the float32 tensor
the model receives, which matches exactly, and it still reports the float64 difference.

The smoke's newest bar is 2026-09-29, not 09-30. It keeps the runner's own rule that a
daily bar dated today (UTC) is not closed. That rule is conservative and is kept rather
than relaxed.

## Suites

| Suite | Environment | Result |
|---|---|---|
| Named route tests (`test_csv_workflow_e2e`, `test_backtrader_broker`, `test_live_model_selection`, `test_mt5_model_runner`, `test_alpaca_model_runner`), before, on `main` `21bab75` | `trading-stack` | 30 passed |
| The same, plus preflight and adapter tests, after, on this branch | isolated venv (`--system-site-packages` from `trading-stack`, this worktree `pip install -e --no-deps`, mechanics installed non-editable from prediction_provider `316c315`) with `LTS_MODULAR_ENGINE` set | 54 passed, 0 skipped |
| Adapter and preflight tests without the engine | `trading-stack` | 22 passed, 2 skipped (TensorFlow section: reason stated) |
| prediction_provider `mechanics/tests` | isolated venv | 19 passed |
| Live lineage: the same named tests plus `test_mt5_symbol_model_compat_preflight`, before on `12bce5f` / after with both M05 commits cherry-picked cleanly (scratch `7899fe4`) | `trading-stack` | 66 passed / 90 passed (66 + 24 new), 0 failed |

Every child ran through `crispdm-run` with a fresh admission, on CPU
(`CUDA_VISIBLE_DEVICES=''`), with caps of 4G (export), 3G (TensorFlow runs) or 1G. The
export queued behind the Traffic cell's reservation. No cap was lowered.

## Deployment (prepared, not activated)

The live runner resolves code through `WorkingDirectory` on the mutable `lts` checkout
with `Restart=always`. That checkout was not switched and nothing was merged beneath its
timers. No service was restarted. The pinned tree is written outside every repository,
under `~/.local/state/lts/pinned-deploys/m05-modular-shadow-<commit>/`: the tree, a
`MANIFEST.json` with per-file sha256, and `ACTIVATION.not-installed.conf`. It
**must not** replace the runner's tree, because it is based on `main` and lacks the 47
commits that the live checkout carries. The only activation it describes is a new,
separate, read-only oneshot unit that runs the smoke. That activation needs its own
authorization.

## Not done, refused or not measured

- No runner consumes `ModularPolicy`. `SelectedLinearPolicy` and the runners are unchanged.
  Wiring a modular tier into a runner is a promotion decision that waits for model
  evidence and an explicit route/tier ruling.
- The MT5 and IBKR routes have no modular smoke. MT5 still needs its attested bar and
  bridge preflight.
- The candidate is not a finalist. Its test partition was never evaluated.
- The quote stream the observer records is crypto-only, so no mixed-frequency stream
  feeds this candidate. Mixed-frequency alignment is exercised by the unit tests only.
- The predictor exporter has no unit test of its own. Its correctness is carried by the
  replay's golden parity against an independent LTS implementation.
- The candidate's `provenance.status: verified` is a local hash binding
  (`scope: local_hash_binding_only`). It is not an independent review.

## Addendum: compatibility on the live lineage

The live checkout is on `satoshi/mt5-unknown-outcome-20260926` (`12bce5f`), not on
`main`. Both M05 commits (`0e63a5a` and `c0a119c`) cherry-pick onto it without conflict,
because they only add files and extend the preflight. The named route suites passed 66
before and 90 after, with the engine test section active. The cherry-pick lives in a
disposable scratch worktree. Nothing was committed to, checked out in, or merged into
the live checkout.

Satoshi, successor technical lead, 2026-09-30.
