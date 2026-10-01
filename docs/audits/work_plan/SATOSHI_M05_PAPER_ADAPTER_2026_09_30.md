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

## Addendum 9: S09 logs and the owner's open questions

**Logs** (in `/home/harveybc/Documents/GitHub/.runtime/m05-paper-adapter-20260930/heuristic-gate/`):
- `s09_green_71fa1a4.log`: the committed tip `71fa1a4`, verbose, **61 passed**. That is
  18 S09 tests, 26 S08 tests and the 17-test healthy subset. It was re-run on worker_b to
  keep the full log; the earlier run printed only its summary line.
- `s09_mutants.log`: the sandboxed rerun of all twelve mutants (2 m wall each). Its first
  line, "green: 43 passed", predates the sandbox-proof test. At that point the S09 file
  held 17 tests, so 17 + 26 = 43. The mutants ran against those 17 tests, and every one
  was killed. Adding the 18th test (the sandbox proof) makes 18 + 26 + 17 = 61.

**Questions 21–23 for the owner.** The current behaviour stands until he rules:
- **21: direction classifiers (phases B/C/D).** Direction-probability sets have no form in
  the forecast-vs-naive contract, so they are never admitted and B/C/D always skip. Should
  a classification baseline be defined for them?
- **22: regime_wfo.** It declares `consumes_learned_predictions = False`. It is allowed and
  recorded as NOT_APPLICABLE_NO_LEARNED_PREDICTIONS, with the declaration in the receipt.
  Is that exemption accepted?
- **23: API mode.** Per-tick API predictions cannot be bound to declared horizons before a
  run, so the API plugin and API-mode runs always skip. Should a binding mechanism be
  specified?

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 10: front E under the autonomous-execution order

**Coordinator rulings on questions 21–23** (recorded as coordinator rulings, not the
owner's):
- B/C/D direction classifiers stay excluded until a classification baseline exists.
- regime_wfo is allowed as NOT_APPLICABLE, with its declaration recorded.
- API mode skips.

**1. RL shadow adapter (lts `c2304d6`).** `app/rl_shadow_adapter.py` handles lane G's
`rl_temporal.policy_bundle.v1` (agent-multi `02db0701`):
- validates every intake field and refuses a missing one by name, including
  `execution_authorized` false and the evidence flags;
- refuses an sb3/torch/gymnasium major.minor mismatch before loading;
- gives read-only shadow decisions with zero orders.

Tests: red at collection, then 43 passed, including real SB3 DQN and SAC bundles.
Mutants killed: execution unchecked (2), zip unchecked (1), versions unchecked (2),
mapping unchecked (1), bar_period unchecked (1).

**2. Paired heuristic arm** (heuristic-strategy `b1383cb`, then `c81ccd0`).
`app/paired_backtest.py` and `run_paired_eth4h.py`:
- mirror lane G's `reconcile_episode` definitions on VALIDATION rows [13699, 15895),
  a split M07 confirmed;
- let the gate choose which horizons the strategy may read; failing ones are excluded
  as a declared reduced experiment and never read;
- give bars without a forecast an explicit hold policy, with the count recorded.

Tests: red at collection, then 10 passed (71 with the S08/S09/healthy suites). Mutants
killed: gate unwired (3), failing columns read (1), same-bar fill (2), Sharpe ddof 0 (1),
reversal not counted (1).

**3. Eligible-model integration path, dormant (lts `db9650f`).**
- The MT5 runner (the ETH 4h demo route) accepts `family: modular` in the shadow tier
  only. Its shadow branch reads recorded snapshot bars from its own bridge store and
  queues no command.
- `require_forecast_eligibility` keeps a modular route dormant: the selector refuses,
  before any model loads, unless the contract's frozen
  `predictor.forecast_naive_evidence.v1` record passes for exactly the horizon the
  action consumes.
- `examples/configs/mt5_eth_4h_modular_shadow_DORMANT.json` reuses the existing MT5
  Demo ETH mandate block unchanged. The account is bound by a placeholder. It is not
  installed as a unit.
- Tests: red 7 failed, then green.
- prediction_provider is not modified, because LTS consumes the file contract directly.

**Suites** (worker_b, `crispdm-run` 2G):

| Run | TF side | RL side (own process) |
|---|---|---|
| Branch before (`2b18e17`) | 110 passed, 8 skipped, 13 errors | — |
| Branch after (`db9650f`) | 117 passed, 8 skipped, 13 errors | 43 passed |
| Live lineage before (`12bce5f`) | 75 passed, 9 skipped, 13 errors | — |
| Live lineage after (scratch `b264a22`) | 153 passed, 9 skipped, 13 errors | 43 passed |

The skips (live SAC golden-parity artifacts absent on worker_b) and the 13 `jose`
errors are identical before and after.

**Finding:** running TF/Keras and torch/SB3 tests in ONE pytest process segfaults on
worker_b, so the RL tests run in their own process.

**Live-lineage port.** The cherry-pick onto `12bce5f` conflicts in the MT5 runner,
because the live lineage has `policy_type` linear/sac. The conflict is resolved in
scratch: `family: modular` takes precedence, otherwise the live selector logic is kept,
and the heartbeat identity is merged. The resolved patch is in
`.runtime/m05-paper-adapter-20260930/live-lineage/mt5_modular_shadow_on_12bce5f.patch`.
Nothing was applied to the live checkout.

**M5PHET** (read-only health check from the coordinator; no restarts):
- `m5phet-chat` is active and running (since 2026-09-29, 0 restarts). The workbench
  `GET /` returns 200 ("M5PHET · Workbench"). The API returns 401 without a token, so
  authentication is enforced and the API answers.
- `hermes-gateway`, `data-gov-m5phet` and `m5phet-tailnet-forward` are active, with 0
  restarts.
- The local interpreter endpoint returns 200.
- No family task was exercised: that would need the API token and is beyond a health
  check.

**Pending:** item 3 (the ETH 4h paired run) waits for M07's first verified R0 cell
(`EVIDENCE_<cid8>.json` and `PREDICTIONS_<cid8>.csv`) and lane G's
`EPISODES.eth_4h.json`.

Logs: `.runtime/m05-paper-adapter-20260930/front-e/`.

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 11: jose errors removed (isolated venv); direction classifier gate

**(1) The 13 `jose` errors.** I did **not** install python-jose into worker_b's
`trading-stack` itself. That environment runs live processes on worker_b: the MT5
execution bridge (`app.mt5_execution_cli`), the ETH model runner (`app.mt5_model_runner`)
and the agent-multi campaign supervisor. The order excludes any live host environment.

What I did instead:
- Made a venv on worker_b (`--system-site-packages` from trading-stack, under my scratch).
- Installed, matched to the coordinator's trading-stack versions: `python-jose[cryptography]==3.5.0`,
  then `bcrypt==5.0.0`, `passlib==1.7.4`, `python-multipart==0.0.32` and
  `itsdangerous==2.2.0`. bcrypt was the next missing module once jose was present.
- pip freeze before and after for the venv shows only those packages and their dependencies
  were added (ecdsa, pyasn1, rsa).
- `trading-stack`'s own freeze is byte-identical before and after.

Rerun at lts `e9553e0`: **130 passed, 0 errors** on the TF side (was 117 passed, 13 errors)
and 43 RL passed. Evidence: `.runtime/m05-paper-adapter-20260930/jose/`.

**(2) Direction classifiers: `predictor.direction_naive_evidence.v1`** (coordinator
design from question 21). heuristic-strategy **`64ab2f9`**.
- `app/direction_naive_gate.py` is stdlib only; contract digest `754f016f…`. Schema:
  `docs/contracts/predictor.direction_naive_evidence.v1.json`.
- The rule, per consumed horizon of each consumed family (long, short): held-out/OOF
  balanced accuracy AND log-loss must both strictly beat BOTH baselines, the TRAIN majority
  class and sign persistence. The forecast gate's refusals apply.
- Phases B/C/D now gate their direction sets through it instead of being excluded. With no
  record they still skip at entry.
- Tests: red at collection, then 18 passed; 89 with the rest of the heuristic suites.
- Mutants killed: tie admitted (2), one baseline (5), balanced accuracy only (4),
  provenance unchecked (4), one family (6), direction kind ignored (1). The phase
  entry/candidate mutants, re-run with the new wiring, are each killed (1).
- The predictor clone on worker_b was unchanged afterwards.
- The schema was sent to M07.

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 12: isolated test venv recorded; cost and sizing sheet

**Test environment.** worker_b's `trading-stack` runs the MT5 bridge, the ETH demo runner
and the campaign supervisor, so nothing is ever installed into it. The coordinator
confirmed this. The isolated test venv is
`~/.local/state/scratch/m05/venv-ts-jose` on worker_b: `--system-site-packages` from
trading-stack, plus `python-jose[cryptography]==3.5.0`, `bcrypt==5.0.0`,
`passlib==1.7.4`, `python-multipart==0.0.32` and `itsdangerous==2.2.0`, with their
dependencies ecdsa, pyasn1 and rsa. Its freezes are in
`.runtime/m05-paper-adapter-20260930/jose/venv_freeze_{before,after}.txt`. No service
ever uses it.

**Cost and sizing sheet:** `docs/audits/work_plan/M05_COST_AND_SIZING_SHEET_2026_10_01.md`.
Every value comes from an existing config or code path, with its file sha256; there are
no new limits and no config changes. It covers:
- the ETH 4h MT5 demo route: stop 1%, take profit 2%, risk at stop 2e-05, gross and margin
  caps 0.3%, 1 position, 0.01-lot ceiling, 4 commands per day;
- the EURUSD paper route: the IBKR L1 canary profile; its service block is a disabled
  example, flagged as a placeholder;
- the paired harness fields.

It flags the sizing and cost mismatch between the harness and the demo route, the
drift in the deployed daily loss budget, and the placeholders.

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 13: ETH 4h paired backtest on M07's four R0 cells (DEVELOPMENT_NOT_CONFIRMATORY)

**Inputs, all verified on worker_b:**
- **Episode:** lane G `EPISODES.json` sha256 `1a7cacbe…`. Validation rows [13699, 15895), data sha `1b447c66…`.
- **View:** predictor `b1f8a74f` `ethusdt_4h_tech_stat_full_model_ready.csv`, sha `1b447c66…`.
- **Evidence and predictions:** M07 `artifacts_r0_v1/EVIDENCE_{10f83766,4a122eff,50ee6e9b,d26350d4}.json`
  and the matching `PREDICTIONS_*.csv`.
- **Manifest:** FROZEN_DEVELOPMENT, so every run is labelled `DEVELOPMENT_NOT_CONFIRMATORY`.
- **Harness:** heuristic-strategy `85b0a45`, frozen `HeuristicParams`, lane G costs.
- **Naive:** `naive_MAE` in the records is M04's strict minimum over persistence, train
  mean (zero return) and seasonal-6. Here that is the train-mean naive on every horizon.

MAE is in z_train units on 2190 identical rows per horizon; skill = 1 − model/naive.

| Cell | h1 | h2 | h3 | h4 | h5 | h6 | Consumed |
|---|---|---|---|---|---|---|---|
| 10f83766 grouped32 s2021 | 0.464832 / 0.464673 ✗ | 0.659675 / 0.657202 ✗ | 0.828914 / 0.827491 ✗ | 0.955298 / 0.955080 ✗ | 1.094785 / 1.087890 ✗ | 1.201653 / 1.198114 ✗ | none: **SKIPPED, strategy not run** |
| 4a122eff grouped32 s2022 | 0.464661 / 0.464673 ✓ | 0.659309 / 0.657202 ✗ | 0.827924 / 0.827491 ✗ | 0.961044 / 0.955080 ✗ | 1.089831 / 1.087890 ✗ | 1.198961 / 1.198114 ✗ | [1] |
| 50ee6e9b control_mlp s2022 | 0.464669 / 0.464673 ✓ | 0.658902 / 0.657202 ✗ | 0.830782 / 0.827491 ✗ | 0.959063 / 0.955080 ✗ | 1.084217 / 1.087890 ✓ | 1.198524 / 1.198114 ✗ | [1, 5] |
| d26350d4 control_mlp s2021 | 0.464612 / 0.464673 ✓ | 0.657928 / 0.657202 ✗ | 0.826506 / 0.827491 ✓ | 0.953524 / 0.955080 ✓ | 1.087738 / 1.087890 ✓ | 1.195745 / 1.198114 ✓ | [1, 3, 4, 5, 6] |

Each cell shows model MAE / same-row naive MAE.

No cell passes every horizon. The three partial cells ran only as declared
reduced-input experiments, with the excluded horizons listed in each result. The margins
are tiny: the largest skill is +0.00198 (d26350d4, h6), and 50ee6e9b at h1 has skill
+0.00001. On several passing horizons the MSE is worse than the naive (for example
4a122eff h1: MSE 0.485568 vs 0.485500). The gate decides on MAE, as the frozen primary
metric requires; the MSE is reported, not used.

**Heuristic arm.** Primary = full episode, 2196 bars, of which the last 6 have no
forecast and take no new entry. Cut = 2190 rows to 15888.

| Cell | Episode | net_return | max_dd | Sharpe (per-bar, ddof=1) | turnover | trades | exposure |
|---|---|---|---|---|---|---|---|
| 10f83766 | both | NOT RUN (SKIPPED) | | | | | |
| 4a122eff | primary / cut | 0.0 / 0.0 | 0 | undefined (zero variance) | 0 | 0 | 0 |
| 50ee6e9b | primary | 0.007448 | 0.022506 | 0.006417 | 40 | 20 | 0.01275 |
| 50ee6e9b | cut | 0.007448 | 0.022506 | 0.006426 | 40 | 20 | 0.01279 |
| d26350d4 | primary | 0.038064 | 0.017811 | 0.029536 | 34 | 17 | 0.01138 |
| d26350d4 | cut | 0.038064 | 0.017811 | 0.029577 | 34 | 17 | 0.01142 |

The no-trade baseline is 0 on every metric, with an undefined Sharpe.

**Beside lane G's RL-D0** (DQN native_flat, seed 101, `RESULT.json` sha `ca286788…`):
net_return 0.383182, max_dd 0.099536, Sharpe 0.040385 (over 2476 bars), turnover 313,
trades 156, exposure 0.8865. Four caveats bear on the pairing:
1. RL-D0's checkpoint was **selected on this same validation episode**
   (`selection_metric: net_return`, `best_validation_checkpoint`), so its validation
   number is optimistically biased. The heuristic arm selected nothing on the episode.
2. Its Sharpe counts 2476 bars, which includes the 280-bar forced-hold context prefix;
   mine counts 2196.
3. RL-D0 used variant A (`A_all_admissible_control`) features. The forecasting cells used
   M07's manifest.
4. RL-D0 is labelled `RESULT` on a `FROZEN_DEVELOPMENT` manifest. Lane G's own
   `validate_result_record` admits RESULT only for `FROZEN`, so the label is lane G's to
   reconcile.

All runs used one CPU job on worker_b under `crispdm-run -m 2G`. Results, declarations and
stdout are in `.runtime/m05-paper-adapter-20260930/eth4h_paired/`.

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 14: all 24 ETH R0 cells; split-half; families harness; equal-bar pairing

**All 24 verified R0 cells** (M07 predictor `58572172`, `ARTIFACTS_INDEX.json`
`37f1f5fc…`). Each was run through the harness, primary and cut, labelled
DEVELOPMENT_NOT_CONFIRMATORY. All cells are reported; **any choice of a cell on this
validation is post-hoc on DEVELOPMENT** (M07's and my statement).
- 9 cells pass no horizon and are **SKIPPED** (strategy not run).
- 15 run as declared reduced-input experiments.
- Of those 15, 11 make **zero trades**. Their passing forecasts are too small to reach the
  frozen 0.5% entry threshold. One of them is e1e1a2df, which passes all six horizons.
- The 4 that trade, on the primary episode:

| Cell | Net | Sharpe | Trades |
|---|---|---|---|
| d26350d4 | 0.0381 | 0.0295 | 17 |
| b9338018 | 0.0682 | 0.0244 | 93 |
| 50ee6e9b | 0.0074 | 0.0064 | 20 |
| 5053628a | 0.0006 | 0.0008 | 30 |

Seeds 2021/2022 summary (`seed_summary_s2021_s2022.json`): control_mlp_huber_adam net
0.0341 ± 0.0483 (2 ran); control_mlp_mae_adamw 0.0228 ± 0.0216 (2 ran); every other
config is 0 or skipped.

**Split-half check** (`split_half_all.json`). Horizons are chosen on the first half of
the 2190 validation origins (MAE strictly below the strict-minimum naive, recomputed from
the predictions and bars in log-return space, with M07's declared mu) and scored on the
second half only.
- Only **3 of 24** cells keep every chosen horizon passing on the held-out half:
  5053628a [2,4,6], d26350d4 [1,4,6], 790dc5b8 [6].
- For most other cells, the horizons chosen on the first half fail on the second.
- Second-half trading:
  - 5053628a: net 0.0114, Sharpe 0.0175, 17 trades.
  - d26350d4: net 0.0067, Sharpe 0.0181, 5 trades.
  - b9338018: net 0.0301, 26 trades, but its horizon fails on the second half.
  - 19e32355: net −0.0664.
- This confirms M07's reading: selecting horizons on this validation is mostly selection
  on noise.

**Equal-bar pairing with lane G** (relabelled `RESULT.json` sha `cdd312d4…`, now
DEVELOPMENT_NOT_CONFIRMATORY). On the same 2196 scored bars:
- RL-D0 s101: Sharpe 0.04289, net 0.3832, max drawdown 0.0995, 156 trades, exposure 0.9995.
- Best heuristic cell d26350d4: Sharpe 0.02954, net 0.0381, max drawdown 0.0178,
  17 trades, exposure 0.0114.

Caveats: both are single-seed numbers, RL-D0 was selected on this episode, and the
heuristic's cell choice is post-hoc.

**Families harness, for M07 campaign 2** (heuristic-strategy `4b5dd7e`):
- `paired_backtest_families` / `run_paired_families.py` take separate hourly and daily
  records and CSVs, paired on DATE_TIME, with per-family gates. Daily drives entry; hourly
  only feeds exit variant E.
- Any candidate id is accepted and named. This is the front H hook, confirmed to H1.
- Tests: red at collection, then 8 (families) + 4 (split-half) green; 101 heuristic total.
- Mutants killed: daily failing read (1), early close ungated (1), entry from hourly
  (survived at first; a test was added, then killed, 1), split not split (1), population
  sd (1), candidate not named (1).

**Seeds table for the best 3 cells** waits for M07's seeds 2023/2024. The tool
(`seed_summary`) is ready.

Evidence: `.runtime/m05-paper-adapter-20260930/eth4h_paired_24/`.

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 15: front E build-out (MAINLINE §4.E) and the integration replay

**Integration test, ETH 4h dormant MT5 demo route** (lts `907e2fb` + `803904c`). The real
`Mt5ModelRunner` ran offline: an isolated bridge store, network forbidden, family
`recorded_forecast`, shadow tier. Inputs were the 2196 recorded validation bars
[13699, 15895) and cell d26350d4's recorded predictions, gated to horizons [1,3,4,5,6]
as a declared reduced experiment.
- It was **crashed after 900 ticks and resumed** from the persisted state for the
  remaining 1296.
- Result: 2196 decisions logged, **0 commands queued**, `execution_authorized` false.
- The per-bar targets are **identical to the paired harness on the same bars** (2196/2196,
  0 mismatches, harness digest `78f47105…`).
- Evidence: `.runtime/m05-paper-adapter-20260930/route-replay/` (part1/part2 receipts,
  decisions JSONL, final state, contract).

**New in lts:**
- `app/recorded_forecast_policy.py`: the naive gate selects the readable horizons, and no
  passing horizon keeps the route dormant. The decision rule mirrors the harness exactly.
  State is persisted per bar with an atomic replace. A late tick catches up bar by bar from
  the declared episode start. Each bar gets a JSONL observability line: decision, reason,
  forecasts, sizing (none in shadow) and cost lines (MODELLED / BROKER_FILL, not
  applicable in shadow).
- Wired into both the MT5 demo and the Alpaca paper runners. Alpaca was exercised only
  with the offline broker double; no credential was touched.
- `tools/recorded_forecast_route_replay.py`: `--stop-after` for crash/resume and
  `--compare` against a harness result.
- Tests: red at collection, then 9. Mutants killed: state not persisted (2), failing column
  read (survived at first; a poisoned-column test was added, then killed, 1), tier
  unchecked (1), no catch-up (1), gate ignored (1).

**New in heuristic-strategy** (`114779b`, `177847a`):
- Campaign-2 smoke: `run_paired_families.py` on synthetic EURUSD/GBPUSD records with M07's
  exact asset strings, separate hourly and daily records, `n_rows` columns and irregular
  weekend bars. 3 assets × 3 gate outcomes, all as expected. `split_half` now uses the
  per-row `n_rows`.
- Explicit cost lines: commission MODELLED from fills; slippage, spread and swap MODELLED 0
  per lane G's specification; `broker_fill_costs` BROKER_FILL NOT_AVAILABLE offline.
- Per-run observability JSONL: header with gate, skipped-by-gate reasons, sizing and costs,
  then one line per bar.
- Ablation runners: `HeuristicParams.direction` long_only / short_only, and
  `run_ablations.py`, which runs both/long/short and each horizon alone, only for a fully
  eligible candidate. No ETH R0 config is eligible.
- Tests: 14 (smoke) + 6 (observability/ablation). Mutants killed: direction ignored,
  commission dropped, broker line claimed, observability skips bars, ablation on an
  ineligible candidate (1 each).

**Full lts suites** (worker_b, isolated venv `venv-ts-jose`, `crispdm-run` 2G):

| Run | TF side | RL side |
|---|---|---|
| Branch before (`42035f1`) | 130 passed, 8 skipped | 43 |
| Branch after (`803904c`) | 139 passed, 8 skipped | 43 |
| Live lineage before (`12bce5f`) | 88 passed, 9 skipped | — |
| Live lineage after: all M05 code commits cherry-picked into scratch (`693f96d`, removed), with the MT5 conflict resolved as before | 175 passed, 9 skipped | 43 |

Every skip is a live SAC golden-parity file that is absent on worker_b. There are 0 errors.

**Seeds 2023/2024:** M07's STATUS shows 48/48 verified, but only the seed-2021/2022 records
are exported. I asked M07 for the remaining pairs; the 4-seed table and the split-half check
will run when they land.

**Front H:** H1 reports that no Kalman arm verifies (the ridge arms collapse to the
train-mean naive; the MLP arms are worse than zero-return), so no records exist. The hook
is ready.

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 16: 4-seed table and split-half over all 48 ETH R0 cells

**Inputs:** M07 predictor `c8b150da`, `ARTIFACTS_INDEX.json` `20231c86…`, `CLOSURE.json`
`c47aaad7…` (12 configs × 4 seeds, all verified). Every cell was run on the primary and cut
episodes with per-run observability files, labelled DEVELOPMENT_NOT_CONFIRMATORY.

**Selection, declared:** the "best 3" are the configs with the lowest 4-seed mean validation
MAE_z. That is a **post-hoc choice on DEVELOPMENT**, and choosing horizons on the same
validation is optimistic; the split-half check measures how much.

Per config, 4 seeds, full episode (heuristic metrics):

| Config | Mean MAE_z | Ran / skipped | Net return mean ± sd | Sharpe mean (n) |
|---|---|---|---|---|
| control_mlp_huber_adam | 0.86454 | 4 / 0 | 0.0201 ± 0.0331 | 0.0117 (3; 1 undefined) |
| control_mlp_mae_adamw | 0.86551 | 4 / 0 | 0.0204 ± 0.0135 | 0.0185 (4) |
| per_feature_huber_adam | 0.86552 | 3 / 1 | 0.0000 ± 0 (no trades) | undefined |
| per_feature_huber_adamw | 0.86555 | 3 / 1 | 0 (no trades) | undefined |
| control_mlp_mae_adam | 0.86599 | 3 / 1 | 0.0013 ± 0.0015 | 0.0094 |
| grouped32_huber_adamw | 0.86599 | 4 / 0 | 0 (no trades) | undefined |
| per_feature_mae_adamw | 0.86623 | 2 / 2 | 0 | undefined |
| per_feature_mae_adam | 0.86635 | 2 / 2 | 0 | undefined |
| grouped32_mae_adam | 0.86689 | 0 / 4 | not run | — |
| grouped32_mae_adamw | 0.86697 | 2 / 2 | 0 | undefined |
| grouped32_huber_adam | 0.86705 | 4 / 0 | 0 | undefined |
| control_mlp_huber_adamw | 0.86849 | 3 / 1 | 0.0063 ± 0.0076 | 0.0130 |

**Best 3, per seed** (consumed horizons → full-episode net, trades | split-half: chosen on
the first half → still passing on the second half, second-half net):

- control_mlp_huber_adam:
  - 2021: [2,4,5] → 0.0, 0 trades | [2,4,5] → [], 0.0
  - 2022: [3,4,5,6] → 0.0682, 93 | [1] → [], 0.0301
  - 2023: [2,5,6] → −0.0033, 22 | [] → not run
  - 2024: [3,5] → 0.0153, 29 | [3] → [], −0.0073
- control_mlp_mae_adamw:
  - 2021: [1,3,4,5,6] → 0.0381, 17 | [1,4,6] → [1,4,6], 0.0067
  - 2022: [1,5] → 0.0074, 20 | [] → not run
  - 2023: [5,6] → 0.0235, 13 | [5,6] → [], 0.0012
  - 2024: [3,4] → 0.0126, 10 | [] → not run
- per_feature_huber_adam: no trades in any seed (forecasts never reach the frozen 0.5% entry
  threshold); 2022 is skipped.

Split-half second half, mean ± sd over the seeds that ran: control_mlp_huber_adam
0.0076 ± 0.0198 (3); control_mlp_mae_adamw 0.0040 ± 0.0039 (2); per_feature_huber_adam 0 (3).

**Reading:**
- The passing horizons change from seed to seed. Only **1 of 12** best-3 seeds
  (control_mlp_mae_adamw 2021) keeps its first-half horizons passing on the second half.
- Second-half returns are near zero, with sd comparable to the mean.
- This agrees with M07's closure (no config passes all six horizons in more than 1 of
  4 seeds). It also stays the headline caveat: horizon choice on this validation is mostly
  noise, and the full-episode returns above are optimistic.

Evidence: `.runtime/m05-paper-adapter-20260930/eth4h_paired_48/` (`cells.json`,
`seed_summary_4seeds.json`, `best3_4seeds.json`, the per-cell results, the split-half
outputs and the observability JSONL files). One CPU job on worker_b, `crispdm-run` 2G.

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 17: EURUSD lake vDh, the first FX evidence, as a declared reduced experiment

**Inputs:** worker_a `~/.local/state/scratch/m07/artifacts_fx_wa/`, plus the EURUSD lake 1h
view `eurusd_1h_from_lake_5m.csv` (sha `ab0ada28…`).
- Copied to worker_b through a coordinator pipe (nothing stored on the coordinator). All 10
  file sha256s are identical on both workers.
- Cells:
  - grouped_all_mae_adamw: 90d91a43 (seed 2021) and 9c4c7577 (seed 2022);
  - control_mlp_mae_adamw: ef02938c (2021) and 66583ab2 (2022).
- Asset: "EURUSD 1h (lake 5m->1h, HistData lineage; DEVELOPMENT)". Hourly family,
  horizons 1..4 h, 18,711 validation origins.
- Clock: `DATE_TIME` is New York local, stamped at bar end. The UTC availability epoch comes
  from `row_id`: first origin 2019-08-13T11:00:00Z (NY 07:00), last 2022-09-22T21:00:00Z.
  Bars and forecasts pair on the same NY stamp; the UTC epochs are recorded in the results.

**Eligibility.** The strategy consumes hourly 1..24 and daily 24..144 h. Evidence exists
only for hourly 1..4, so **full-consumption eligibility is NOT ELIGIBLE**. What is missing
for deployment: hourly 5..24, the whole daily family, a FROZEN manifest, and FX-calibrated
sizing and costs (see below). This run is a **DECLARED REDUCED experiment**: entry from
hourly 1..4 only.

Gate on MAE_z, model vs same-row strict-minimum naive:

| Cell | Seed | h1 | h2 | h3 | h4 | Consumed |
|---|---|---|---|---|---|---|
| 90d91a43 grouped_all | 2021 | 0.509093 vs 0.509587 ✓ | 0.722536 vs 0.722995 ✓ | 0.893288 vs 0.893783 ✓ | 1.039291 vs 1.039615 ✓ | [1,2,3,4] |
| 9c4c7577 grouped_all | 2022 | 0.508990 ✓ | 0.722291 ✓ | 0.893372 ✓ | 1.039081 ✓ | [1,2,3,4] |
| 66583ab2 control_mlp | 2022 | 0.508706 ✓ | 0.722800 ✓ | 0.893707 ✓ | 1.039363 ✓ | [1,2,3,4] |
| ef02938c control_mlp | 2021 | 0.508816 ✓ | 0.722949 ✓ | 0.893498 ✓ | 1.040117 vs 1.039615 ✗ | [1,2,3] |

The gate decides on MAE only. M07's index flag `beats_zero_return` also requires MSE, so it
marks 66583ab2 h2 false; my gate does not use MSE.

**Strategy results**, DEVELOPMENT_NOT_CONFIRMATORY. 19,385 bars from the first origin to the
last origin + 4; 674 bars without a forecast held. **All four cells: 0 trades**, so net
return 0, drawdown 0, exposure 0 and an undefined Sharpe. That is identical to the no-trade
baseline.

Cost lines: commission MODELLED 0 (no fills); slippage, spread and swap MODELLED 0;
broker_fill_costs BROKER_FILL NOT_AVAILABLE.

The reason is structural, not a failure of the forecasts. The heuristic's frozen entry
threshold is a 0.5% predicted move (ETH-scaled), and EURUSD 1–4 h forecasts are about
1e-4 in log return. In addition, the paired harness uses lane G's ETH sizing (1 unit on
10,000) and commission (0.001 per side). Both are uncalibrated for FX: a real EURUSD spread
is about 1e-4. An FX run needs FX-scaled parameters and costs, **declared before** any run.
Tuning them on this validation would be selection on the same data. That is a
coordinator/owner decision; nothing was tuned here.

**Split-half** (first 9,355 origins choose, last 9,356 are held out; zero-return naive with
mu = 0 declared, because M07's train mu for EURUSD was not provided and the train-mean naive
differs by less than 1e-3):

| Cell | Chosen on first half | Still passing on second half | Second-half skill |
|---|---|---|---|
| 90d91a43 | [1,2,4] | [1,2,4] | +0.0018 / +0.0011 / +0.0005 |
| 9c4c7577 | [] | — | — |
| 66583ab2 | [1,4] | [1,4] | +0.0028 / +0.0003 |
| ef02938c | [1] | [1] | +0.0021 |

Unlike ETH, chosen horizons mostly hold on the held-out half, but the margins are tiny
(0.03–0.28%).

Evidence: `.runtime/m05-paper-adapter-20260930/fx_lake_vdh/` (results, observability
JSONL, `fx_cells.json`, `fx_seed_summary.json`) and `fx_sha_{worker_a,worker_b}.txt`. One CPU
job on worker_b, `crispdm-run` 2G.

Satoshi, successor technical lead, 2026-10-01 (UTC).

## Addendum 18: EURUSD vDh under the coordinator's pre-declared FX rules: NOT economically viable

**Declared before the run, never tuned** (heuristic-strategy `560fa89`). Cost profile
`docs/contracts/fx_profile.eurusd_ibkr_canary.v1.json`, a versioned object:
- `spread_cap_price` 0.0003, from the IBKR canary profile;
- modelled round-trip spread 1e-4 (half charged per side);
- commission per side 0.001, the harness value. That is lane G's ETH figure, **not**
  IBKR's, and is flagged as such;
- slippage and swap 0;
- broker fills NOT_AVAILABLE.

The SIGN rule: position = sign of the predicted cumulative return at the strongest passing
horizon, defined as the largest record skill. It is held for that horizon's bar count,
trades never overlap, and there is no threshold and no grid. The frozen 0.5% rule gave
0 trades (addendum 17). The bootstrap interval is NOT_AVAILABLE: C2's block-bootstrap rule
has not been delivered to M05.

Per cell (h* = 1 in every cell; 18,710–18,711 one-bar trades). Edge is per unit, close to
close; z uses sigma 0.001226 and mu −2.1e-6, recovered from the predictions' own
z/log-return pairs.

| Cell | Seed | Hit rate | Mean gross edge (price) | Edge (z) | Break-even round trip | Ratio to the 1e-4 spread |
|---|---|---|---|---|---|---|
| 90d91a43 grouped_all | 2021 | 0.5182 | 2.15e-05 | 0.0156 | 2.15e-05 | 0.21× |
| 9c4c7577 grouped_all | 2022 | 0.5154 | 1.50e-05 | 0.0105 | 1.50e-05 | 0.15× |
| 66583ab2 control_mlp | 2022 | 0.5200 | 2.16e-05 | 0.0159 | 2.16e-05 | 0.22× |
| ef02938c control_mlp | 2021 | 0.5216 | 2.75e-05 | 0.0199 | 2.75e-05 | 0.28× |

Episode metrics (harness execution: fill at next open, 1 unit on 10,000 cash, so the
fractions are tiny):

| Cell | GROSS net return | GROSS Sharpe | Net, spread only | Net, declared profile | Exposure |
|---|---|---|---|---|---|
| 90d91a43 | +0.000040 | 0.0196 | −0.000030 | −0.0016 | 0.965 |
| 9c4c7577 | +0.000028 | 0.0137 | −0.000027 | −0.0013 | 0.965 |
| 66583ab2 | +0.000041 | 0.0201 | −0.000028 | −0.0016 | 0.965 |
| ef02938c | +0.000051 | 0.0253 | −0.000016 | −0.0015 | 0.965 |

The no-trade baseline is 0 on every metric.

**Verdict:**
- The sign of the forecast carries a real but tiny edge: a hit rate of 51.5–52.2% over
  ~18.7k trades per cell.
- The break-even round-trip cost is 1.5–2.8e-5 price units, **0.15–0.28× the modelled 1e-4
  spread alone**. Net of the spread alone, every cell loses; net of the declared profile it
  loses more.
- The result is **NOT economically viable** at any realistic EURUSD spread.
- DEVELOPMENT_NOT_CONFIRMATORY; no selection on validation.

Evidence: `.runtime/m05-paper-adapter-20260930/fx_lake_vdh/sign_rule/sign_rule_fx.json`.
Tests (heuristic-strategy): sign rule 5, red, then green. Mutants killed: overlap allowed (1),
spread not charged (1), mu ignored (1).

Satoshi, successor technical lead, 2026-10-01 (UTC).
