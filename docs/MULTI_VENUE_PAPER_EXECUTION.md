# Multi-Venue Paper Execution

Status: Alpaca and IBKR Paper observation active; OANDA not configured
Date: 2026-07-29

## Account State

User-reported:

- Alpaca Trading API account: created and verified;
- IBKR Individual Margin account: created and verified;
- OANDA Global Markets live account: compliance review pending;
- OANDA Global Markets MT5 demo: still requires local creation/credentials;
- Alpaca Paper credentials: provisioned locally and verified by the read-only
  preflight;
- IBKR TWS Paper: authenticated read-only observer active on
  `127.0.0.1:7497`; all six initial contracts qualified with zero positions
  and zero orders.

Never commit or paste credentials, raw account IDs or recovery information.

## Ownership

LTS owns:

- one global NAV and portfolio state;
- customer/global risk and capital reservations;
- virtual strategy sleeves and canonical asset exposure;
- capability-aware venue selection;
- idempotent route/order identities;
- consolidated reconciliation and audit.

MT5, Alpaca and IBKR are execution adapters only. Demo balances are not summed
to fabricate portfolio capital.

## Initial Venue Roles

| Adapter | Initial role | Execution eligibility |
| --- | --- | --- |
| OANDA Global Markets MT5 | FX and available crypto-CFD calibration | eligible after MT5 demo capability and protected-canary gates |
| Alpaca Trading API Paper | crypto data/API and long-only control | shadow-only until native server-side SL+TP satisfies the common contract |
| IBKR Individual Margin Paper | equities/ETF and broad multi-asset calibration | eligible after Paper/TWS or Web API capability and protected-canary gates |

The existing `oanda_broker` and OANDA Practice laboratory use REST v20 and are
not OANDA Global Markets adapters.

## Protection

Every risk-increasing order requires broker-side SL and TP. The adapter rejects
an order before submission when either is absent. If entry fills but both
protections cannot be confirmed, the adapter cancels residual orders, flattens
the new exposure and emits a critical alert.

Client-side polling is not accepted as the sole stop-loss mechanism.

## OANDA MT5 Bridge

The preferred adapter is a thin Expert Advisor:

1. `OnTimer` polls a signed, allowlisted LTS command endpoint.
2. It validates nonce, expiry, idempotency, account, symbol, volume, margin,
   order type, SL and TP.
3. `OrderCheck` precedes `OrderSend`.
4. `OnTradeTransaction` reports acknowledgements and state transitions.
5. Periodic full snapshots repair missed events.

The EA performs no model inference, portfolio allocation or global risk
calculation. `WebRequest` is transport for demo/live MT5 and is not available
inside the MT5 Strategy Tester.

## Activation Sequence

1. Provision one untracked local secret file per venue.
2. Authenticate read-only and record account fingerprints and adapter versions.
3. Discover instruments, short/order/protection capability, precision, size,
   margin, costs, market hours and data entitlements.
4. Observe each venue read-only for 24 hours.
5. Review evidence and run minimum-size protected canaries on eligible venues.
6. Run a seven-day consolidated paper portfolio with one synthetic NAV.
7. Require explicit approval before any real-capital pilot.

## Active Observation Runtime

Omega currently runs three five-minute user timers:

- `lts-alpaca-paper-observer.timer` records account/instrument capabilities,
  quotes, endpoint latency and reconciliation facts;
- `lts-ibkr-paper-observer.timer` waits without failing while TWS is closed and
  runs the read-only contract preflight when Paper port `7497` is available;
- `lts-paper-execution-watchdog.timer` checks freshness, endpoint failures,
  missing quotes, unexpected exposure and venue availability.

The watchdog writes restart-safe state under `~/.local/state/lts`, records
event transitions in SQLite and sends deduplicated Telegram alerts through the
existing Hermes bot configuration. It also emits a sanitized discussion packet
for Hermes. A bounded DeepSeek review runs every 12 hours with only the `todo`
toolset and cannot place orders, change risk, enqueue jobs or promote models.
Interesting one-hour and four-hour moves are discussion candidates, never
automatic trading signals.

## Alpaca Paper Credentials

Generate credentials while the Alpaca dashboard is in Paper Trading mode, then
store them locally with one interactive command:

```bash
./examples/scripts/configure_alpaca_paper.sh
```

The script hides the secret while it is entered and writes
`~/.config/lts/alpaca-paper.env` with mode `600`. Credentials never belong in
Git, documentation, command arguments or chat.

## IBKR Paper Startup

IBKR has no API key/secret pair. Start TWS for Linux in Paper mode, authenticate
interactively with 2FA, enable socket clients, retain Read-Only API and use
port `7497`. Then run:

```bash
./examples/scripts/enable_ibkr_paper_observer.sh
```

IB Gateway Paper may later use port `4002`, but the initial laboratory is
intentionally pinned to local TWS Paper `7497`.

## OANDA Credential Boundaries

If the account division supplies a REST-v20 Practice account ID and token:

```bash
./examples/scripts/configure_oanda_practice.sh
```

OANDA Global Markets MT5 credentials are different: login, password and
`OANDA_Global-Demo-1` are entered inside MT5 Desktop and must not be stored by
the Linux observer. MT5 Web Trader cannot run Expert Advisors. The preferred
host is `dragon`: it has 30 GiB RAM, 32 CPU threads, KVM and substantially more
free disk than `gamma`, while remaining stationary. The VM uses 8 GiB RAM,
4 vCPU, a 100 GiB sparse disk, NAT, UEFI and TPM 2.0. Gamma is excluded because
it has 14 GiB RAM, limited free disk and owns both the internal 5070 Ti and
external 5090. Omega is excluded because it travels. Native dual boot removes a
Linux worker; Wine remains a compatibility fallback.

Host and VM creation are reproducible:

```bash
sudo ./examples/scripts/setup_mt5_vm_host.sh
./examples/scripts/create_mt5_windows_vm.sh /path/to/Windows11.iso
```

## Required Next Inputs

- start IBKR TWS in Paper mode on Omega and confirm port `7497`;
- create the OANDA Global Markets MT5 demo and retain its credentials locally;
- select and provision the Windows VM for the MT5 terminal and EA bridge;
- obtain REST-v20 Practice credentials only if the OANDA division exposes that
  API; MT5 credentials cannot be used with REST v20.

The complete architecture, OLAP contract and social-trading boundary are in:

```text
agent-multi/docs/work_plan/22_MULTI_VENUE_PAPER_EXECUTION_AND_SOCIAL_TRADING.md
```
