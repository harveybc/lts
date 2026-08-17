# IBKR Paper Continuity Runbook

Status: Paper probation. No real-capital authority.

IBKR remains useful only if its broad instrument access is worth its terminal
session overhead. This runbook makes that an evidence-based decision rather
than accepting repeated operator frustration indefinitely.

## Supported Seat

- Dedicated local, always-on host or VM; never a cloud desktop controlled by an
  autonomous agent.
- IB Gateway **Offline** release preferred after a flat maintenance window;
  TWS Offline is accepted during migration.
- API bound to loopback only. Broker username, password and 2FA remain human
  inputs and are never stored in Git, prompts, agent memory or automation.
- Global Configuration -> Lock and Exit: **Auto restart enabled**, **Auto
  logoff disabled**, with a declared daily maintenance time.
- Human reauthentication is scheduled weekly before the broker session expires.
- Every risk-increasing order reaches the broker as a native GTC bracket with
  both stop-loss and take-profit. A terminal outage may pause model decisions,
  but cannot remove broker-held protection.

## Automated Continuity Contract

The runner retries transient construction/session failures with exponential
backoff capped at 30 seconds. `tws_continuity_monitor.py` runs independently and
classifies the direct state as:

| State | Meaning | Required action |
| --- | --- | --- |
| `authenticated` | process, API and fresh connected heartbeat agree | none |
| `login_or_api_required` | process exists but API socket is absent | authenticate or restore socket clients |
| `api_session_degraded` | API listens but functional heartbeat is not connected | inspect session and runner journal |
| `process_absent` | terminal/gateway process is absent | start the Paper seat and authenticate |

An outage with unresolved or unknown exposure is P0. A proven-flat outage is
P1. Repeated checks update one deduplicated incident with the original outage
time and elapsed duration; they do not flood Telegram.

## Restart and Recovery

1. Do not assume a terminal restart changed broker-held orders.
2. Reconnect and obtain direct account, position, open-order and execution
   facts from the broker.
3. Reconcile those facts against the immutable local effect contract.
4. Keep any safety hold until the signed owner-resume path proves zero exposure
   and zero open orders when that proof is required.
5. Record terminal downtime, reconnect count, unattributed exits and protection
   discrepancies in the Paper seat evaluation card.

## Fourteen-Day Acceptance Window

Start the clock only after Auto restart/Auto logoff are configured and direct
evidence confirms a healthy Paper session. Retain IBKR as an active execution
venue only if all conditions hold for 14 consecutive days:

- no unplanned session outage longer than 10 minutes;
- every planned daily restart reconnects within 2 minutes;
- no duplicate order or position effect after reconnect;
- every open exposure has directly verified broker-native SL and TP;
- every exit reconciles to direct broker facts without residual exposure;
- no outage alert is missed and no alert flood hides a distinct incident;
- weekly human reauthentication is completed in its scheduled window.

Any protection loss, wrong-account binding, duplicate exposure or unresolved
reconciliation fails the window immediately. Two terminal/session outages over
10 minutes in one window retire IBKR from continuous model execution and return
it to read-only calibration until a new acceptance window passes. Alpaca and
MT5 continue independently; IBKR is not allowed to block those venues.

## Migration Window

Migrate from TWS to IB Gateway only while direct broker facts prove the account
flat with zero open orders. Change the local port/profile in one bounded step,
verify account fingerprint and socket binding, run a zero-submit preflight, and
then resume the Paper model runner. Never switch terminals while relying on an
open bracket.
