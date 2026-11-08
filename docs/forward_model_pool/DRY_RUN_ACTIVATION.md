# Dry-run activation plan

This plan does not activate a route. `orders_submitted = 0`.
`PAPER_DEMO_EXECUTING = false`. `SHADOW_OBSERVING = false`.

## What the offline tools do

1. `tools/forward_pool_probe_parity.py` reloads the categorical bundle on CPU through the exporting code root, compares the saved probe logits, and compares `command_for` and `Sizing.order_units` with the local contract. It refuses stochastic mode. It prints hashes and counts only.
2. `tools/forward_pool_offline.py --state-dir DIR --fixture FILE` applies one hourly fixture under an isolated directory. A directory inside the live ledger roots is refused. A failure writes `FAILURE.json` with a reason code. The lock is exclusive and the state file is replaced atomically. Retries of a lock collision stop at three.

## What activation still requires

A first EURUSD paper or demo route is not created by either script. `tools/mint_paper_capability.py` is a single-use IBKR capability tool. It is not an MT5 activation procedure, and this plan does not ask the owner to run it for EURUSD.

1. Reuse the existing MT5 demo authorization path. This delivery does not mint a capability and does not restart the VM.
2. Bind a route only after EURUSD symbol, protection, margin and calendar facts are read from that path. The running Alpaca SPY service is not this route.
3. Start the route only through the existing authorized demo procedure. No unit is enabled here.

Until those three facts exist, the pool may keep shadow ledgers and a queued successor. It may not submit.

## Restart

A new process on the same isolated directory reloads the cohort, the last processed bar, the warmup list, route effects, the unknown-outcome halt and close history. It does not emit catch-up orders for bars already marked processed. A journaled effect whose transport had started, and any `effect_unknown`, must be reconciled before another submission. Executor cash is not replaced by a shadow balance.
