# LTS MT5 Model Bridge Installation

This EA is for the OANDA MT5 **Demo** account only. It replaces
`LtsMt5ReadOnlyBridge` on the chart and refuses to start on a live account.

## Install

1. In MT5, remove `LtsMt5ReadOnlyBridge` from its chart.
2. Open MetaEditor, copy `LtsMt5ModelBridge.mq5` into
   `MQL5/Experts`, and compile it. Compilation must report zero errors.
3. In MT5, enable Algo Trading and attach `LtsMt5ModelBridge` to an
   `ETHUSD` H4 chart.
4. Keep `http://192.168.122.1:8766` in the MT5 WebRequest allowlist.
5. Set the inputs below. Reuse the existing bridge secret; do not save the
   secret in Git, the ISO, screenshots, or chat.

## Required Inputs

| Input | Value |
| --- | --- |
| `InpBridgeUrl` | `http://192.168.122.1:8766` |
| `InpBridgeSecret` | existing MT5 bridge secret |
| `InpExecutionEnabled` | `true` |
| `InpTradeSymbol` | `ETHUSD` |
| `InpMaximumVolume` | `0.01` |
| `InpMaximumDeviationPoints` | `20` |
| `InpMagic` | `26080301` |
| `InpClosedBarHistory` | `800` (minimum; required for causal SAC features) |
| `InpTimerSeconds` | `15` |
| `InpSnapshotEveryTimers` | `4` |

Do not attach both bridge EAs at the same time. A successful start prints
`LTS MT5 Demo execution bridge initialized` and the adapter version reported
to Linux becomes `lts.mt5.ea.execution.v2`.

Every risk-increasing order is submitted as a market request containing both
native stop-loss and take-profit prices. The EA rejects unsigned commands,
wrong accounts, wrong symbols, oversized volume, missing model hashes,
foreign positions, and Live accounts.

The EA also publishes the position's Unix open time and at least 800 closed H4
bars. This lets the selected SAC policy reconstruct the same 2,660-value
observation used in training, including true holding duration and unrealized
PnL. A shorter history is refused; it is never padded with invented values.
