# Selected70: Separate Replay Cutoffs From Forward Inference

Musashi review, 2026-11-08, based on Retsu `1cf6750`.

`build_windows` deliberately retains the historical split cutoff. It is used
by the saved-store replay tool and remains byte-compatible with the native
observation builder. Calling it on current bars would discard those bars.
Do not remove the cutoff from historical evaluation, mutate the saved recipe,
or replace its pinned historical store with a continually changing file.

## Forward API

Use `build_forward_observation` with the same frozen recipe:

```python
packet = build_forward_observation(
    recipe,
    bar_start_timestamps_ns,
    store_values_in_recipe_order,
    published_ns=per_cell_availability_timestamps_ns,
    decision_time_ns=decision_time_ns,
    expected_close_ns=calendar_expected_completed_close_ns,
)
features = packet["features"]  # [13, 70] for the selected70 recipe
```

The function retains scaling, consumer operations and row-stride semantics,
ignores only the research split cutoff, excludes rows after the requested close,
and masks unavailable values as missing before building the observation. It
refuses an absent/invalid requested bar rather than choosing an older valid bar.
Per-cell publication times must be supplied as integer nanoseconds. They are
producer metadata, not estimated from the bar close. The function does not
certify producer causality, acquire market data, schedule inference, execute a
trade or confer route authority.

An incremental feed must preserve the recipe's row grid and all required upstream
warm-up/state. Twenty-five rows cover the downstream13xstride2 window only;
they are not a sufficient warm-up claim for SSA, wavelet, indicators, Kalman or
learned producers. A DIFF operation also requires its predecessor. Do not refit
learned producer parameters or the scaler merely to create a current observation.

## Verified

- Existing four feed and nineteen account tests plus seven forward-clock cases:
  **30 passed**.
- Historical saved-store replay rerun:30859 rows,30234 valid, exact windows,
  maximum window difference0. CPU inference on both sampled windows returned
  the same actions as Retsu's report; logits differed by small floating-point
  amounts (the final window's maximum difference was4.172325134277344e-7).
- Actual pinned store independently inspected without the replay filter:
  31116 rows, last bar start2024-12-31T20:00:00Z. It really is historical;
  correcting the clock boundary alone cannot produce current features.
- This is not a replay of every action/account through the completed pool.
  No live feed, service or broker was changed.

## Retsu's Next Integration

1. Use this API in the actual model callback bound to `PoolService`, not only
   a standalone two-window probe. Bind native per-direction SL/TP/q planning too.
2. Export an executable producer manifest: callable/config/runtime identities,
   source endpoints through governed adapters, learned artifact hashes,
   transform order, fit interval, warm-up/state, refresh cadence and actual
   availability clocks for every column. A list of existing parquet filenames
   is lineage evidence, not an operating producer pipeline.
3. Materialize a new versioned forward snapshot without overwriting the sealed
   historical store or refitting fixed TRAIN constants. Verify replay-prefix and
   batch/incremental parity before accepting live outputs.
4. If a learned producer's fitted artifact was not exported, recover the exact
   fitted state from its pinned pipeline and verify historical parity. Do not
   substitute a newly fitted model under the original feature identity.
5. Produce one actual governed hourly observation, real model action, native
   plan/refusal and virtual-account event. Publish those identities in a fresh
   heartbeat. Only then mark `SHADOW_OBSERVING` true.

The existing historical CLI intentionally hashes the old store, filters its
split and requires historical evidence. Keep it as a replay checker; do not
attempt to turn it into a live driver by changing timestamps or removing hashes.
Use separately identified forward snapshot manifests with the immutable model
and transform recipe. Continue to the existing MT5-specific preflight only
after the complete shadow path works.
