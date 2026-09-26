# The five MT5 failures, read from the store that actually holds them

**Forensic finding of the successor technical lead, 2026-09-26.**
Repository `lts`, branch `satoshi/mt5-five-failures-forensics-20260926`, from
`12bce5f` (`satoshi/mt5-unknown-outcome-20260926`).
Signed: **Satoshi** (Satoshi III, Mujuro Utsutsu), successor technical lead.

Read-only throughout. No broker mutation, no order, no cancel, no modify, no
`execution_authorized: true`, no service started, stopped or restarted, and the
migration's `--apply` was not run.

---

## 0. The headline, before the detail

1. **The five failures are not one population. They are 1 + 4.** Exactly **one**
   is the case the defect was built for: an `open_short` on 2026-08-03 whose
   stored result carries `result_code: 0` — no machine code at all. The lane's
   own classifier types it `effect_unknown` / `no_machine_code_reported`. The
   other **four** carry `result_code: 10018` (`TRADE_RETCODE_MARKET_CLOSED`) and
   are typed `failed` / `retcode_proves_no_order_exists` — proven, correctly
   labelled, correctly slot-releasing.
2. **None of the five left an unauthorized position, and this is now settled
   from retained records rather than inferred from present exposure.** For the
   one unknown case there are 13 consecutive account snapshots across the
   17-minute window after it completed, all reading `positions_total 0` and
   `orders_total 0`, and zero `trade_events` rows before the next command. That
   is the positive observation the ruling demands. The two failed **closes** did
   leave an effect of a different kind: the position they were meant to flatten
   stayed open and was closed by the next scheduled close roughly four hours
   later.
3. **A budget slot was freed wrongly, exactly once — and something worse than
   the budget happened.** The mislabelled command was immediately **retried
   under the same decision key**, and the retry placed a real order. Under the
   fixed code that retry is refused outright by the unresolved-route gate, not
   by the budget. The budget itself was never the binding constraint that day
   (2 opens against a ceiling of 4).
4. **The account is a demo account, and the evidence is strong but
   self-declared.** `environment: "demo"` in the deployed bridge config, in the
   runner config (`venue: mt5_demo`), and in **all 220,643 heartbeat rows with
   no second distinct value**, against a ~10,000 USD round balance and
   `max_volume 0.01`. **But there is doubt, and I am naming it:** the lane's L1
   attestation tables (`l1_capabilities`, `l1_broker_facts`, `l1_effects`) are
   **empty**, so nothing broker-side independently certifies the trade server as
   a demo server. See §4.
5. **The relayed claim "has never placed an order" is refuted.** The command
   vocabulary is `{open_long, open_short, close}` — there is no read-only verb.
   All 53 commands are order operations; **42 opens succeeded** and 6 closes
   succeeded. The lane traded on that account from 2026-08-03 to 2026-09-01.

---

## 1. Where the records are, and how they were read

The prior ruling recorded "There is no live corpus … no `mt5-bridge.sqlite`
exists anywhere on the filesystem". That is true of **this** host and false of
the lane. The DR02 index corrected it one step further ("its store is not on
this host") and then stated that **"no per-order acknowledgement, retcode,
ticket or fill is retained on this host; only the two aggregate counters."**

That second sentence is also true of this host, and it is not the end of the
matter, because the store is reachable **read-only** by a transport this fleet
already documents. Retained evidence under
`~/.local/state/lts/evidence/mt5-direct/` (six snapshots, 2026-08-15/16) carries
its own transport string:

```
transport: ssh -o BatchMode=yes <bridge-host> python3 - (sqlite file:...?mode=ro URI)
```

I reused exactly that: key-based SSH already configured for this operator, and
every connection opened as `file:<path>?mode=ro`, which cannot write even if the
reading code were wrong. **No credential was hunted for and none was needed
beyond the SSH key already present.** The store is 1.4 GB, last written
2026-09-08, and holds 53 `execution_commands`, 55,218 `account_snapshots`,
220,643 `heartbeats` and 437 `trade_events`.

Two named refusals stand about reading:

* **REFUSED — `/v1/report`.** The only HTTP route that returns per-command rows
  is HMAC-authenticated against the bridge secret named by `secret_env`. That
  variable is **not present** in this host's environment and the bridge env file
  does not exist here. Had SSH not been available, per-command forensics would
  have stopped at that missing secret, and I would have named it and stopped.
  `/v1/status`, which the watchdog reads, is unauthenticated and carries **only
  the two aggregate counters** — it cannot answer any question in this task.
* **NOT MEASURED — the migration against this corpus.** The dry run is
  read-only and would be safe, but it was not executed here: the tool resolves a
  local path that does not exist on this host, and pointing it at the live store
  over the network is outside this task's remit. §3 computes the same verdicts by
  calling the committed classifier directly on the real rows, which is the same
  code path the migration uses.

The deployed configuration, read from the bridge host with secrets stripped:

| key | value |
|---|---|
| `schema` | `lts.mt5.execution_bridge_config.v2` |
| `environment` | `demo` |
| `execution_enabled` | `true` |
| `allowed_symbols` | `["ETHUSD"]` |
| `max_volume` | `0.01` |
| `max_open_commands_per_day` | **4** |
| runner `max_concurrent_positions` | **1** |
| runner `execution_tier` | `demo_research_canary` |

No account identifier, host name, IP or token is reproduced in this document.
Order and deal tickets are masked. The bridge host is referred to by role.

---

## 2. Question 1 — what the five failures actually are

53 commands total, 2026-08-03T22:21:21Z to 2026-09-01T21:01:00Z, one account,
one symbol, one model session. By action and state:

| action | succeeded | failed |
|---|---|---|
| `open_long` | 16 | 0 |
| `open_short` | 26 | 3 |
| `close` | 6 | 2 |
| **total** | **48** | **5** |

The five, each classified by **calling the committed classifier**
(`app.mt5_unknown_outcome.outcome_for_historical_row`) on the real stored row —
not by my reading of it. The lane's taxonomy is used as written; no label is
invented.

### F1 — `open_short`, 2026-08-03 — THE ONE THAT MATTERS

| | |
|---|---|
| created / delivered / completed | 22:21:21Z / 23:30:24Z / 23:30:24Z |
| stored result | `success: false`, **`result_code: 0`**, `order_ticket "0"`, `deal_ticket "0"`, `message "order_check_refused:Done"` |
| classifier verdict | **`effect_unknown`** / `no_machine_code_reported` |
| refusal kind | `transport_failure` |
| consumes budget slot | **True** (the old code freed it) |

**Can the evidence distinguish a refusal before sending from a send never
observed? No — not from the command record.** `result_code: 0` is not a
`TRADE_RETCODE_*` at all; it is the absence of a machine code. The prose
`order_check_refused` does read like a pre-send refusal, and **prose is not
evidence in this lane by rule** — §2.5 of the ruling forbids reading messages,
and an AST guard enforces it. The message even contains the word `Done`, which
is the human label of `TRADE_RETCODE_DONE`; a classifier that read messages
could have inverted this case entirely. So the record is precisely the false
negative the fix was built for: recorded `failed`, meaning "proven no order
exists", on evidence that proves nothing.

Note the 69-minute gap between creation and delivery. The command sat
undelivered from 22:21:21Z to 23:30:24Z and completed 15 ms after delivery.

### F2 — `close`, 2026-08-18 · F3 — `open_short`, 2026-08-25 · F4 — `close`, 2026-08-31 · F5 — `open_short`, 2026-09-01

| | F2 | F3 | F4 | F5 |
|---|---|---|---|---|
| created (UTC) | 08-18 21:00:48 | 08-25 21:00:58 | 08-31 21:00:58 | 09-01 21:01:00 |
| completed (UTC) | 21:01:00 | 21:01:07 | 21:01:13 | 21:01:14 |
| `result_code` | **10018** | **10018** | **10018** | **10018** |
| message | `close_send_failed:Market closed` | `order_send_failed:Market closed` | `close_send_failed:Market closed` | `order_send_failed:Market closed` |
| tickets | `0` / `0` | `0` / `0` | `0` / `0` | `0` / `0` |
| classifier verdict | `failed` / `retcode_proves_no_order_exists` | same | same | same |
| refusal kind | `market_closed` | `market_closed` | `market_closed` | `market_closed` |
| consumes slot | False (correctly free) | False | False | False |

**Can the evidence distinguish? Yes, for all four.** `10018`
(`TRADE_RETCODE_MARKET_CLOSED`) is a structured refusal the trade server issues
*before* placing anything; it is in the lane's proves-no-order set. All four were
delivered and answered within 9–15 seconds. Their `failed` label was already
correct and the migration would write nothing for them.

### The pattern nobody named: all five sit in one two-hour window

Commands grouped by UTC hour of creation:

| UTC hour | commands | failed |
|---|---|---|
| 21 | 5 | **4** |
| 22 | 1 | **1** |
| all other hours (14 hours) | 47 | **0** |

**Zero of 47 commands failed outside 21:00–22:59 UTC; five of six failed inside
it.** The four `10018`s are the venue's daily rollover break, and the runner's
4-hour bar boundary lands inside it. This is a deterministic, predictable,
recurring scheduling defect, not bad luck — and it is a **separate finding from
the store defect**, worth its own fix (skip or defer the 21:00 UTC boundary
rather than sending into a closed market). F1, at 22:21, is the one outside the
`10018` group in both code and character.

---

## 3. Question 2 — could any of them have left an effect?

**What present exposure does and does not establish.** Today's status reads
`orders_total 0`, `positions_total 0`, `all_authorized true`. Three reasons that
is weaker than it looks:

1. It is **not current**. `exposure_reconciliation` and `latest_snapshot` are
   both computed from the **latest stored account snapshot**, received
   **2026-09-08T22:28:55Z** — 17.9 days ago. It is an 18-day-old observation
   republished live, not a live observation.
2. `all_authorized` is **vacuous at zero exposure**. Its definition is
   `all_authorized = not unexpected and not orders`; with no positions the
   matching loop never executes, so `true` means "the snapshot contained
   nothing", never "exposure was verified against authorized commands".
3. It says nothing about the **past**. A position opened and closed between two
   failures would leave today's counters at zero.

**So I did not rely on it.** Each case is settled against the retained snapshot
and trade-event streams instead.

### F1 — settled: no effect, and settled the way the ruling requires

| evidence | value |
|---|---|
| account snapshots strictly between F1's completion (23:30:24Z) and the next command (23:47:14Z) | **13** |
| sum of `positions_total` across those 13 | **0** |
| sum of `orders_total` across those 13 | **0** |
| `trade_events` rows before 23:47:14Z | **0** |
| first non-zero `positions_total` in the entire 55,218-snapshot history | **2026-08-03T23:47:14.765Z** — the *next* command's fill |

Thirteen consecutive one-minute observations of a flat account, and an empty
trade-event log, covering the whole window in which F1's order would have had to
appear. **F1 left no order and no position.** Its true state is `failed`; the old
code reached the right label by the wrong route, on evidence that could not
support it.

This carries a finding the ruling does not yet use. §2.3 says an `effect_unknown`
exits "only through a READ-side broker query", and §4 reports "no live corpus"
to migrate. Both are more pessimistic than the records: **the positive
observation that no order exists is already retained, offline, in
`account_snapshots` and `trade_events`.** A reconciliation for F1 can be
produced from the store itself with no broker call at all. See §6.

### F2 and F4 — the failed closes DID leave an effect

`10018` proves no close order was placed. The consequence is not an
unauthorized position but an **unflattened** one:

| | F2 (2026-08-18) | F4 (2026-08-31) |
|---|---|---|
| `positions_total` from 20:55 to 23:25 | **1 throughout** | **1 throughout** |
| flattened by | next scheduled `close`, 08-19 01:00:49Z, succeeded | next scheduled `close`, 09-01 01:01:01Z, succeeded |
| exposure carried beyond intent | **~4 hours** | **~4 hours** |

An intended exit that the venue refused, twice, each time extending live demo
exposure by one 4-hour bar. Correctly labelled by the store, and **invisible in
the taxonomy**, which is about whether an *order exists*, not about whether an
intended *flattening* failed. Named here as a gap, not a defect.

### F3 and F5 — settled: no effect

Both `10018` on `open_short`; the four snapshots immediately following each
read `positions_total 0`, and `trade_events` in the following two hours is **0**
for both. Corroborated in the same way as F1.

### The retained records settle all five

No case requires a live broker query. **REFUSED — no claim about the present
moment.** Nothing here establishes the account's state *today*: the newest
observation of any kind is 2026-09-08. A position opened on that account after
the EA went silent would be invisible to every record I read, and I make no
claim that none exists.

---

## 4. Question 3 — was a budget slot freed wrongly?

**Yes. Once. And the real harm landed somewhere else.**

The budget: `max_open_commands_per_day = 4`, counted per **UTC calendar day**
over `action LIKE 'open_%'`, old predicate `state != 'failed'`.

Opens per UTC day, with failed opens marked:

| day | opens | failed opens |
|---|---|---|
| 2026-08-03 | 2 | **1 (F1)** |
| 08-05 | 4 | 0 |
| 08-10 | 1 | 0 |
| 08-12 | 3 | 0 |
| 08-17 | 3 | 0 |
| 08-18 | 1 | 0 |
| 08-19 | 4 | 0 |
| 08-20 | 3 | 0 |
| 08-21 | 3 | 0 |
| 08-24 | 3 | 0 |
| 08-25 | 4 | **1 (F3)** |
| 08-26 | 4 | 0 |
| 08-27 | 4 | 0 |
| 08-28 | 2 | 0 |
| 08-31 | 2 | 0 |
| 09-01 | 2 | **1 (F5)** |

**Wrongly freed: exactly one slot, 2026-08-03, by F1.** F3 and F5 are
`retcode_proves_no_order_exists`, so releasing their slots was correct then and
remains correct under the fix. No day ever exceeded 4 opens.

**Did the wrongly freed slot admit an entry that correct accounting would have
refused? No — not at the budget gate.** On 2026-08-03 only 2 opens existed. When
the next open was enqueued at 23:47:00Z the old predicate counted 0 held slots;
correct accounting would have counted 1. Both are below 4, so the budget would
have admitted it either way. On 2026-08-25 the failed open was the **last** of
four, so it could not have displaced anything. **The budget was never the
binding constraint in this store's entire history.**

**But the mislabel did admit an order, through the other gate.** The command
enqueued at 23:47:00Z — 17 minutes after F1 was written `failed` — carries this
idempotency key:

```
<model>:2026-08-03T17:00:00Z:2026-08-03T21:00:00+00:00:retry:5aeea9c
```

F1's key is that string **without** the `:retry:5aeea9c` suffix. Same stop-loss
(1880.42), same take-profit (1824.56), same `input_sha256`. **It is a retry of
the very command whose outcome was unproven**, and it succeeded:
`result_code 10009` (`TRADE_RETCODE_DONE`), a real order ticket (masked
`402175••`) and deal ticket (masked `410536••`), a live short position, and the
first non-zero exposure in the account's history. It is the **only**
`:retry:`-suffixed key in all 53 commands.

Under the fixed code that retry is **refused before the budget is even
consulted**. `enqueue` first checks the route for an unresolved command
(`UNRESOLVED_STATES = {pending, delivered, effect_unknown}`, matched on account
and symbol) and raises:

> An unresolved MT5 effect on this route has never been observed; it is
> reconciled by a read-side broker query before any new order exists

So the harm is real and it is precisely the harm the ruling names — a second
order placed against a decision whose first attempt might already have placed
one, with `max_concurrent_positions: 1` in force. It was **harmless in outcome
only because F1 truly left nothing**, which was establishable afterwards from
snapshots and was never establishable from the command record the gate actually
consulted.

**Correction to the ruling's published count.** §4 of `12bce5f` publishes "entry
budget slots reclaimed (now held, not free): **5**". That is the count over the
9-row synthetic corpus, and its NOTE says so. Against the **real** store the
number is **1**, and the other four `failed` rows are confirmed by their own
evidence. The ruling's arithmetic is not wrong; it has simply never been run
against the live corpus, and this is the first statement of what that corpus
holds.

---

## 5. Question 4 — the account and the authorization

**This matters more than anything else here, so the evidence is separated from
the inference.**

### What is established

| fact | source |
|---|---|
| `environment: "demo"` | deployed bridge config on the bridge host |
| `environment: "demo"`, `venue: "mt5_demo"` | deployed model-runner config |
| `environment: "demo"` in **220,643 of 220,643** heartbeat rows, **one distinct value**, 2026-08-01 → 2026-09-08 | `heartbeats` table |
| `venue: "mt5_demo"` | the single `live_model_sessions` row |
| balance = equity = 9996.84 USD, opening ~10,000 USD | `account_snapshots` |
| `max_volume 0.01`, one symbol, `max_open_commands_per_day 4`, `max_concurrent_positions 1` | configs |
| `execution_tier: "demo_research_canary"` | runner config |
| `trade_allowed: 1`, `connected: 1` on the last heartbeat | `heartbeats` |

A round 10,000 USD balance, 0.01-lot caps and a uniform `demo` declaration
across 220,643 independent rows is a coherent, multi-sourced demo picture. On
the balance of the evidence, **this is a demo account.**

### Where the doubt is, stated plainly

* **Nothing broker-side attests it.** `environment` is a string the EA reports
  and the config declares. The lane's own attestation tables —
  `l1_capabilities`, `l1_broker_facts`, `l1_effects` — are **all empty (0
  rows)**. The L1 capability ledger that exists to carry exactly this kind of
  certification was never populated for this lane. **There is therefore no
  independent verification that the trade server is a demo server; there is a
  consistent self-declaration.**
* **The lane has an entitlement code for precisely this confusion and it never
  fired.** `MT5_ENTITLEMENT_RETCODES` includes `10032`
  (`TRADE_RETCODE_ONLY_REAL`). Its absence is consistent with demo but is not
  proof.
* **`bridge_version` disagrees with itself.** The store's `metadata` table says
  `lts.mt5.bridge.readonly.v1`; the live endpoint reports
  `lts.mt5.bridge.execution.v2`. The metadata row is a leftover from the
  read-only era in which the file was created (2026-08-01) and was never
  updated when the same file was adopted by the execution bridge. A reader who
  trusted `metadata` would conclude this lane cannot trade. It can, and it did.

### Does `execution_enabled: true` mean what it appears to mean?

**Yes, unambiguously, and it is not theoretical.** `execution_enabled: true` in
the deployed config, `read_only: false` at runtime, and the command vocabulary
`{open_long, open_short, close}` with **no read-only verb** — every command this
bridge can carry is a mutating order operation. **42 opens and 6 closes
succeeded**, with real order tickets, real deal tickets and a real position
history. The bridge process and the model runner are **both running right now**
on the bridge host.

### Does `all_authorized: true` mean what it appears to mean?

**No.** Three defects in reading it, all named in §3: it is **vacuous** at zero
exposure (`not unexpected and not orders`, with the matching loop never
entered), it is computed from an **18-day-old** snapshot, and it is silent about
the past. It currently means "the last snapshot we ever received contained
nothing", and nothing more.

One further gap found while reading that function: it releases an authorized
ticket on `action == "close_position"`, but the action vocabulary spells that
verb **`close`**. The branch is unreachable, so a closed position's ticket is
never retired from the authorized set. Harmless today (tickets are unique and
exposure is zero) and named here rather than left.

### One governance observation, not a defect of this store

The retained 2026-08-16 evidence shows the model manifest carrying
`live_execution_eligible: false` and `live_inference_eligible: false` while the
runner was in state `monitoring` with `read_only: false` and a live position
open. The eligibility flags on the artifact and the execution the lane performed
do not agree. **NOT INVESTIGATED HERE** — it belongs to the manifest/mandate
chain, not to the five failures, and I flag it for the owner rather than ruling
on it.

---

## 6. Question 5 — the stale bridge, and a recommendation (not executed)

### What is established

| fact | value |
|---|---|
| last heartbeat received | **2026-09-08T22:28:55.917Z** (17.9 days) |
| last account snapshot | 2026-09-08T22:28:55.936Z — `positions 0`, `orders 0`, balance = equity = 9996.84 USD |
| last command created | **2026-09-01T21:01:00Z — which is F5**, a `10018` failure |
| last non-zero exposure | 2026-09-01T17:00:58Z, flattened by the succeeded close at 09-01T17:01 |
| commands `pending` or `delivered` | **0 — nothing whatsoever is queued** |
| bridge process | **running**, uptime ~11d 16h |
| model runner process | **running**, uptime ~11d 16h, heartbeat rewritten minutes ago |
| runner state | **`snapshot_stale`** — fail-closed, refusing to act |
| MT5 terminal VM | **running**, uptime ~11d 16h |
| monitor event | `mt5_bridge_stale`, **critical**, activated 2026-09-22T01:03:48Z, 4,247 `repeated` rows, the **only** active event key in the fleet |
| store WAL last written | 2026-09-14 23:21 |

**The diagnosis is not "the bridge is down".** The bridge is up and answering.
The runner is up and correctly refusing. The Windows VM is up — but its uptime
of ~11 days means it **restarted around 2026-09-15, and the EA inside the
terminal has not posted a heartbeat since 2026-09-08**. The broken link is the
EA inside the MT5 terminal, not the Linux side.

**Nothing is queued behind the staleness**, and that is the good news that makes
this safe to leave: every command is terminal (48 succeeded, 5 failed), the
account was **flat** at the last observation, and the runner's
`snapshot_max_age_seconds` guard is doing its job. Eighteen days of a critical
alarm bought nothing worse than lost research time. The one durable oddity is
the single `live_model_sessions` row, still `state: active` after 18 silent days.

**Why nobody acted** is not in these records. **REFUSED — no finding on
operator behaviour.** The alarm did fire, 4,247 times, and reached whatever the
watchdog reports to; whether it was seen is outside anything I can read.

### Recommended order of operations for the owner — NOT EXECUTED

Nothing below was performed. Steps 4 and 6 are the only mutating steps and both
belong to the owner.

1. **Look before touching.** On the bridge host, confirm what §6 states:
   `positions_total 0` and `orders_total 0` on the last snapshot, and zero
   `pending`/`delivered` commands. If either is non-zero, stop and reconcile
   first — do not bring an EA back up against unknown exposure.
2. **Inspect the terminal inside the VM**, not the Linux services. Expected
   cause: the EA is not attached or not permitted after the ~2026-09-15 VM
   restart. Check that the EA is on its chart, that algorithmic trading is
   enabled, and that the terminal is logged in to the demo server. Terminal
   build moved 6090 → 6140 over this lane's life; a build upgrade that dropped
   the EA's permission is a plausible cause and is checkable.
3. **Do not restart the bridge or the runner.** Both are healthy and
   fail-closed; restarting them loses the `snapshot_stale` evidence and gains
   nothing. The heartbeat gap is upstream of both.
4. **Let the heartbeat return first, and watch it for one full snapshot cycle
   before allowing any command.** `mt5_bridge_stale` should clear on its own.
   Confirm `positions_total 0` on the fresh snapshot: any position that appeared
   during the 18 silent days is unauthorized by construction and must be
   adjudicated by the owner before the runner is allowed to act.
5. **Close the stale session row** once the lane is healthy, so the single
   `active` `live_model_sessions` row stops asserting a session that ended
   18 days ago.
6. **Only then**, if the lane is to run again: fix the 21:00 UTC boundary (§2)
   before the next rollover, since a resumed lane will otherwise reproduce F2–F5
   on schedule.

### Recommended fix-forwards, in the order the evidence argues for them

1. **Reconcile F1 offline from the records this store already holds.** §3 shows
   the positive no-order observation exists in `account_snapshots` and
   `trade_events`. The ruling's §2.3 admits only a live broker query; that is
   stricter than necessary and, with the EA silent, currently unsatisfiable. A
   read-side observation built from the retained streams — same
   `OrderObservation` type, a declared read-side query naming the snapshot and
   trade-event tables, its own observation timestamp — would discharge the one
   real `effect_unknown` in the fleet without a single broker call. This is the
   highest-value next step and it is blocked today by nothing but the
   vocabulary.
2. **Run the dry migration against a read-only copy of the real store** and
   publish the result, replacing the 9-row synthetic counts with 1 relabel and 4
   confirmations. Note that the deployed store has **no `execution_command_outcomes`
   table** — it is running pre-fix code — so `--apply` is a schema change as well
   as a data change, and belongs behind the owner's attestation exactly as §5 of
   the ruling says.
3. **Fix the 21:00 UTC scheduling collision.** Five of six commands in that
   window failed; none outside it ever did.
4. **Give a failed `close` a name of its own.** F2 and F4 are correctly
   `failed` and still left exposure standing for an extra bar. The taxonomy
   answers "does an order exist", not "did an intended flattening fail".
5. **Repair the two cosmetic-but-misleading records**: the unreachable
   `close_position` branch in `exposure_reconciliation`, and the `metadata`
   row that still calls this store `lts.mt5.bridge.readonly.v1`.
6. **Populate the L1 attestation tables, or stop implying they exist.** They are
   empty, and they are the only place a broker-side demo certification could
   live (§5).

---

## 7. What is NOT done, refused, or not measured

* **No mutating call of any kind.** No order, cancel, modify or flatten. No
  `execution_authorized: true`. No service started, stopped or restarted. No
  `--apply`. Every database connection used `file:<path>?mode=ro`.
* **REFUSED — `/v1/report`**, the authenticated per-command route: the bridge
  secret is not available on this host and was not sought. §1.
* **REFUSED — any claim about the account today.** The newest observation of any
  kind is 2026-09-08T22:28:55Z. §3.
* **REFUSED — any finding on why the critical alarm went unacted for 18 days.**
  Not in these records. §6.
* **NOT MEASURED — the migration tool against this corpus.** §1. The verdicts in
  §2 come from calling the committed classifier on the real rows, which is the
  same code the migration uses, but the tool's own reporting path was not
  exercised against the live store.
* **NOT VERIFIED — the retcode tables**, still vendor documentation transcribed
  by RP149. The four `10018`s are the first `TRADE_RETCODE_*` values from this
  broker's server that this lane has ever classified against real records, and
  `10018` behaved as documented. `10011/10012/10024/10028/10031` remain
  untested on this server: **no command in this store ever returned one.**
* **NOT INVESTIGATED — the manifest eligibility contradiction** (§5,
  `live_execution_eligible: false` while executing). Flagged for the owner.
* **No test suite was run for this branch.** It adds no code. The suites of
  `12bce5f` stand as that branch published them.
* **No linter or type checker** is configured in this repository.
* **No account identifier, host name, IP, token or secret** is written here.
  Tickets are masked; the bridge host is named by role.
