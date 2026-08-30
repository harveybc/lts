# Alpaca recorded capture — what was observed, and what was substituted

This set preserves the **categorical shape** of an owner-authorized
read-only export. Every value that could identify an account, an
order or a moment has been substituted. The two categories are kept
apart here so nobody has to guess which is which.

## Observed — these are facts about the real response

- one **SPY short** position;
- one **open SPY buy** order;
- order `status` = `new`;
- `order_class` = `bracket`;
- `type` = `limit`;
- `position_intent` = `buy_to_close`;
- `legs` is **null/empty** on that open order.

That combination is the whole point of this fixture: after the
bracket parent fills, the endpoint returns the resting protective
child at the **top level**, with no nested legs, and Alpaca declares
what it is for with `position_intent`. Read without that field, a BUY
standing against a SHORT position looks exactly like a reversal —
which is how it was previously classified as an **entry** and offered
for cancellation during wind-down.

## Substituted — these are NOT observations

- every identifier: account id, account number, asset id, order id;
- every price and quantity;
- every timestamp;
- the account fingerprint, which is derived from a synthetic id.

No real identifier, price, quantity, timestamp, credential, host or
private path appears in this repository.

## Supporting provenance, not evidence

A durable local lifecycle independently records a real bracket parent
with three broker ids and two protection legs. It corroborates the
shape above; it is not a substitute for the direct payload and no
fact here is taken from it.
