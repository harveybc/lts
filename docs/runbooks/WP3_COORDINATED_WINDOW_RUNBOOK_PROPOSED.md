# Coordinated weekly-flat window — PROPOSED runbook

**Status: PROPOSED, NOT EXECUTED.** Nothing in this document has been
run. It describes a future coordinated window so that the shape of the
activation can be reviewed before anyone is asked to perform it. No
step here is authorised by the WP3 implementation order, which permits
implementation and effect-free tests only.

Activation is not automatic and never will be by virtue of this file.
Each numbered step below requires the owner's explicit instruction at
the time, and any step may be refused by the operator on the day.

## Preconditions that must ALL hold before the window opens

1. Independent acceptance of the WP3 return package.
2. A live venue calendar bound to the traded symbol, whose identity
   matches the policy's `calendar_identity`.
3. Direct venue evidence available for both venues, verified against a
   policy that allowlists only real venue sources. `simulator_bar_local`
   is refused by name and cannot satisfy this precondition.
4. Zero outstanding flatten obligations in the deployable custody for
   the venue, account and symbol in question. One outstanding
   obligation blocks; several require an operator disposition before
   anything else proceeds.
5. The authority code identity in the deployment matches the reviewed
   digest recorded in the acceptance.
6. A dry run over CURRENT captures, produced minutes before the window,
   whose `would_send` is what the operator expects.

If any precondition is unavailable rather than false, the window does
not open. "We could not tell" is not "nothing is wrong".

## Proposed sequence

| # | Step | Evidence required to proceed |
|---|---|---|
| 1 | Confirm the calendar interval and the wind-down and forced-flatten boundaries for the symbol | calendar identity matches policy |
| 2 | Run the dry run and read `would_send` aloud with the owner | `writes_performed == 0` |
| 3 | Enter WIND_DOWN. New entries blocked; only pending ENTRY orders cancelled; native protection preserved | direct order evidence showing each cancelled identity was registered as an entry |
| 4 | Observe each cancellation's terminal venue verdict | `Canceled` / `Cancelled` / `Expired`; a rejection or a fill-before-cancel stops the window |
| 5 | Enter FORCED_FLATTEN. Request the close. Protection stays alive until the close completes | the close request is recorded in durable custody BEFORE it is sent |
| 6 | Await confirmation | direct venue evidence showing zero positions AND zero orders. Nothing else confirms |
| 7 | If confirmation does not arrive, stop | the obligation stays open, the watchdog reports `flatten_failed`, and the operator disposes |
| 8 | Record the outcome in durable custody | `flatten_confirmed`, or a terminal non-success state |

## What may NOT happen during the window

- No step may treat a fresh or empty account reading as a discharge of
  an obligation opened by a different session.
- No protective order may be cancelled to make a flatten easier.
- No confirmation may rest on simulator provenance.
- No automatic retry. A failure is an operator decision, not a loop.

## Rollback

There is no "undo" for a sent order. The rollback is the same as the
failure path: the obligation stays open in durable custody, every risk
increase stays blocked, and the operator disposes of it explicitly. A
blocked account is the safe state.
