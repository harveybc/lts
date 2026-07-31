# Multi-Venue Paper Execution

Status: Alpaca and IBKR Paper observation active; MT5 VM and read-only bridge in commissioning
Date: 2026-07-30

## Account State

User-reported:

- Alpaca Trading API account: created and verified;
- IBKR Individual Margin account: created and verified;
- OANDA Global Markets live account: compliance review pending;
- OANDA Global Markets MT5 demo: credentials remain user-owned and must be
  entered only inside MT5 Desktop;
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

The first implemented capability is intentionally read-only:

1. `LtsMt5ReadOnlyBridge.mq5` refuses non-demo accounts and refuses to start
   when its read-only switch is disabled.
2. `OnTimer` posts authenticated heartbeats and full account, position, order,
   symbol, quote, spread and volume-constraint snapshots.
3. `OnTradeTransaction` reports idempotent event facts.
4. Requests use HMAC-SHA256, timestamp bounds and persistent nonce replay
   protection.
5. The Linux bridge persists restart-safe SQLite OLAP facts and exposes no
   executable command.
6. The broker plugin fails every mutation closed until protected canaries are
   explicitly enabled.

The later execution-capable EA must validate nonce, expiry, idempotency,
account, symbol, volume, margin, order type, SL and TP. `OrderCheck` must
precede `OrderSend`, and entry without both accepted broker-side protection
legs is forbidden. The EA performs no model inference, portfolio allocation
or global risk calculation. `WebRequest` is transport for demo/live MT5 and is
not available inside the MT5 Strategy Tester.

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
  runs the authenticated read-only contract preflight when Paper port `7497`
  is available. A nonblocking file lock prevents overlapping preflights;
- `lts-paper-execution-watchdog.timer` checks freshness, endpoint failures,
  missing quotes, unexpected exposure and venue availability. IBKR health
  requires a recent completed authenticated session plus reconciliation facts;
  a reachable TCP port alone is only diagnostic evidence. Its MT5 input will
  be pointed at Dragon's bridge facts after the first heartbeat.

Dragon runs `lts-mt5-bridge.service` plus an independent five-minute
`lts-mt5-bridge-watchdog.timer`. The bridge listens on TCP `8766`, with host
firewall access restricted to the libvirt NAT subnet and Tailscale. Its local
watchdog reports missing/stale heartbeats, broker disconnection and unexpected
exposure through the existing Hermes Telegram channel even if Omega is
offline. The shared secret lives at `~/.config/lts/mt5-bridge.env` with mode
`600`; that value is entered locally into the EA and is never committed or
pasted.

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

Dragon can expose GNOME's system RDP login to Remmina without requiring a
physical visit. The setup uses a dedicated TLS certificate and RDP credential,
allows input control, and limits TCP `3389` to Dragon's local Wi-Fi subnet and
Tailscale interface:

```bash
ssh -t dragon \
  'cd ~/Documents/GitHub/lts && sudo ./examples/scripts/enable_dragon_remote_login.sh'
```

Use `192.168.1.235` while Omega is connected to the local Wi-Fi and
`100.110.215.85` over Tailscale. Keep the RDP credential distinct from system,
broker, GitHub, and email credentials. The Windows VM remains a separate
connection; Dragon remote login is for administering the Ubuntu host.

In Remmina's RDP profile, set **Colour depth** to **Automatic (32 bpp)** and
**Network connection type** to **Auto-detect**. Quick Connect can persist
`32 bpp` plus `None`; that combination does not advertise the Graphics Pipeline
required by GNOME Remote Login.

Verified runtime state on 2026-07-29:

- KVM, libvirt 12.0.0, QEMU 10.2.1 and the persistent NAT network pass host
  validation on Dragon;
- the official Windows 11 25H2 English International x64 ISO matches
  Microsoft's published SHA-256
  `66b7b4b71763ed6f9b2ce29326ed9284544da6f5283d00329921540c01aaaeea`;
- `lts-mt5-paper` is defined and running with the declared resources, UEFI,
  Secure Boot and TPM 2.0;
- Windows Setup has booted and awaits the user's interactive language,
  licensing, edition and account choices. MT5 is not installed yet.

Implemented and tested on 2026-07-30:

- authenticated FastAPI bridge, SQLite OLAP and fail-closed broker plugin;
- read-only MT5 EA source under `mt5/MQL5/Experts`;
- HMAC timestamp/nonce replay tests, snapshot/event persistence tests and MT5
  watchdog tests;
- pinned bridge-only Python dependencies matching the validated Omega
  `trading-stack` environment;
- reproducible Dragon service and firewall scripts.

The EA source is not accepted as operational until MetaEditor compiles it with
zero errors inside the installed terminal.

## Dragon Bridge Setup

After pulling the matching LTS commit on Dragon:

```bash
./examples/scripts/configure_mt5_bridge.sh
sudo ./examples/scripts/enable_mt5_bridge_host_firewall.sh
curl http://192.168.122.1:8766/health
```

Inside MT5:

1. log in to the OANDA demo locally;
2. add `http://192.168.122.1:8766` to
   **Tools > Options > Expert Advisors > Allow WebRequest for listed URL**;
3. copy `mt5/MQL5/Experts/LtsMt5ReadOnlyBridge.mq5` into the terminal data
   folder's `MQL5/Experts` directory;
4. compile it in MetaEditor and require `0 errors`;
5. attach it to one chart, keep `InpReadOnly=true`, set the bridge URL and
   enter the locally displayed bridge secret;
6. verify heartbeats and snapshots before beginning the 24-hour clock.

## Required Next Inputs

- complete Windows Setup in the verified Dragon VM, then install MT5 Desktop
  and enter the demo credentials only inside the terminal;
- compile the read-only EA with zero errors and verify an authenticated
  heartbeat plus full snapshot;
- obtain REST-v20 Practice credentials only if the OANDA division exposes that
  API; MT5 credentials cannot be used with REST v20.

The complete architecture, OLAP contract and social-trading boundary are in:

```text
agent-multi/docs/work_plan/22_MULTI_VENUE_PAPER_EXECUTION_AND_SOCIAL_TRADING.md
```
