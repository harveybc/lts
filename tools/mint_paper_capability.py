#!/usr/bin/env python3
"""Offline, owner-only minting of one single-use IBKR Paper capability.

This tool is the ONLY writer of the capability store (owner constraint:
privileged authority separation). It must be run by the owner in an
interactive terminal — it refuses pipes, subprocesses and any non-TTY
invocation — and it requires the confirmation phrase to be typed at the
prompt. It imports no broker library and opens no socket.

Storage is fixed and protected: ``~/.lts/paper_capabilities`` is created
with mode 0700 and the capability file with mode 0600. One capability
authorizes at most ONE protected bracket (``max_entries`` is hard-coded
to 1) and expires after at most one hour (default 15 minutes).

The executor consumes the capability by burning its digest atomically in
the durable L0 ledger; this tool never touches that ledger and the
executor never writes this store.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ibkr_l1_adapter import FINGERPRINT_ALGORITHM, L1Profile  # noqa: E402
from app.ibkr_l1_capability import (  # noqa: E402
    CAPABILITY_STORE,
    CAPABILITY_SCHEMA_VERSION,
    DEFAULT_VALIDITY_SECONDS,
    MAX_VALIDITY_SECONDS,
    capability_digest,
    validate_capability,
)

CONFIRMATION_PHRASE = "MINT ONE PAPER CAPABILITY"


def mint_payload(
    profile: L1Profile,
    *,
    quantity_ceiling: float,
    max_risk_fraction_at_stop: float,
    validity_seconds: int,
    contract_con_id: int | None,
    now: datetime | None = None,
) -> dict:
    """Construct the capability document. Pure; no filesystem, no network."""
    now = now or datetime.now(timezone.utc)
    if not 0 < validity_seconds <= MAX_VALIDITY_SECONDS:
        raise ValueError(
            f"validity_seconds must be in (0, {MAX_VALIDITY_SECONDS}]"
        )
    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "venue": "ibkr_paper",
        "host": "127.0.0.1",
        "port": 7497,
        "profile_hash": profile.profile_hash,
        "profile_schema_version": profile.schema_version,
        "account_fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "account_fingerprint": profile.account_fingerprint,
        "asset_id": profile.asset_id,
        "instrument": profile.instrument,
        "contract_con_id": contract_con_id,
        "max_risk_fraction_at_stop": max_risk_fraction_at_stop,
        "quantity_ceiling": quantity_ceiling,
        "max_entries": 1,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=validity_seconds)).isoformat(),
        "nonce": secrets.token_hex(32),
    }


def write_capability(payload: dict, store_dir: Path) -> Path:
    """Write one capability file into the protected store (0700/0600)."""
    store_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(store_dir, 0o700)
    path = store_dir / f"capability_{payload['nonce'][:8]}.json"
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="L1 profile v2 JSON path")
    parser.add_argument("--quantity-ceiling", type=float, required=True)
    parser.add_argument("--max-risk-fraction", type=float, default=0.005)
    parser.add_argument(
        "--validity-seconds", type=int, default=DEFAULT_VALIDITY_SECONDS,
        help=f"expiry window, at most {MAX_VALIDITY_SECONDS}",
    )
    parser.add_argument("--contract-con-id", type=int, default=None)
    args = parser.parse_args()

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "REFUSED: minting requires an interactive owner terminal "
            "(no pipes, no subprocess, no chat-driven invocation).",
            file=sys.stderr,
        )
        return 2

    profile = L1Profile.load(args.profile)
    payload = mint_payload(
        profile,
        quantity_ceiling=args.quantity_ceiling,
        max_risk_fraction_at_stop=args.max_risk_fraction,
        validity_seconds=args.validity_seconds,
        contract_con_id=args.contract_con_id,
    )
    # self-check against the same validator the gate uses, before writing
    validate_capability(payload, profile=profile)

    print("About to mint ONE single-use IBKR Paper capability:")
    print(f"  instrument        {payload['instrument']}")
    print(f"  quantity ceiling  {payload['quantity_ceiling']}")
    print(f"  max risk at stop  {payload['max_risk_fraction_at_stop']}")
    print(f"  expires           {payload['expires_at']}")
    typed = input(f"Type the phrase '{CONFIRMATION_PHRASE}' to proceed: ")
    if typed.strip() != CONFIRMATION_PHRASE:
        print("REFUSED: confirmation phrase mismatch.", file=sys.stderr)
        return 3

    path = write_capability(payload, CAPABILITY_STORE)
    print(f"Minted: {path}")
    print(f"Digest: {capability_digest(payload)}")
    print("The file content (including the nonce) stays outside Git and chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
