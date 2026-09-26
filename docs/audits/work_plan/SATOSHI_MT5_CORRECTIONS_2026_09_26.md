# Three MT5 corrections, one of them to my own ruling

**Corrections of the successor technical lead, 2026-09-26.**
Repository `lts`, branch `satoshi/mt5-corrections-20260926`, from `f1109ba`
(`satoshi/mt5-five-failures-forensics-20260926`).
Signed: **Satoshi** (Satoshi III, Mujuro Utsutsu), successor technical lead.

Read-only against every live thing. No broker mutation, no order, no cancel, no
modify, no `execution_authorized: true`, no service started, stopped or
restarted, and the migration's `--apply` was not run. Every database connection
opened `file:<path>?mode=ro`; the store's mtime is unchanged and verified (§5).
No account identifier, host name, IP or token is written anywhere here; the
bridge host is referred to by role and tickets are masked.

---

## 0. The headline

1. **Correction 1 is mine and it is now implemented — and it does NOT rescue the
   one real unknown effect.** Reconciliation admits a second source, the lane's
   own retained `account_snapshots` and `trade_events`, with six conditions that
   can each refuse. Against the real rows the one `effect_unknown` in this fleet
   **refuses as inconclusive**: its window holds a **424.7-second hole**, present
   on the terminal's own clock, in which the account was not observed at all. The
   ruling was too strict; the records are also thinner than the forensics said.
   The command keeps its state and its slot, and its route stays blocked — by
   evidence this time, not by vocabulary.
2. **Correction 2, the unreachable branch, is fixed and pinned.**
   `exposure_reconciliation` branched on `"close_position"`, a verb the vocabulary
   has never contained. It is now `_CLOSE_ACTIONS`, the vocabulary is asserted to
   be partitioned at import, and an AST test refuses the old spelling anywhere in
   the module. The same mismatch appears **nowhere else** in that path (§2).
3. **Correction 3, the scheduling defect, is repaired.** A command is no longer
   issued into a known venue break. The break comes from configuration or is
   **derived from the venue's own refusals in the record** — for this store,
   `21:00 -> 21:30 UTC` from four `MARKET_CLOSED` refusals on four distinct dates
   and the observed minute at which the venue was next seen accepting. Nothing is
   hardcoded; a record that cannot establish a break derives none and says so.
4. **Two published numbers of mine are amended by erratum**, in the original
   return rather than edited away: "5 slots reclaimed" (the real store says
   **1**) and "this lane has never placed an order" (**42 opens succeeded**).
   `docs/audits/work_plan/SATOSHI_MT5_UNKNOWN_OUTCOME_2026_09_26.md` §9.
5. **The eligibility flag is investigated and NOT adjudicated.** `live_execution_eligible`
   gates one thing in code: promotion to the `promoted_paper` tier. The lane ran
   `demo_research_canary`, where the gate is `research_validated`, which held. So
   by the code as written nothing on this route read that flag, and the one reader
   that did read it reports rather than refuses. Whether it should have barred the
   execution is a mandate question, and I do not rule on it (§4).

---

## 1. Correction 1 — reconciliation admits retained records, and still refuses F1

### The rule as it now stands

`app/mt5_unknown_outcome.py`, ruling item 3. Two admissible sources, and neither
may be satisfied by a retry, a guess, a timeout or an operator's assumption:

* `SOURCE_LIVE_BROKER_QUERY` — a live read-side query, unchanged;
* `SOURCE_RETAINED_RECORDS` — the lane's own `account_snapshots` and
  `trade_events`, read through `QUERY_RETAINED_RECORDS` (`lts.mt5.retained_records`),
  a declared read-side query that is refused by name if offered as mutating.

The retained source has its **own evidence token**,
`retained_records_observed_no_order`, so a reader can tell the two sources apart
without opening the detail. The exit is still an APPENDED event, still carries its
own observation timestamp, and now also carries the source, the tables and the row
ids it rests on.

### The six conditions, each of which can refuse

`RetainedRecordWindow` carries the rows; `observation_from_retained_records`
admits or raises `ReconciliationInconclusive`. The window runs from the moment the
unknown command **completed** to the creation of the route's **next** command —
the only interval in which an order from it could appear and still be attributable
to it.

| # | condition | why it exists |
|---|---|---|
| 1 | an observation at or before the completion, reading flat | the account's state must be known when the window opens |
| 2 | an observation at or after the window's end — or, for a window that runs to the end of the record, a record still FRESH within the declared budget | coverage at the far end; **a stale store cannot settle a window that reaches the present**, which is exactly the silent-bridge case |
| 3 | every observation inside reads flat, with both counts PRESENT | a missing count is never read as zero |
| 4 | zero `trade_events` received for the window | a pushed transaction is a positive event and refuses outright |
| 5 | no interval of the window unobserved for longer than the **declared** budget — the sequence runs from the opening observation through every inside observation to the window's own end | this is the condition that stops "sampled across a gap" from passing as "continuous" |
| 6 | the retained source may only ever report `order_exists=False` | it can witness that nothing is there; it cannot attribute a position it DOES see to one command rather than another, and a reconciliation that guessed the attribution would be the same class of error the module exists to forbid |

**The budget is never invented.** It is declared: the deployed bridge config's
`stale_heartbeat_seconds` (**180 s**), passed in by the caller. The report tool
refuses to run without a declared budget.

### What the real rows say

`tools/mt5_retained_reconciliation_report.py` — read-only, writes nothing,
reconciles nothing — run against the masked extract of the deployed store
(`docs/audits/evidence/mt5_corrections_20260926/`):

| | |
|---|---|
| candidates (an unobserved effect under either label) | **1** |
| admissible — the retained record settles it | **0** |
| inconclusive — the retained record refuses | **1** |

The one candidate is the `open_short` of 2026-08-03 (`result_code: 0`), which the
store still labels `failed` because the migration has never been applied and the
committed classifier types `effect_unknown`. Its window:

| | |
|---|---|
| window | 2026-08-03T23:30:24.951197Z -> 23:47:00.000173Z, closed by the next command |
| bracketed at both ends | yes (snapshot ids 3836 and 3849) |
| observations strictly inside | **12**, ids 3837-3848, no id missing, all `positions_total 0` and `orders_total 0` |
| `trade_events` received for the window | **0** |
| **largest unobserved interval** | **424.7 s**, 23:39:09.928Z -> 23:46:14.636Z |
| declared budget | 180.0 s |
| verdict | **inconclusive**, and the refusal names the gap |

**The hole is real, not a transport artefact.** On the terminal's own clock the
snapshot gap is 425 s and the heartbeat gap 410 s, at
`terminal_observed_at 23:39:09 -> 23:46:14`. The EA stopped observing and stopped
posting; it did not buffer and catch up. The lane's whole-day cadence either side
is 60 s for snapshots and 15 s for heartbeats, and the comparable windows of two
other failures are continuous at 60 s — so this is a hole in this window, not the
normal texture of the stream.

**What narrows the doubt without closing it**, recorded for the owner and the
auditor and deliberately **not** an admission condition in code:

* balance and equity read exactly `10000.0` at 23:39:09 and again at 23:46:14 and
  23:46:59, so a completed round trip inside the hole would have had to leave the
  realized balance untouched to stored precision;
* the first non-zero `positions_total` in the entire 55,218-snapshot history is
  23:47:14.765Z — the **next** command's fill;
* no trade event of any kind was received before that fill.

A position opened inside the hole and still open would have shown at 23:46:14. The
residual is a round trip opened and closed inside 7 minutes — and with a stop
~0.4 % away on a 4-hour ETHUSD bar that is not a theoretical residual. **So the
honest answer is that the retained record does not settle it, and I am not going
to write a condition that lets it.**

### What this buys, given that

The path is live and useful for every future unknown: the same window built over a
normal 60-second stream is admitted, releases the slot, unblocks the route and
appends an event naming its source and rows — proven end to end on a real SQLite
file in `test_the_store_builds_the_window_and_the_slot_is_released`. What it does
not do is manufacture a release for the one case whose record has a hole in it.

**Every refusal keeps the command's state and its slot**, and there is a test for
exactly that (`test_a_refusal_keeps_the_state_and_the_slot`): the command stays
`effect_unknown`, the day's slot stays consumed, no outcome is appended, and the
route still refuses a new order.

---

## 2. Correction 2 — the branch that had never executed

`Mt5ExecutionStore.exposure_reconciliation` retired a closed position's ticket
from the authorized set on `action == "close_position"`. The vocabulary is
`{open_long, open_short, close}`. The branch had never run, so a ticket the lane
had already closed was never retired.

Fixed three ways, because a typo that a test cannot see will come back:

1. the close verb is named once, `_CLOSE_ACTIONS = frozenset({"close"})`, and the
   branch tests membership;
2. `_ACTIONS` is now **derived** as `_OPEN_ACTIONS | _CLOSE_ACTIONS` with an
   import-time assertion that the partition is exact and disjoint, so adding a
   third kind of action fails at import instead of leaving a branch quietly
   unreachable;
3. `test_a_closed_position_stops_being_authorized` fails on the old spelling, and
   `test_no_mt5_path_branches_on_a_verb_the_vocabulary_lacks` walks the module's
   AST and refuses the string `"close_position"` anywhere in it.

**Does the same mismatch appear anywhere else in that path? No.** Searched across
the repository: `close_position` survives only in `app/alpaca_l1.py` (a real
Alpaca SDK method), in three test files as a broker double, and in `tools/session_directive_dry_run.py`
(a refusal stub) — all of them different vocabularies, all correct. The MT5 path
had exactly one occurrence and it is gone. The `metadata` row that still calls this
store `lts.mt5.bridge.readonly.v1` is the other cosmetic-but-misleading record the
forensics named; it lives in the deployed database, not in code, and changing it
would be a write to the live store, so it is left for the owner.

---

## 3. Correction 3 — the scheduling repair

### What the record establishes

Zero of 47 commands created outside 21:00-22:59 UTC ever failed; five of the six
created inside it did. Four of those carry `10018` (`MARKET_CLOSED`) and were
created at **21:00:48, 21:00:58, 21:00:58 and 21:01:00 UTC** on four distinct
dates. Minute-of-day 1260 (21:00) holds 3 commands, all failed; 1261 (21:01) holds
1, failed. **No command created at either minute has ever succeeded**, and the
nearest later minute at which one did succeed is **1290 (21:30)**.

### The repair

`app/mt5_venue_break.py` is new and does one thing: decide whether this is a
moment at which a command may be issued at all. It never decides what the command
would be, never touches a size, a stop, a target or a direction, and never touches
the outcome vocabulary.

**Configuration is authoritative** when it declares breaks
(`venue_breaks: [{"start_utc": "21:00", "resume_utc": "21:30"}]`), strictly parsed:
a malformed declaration raises rather than defaulting, a window that would wrap
midnight is refused as two breaks, and a window longer than 120 minutes is refused
as an owner decision rather than a schedule detail.

**Otherwise the break is derived from the record**, and the derivation can refuse.
It reads only machine codes — `MARKET_CLOSED_RETCODES` is *computed* from the
committed `MT5_RETCODE_KINDS` table, never transcribed again — and requires:

* the refusal observed on at least **2 distinct UTC dates** (one refusal is an
  incident, not a schedule);
* **no command succeeded inside** the window the refusals span — a success inside
  it proves the window is not a break, and the derivation refuses rather than
  declaring one anyway;
* the record itself shows a minute **after** the refusals at which a command
  succeeded; that observed minute, not a pad or a guess, is where the break
  resumes;
* the result is under the ceiling.

Against this store that yields `21:00 -> 21:30 UTC`, `source: observed_record`,
with its evidence naming the four refusal rows, the four dates, the refused
minutes and the resume minute. If any condition fails the lane carries **no**
break and records `venue_break_undeclared: <reason>` — it never falls back on a
guessed window.

**Where it bites.** `Mt5ModelRunner.tick` consults it immediately after the
snapshot-freshness gate, before any command can be enqueued, so it covers all four
enqueue paths (the model entry, the model close, the model-switch drain and the
unprotected-position close). Inside a break the tick returns
`state: venue_break_deferred`, `refusal: venue_break`, `commands_queued: 0`, the
break with its source and evidence, and `resumes_at`. **Nothing is lost:** the
closed bar that produced the decision is still the closed bar when the venue
reopens, and the decision's own `as_of` already sits four hours after that bar,
with an eight-hour validity — so a 30-minute deferral changes no trading logic.

**Tests.** `test_the_runner_defers_inside_a_venue_break` fails on the old code
(which had no gate: `command_queued` instead of `venue_break_deferred`, with a row
in `execution_commands`). Its mirror,
`test_the_runner_issues_normally_when_no_break_is_declarable`, exists so the
repair cannot be "block everything". `test_the_runner_derives_the_break_from_its_own_store`
proves the derivation runs end to end from a real store.

---

## 4. The eligibility flag — a finding, NOT an adjudication

**I do not rule on whether the execution was authorized, and no authorization
value was changed or read for a decision.** What follows is what the records and
the code establish, for the auditor and the owner.

### What the evidence shows

Five retained direct-evidence snapshots under `~/.local/state/lts/evidence/mt5-direct/`
(2026-08-16T01:46:48Z -> 04:22:41Z, plus a `latest.json` copy of the last), all
carrying **one** manifest, byte-identical across all five:

| field | value in all five |
|---|---|
| manifest schema | `prediction_provider.live_linear_manifest.v1` |
| `research_validated` | **true** |
| `live_inference_eligible` | **false** |
| `live_execution_eligible` | **false** |
| runner state / `read_only` / positions | `monitoring` / **false** / **1 open** |

So the artifact was marked ineligible for live execution while the runner was
monitoring a live position on a demo account. That is the contradiction the
forensics flagged.

### What the flag governs, and what actually read it

Searched across `lts` and `prediction_provider`. The flag is **written** by
`prediction_provider`'s `mechanics/tools/train_live_linear_policy.py` (which emits
`false` for a freshly trained policy) and is **read** in exactly three places:

| reader | what it does with it |
|---|---|
| `app/live_model_selection.py` | refuses the manifest **only** when `execution_tier == "promoted_paper"` |
| `app/live_sac_selection.py` | the same, **only** on `promoted_paper` |
| `tools/controller_inventory.py` | includes it in an authority join and **reports** `sac_champion_authoritative` — it refuses nothing and gates nothing |

The lane's configured tier is **`demo_research_canary`**, on which both selectors
check a different predicate — `research_validated is True` — which this manifest
satisfies. **So, by the code as written, no component on this route was supposed to
read `live_execution_eligible` at all**, and the one component that did read it
reported rather than refused.

Two readings are therefore open, and choosing between them is a mandate question
and not mine:

* the tier vocabulary **deliberately** separates "validated research artifact,
  demo canary" from "promoted for Paper execution", in which case nothing should
  have refused and nothing failed to; or
* `live_execution_eligible: false` is meant as a floor on live order placement of
  any kind, in which case the demo canary tier is a gap in the gate rather than a
  tier, and 53 commands ran through it.

**I record both and rule on neither.**

### How many commands fall in the window

Three defensible counts, because "the window" can mean three things:

| reading of "the window" | commands |
|---|---|
| created inside the window in which the flag is DIRECTLY observed false (2026-08-16T01:46:48Z -> 04:22:41Z) | **0** — the lane was monitoring, not issuing |
| created at or after the first of those observations | **43** |
| carrying the artifact **and** config hashes the flagged manifest declared (`539f946071a1…` / `193cbeaecc75…`) | **53 of 53** — 48 succeeded, 5 failed, 2026-08-03T22:21:21Z -> 2026-09-01T21:01:00Z |

The third is the one I would put in front of the owner, with its limit stated: the
manifest's **content** is pinned only at those five observations, and whether the
flag held the same value outside them **is not established by these records**. The
artifact and config it pointed at are carried by every command in the store, and
the single `live_model_sessions` row carries the same pair — so the identity chain
is continuous even though the flag's history is not observed.

---

## 5. Read-only discipline, and the store's mtime

The store does not live on this host. It was read exactly as the fleet's own
retained evidence documents its transport: key-based SSH already configured for
this operator, each reader piped in on standard input (so no file was left on that
host), and every connection opened `file:<path>?mode=ro`.

**Verified, before the first read and after the last, unchanged:**

```
mt5-bridge.sqlite       1425489920 bytes   2026-09-08 17:22:55.937449189 -0500
mt5-bridge.sqlite-wal      4700952 bytes   2026-09-14 23:21:50.109108896 -0500
mt5-bridge.sqlite-shm        32768 bytes   2026-09-14 23:21:50.116109003 -0500
```

Four separate read sessions, same values afterwards; the WAL timestamp matches the
one the prior forensics published. Everything heavier than a single `ls` ran under
`$HOME/.local/bin/crispdm-run -m 2G -t <wall> -n mt5fix --`, CPU only with
`CUDA_VISIBLE_DEVICES=''`. No refusal from the guard was re-asked with a bigger
cap; none was issued.

**The account.** Demo on strong but **self-declared** evidence: `environment: demo`
in the deployed bridge config, `venue: mt5_demo` in the runner config, `demo` in
all 220,643 heartbeat rows with no second distinct value, a ~10,000 USD round
balance and a 0.01-lot cap. `l1_capabilities`, `l1_broker_facts` and `l1_effects`
are **all empty**, so nothing broker-side certifies the trade server. **Nothing in
this document assumes a certification we do not have.**

---

## 6. Tests

Named environment on the invocation, because `lts`'s conftest imports
`sqlalchemy`, `app.database` and `fastapi.testclient` at collection time and the
default interpreter has none of them. **The conftest was not touched.**

```bash
CUDA_VISIBLE_DEVICES='' $HOME/.local/bin/crispdm-run -m 2G -t 40m -n mt5fix -- \
  <env-with-requirements.txt>/bin/python -m pytest tests/unit -q
```

| run | result |
|---|---|
| `tests/unit/test_mt5_corrections.py` | **36 passed** |
| `tests/unit` after these corrections | **1167 passed, 348 skipped** |
| `tests/unit` at `f1109ba`, same interpreter | **1130 passed, 348 skipped** |

The 37 new passes are this branch's 36 tests plus one extra case in the published
`test_every_declared_read_query_can_reconcile`, which is parametrized over
`READ_SIDE_QUERIES` and therefore now also exercises the retained-record query.
The 348 skips are pre-existing and environmental (an absent sibling checkout); the
count differs from the 1478 the unknown-outcome branch published because that was
a different interpreter and plugin set, not because anything was lost.

**With the two old lines restored, exactly three of the new tests fail**, and they
are the three designated as regression proofs:

```
FAILED test_no_mt5_path_branches_on_a_verb_the_vocabulary_lacks
FAILED test_a_closed_position_stops_being_authorized
FAILED test_the_runner_defers_inside_a_venue_break
        AssertionError: assert 'command_queued' == 'venue_break_deferred'
```

The lines were restored in a scratch copy, measured, and reverted; nothing of that
experiment is committed.

---

## 7. What is NOT done, refused, or not measured

* **No mutating call of any kind.** No order, cancel, modify or flatten. No
  `execution_authorized: true`. No service started, stopped or restarted. No
  `--apply`. No write to the deployed store, and its mtime proves it (§5).
* **REFUSED — no reconciliation was appended anywhere.** The retained-record exit
  is implemented and reported; recording one against the deployed store is a write
  and belongs to the owner, behind the same attestation as the migration.
* **REFUSED — no adjudication of the eligibility flag** (§4), and no authorization
  value read for a decision or changed.
* **REFUSED — no claim about the account today.** The newest observation of any
  kind in these records is 2026-09-08T22:28:55Z.
* **REFUSED — the bridge being stale is a finding, not a task.** Nothing was done
  about it, and the forensics' recovery order for the owner stands unexecuted.
* **NOT MEASURED — the tool against the deployed store in place.** It was run
  against a masked, row-identical local replica, because running it on the bridge
  host would mean placing this branch's code there. The verdict is a function of
  the rows and the rows are published.
* **NOT ADMITTED — the balance-invariant argument.** That a completed round trip
  inside the 424.7-second hole would have moved the realized balance is recorded
  as a corroborant for the owner (§1) and is deliberately not a condition in code:
  it would let a cumulative inference stand in for an observation, and the residual
  it leaves is real.
* **NOT VERIFIED — the retcode tables.** Still vendor documentation transcribed by
  RP149. `10018` is the only `TRADE_RETCODE_*` this broker's server has ever
  returned to this lane, and the venue-break derivation rests on it; the transport
  codes remain untested on this server.
* **NOT FIXED — the `metadata` row** that still calls the deployed store
  `lts.mt5.bridge.readonly.v1`. It is a row in a live database, not code (§2).
* **NOT FIXED — a failed `close` still has no name of its own.** The taxonomy
  answers "does an order exist", not "did an intended flattening fail"; the two
  failed closes left exposure standing for an extra bar and are correctly
  `failed`. Named again, not widened here.
* **No linter or type checker** is configured in this repository; nothing here
  claims to be lint- or type-clean.
* **No account identifier, host name, IP, token or secret** is written in this
  document or in the evidence beside it. Fingerprints are replaced by a
  same-length stand-in and tickets are masked; the bridge host is named by role.
