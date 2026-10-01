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

## Addendum 2: re-export against M01's tested tip `64a91a74`

I re-exported once, as the coordinator ordered, on the secondary worker (worker_b). It
ran in its pinned campaign environment `envs/tensorflow` (Python 3.12.13, TF 2.21.0,
Keras 3.13.2), never trading-stack's Keras 3.15. The engine tree is a `git archive` of
predictor `64a91a74b5ab168b31eb58b58c575fd45dc66c61`, with the exporter from this lane
(predictor `a3218b78`) overlaid. The data, seed and recipe match the `556c5f3e` export.
Every child ran through `crispdm-run` (4G export, 3G replay, 2G/1G for the rest) with
CUDA hidden.

| Item | `556c5f3e` export (v1) | `64a91a74` export (v2) |
|---|---|---|
| Engine sha256 | `2daa0d3d…` | `2851a7ee…` |
| Keras | 3.15.0 (trading-stack) | 3.13.2 (pinned) |
| Contract sha256 | `60a55af0…` | `480dafaa…` |
| Validation MAE (zero naive 0.007632) | 0.007595, skill 0.0048 | 0.007610, skill 0.0028 |
| Fit | 7 epochs, best 1 | 8 epochs, best 2 |
| Replay | REPLAY_PASS | **REPLAY_PASS**: 12 points, deterministic, causal, window 0.0, forecast 7.5e-8, bottleneck 4.2e-7 |
| Replayed actions | 12 hold | 11 hold, 1 long |

The v2 contract records both tips and both Keras versions. `engine.source_commit` is
`64a91a74…` and `engine.keras_version` is `3.13.2`. `engine.previous` carries
`556c5f3e…` with Keras `3.15.0`, plus v1's contract and archive digests. The v1
contract predates the `keras_version` field. Its 3.15.0 is the version observed in the
environment the v1 export actually ran in, passed explicitly; it is not inferred.

**Golden parity against the `556c5f3e` export: `FEATURE_SIDE_IDENTICAL`.** All 12
as-of windows match bit for bit (max difference 0.0). The bars digest, train-only
scaler, feature definitions, time contract and action contract are equal. The model
side differs (max forecast difference 0.047, bottleneck 0.257) because the weights
differ. That is recorded as information, not as a failure: the model was retrained
under a different engine identity and Keras minor version. Following M01's rule
(refuse a major.minor mismatch), the 3.15.0 archive was not deserialized under 3.13.2.
The engine adds `schema`, `regime` and `alignment_probe` to the normalized config. The
adapter's contract checks do not depend on them.

**Route tests on worker_b (trading-stack), on lts `329d9a1`:** 30 passed before the
re-export and 30 passed after. The lts code did not change. The adapter itself is
unchanged. It does not yet refuse a Keras major.minor mismatch the way M01's
`load_bundle` does; that is a candidate follow-up, not done here.

One attempt was discarded and is kept: the first v2 export was labelled with a mistyped
engine commit, so it was re-run with the full commit. The metrics are identical, which
shows the fit is deterministic. Nothing was wired to a runner, and the v2 candidate is
still plumbing with no skill.

Evidence: `$HOME/Documents/GitHub/.runtime/m05-paper-adapter-20260930/m01-reexport-64a91a74/`
(`candidate-v2/`, `replay-v2/replay_receipt.json`, `cross_engine_parity.json`,
`routes/before.log`, `routes/after.log`, the discarded attempt). The earlier receipts
stay in place beside it. The v2 contract's engine path is in worker_b's scratch tree,
so v2 replays there, and that is where it was replayed.

Satoshi, successor technical lead, 2026-09-30.

## Addendum 3: Keras pin, fail closed (coordinator decision)

Commit `ee898b8`. `ModularPolicy.load` now compares the contract's
`engine.keras_version` with the running Keras before the engine module or the archive
is touched. A major.minor mismatch is refused with a message that names both versions.
A contract without the field is refused unless `allow_unpinned_keras=True` is passed
explicitly. That flag exists for v1-era replays, is off by default, and never admits a
recorded mismatch. The replay tool exposes it as `--allow-unpinned-keras` and records
it in the receipt. The smoke stays strict. Tests cover the three cases (match,
mismatch, missing field), plus one test without TensorFlow.

The route tests ran on worker_b (trading-stack, Keras 3.15.0, with `LTS_MODULAR_ENGINE`
set to M01's engine). The suite was the named route tests plus the M05 adapter and
preflight tests. Before, at `cdae578`: 54 passed. After, at `ee898b8`: 58 passed
(54 + 4 new), 0 skipped. The v2 candidate replayed under the strict pin in the pinned
Keras 3.13.2 environment: REPLAY_PASS. The v1 candidate (no field) now refuses unless
the flag is given. Evidence: `.runtime/m05-paper-adapter-20260930/keras-pin/`.

Satoshi, successor technical lead, 2026-09-30.

## Addendum 4: lane E, the installed consumer end to end in shadow (PRE-INTEGRATION)

Orders: predictor master `ac125db9`, section 3 row E, and the reconciliation's "M03 +
M05". The weekly paper/observer lanes were left untouched and running. The live `lts`
checkout (`12bce5f`, unit `lts-alpaca-model-runner`) was never modified, switched or
restarted. No deployment was made and no activation file was written.

**Consumer.** `AlpacaModelRunner` (`app/alpaca_model_runner.py`) serves the SPY 1d
route. Commit `9bd5463` lets it consume a modular policy:
- `model.family: modular` selects `SelectedModularPolicy` (`app/modular_runner_policy.py`).
- That selector refuses every tier except `shadow_inference_only`, and any contract
  that claims execution authority, before `tick()` can run. An unknown family is
  refused too.
- A shadow policy takes a branch at the top of `tick()`: the runner's own `_bars()`
  closed-bar fetch, then the adapter observation, modular inference and action, then
  one `due_bar_decisions` fact (`outcome: shadow_inference_only`).
- The branch returns before any account, session, position, quote or order logic.
  It ignores `allow_execution`.
- The linear path is unchanged. The heartbeat reports the modular identity with
  `read_only: true`.

**End-to-end test.** `tests/unit/test_alpaca_runner_modular_shadow.py` (9 tests) and
`tools/modular_runner_shadow_e2e.py` drive the real `__init__` and `tick()`. The broker
double serves bars through `stock_bars` and raises on every other attribute. It
uses dummy credentials under dedicated variable names, a dummy account fingerprint,
and a fresh ledger.

**Receipt** `runner_shadow_e2e_receipt_PRE-INTEGRATION.json` (sha256 `6da731bb…`):
**RUNNER_SHADOW_PASS**.
- Contract `480dafaa…` (engine `64a91a74`, Keras 3.13.2; previous `556c5f3e`, Keras
  3.15.0).
- Population: SPY 1d, recorded Alpaca IEX, 1,511 bars (2020-07-27 to 2026-07-31).
  12 decision points, 2026-07-16 to 2026-07-31.
- Scale: the profile's one share and four orders per day. Declared but **unused**:
  the shadow tier never sizes or submits an order.
- Execution was requested on every tick and ignored. All 12 ticks returned
  `shadow_inference_only`. The only broker attribute touched was `stock_bars`.
- No session or effect rows were written. The heartbeat was `read_only`.
- Actions: 11 hold, 1 long.
- The runner's 12 input and output digests equal the adapter replay receipt's
  (`73ba5a43…`) exactly.

**Latent causality probe** (in the replay tool; receipt
`replay_v2_latent_probe_PRE-INTEGRATION.json`, sha256 `c015527b…`). Each of the 24
input steps of a recorded window was perturbed in turn. In every case, no
bottleneck step before `i // 4` moved (largest change 0.0), and the earliest step
that moved was exactly `i // 4` for all 24. Verdict: REPLAY_PASS.

**Bottleneck semantics, both recorded.**
- Old core (`556c5f3e`/`64a91a74`, exercised here): causal-attention Transformer over
  the 12-step fused branch grid, then three learned compression stages to 6
  right-edge tokens × 8 channels.
- Lane A's new core (`c0d7b07b`, engine `da4ce7b4`): also (6, 8), but on the
  right-edge grid [4, 8, 12, 16, 20, 24] after residual Conv1D stages 24→12→6→6 over
  24-step branches.
- Not yet exercised: lane A has not published an installed integrated package.

**Suites** (worker_b, `crispdm-run` ≤3G, CUDA hidden; measured wall times):

| Run | Env | Result | Wall time |
|---|---|---|---|
| Named routes + adapter + preflight, before at `4e88c01` | trading-stack | 58 passed | 11 s |
| The same + runner shadow tests, after at `9bd5463` / final `4f03cef` | trading-stack | 67 / 67 passed | 11.5 s / 13.3 s |
| Live lineage, before at `12bce5f` (+ `test_mt5_symbol_model_compat_preflight`) | trading-stack, scratch cherry-pick | 66 passed | 1.1 s |
| Live lineage, after: all four M05 code commits cherry-picked cleanly (`cccbba6`) | trading-stack | 103 passed | 11.5 s |
| **Installed, PRE-INTEGRATION**: predictor `64a91a74` and lts `9bd5463` installed non-editable into separate venvs on `envs/tensorflow` (Keras 3.13.2); tests run from a copy outside the tree, so `app` and the engine resolve from site-packages | venv | 67 passed, 0 skipped | 13 s (109 s with admission) |

**Installed-environment findings.**
- Installing predictor and lts into one environment merges their two top-level `app`
  packages. That is why the engine and the consumer live in separate venvs.
- lts's non-editable install omits `plugins_core`, `plugins_aaa` and
  `feeder_plugins`, so `tests/conftest.py` cannot import. Those three directories were
  copied next to the tests. `app` and the adapter still come from the install.
- Both are packaging gaps for their owners to decide on. They are not changed here.

**Not done.**
- No re-export or installed run against lane A's package yet: it is not yet an
  installed, integrated package. When it is: re-export, re-run golden parity and the
  latent probe, and record both semantics in the contract.
- No real broker run of the shadow branch. Against the shared Paper account a real
  shadow deployment would need its own ledger, and it is not authorized.
- Position control (in the live lineage) is not applied to shadow decisions.
- Nothing is promoted. A synthetic donor or a forecasting metric does not open real
  money.

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 5: INTEGRATED_3ecdb256

As the coordinator ordered, this is a single re-export against lane A's declared
integrated revision, predictor `3ecdb2565ef35dec7600109516dde86d2488d921`. It ran on
worker_b in the pinned `envs/tensorflow` (Keras 3.13.2), with every child under
`crispdm-run` (≤4G) and CUDA hidden. Nothing ran against a real broker, nothing was
promoted, and the live checkout was untouched.

**Adapter change.** Commit `e4186cf`. At `3ecdb256` the engine is a package
(`predictor_plugins/modular_temporal/`, 10 modules), so the adapter now pins and loads
a package too. For a directory, `engine.sha256` is the digest of canonical JSON
`{relative .py path: sha256}`, with `__pycache__` excluded. The predictor exporter
(`303042dc`) writes the same definition. The package is imported under a unique
pinned name, with no fallback. A new test covers the digest. The installed package's
digest equals the tree's: `2939b82d…`.

**Contract `19bba8e5…`** (`candidate-v3-INTEGRATED_3ecdb256`) records:
- Engine kind `package`, commit `3ecdb256`, Keras 3.13.2.
- `previous`: `64a91a74`, Keras 3.13.2, kind `file`, with lineage back to `556c5f3e`,
  Keras 3.15.0.
- Both bottleneck semantics, both shape (6, 8):
  - `semantics` (new core): 24-step causal Conv1D branches preserve the grid;
    positional encoding, projection and two causal Transformer blocks over 24 fused
    steps; residual Conv1D stages 24→12→6→6 over complete adjacent windows; latent
    steps on the right-edge grid [4, 8, 12, 16, 20, 24].
  - `previous_semantics` (old core): branches compress 24→12; Transformer over 12
    steps; compression to 6 right-edge tokens.

**Results:**

| Check | Result |
|---|---|
| Fit (local smoke) | 7 epochs, best epoch 1; validation MAE 0.007637 vs zero naive 0.007632 (skill −0.0006, worse than zero); persistence 0.011559. No skill. |
| Replay `replay_v3_INTEGRATED_3ecdb256.json` (`f14dbe82…`) | **REPLAY_PASS**: 12 points, deterministic, causal; window 0.0; forecast 4.3e-7; bottleneck 8.9e-7 |
| Latent probe, residual core | **passed**: earliest moved latent step = i//4 for all 24 inputs; change before the allowed step 0.0 |
| Golden parity vs the `64a91a74` export (`050d8ebf…`) | **FEATURE_SIDE_IDENTICAL**: 12/12 windows bit-identical; bars, scaler, features, time and action contracts equal. Model side differs (forecast 0.081, bottleneck 2.26): information, by design |
| Runner shadow receipt (`d2bb97cf…`) | **RUNNER_SHADOW_PASS**: 12 ticks, all `shadow_inference_only`; orders 0; `execution_authorized` false; only `stock_bars` touched; 10 hold / 2 long; runner digests equal the replay's 12/12 |
| Installed suites: predictor `3ecdb256` and lts `e4186cf` installed non-editable in separate venvs (Keras 3.13.2); engine from site-packages | **68 passed**, 0 skipped |
| Named routes + adapter + preflight + runner, trading-stack (Keras 3.15.0), before at `4f03cef` (engine 64a91a74) / after at `e4186cf` (engine 3ecdb256) | 67 / **68** passed |
| Live lineage: all M05 code commits cherry-picked onto `12bce5f` (scratch `3d62da3`, removed) | **104** passed (66 before) |

Evidence: `$HOME/Documents/GitHub/.runtime/m05-paper-adapter-20260930/integrated-3ecdb256/`.

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 6: forecast-versus-naive eligibility gate for the heuristic strategy

Owner order: predictor `b327b771`, section 5. Commit `a30a2b9`. The gate applies to the
heuristic strategy. It does not apply to RL policy admission (section 6).

**What it is.** `app/forecast_naive_gate.py` consumes M04's frozen
`predictor.forecast_naive_evidence.v1` records (predictor
`tools/modular_forecast_evidence.py`, `de164a09`), adopted as-is, one record per
prediction family. Its `evidence_sha256` is re-derived exactly as M04's `verify()`.
The asset's `strategy_config.forecast_evidence` declares, per family:
- the record (`evidence_sha256`, file);
- the model (`model_sha256`);
- `period_hours`, `metric_space`, `scaler_identity`;
- the mapping of each prediction the strategy actually consumes onto a record horizon.
  M04 leaves that mapping to the consumer. Its length must equal what the strategy
  receives, so nothing is dropped and nothing is invented.

**The rule.** The status is ELIGIBLE only if every consumed horizon of BOTH families
satisfies all of these:
- the record verifies, is the declared one, and freezes MAE as primary;
- its provenance is `held_out_validation` or `chronological_oof`, with `test_used` and
  `reserved_trading_test` false. The reserved trading test, an unknown provenance or
  None is refused;
- asset, period, scale, scaler and candidate match the declaration, and the horizon
  was scored on the population's own rows;
- model and naive MAE are finite, naive MAE is not zero, and model MAE is strictly
  below naive MAE.

Anything else gives `SKIPPED_NOT_BETTER_THAN_NAIVE`, listing every failure (reason,
family, horizon, detail). A favourable mean is reported but never overrides a failing
member. The receipt (`lts.forecast_naive_gate_decision.v1`) carries:
- per horizon, MAE and MSE with the same-row baseline and skill/delta, or
  NOT_AVAILABLE with the reason (zero naive gives no infinite skill; the record holds
  no NaN or inf);
- per family, the provenance, the population identity and the baselines. Persistence
  decides eligibility. A seasonal naive is reported when the evidence carries
  `seasonal_naive` (per horizon `seasonal_naive_MAE/MSE`), and is NOT_AVAILABLE
  otherwise. It is never used for eligibility.

**Wiring.** `run_heartbeat_cycle` calls `_forecast_gate` before
`_compute_heuristic_signal` and fails closed: missing configuration, an unreadable
file, any exception, or a mapping mismatch each SKIP. A skipped asset invokes the
strategy zero times and writes no order. Decisions are returned in
`results["forecast_gate"]`.

**Tests** (`tests/unit/test_forecast_naive_gate.py`, 27). They were red at collection
before the module existed. The nine required cases are all green:
1. one failing short horizon;
2. one failing long horizon;
3. a favourable mean masking a failing member;
4. a tie;
5. a zero naive;
6. a missing or NaN metric (5 variants);
7. mismatched rows, scaler or period (4 variants);
8. one genuinely passing configuration;
9. **zero `compute_signal` invocations through the real `run_heartbeat_cycle`** (real
   in-memory DB, portfolio and asset; only the external prediction provider is
   substituted).

Also green:
- missing evidence and a substituted evidence file each invoke the strategy zero times;
- a passing gate invokes the strategy exactly once;
- every provenance refusal, metric switch, tampering, candidate or asset mismatch, and
  horizon-mapping case;
- the baselines receipt.

**Mutants** (worker_b): every one of the five is killed.

| Mutant | Tests failing |
|---|---|
| tie admitted | 1 |
| gate unwired | 3 |
| zero naive admitted | 1 |
| provenance unchecked | 4 |
| failing member suppressed | 4 |

**Suites** (worker_b, trading-stack, `crispdm-run` 3G; nothing ran on the coordinator):

| Run | Result |
|---|---|
| Branch before `c734f34` | 77 passed, 13 errors |
| Branch after `a30a2b9` | 110 passed, 13 errors |
| Live lineage `12bce5f` (scratch) | 75 passed, 13 errors |
| Live lineage + all M05 commits (`7ced25e`, scratch, removed) | 146 passed, 13 errors |

The suites are the named routes plus `tests/test_web_ui.py` (which holds the heartbeat
tests) plus the M05 suites. The 13 errors are identical in all four runs (same digest
of the error list). They are `TestTemplateRendering` and `TestAuthIntegration` failing
to import `jose`, which is not installed in worker_b's trading-stack: an environment
gap that this change neither causes nor fixes. Both heartbeat tests pass.

**Conditioning contract (M01 provenance v1).** `ModularPolicy.load(require_conditioning="OPERATIONAL")`
refuses UNKNOWN (also when absent) and SYNTHETIC_OFFLINE with
`CONDITIONING_CONTRACT_NOT_OPERATIONAL`, before the engine is touched. The shadow tier
records the value and does not require it. Tests cover OPERATIONAL, UNKNOWN, absent,
SYNTHETIC_OFFLINE and unrecognised values. The v3 contract's engine digest is unchanged.

**Not done.**
- No real financial evidence record exists yet for the heuristic strategy's
  prediction models. Every live asset therefore stays SKIPPED until M04 issues one.
- The gate is not wired into the separate heuristic-strategy repository's backtest
  runner. That needs the same gate in that repository and an explicit order.

Evidence: `$HOME/Documents/GitHub/.runtime/m05-paper-adapter-20260930/naive-gate/`.

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 7: the heuristic-strategy backtest runner is gated too

**Count reconciliation for addendum 6.** The lts gate suite had **26** tests when
the five mutants ran (each mutant run shows 25+1, 23+3, …). The receipt/baselines
test was added afterwards, bringing it to **27**. That second count is what the
110-passed suite and this round measure. Both numbers are correct for their own
moment.

**lts change, commit below.** M04 fixed the seasonal-naive form in `b5ed0982`: a
top-level `seasonal_naive` {period_steps, declared, definition} and a nested
`per_horizon[i].seasonal_naive` that is either {naive_MAE, naive_MSE, MAE, MSE} or
{status: NOT_AVAILABLE, reason}. The lts gate now reads that nested form. It is
report-only and never decides. Gate suite: 27 passed on worker_b.

**heuristic-strategy**, branch `satoshi/s08-backtest-naive-gate-20261001` from
`master` `5c87a25`, tip **`e6431e6`**, pushed. It ran in its own worktree; the main
checkout's branch and untracked files were left untouched.
- `app/forecast_naive_gate.py` is stdlib only. It does not import lts, because no
  shared package exists. It implements the same contract and pins it as `CONTRACT`;
  its digest **`5a5685893504cf84112132c639e255d14393cbad74fe3b3ccc147363f7bfdc27`**
  is carried by every decision and pinned in a test.
- Families are `hourly` and `daily`. The run config declares `asset` and
  `forecast_evidence`; the consumed counts are the prediction files' column counts.
- API-source runs skip, because per-tick consumption cannot be bound before the run.
- Runs with no prediction files skip, because the plugin would synthesize oracle
  predictions from the base data. That is not learned evidence.
- `run_processing_pipeline`, which `app/main.py` calls, decides before the plugin
  sees data. A SKIP evaluates the strategy zero times and writes no trades, summary,
  plot or parameters. The receipt is `naive_gate_receipt_file`.
- The trading-period MAE table the pipeline already printed now shows same-row
  persistence and skill. It is labelled as not eligibility evidence.

**Tests, worker_b** (venv on trading-stack, `crispdm-run` 2G, scratch under
`~/.local/state/scratch/m05/hs`):
- **Red:** the new test file against `master`: 1 error at collection.
- **Green:** **26 passed** in 10.2 s.
  - The required cases: short fail, long fail, favourable mean masking a member, tie,
    zero naive, missing/NaN, mismatched rows/scaler/period, genuine pass.
  - Provenance (trading test, test, unknown, None).
  - Metric switch, tampering, candidate and asset refusals.
  - Consumption mapping, API and auto-generated skips, and the seasonal report.
  - The contract digest pin.
  - The REAL `run_processing_pipeline` with the real `ls_pred_strategy` plugin on
    the bundled EURUSD data: 0 evaluations on failure, 0 on missing evidence, ≥1
    when it passes.
  - The REAL `app/main.py` in a subprocess: SKIPPED, no trades/summary/plot/
    parameters, and the receipt names the tie.
- **Regression:** the repository's healthy subset (AGENTS.md) passed 17 on `master`
  and 17 after.
- **Mutants:**

| Mutant | Tests failing |
|---|---|
| tie admitted | 2 |
| gate unwired | 3 |
| zero naive admitted | 1 |
| provenance unchecked | 4 |
| failing horizon suppressed | 4 |

**Not gated.** These runners call `plugin.evaluate_candidate` directly and bypass
`run_processing_pipeline`:
- `run_wfo.py` / `app/walk_forward_optimizer.py`;
- `run_phase_b_cnn.py`, `run_phase_c_ensemble.py`, `run_phase_d_neat.py`;
- `run_oracle_ceiling.py` (oracle by design);
- `sweep_noise.py`.

The per-tick API plugin is also ungated when it is driven outside the pipeline.
lts `plugins_broker/backtrader_simulation_broker.py` was not inspected for strategy
invocation. Each of these needs the same gate or an explicit diagnostic exception
order.

Evidence: `$HOME/Documents/GitHub/.runtime/m05-paper-adapter-20260930/heuristic-gate/`.

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 8: S09, every direct-evaluation runner gated; incident INCIDENT_S09-MUT-01

heuristic-strategy `satoshi/s08-backtest-naive-gate-20261001`, tip **`71fa1a4`**. The
contract digest is unchanged: `5a568589…`.
- **Shared mechanism:** `app/runner_naive_gate.py` decides at entry, before any data or
  model loads, and then again for each evaluated prediction set.
- **Gated runners:**
  - `run_wfo.py`, with `run_walk_forward` now refusing to run without an allowing gate;
  - `run_phase_b_cnn.py`, `run_phase_c_ensemble.py` and `run_phase_d_neat.py`.
    Direction-probability sets have no form in the contract, so they are never
    admitted;
  - `sweep_noise.py`, which no longer does any work at import time;
  - `plugin_api_predictions`, which refuses however it is driven.
- **regime_wfo:** declares `consumes_learned_predictions = False` and is recorded
  `NOT_APPLICABLE_NO_LEARNED_PREDICTIONS`. An owner ruling is welcome.
- **Oracle:** `run_oracle_ceiling.py` refuses without `--diagnostic-oracle`. With the
  flag, the bypass is receipted and every output is stamped
  `DIAGNOSTIC_ORACLE_NOT_A_STRATEGY_RESULT`.

**Tests** (worker_b): red 16 failed / 1 passed, then green **18** (S09) + 26 (S08) + 17
(healthy subset) = **61 passed** on the committed tip. All twelve mutants are killed
(see the incident file for the table).

**Incident.** The unsandboxed `d_entry` mutant ran real NEAT training in worker_b's
predictor clone for 15 minutes. Three tracked files were backed up and restored. The
details are in `docs/audits/evidence/S09/INCIDENT_S09-MUT-01.md` on that branch. The
standing rule from now on: never run checkout/reset/clean in a checkout I do not own.

**lts `plugins_broker/backtrader_simulation_broker.py`** was inspected, not gated (as
ordered). It makes no direct strategy or prediction call of its own.
`run_simulation(strategy_fn)` invokes a callable supplied by the caller on each bar. The
only caller in lts is `tests/unit/test_backtrader_broker_simulation.py`. A future caller
that passes a heuristic-strategy callable fed with learned predictions would need the gate
at that caller.

Satoshi, successor technical lead, 2026-10-01 (UTC).
