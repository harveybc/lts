# IBKR Paper suspension

Status: `suspended_by_owner`

IBKR Paper was removed from active unattended execution on 2026-08-23 after
repeated TWS session loss, modal login prompts and manual reauthentication.
Alpaca Paper and OANDA MT5 Demo remain the active execution venues.

## Preserved evidence

- Execution ledger: `~/.local/state/lts/ibkr-model-execution.sqlite`
- Observation ledger: `~/.local/state/lts/ibkr-paper-lab.sqlite`
- Historical result: 12 closed `USD.CAD` Paper exposures
- Source, tests, profiles and owner resume capabilities remain intact.

Do not delete or rewrite these files. They are the evidence needed to compare
IBKR with the active venues and to resume development later.

## Suspension controls

- `lts-ibkr-model-runner.service`: stopped and disabled
- `lts-ibkr-paper-observer.timer`: stopped and disabled
- `lts-tws-continuity-monitor.timer`: stopped and disabled
- Their services require the absent marker
  `~/.config/lts/enable-ibkr-paper`.
- The paper watchdog runs with `LTS_SUSPEND_IBKR=1`; it records IBKR as
  owner-suspended and emits no offline or stale alerts for it.
- TWS is not running and port 7497 is not listening.

## Reactivation

Reactivation is an explicit owner operation, not an automatic recovery:

```bash
touch ~/.config/lts/enable-ibkr-paper
sed -i '/^LTS_SUSPEND_IBKR=/d' \
  ~/.config/lts/paper-execution-watchdog.env
systemctl --user daemon-reload
systemctl --user unmask lts-ibkr-model-runner.service \
  lts-ibkr-paper-observer.service lts-ibkr-paper-observer.timer \
  lts-tws-continuity-monitor.service lts-tws-continuity-monitor.timer
systemctl --user enable --now lts-ibkr-paper-observer.timer \
  lts-tws-continuity-monitor.timer
```

Then start TWS Paper manually. Before enabling the model runner, independently
verify the connected Paper account, zero open orders, zero positions and a
fresh API session on loopback. Reconcile any unresolved effect through the
existing signed owner-resume process. Only then run:

```bash
systemctl --user enable --now lts-ibkr-model-runner.service
```

If TWS continuity remains unsuitable, leave IBKR suspended and use its adapter
only for bounded experiments.

## Active replacement

`USDCAD` is available and trade-enabled in the OANDA MT5 Demo account. The
planned replacement is a separate `USDCAD` MT5 model route with its own magic
number, risk accounting and evidence stream. The existing `ETHUSD` route must
remain operational while that profile is introduced.
