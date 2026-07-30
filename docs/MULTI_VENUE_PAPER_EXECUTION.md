# Multi-Venue Paper Execution

Status: specified; account credentials not yet provisioned to LTS
Date: 2026-07-29

## Account State

User-reported:

- Alpaca Trading API account: created and verified;
- IBKR Individual Margin account: created and verified;
- OANDA Global Markets live account: compliance review pending;
- OANDA Global Markets MT5 demo: still requires local creation/credentials.

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

## Required Next Inputs

- Alpaca Paper key ID and secret stored locally, never in chat/Git;
- IBKR Paper username and TWS login/API availability;
- OANDA MT5 demo login, password and `OANDA_Global-Demo-1` server;
- Windows host selected for the OANDA MT5 terminal and EA.

The complete architecture, OLAP contract and social-trading boundary are in:

```text
agent-multi/docs/work_plan/22_MULTI_VENUE_PAPER_EXECUTION_AND_SOCIAL_TRADING.md
```
