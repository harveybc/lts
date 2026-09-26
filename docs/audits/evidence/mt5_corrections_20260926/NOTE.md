# What these files are, and what they are not

Evidence for `docs/audits/work_plan/SATOSHI_MT5_CORRECTIONS_2026_09_26.md`.
Collected 2026-09-26 by the successor technical lead. **Read-only throughout.**

## How the deployed store was read

The MT5 execution bridge's SQLite store does not live on this host. It was read
the way the fleet's own retained evidence documents — key-based SSH already
configured for this operator, the remote reader opening the file as
`file:<path>?mode=ro`, which cannot write even if the reading code were wrong.
Nothing was written to that host: every reader was piped in on standard input and
left no file behind.

**The store's mtime was recorded before the first read and verified after the
last**, and it is unchanged:

```
mt5-bridge.sqlite       1425489920 bytes   2026-09-08 17:22:55.937449189 -0500
mt5-bridge.sqlite-wal      4700952 bytes   2026-09-14 23:21:50.109108896 -0500
mt5-bridge.sqlite-shm        32768 bytes   2026-09-14 23:21:50.116109003 -0500
```

Those are the same values before and after four separate read sessions, and the
WAL timestamp matches the one the prior forensics published.

**No mutating call of any kind** was made: no order, cancel, modify or flatten,
no `execution_authorized: true`, no service started, stopped or restarted, and
the migration's `--apply` was not run.

## `store_extract_masked.json`

A read-only extract of exactly the rows the corrections rest on: all 53
`execution_commands`, the 152 `account_snapshots` between 2026-08-03T22:00Z and
2026-08-04T01:00Z, and the 5 `trade_events` up to the same bound. Row ids are the
store's own primary keys, so every claim can be taken back to a row.

**It is masked, and the masking is stated so nobody mistakes it for the store.**
Every `account_fingerprint` is replaced by a same-length stand-in of zeroes, and
every order and deal ticket is truncated with `*`. Nothing else is altered:
timestamps, states, result codes, volumes and the commands' own messages are
verbatim. No host name, IP, token or credential appears anywhere.

## `retained_reconciliation_report.json` / `.txt`

`tools/mt5_retained_reconciliation_report.py` run against a local SQLite replica
built from that extract, with the continuity budget declared on the command line
as **180 s** — the deployed bridge config's own `stale_heartbeat_seconds`.

The tool **writes nothing and reconciles nothing.** It reports, for each command
whose effect nobody has observed, whether the retained record MEETS the
correction's admission conditions. Its verdict here:

| | |
|---|---|
| candidates | 1 |
| admissible — retained records settle it | **0** |
| inconclusive — retained records refuse | **1** |

and the refusal is named: a 424.7-second gap against a 180-second budget.

It was run against the masked replica rather than against the deployed store
because running it there would mean placing this branch's code on the bridge
host. The rows are identical; the verdict is a function of the rows.

## `window_continuity.json`

The gap measurements behind that refusal, including the same hole measured on the
**terminal's own clock** (so it is not a transport delay that caught up), the
comparable windows of two other failures which are continuous at 60 s, and the
corroborants — a realized balance unchanged across the hole, zero trade events,
the first non-zero position in the whole 55,218-snapshot history belonging to the
next command — which **narrow the doubt and are deliberately not admission
conditions in code**.

## `eligibility_window.json`

The manifest eligibility question, gathered as a finding for the auditor and the
owner. **Nothing here adjudicates it**, no authorization value was read for a
decision or changed, and no ruling is made on whether the execution was
authorized. It records what the five retained 2026-08-16 direct-evidence
snapshots show, which code paths read the flag, and three different ways of
counting how many commands fall in "the window" — with the reading that the
manifest's content is pinned only at those five observations.

## What is NOT here

* **No live corpus was migrated** and no reconciliation was appended anywhere.
* **The deployed store's own `--apply` migration was not run**, in dry mode or
  otherwise, against the live file; §1 of the work plan says why.
* **Nothing about the account today.** The newest observation of any kind in
  these records is 2026-09-08T22:28:55Z.
* **No broker-side certification of the account.** `l1_capabilities`,
  `l1_broker_facts` and `l1_effects` are all empty in that store, so the demo
  evidence is strong and self-declared, and nothing here assumes otherwise.
