# MT5 USDCAD activation runbook (order 2026-08-23 P0)

STATUS: PREPARED — nothing here is executed until the §2.9 preflight
packet is independently verified. The ETHUSD chart, EA instance,
runner, session, ledger and position state are NEVER touched.

## A. The one human MT5 action (§2.8 — cannot be done from Linux)

In the SAME MT5 Demo terminal that already runs the ETHUSD chart:

1. Open a NEW chart: symbol `USDCAD`, timeframe `H4`.
2. Drag `LtsMt5ModelBridge` onto that NEW chart only.
3. In the EA inputs set EXACTLY:
   - `InpMagic` = `26080302`  (ETHUSD keeps its own 26080301)
   - `InpExecutionEnabled` = `true`
   - every other input identical to the ETHUSD chart's values.
4. Confirm the WebRequest allowlist already contains the bridge URL
   (it does — same terminal, same bridge; do not edit it).
5. Do NOT open, close, or modify anything on the ETHUSD chart.

That is the entire human step. Updating the EA .ex5 (for the
symbol-scoped poll) recompiles once and applies to BOTH charts on
their next EA reload — reload the USDCAD chart EA freely; reload the
ETHUSD chart EA only in the coordinated window below.

## B. Linux-side sequence (operator, after packet verification)

1. Deploy branch `satoshi/mt5-usdcad-dual-symbol-20260823` on dragon.
2. Recompile/copy the updated EA into the terminal's `MQL5/Experts`.
3. Coordinated window (ETHUSD flat, no pending commands):
   a. add `"USDCAD"` to `allowed_symbols` in
      `~/.config/lts/mt5_execution_bridge.json`;
   b. restart `lts-mt5-execution-bridge.service`;
   c. reload the EA on BOTH charts (now polling with `&symbol=`).
4. Deploy the historical model artifacts to
   `~/.local/share/prediction-provider/live/usdcad_4h_linear_live_v1/`.
5. Capture read-only CopyRates bar evidence (12+ H4 opens) and run:
   `python tools/mt5_symbol_model_compat_preflight.py
    --profile examples/configs/mt5_usdcad_model_runner_v1.json
    --other-profile examples/configs/mt5_eth_model_runner_v1.json
    --bars-evidence <capture.json> --out-json <preflight.json>`
   REFUSED => stop; nothing else proceeds.
6. Install and start `systemd/lts-mt5-usdcad-model-runner.service`.
7. Publish the §2.9 preflight packet (symbol facts incl.
   trade_mode=4 and min volume 0.01, zero USDCAD positions/orders,
   effective profile + hashes, service states, rollback: stop the
   USDCAD unit + remove USDCAD from allowed_symbols — ETHUSD
   unaffected; expected first decision at the next H4 close).

## C. Rollback

`systemctl --user stop lts-mt5-usdcad-model-runner` on dragon, remove
`USDCAD` from `allowed_symbols`, restart the bridge. The ETHUSD route
continues uninterrupted; the USDCAD EA instance goes quiet (no
commands are ever delivered to it).
