"""WP3 C7 — a PROPOSED operator export for a sanitized Alpaca capture.

This command is proposed, not performed. Running it without
``--i-am-the-owner`` prints the plan and exits: it does not read the
private state tree, it does not connect to Alpaca, and it holds no
credential and no client. The owner is the only one who can execute
it, and even then it never leaves the local machine.

Why it exists at all: the WP3 evidence layer has no recorded Alpaca
payload, because the durable evidence lives in the operator's private
state tree that this public repository's rules forbid reading or
quoting, and opening a connection is forbidden by the WP3 order. The
gap is real and stays declared. This is the shape of the action that
would close it, published so it can be reviewed before anyone runs it.

The three stages are deliberately separate, and only the last one
produces anything a public repository may contain:

1. **read** a minimal set of fields from the private read-only store,
   in a single ``mode=ro`` connection, selecting nothing beyond what
   the WP3 parsers require;
2. **stage** the result into a private staging path (``0700`` /
   ``0600``) and validate it there against the WP3 parsers, so an
   unusable capture is discovered before anyone looks at it;
3. **redact** into a public fixture: every identifier replaced by a
   stable synthetic token, and the mapping kept only in the private
   staging path.

A field this repository must never contain — an account number, a
server name, a host, a path under the operator's home — is dropped at
stage 3 rather than renamed, because a renamed identifier is still an
identifier.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The fields each WP3 parser requires. The export selects EXACTLY
# these; anything else is not needed and is therefore not read.
REQUIRED_FIELDS = {
    "account_session": {
        "account": ["id", "account_number", "status",
                    "trading_blocked", "cash", "equity"],
        "clock": ["timestamp", "is_open", "next_open", "next_close"],
    },
    "positions": {
        "positions[]": ["asset_id", "symbol", "qty", "side",
                        "avg_entry_price"],
        "": ["observed_at"],
    },
    "open_orders": {
        "orders[]": ["id", "symbol", "side", "qty", "status",
                     "order_class", "type", "legs"],
        "orders[].legs[]": ["id", "side", "type", "qty", "status"],
        "": ["observed_at"],
    },
}

# Redacted at stage 3 — replaced by a stable synthetic token whose
# mapping never leaves the private staging path.
PSEUDONYMISE = ("id", "asset_id", "account_number")

# Dropped entirely. A renamed identifier is still an identifier.
DROP = ("account_blocked_reason", "created_at", "currency",
        "crypto_status", "admin_configurations")

PLAN = [
    ("read", "open the private read-only ledger with mode=ro and "
             "SELECT only the fields listed in REQUIRED_FIELDS"),
    ("stage", "write to a private staging path at 0700/0600 and "
              "validate it there with the WP3 parsers"),
    ("redact", "pseudonymise the identifier fields, drop the rest, "
               "and emit the public fixture"),
]


def describe() -> dict:
    return {
        "schema": "lts.propose_alpaca_capture_export.v1",
        "status": "PROPOSED — NOT EXECUTED",
        "performs_any_read_of_private_state": False,
        "connects_to_alpaca": False,
        "holds_credentials": False,
        "stages": [{"stage": name, "action": action}
                   for name, action in PLAN],
        "fields_selected": REQUIRED_FIELDS,
        "pseudonymised_at_redaction": list(PSEUDONYMISE),
        "dropped_entirely": list(DROP),
        "owner_gate": "--i-am-the-owner is required; without it this "
                      "command prints this plan and exits 0 without "
                      "touching anything",
        "note": "until an owner runs this and a redacted fixture is "
                "reviewed, WP3 C7 stays INCOMPLETE and the Alpaca "
                "bracket shape remains unverified against a recorded "
                "payload",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="PROPOSED operator export for a sanitized Alpaca "
                    "capture. Prints the plan unless the owner "
                    "explicitly executes it.")
    parser.add_argument("--i-am-the-owner", action="store_true",
                        help="the owner asserts they are running "
                             "this deliberately")
    parser.add_argument("--private-ledger", type=Path, default=None)
    parser.add_argument("--staging", type=Path, default=None)
    parser.add_argument("--public-fixture", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.i_am_the_owner:
        print(json.dumps(describe(), indent=1))
        return 0

    # Even under the owner gate this build performs no read. The
    # execution body is deliberately absent: publishing a command that
    # COULD read the private tree, in a public repository, is itself
    # the thing the rules forbid. The owner implements the three
    # stages locally, or instructs that they be implemented under
    # review, with the plan above as the contract.
    print(json.dumps({
        **describe(),
        "status": "OWNER GATE PASSED — EXECUTION BODY DELIBERATELY "
                  "ABSENT",
        "reason": "a public repository must not carry code capable of "
                  "reading the private state tree, even behind a "
                  "flag. Implement the three stages under review, "
                  "against the field list above.",
    }, indent=1))
    return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
