# MT5 unknown-outcome migration — dry-run evidence, 2026-09-26

`dry_run_recorded_corpus.txt` is the verbatim stdout of

    python tools/mt5_unknown_outcome_migration.py --database <corpus> \
        --json-report dry_run_recorded_corpus.json

and `dry_run_recorded_corpus.json` is the machine-readable report of the same
run. The `database` field is a placeholder: the run was made against a
throwaway file outside the repository.

## What the corpus is, and what it is NOT

It is a **recorded corpus**, built by
`tests/unit/test_mt5_unknown_outcome.py::_corpus` against a temporary SQLite
file: nine historical `execution_commands` rows, one per shape the old
collapsing line (`state = "succeeded" if payload.success else "failed"`) could
leave behind. Every row was written by that helper. The account fingerprint in
it is a synthetic hex string and is not an account identifier.

It is **not** a copy of a live corpus, because there is no live corpus. Verified
2026-09-26 on this host: no `mt5-bridge.sqlite` exists anywhere on the
filesystem, no MT5 execution bridge process is running, and RP149 recorded that
the MT5 Demo lane has never placed an order (no confirmed account, no
acknowledgement, no fill). Run against the configured live path the tool exits
with `error: no such database`, which is the honest state of that lane.

So the counts below measure the MIGRATION, not the fleet's history. When the
bridge does run and accumulate rows, the same command re-run against the real
database produces the real counts, and the row-level plan is printed before
anything is written.

## Counts on the recorded corpus

| | |
|---|---|
| commands examined | 9 |
| relabelled `failed` -> `effect_unknown` | 5 |
| `failed` confirmed from its own evidence (no record written) | 2 |
| out of scope (not a `failed` row) | 2 |
| entry budget slots reclaimed (now held, not free) | 5 |

By evidence, for the seven `failed` rows re-read:

| evidence | rows | reading |
|---|---|---|
| `retcode_leaves_outcome_unproven` | 1 | TRADE_RETCODE_TIMEOUT: sent, never confirmed |
| `no_machine_code_reported` | 1 | a result with no retcode at all |
| `delivered_without_a_result` | 1 | delivered to the EA, no result ever recorded |
| `result_contradicts_itself` | 1 | recorded failed, stored result reports DONE |
| `record_cannot_support_either_reading` | 1 | the stored result is not readable; the row says so |
| `retcode_proves_no_order_exists` | 1 | TRADE_RETCODE_MARKET_CLOSED: nothing was placed |
| `never_delivered_to_the_ea` | 1 | never handed to the EA, so nothing was sent |
