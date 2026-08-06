# One-Seat-at-a-Time Heartbeat Hash Enrichment (WP4 / finding 139)

Status: PLAN ONLY — no venue restarted. Owner authorization is
conditional (2026-08-06): Alpaca first while flat; MT5 when flat or
with position and SL/TP verified directly; one venue at a time; with
rollback.

## Why

`controller_inventory` v3 grants SAC authority only on an exact join of
model id, artifact, config, input-feature, preprocessing and manifest
hashes plus fresh heartbeat, active unit and all eligibility
predicates. The MT5 runner heartbeat publishes none of those hashes, so
its authority is permanently `unavailable` — not a defect of the seat,
a defect of its telemetry.

## Change (identical shape per runner)

Add to each runner's heartbeat payload, from values it ALREADY holds:
`artifact_sha256`, `config_sha256`, `input_feature_sha256`,
`preprocessing_sha256`, `manifest_sha256`. No decision logic, no order
path, no risk parameter changes.

## Sequence — Alpaca first

1. **Preconditions (all direct facts, no inference):** account flat —
   zero positions and zero open orders in the latest snapshot; unit
   active; heartbeat fresh; a rollback copy of the current unit +
   package revision recorded.
2. **Deploy:** update code, `systemctl --user restart
   lts-alpaca-model-runner.service`.
3. **Verify within one bar:** unit active; heartbeat fresh and now
   carrying all five hashes; `controller_inventory` join reports
   `linear_shadow_control` (never authority — it is a linear model);
   no new orders were emitted by the restart; account still flat.
4. **Rollback trigger:** any failed verification → restore the previous
   revision and restart; record the incident.
5. **Soak:** at least one full decision cycle before touching MT5.

## Sequence — MT5 second

1. **Preconditions:** Alpaca soak clean; AND EITHER account flat OR the
   open position's native SL and TP are verified **directly from the
   venue snapshot** (not from our ledger alone) and recorded with
   ticket ids.
2. Deploy and verify exactly as above, on Dragon.
3. **Extra guard:** the MT5 seat carries live exposure; the restart must
   be performed at a bar boundary with the bridge's command queue empty,
   and the post-restart snapshot must show the same position, same SL,
   same TP, same ticket.
4. Rollback identical.

## Explicitly out of scope

Granting SAC authority to any seat. That requires a champion artifact,
its eligible manifest, and observation parity — none of which exist
today.
