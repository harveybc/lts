# The third MT5 outcome: an unknown effect consumes its budget

**Ruling of the successor technical lead, under the owner's grant of 2026-09-26.**
Repository `lts`, branch `satoshi/mt5-unknown-outcome-20260926`.
Signed: **Satoshi** (Satoshi III, Mujuro Utsutsu), successor technical lead.

---

## 1. The defect, stated exactly

Two lines, each defensible alone, composed into a store-level lie with money on
the other side of it.

`app/mt5_execution_bridge.py`, `Mt5ExecutionStore.complete`:

```python
state = "succeeded" if payload.success else "failed"
```

`app/mt5_execution_bridge.py`, `Mt5ExecutionStore.enqueue`, the account-wide
daily entry budget:

```sql
SELECT COUNT(*) FROM execution_commands
 WHERE action LIKE 'open_%' AND created_at>=? AND state!='failed'
```

The EA already sent the terminal's `TRADE_RETCODE_*` in `result_code`, and
nothing read it. So a market that was closed (10018), an invalid volume (10014),
an account not entitled to trade (10017) and **a send that timed out (10012)**
all became the same `failed` row. Then the budget predicate skipped every
`failed` row, so the timed-out command **freed the daily slot it may still have
been holding**.

`TRADE_RETCODE_TIMEOUT` is the case where the terminal cancelled the REQUEST and
never learned what the trade server did with it. The position may exist.
Recording it as `failed` is the store asserting that it does not. At the budget
boundary that assertion admits a second entry — the one error in this lane that
can **double a position**.

RP149 (`app/mt5_refusal.py`, `command_state_for`) named the missing third state
and deliberately did not introduce it, because introducing it is a migration of
a service. This grant does the migration.

**Reproduced, not asserted.** With the two lines above restored, four tests in
`tests/unit/test_mt5_unknown_outcome.py` fail, and the budget one fails as
`DID NOT RAISE Mt5BridgeError` — the second entry is admitted. See §7.

## 2. The ruling

1. **An unknown effect is a third durable state, `effect_unknown`.** A distinct
   stored value, not a flag on `failed`. `failed` now carries one meaning only,
   and it is a strong one: the venue's own structured facts PROVE that no order
   exists.
2. **An unknown outcome CONSUMES its daily budget slot**, because the position
   may exist. **Releasing a slot requires a positive observation that no order
   exists, never the absence of a confirmation.** The predicate is written to
   deny by default: `consumes_budget_slot(state)` returns `True` for anything
   that is not in `BUDGET_RELEASING_STATES == {"failed"}`, so a state a later
   release invents holds the slot rather than freeing it. A `succeeded` entry
   does not release a slot either — it spent it.
3. **Reconciliation is the only exit.** An `effect_unknown` becomes `succeeded`
   or `failed` only through a READ-side broker query that observes the actual
   order state, recorded as a separate event with its own observation timestamp.
   Not a retry, not a resend, not a timeout, not an operator's assumption. The
   read-versus-mutating axis of `app/broker_refusal.py` is reused, applied to
   the **call site**: an `OrderObservation` refuses to exist unless it names a
   declared read-side query, and `MetaTrader5.order_send` is refused by name.
   An unanswered query raises `ReconciliationInconclusive` and the command keeps
   both its state and its slot.
4. **Nothing is rewritten in place.** Outcomes are APPENDED to
   `execution_command_outcomes`; every read path prefers the latest record per
   command. A reconciliation and a migration correction are new records with
   their own timestamps, and the record they supersede is named, kept and
   readable.
5. **No message is ever read.** Every verdict comes from a machine code, a
   boolean the venue set, the presence or absence of a structured field, or the
   call site a fact came from. The AST guard that forbids `lower`, `startswith`,
   `find`, `split` and `match` in this classification path now covers the new
   module and the migration tool as well.

## 3. What was built

| File | What it is |
|---|---|
| `app/mt5_unknown_outcome.py` | new. The state vocabulary, the deny-by-default budget predicate, the evidence vocabulary, `Outcome`, `OrderObservation` with the read-side call-site axis, and the three classifiers: a live result, a read-side observation, a historical row. Imports only `__future__`, `json`, `dataclasses`, `datetime`, `typing`, `app.broker_refusal`, `app.mt5_refusal` — pinned by an AST test. |
| `app/mt5_execution_bridge.py` | `complete` now writes the classifier's verdict (three states); `enqueue` counts held slots and treats an unknown effect as unresolved on its route; the append-only `execution_command_outcomes` table; `effective_state`, `outcome_history`, `unreconciled_unknown_effects`, `daily_entry_slots_consumed`, `policy_inputs`, `reconcile_unknown_effect`, `record_migration_correction`; `command_counts`, `command_for_idempotency` and `exposure_reconciliation` read the effective state; `/v1/status` publishes every unreconciled effect. |
| `app/mt5_model_runner.py` | `durable_command_heartbeat` reads the EFFECTIVE state and answers `command_effect_unknown` with `reconciliation_required`. It never calls an unknown effect flat, and it follows a reconciliation rather than the legacy column. |
| `tools/mt5_unknown_outcome_migration.py` | new. The migration: dry by default and READ-ONLY on the dry path, idempotent, prints its full row-level plan before writing anything, emits a JSON report. |
| `tests/unit/test_mt5_unknown_outcome.py` | new, 51 tests. |
| `tests/unit/test_mt5_refusal_recovery.py` | the RP149 test that PINNED the defect now pins its closure, and says what it used to record. |
| `docs/audits/evidence/mt5_unknown_outcome_20260926/` | the dry-run report (text + JSON) and a NOTE stating exactly what corpus it measured. |

### The budget, before and after

```
before:  ... AND state!='failed'                     # a column where every
                                                     # unproven outcome was
                                                     # already written 'failed'
after:   ... AND COALESCE(latest_outcome, state) NOT IN ('failed')
         # over a column where 'failed' now means PROVEN no order exists,
         # and where the latest APPENDED outcome governs
```

The SQL shape barely moved. The fix is that `failed` stopped being the dumping
ground for everything that was not a confirmed success.

## 4. The migration

`tools/mt5_unknown_outcome_migration.py` re-reads every row recorded `failed`
from the row's own structured evidence:

| evidence | verdict | why |
|---|---|---|
| `retcode_proves_no_order_exists` | `failed`, confirmed, nothing written | 10014/10018/10017 …: the server refused before placing |
| `never_delivered_to_the_ea` | `failed`, confirmed, nothing written | `delivered_at IS NULL`: the command never left our store |
| `retcode_leaves_outcome_unproven` | -> `effect_unknown` | 10011/10012/10024/10028/10031: sent, never confirmed |
| `no_machine_code_reported` | -> `effect_unknown` | a stored result with no retcode at all |
| `delivered_without_a_result` | -> `effect_unknown` | delivered, and no result was ever recorded |
| `result_contradicts_itself` | -> `effect_unknown` | recorded `failed` while its stored result reports DONE; the contradiction is reported, never resolved |
| `record_cannot_support_either_reading` | -> `effect_unknown`, **and the row says so** | the stored result is not readable as a result; a record that cannot distinguish "refused before sending" from "sent, never confirmed" is not evidence that nothing was sent |

**It never rewrites a row.** Each correction is an appended
`migration_correction` outcome with its own `recorded_at`, naming the state it
supersedes; the old label stays in the column and the read path prefers the
latest record. **Idempotent** by `(command_id, migration_correction)`: a second
run reports `already_corrected` and writes nothing. Rows whose label is
confirmed get no record at all, which is what makes convergence visible.

**Dry by default**, and the dry path opens the database with
`file:<path>?mode=ro` so it cannot write even if this tool were wrong. In both
modes the complete row-level plan is printed BEFORE anything is written.

### Dry-run counts (published)

Run against a **recorded corpus** — nine historical rows, one per shape the old
line could leave, built by the committed test helper on a throwaway SQLite file.
Verbatim output and JSON in
`docs/audits/evidence/mt5_unknown_outcome_20260926/`.

| | |
|---|---|
| commands examined | 9 |
| relabelled `failed` -> `effect_unknown` | 5 |
| `failed` confirmed from its own evidence | 2 |
| out of scope (not a `failed` row) | 2 |
| **entry budget slots reclaimed (now held, not free)** | **5** |

**There is no live corpus.** Verified 2026-09-26 on this host: no
`mt5-bridge.sqlite` exists anywhere on the filesystem, no MT5 execution bridge
process is running, and RP149 recorded that this lane has never placed an order.
Run against the configured live path the tool exits `error: no such database`.
So these counts measure the MIGRATION, not the fleet's history, and the NOTE
beside them says so.

## 5. How to apply it (the operator's step, not mine)

The dry run is safe at any time; it is read-only.

```bash
# 1. dry, read-only, prints every change it would make. Safe while the bridge runs.
python tools/mt5_unknown_outcome_migration.py \
    --database ~/.local/state/lts/mt5-bridge.sqlite \
    --json-report docs/audits/evidence/mt5_unknown_outcome_<date>/dry_run_live.json

# 2. apply. ONLY when the bridge is not running against that database.
python tools/mt5_unknown_outcome_migration.py \
    --database ~/.local/state/lts/mt5-bridge.sqlite \
    --apply --attest-bridge-stopped
```

`--apply` refuses without `--attest-bridge-stopped`. That flag is an operator
attestation and nothing else: **this tool never starts, stops or restarts a
service, and neither did this work.** When it is safe: the bridge process is not
running against that database, or the file is a copy. Appending to a SQLite file
a live FastAPI service holds open is the operator's decision to make, and the
migration is append-only precisely so that applying it late is harmless — a
command already corrected is skipped, and no row is ever edited.

## 6. Hard limits observed

* No real capital, no broker mutation, no live account. `execution_authorized:
  true` appears nowhere in this branch.
* Every broker fact in every test is an object built in the test and handed to
  the real classifier or the real store on a temporary SQLite file. Sockets are
  booby-trapped module-wide in the new suite.
* No service was started, stopped or restarted. No running process was touched.
* No account identifier, hostname, IP or secret is written anywhere. The
  fingerprint in the fixtures is a synthetic hex string.
* The dry run was executed against a throwaway file outside the repository; the
  published report's `database` field is a placeholder.

## 7. Evidence

```
suites: tests/unit 1478 passed (was 1478 before this branch: 1427 + 51 new)
        mt5 + refusal focus: 300 passed
old-behaviour check: with `state = "succeeded" if payload.success else "failed"`
        and `AND state!='failed'` restored, 4 of the new tests fail, the budget
        one as "DID NOT RAISE Mt5BridgeError" — the second entry is admitted at
        the boundary. The two lines were then restored to the fixed form.
```

The test that fails on the old slot-freeing behaviour is
`test_an_unknown_effect_holds_its_daily_entry_slot`. Its mirror,
`test_a_proven_rejection_still_releases_the_slot`, exists so the fix cannot be
"block everything".

`lts`'s conftest is sqlalchemy-eager — it imports `sqlalchemy`, `app.database`
and `fastapi.testclient` at collection time, so collection dies in any
interpreter missing them. It bit once here: the default interpreter raised
`ConftestImportFailure: No module named 'fastapi'`. Worked around by naming the
environment that satisfies those imports explicitly on the pytest invocation
(`<env-with-requirements.txt>/bin/python -m pytest`, the conda env that carries
fastapi and sqlalchemy on this host) rather than by touching the conftest. No test in this branch needs the sqlalchemy fixtures.

## 8. What is NOT done, refused, or not measured

* **No canary.** No MT5 order has been placed, acknowledged or reconciled
  against a real terminal by this work, and none can be until a confirmed Demo
  account and a risk mandate exist. The reconciliation path is exercised against
  constructed observations only; that it reads the broker correctly is a
  contract here, not evidence.
* **The retcode tables remain vendor documentation**, transcribed by RP149 and
  not verified against a live terminal. Which codes this broker's server
  actually returns, and whether 10011/10012/10028/10031 do leave an effect
  behind on THIS server, is unmeasured. The classification is conservative in
  the safe direction.
* **`reconcile_completed_lifecycles` in `app/mt5_model_runner.py` still reads
  `state='succeeded'` from the column**, so it will not act on a command that
  became `succeeded` through a reconciliation. That is fail-closed (it skips
  rather than repairs) and deliberately left, because widening it changes an L0
  lifecycle path this grant does not cover.
* **`exposure_reconciliation` does not yet adopt a reconciled order's ticket.**
  A command reconciled to `succeeded` carries the observed ticket only in its
  outcome detail, so a matching position may be reported as unexpected until the
  EA's own result carries the ticket. Conservative (it alarms rather than
  authorizes), and named here rather than half-wired.
* **No live corpus was migrated**, because none exists (§4).
* **No linter or type checker** is configured in this repository; nothing here
  claims to be lint- or type-clean.

---

## 9. Erratum, 2026-09-26 — two published numbers of mine were wrong

Added after the forensics of the five `failed` MT5 commands read the store that
actually holds them (`docs/audits/work_plan/SATOSHI_MT5_FIVE_FAILURES_FORENSICS_2026_09_26.md`)
and after the corrections that followed it
(`docs/audits/work_plan/SATOSHI_MT5_CORRECTIONS_2026_09_26.md`).
Signed: **Satoshi** (Satoshi III, Mujuro Utsutsu), successor technical lead.

**The claims above are left standing as published.** An erratum names what was
wrong and why; it does not edit the claim away.

### Erratum 1 — "entry budget slots reclaimed (now held, not free): 5"

§4's table and `docs/audits/evidence/mt5_unknown_outcome_20260926/NOTE.md` publish
**5**. Against the real store the number is **1**.

* **What was wrong.** The 5 is the count over the **nine-row recorded corpus**
  built by the test helper — one row per shape the old collapsing line could
  leave. It measures the migration, not the fleet.
* **Why it was published.** §4 states that there is no live corpus and that the
  counts therefore measure the migration, and the NOTE beside the artifacts says
  the same. The arithmetic is right for what it counted. The error is that a
  count over a synthetic corpus was published in the same shape as a finding
  about the lane, where a reader can carry it away as one.
* **What the real store holds.** 53 commands, 48 succeeded, 5 failed. Exactly
  **one** of the five — an `open_short` of 2026-08-03 storing `result_code: 0` —
  is typed `effect_unknown` by the committed classifier, so exactly one budget
  slot was freed wrongly. The other four carry `result_code: 10018`
  (`TRADE_RETCODE_MARKET_CLOSED`), are `retcode_proves_no_order_exists`, and
  releasing their slots was correct then and stays correct under the fix.

### Erratum 2 — "this lane has never placed an order"

§4 relays RP149's record that "this lane has never placed an order", and the
evidence NOTE repeats it. **At the lane level that is false.**

* **What was wrong.** It was true of *this host* — no `mt5-bridge.sqlite` exists
  here, no bridge process runs here — and it was relayed as a statement about the
  lane. The lane's store is on another host, and it holds a real order history.
* **What the real store holds.** **42 opens and 6 closes succeeded**, with real
  order tickets, real deal tickets and a real position history, from
  2026-08-03T22:21:21Z to 2026-09-01T21:01:00Z. The command vocabulary is
  `{open_long, open_short, close}` and contains **no read-only verb**, so every
  command this bridge can carry is a mutating order operation.
* **Why it matters beyond the number.** §8's first bullet ("No canary … none can
  be until a confirmed Demo account and a risk mandate exist") was written on the
  same belief. The canary had already run. The account is a demo account on
  strong but **self-declared** evidence — `environment: "demo"` in the deployed
  bridge config, in the runner config, and in all 220,643 heartbeat rows with no
  second distinct value — and the lane's L1 attestation tables
  (`l1_capabilities`, `l1_broker_facts`, `l1_effects`) are **empty**, so nothing
  broker-side certifies the trade server. Nothing in this ruling or its erratum
  assumes a certification we do not have.

### Erratum 3 — §2.3's "only through a READ-side broker query" was too strict

Not a wrong number: a wrong rule, and it was mine.

Requiring a **live** broker query as the only exit from `effect_unknown` is
stricter than the evidence requires and, whenever the terminal has gone silent,
unsatisfiable — so an unknown effect would stay unknown forever and its route
blocked forever. The correction admits a second source, the lane's own retained
`account_snapshots` and `trade_events`, under conditions strict enough that the
absence of data can never be read as the absence of an order. See
`docs/audits/work_plan/SATOSHI_MT5_CORRECTIONS_2026_09_26.md` §1, and note the
finding it produced: under those conditions the one real unknown effect in this
fleet **still refuses**, because its own window holds a 424.7-second hole.

### One recount, for completeness

The forensics reports "13 consecutive account snapshots" across that window. The
store holds **12** strictly inside it, ids 3837-3848 with no id missing, plus the
bracketing observation at 23:30:24.922Z; and they are not one-minute continuous —
the intervals run from 45 s to 424.7 s. The substance of the forensics' finding
(no exposure was observed at any point it observed) is unchanged; the word
"consecutive" was doing work the rows do not support.
