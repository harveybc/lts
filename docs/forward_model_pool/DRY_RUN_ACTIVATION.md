# Dry-run activation plan

This plan does not activate a route. `orders_submitted = 0`.
`PAPER_DEMO_EXECUTING = false`. `SHADOW_OBSERVING = false`.

## What the offline tools do

1. `tools/forward_pool_probe_parity.py` reloads the categorical bundle on CPU through the exporting code root, compares the saved probe logits, and compares `command_for` and `Sizing.order_units` with the local contract. It refuses stochastic mode. It prints hashes and counts only.
2. `tools/forward_pool_offline.py --state-dir DIR --fixture FILE` applies one hourly fixture under an isolated directory. A directory inside the live ledger roots is refused. A failure writes `FAILURE.json` with a reason code. The lock is exclusive and the state file is replaced atomically. Retries of a lock collision stop at three.

## What activation still requires

A first EURUSD paper or demo route is not created by either script. The owner must:

1. Issue a paper or demo capability through the existing mint procedure (`tools/mint_paper_capability.py` or the IBKR mandate tool). This delivery does not run those tools.
2. Bind that capability to a route whose venue can implement EURUSD with one position, mandatory stop and target, leverage 1:50, the 12-hour opening embargo and the 12-hour forced flat. MT5 and Alpaca are recorded as disabled until that proof exists.
3. Start the route only through the existing authorized paper/demo procedure. No unit is enabled here.

Until those three facts exist, the pool may keep shadow ledgers and a queued successor. It may not submit.

## Restart

A new process on the same isolated directory reloads the cohort, the last processed bar and the warmup list. It does not emit catch-up orders for bars already marked processed. An `effect_unknown` route effect must be reconciled before another submission of that effect.
