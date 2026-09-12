# Alpaca paper runner — transient-failure recovery (R4, 2026-09-11)

**Status:** correction landed in code and tests. **NOT yet applied to the
running service.** Restarting the runner is an operator action after
external review, not a side effect of a fix.

## What was wrong

`plugins_broker` / `app/alpaca_paper_lab.py` wraps every transport error:

```python
except requests.RequestException as exc:
    raise AlpacaPaperError(f"{endpoint} request failed: "
                           f"{type(exc).__name__}") from exc
```

`classify_runner_exception` inspected only the exception it was handed,
so `AlpacaPaperError` — an unknown type — was **fatal**. The runner then
parked on `fatal_retry_seconds` (3600 s) with a degraded heartbeat, while
the read-only observer on the same host was reconnecting normally.

Observed heartbeat before the fix:

```json
{"state": "degraded_error", "phase": "fatal",
 "error": "AlpacaPaperError: account request failed: ConnectionError"}
```

This was a **taxonomy and recovery failure**. It was never an
authorization to send another order.

## What changed

`app/runner_retry_taxonomy.py`

* `transient_cause()` follows the **explicit** `__cause__` chain that
  `raise ... from` builds, bounded at `MAX_CAUSE_DEPTH = 8` and
  cycle-safe. `__context__` is deliberately **not** followed: a config
  refusal raised inside an `except ConnectionError` block must not become
  retryable.
* The message is still never read. `"cannot connect to Alpaca Live"` is a
  fatal refusal and stays fatal.
* `requests`' timeout family (`Timeout`, `ConnectTimeout`, `ReadTimeout`,
  `ProxyError`) is named explicitly — none of them subclasses
  `TimeoutError` or `ConnectionError`.
* `backoff_seconds(consecutive, base, cap)` — bounded exponential.

`app/alpaca_model_runner.py`

* consecutive transient failures back off 15 s → 30 s → … → 300 s cap; a
  good tick resets the counter to zero.
* the degraded heartbeat now names `transient_cause`,
  `consecutive_transient` and `retry_in_seconds`.
* the fatal path is unchanged: wrong account, bad artifact, schema or
  binding refusals still park on `fatal_retry_seconds`.

Optional per-deployment keys in the runner config, with the defaults
above: `transient_backoff_base_seconds`, `transient_backoff_cap_seconds`.

`app/ibkr_model_runner.py` shares the classifier and therefore inherits
the cause-chain fix; its own connect-backoff loop was **not** modified.

## Operator recovery procedure

Run only after the external review of this order. Each step is
read-first, and no step sends, cancels or modifies an order.

1. **Confirm connectivity independently of the runner.** The read-only
   observer is the authority here:

   ```
   systemctl --user start lts-alpaca-paper-observer.service
   systemctl --user status lts-alpaca-paper-observer.service --no-pager
   ```

2. **Read the runner's current heartbeat** and record the pre-restart
   state (it is evidence, not noise):

   ```
   cat ~/.local/state/lts/alpaca-model-runner-heartbeat.json
   ```

3. **Confirm the open paper order is untouched.** The correction does not
   cancel it and neither does this procedure. Inspect it through the
   read-only path before and after; the two readings must agree.

4. **Restart the runner** so the corrected classifier is loaded:

   ```
   systemctl --user restart lts-alpaca-model-runner.service
   ```

5. **Verify recovery, not silence.** Within one `loop_seconds` the
   heartbeat must either advance with `state` not `degraded_error`, or
   show `phase=connect` with `consecutive_transient` and
   `retry_in_seconds` present. A heartbeat that stays on `phase=fatal`
   means the failure was genuinely fatal and must be read, not retried.

6. **Do not run an extraordinary tick.** `--once` exists for
   diagnostics; using it to "catch up" a missed bar would create a
   decision outside the cadence the ledger assumes.

## What this does not do

* It grants no trading permission and changes no risk envelope.
* It does not cancel, replace or re-price the open paper order.
* It does not run an out-of-cadence tick.
* It does not weaken any fatal class: account, artifact, schema and
  binding refusals remain fatal, wrapped or not.
