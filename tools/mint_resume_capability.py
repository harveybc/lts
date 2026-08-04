#!/usr/bin/env python3
"""Mint ONE single-use resume_after_reconciliation capability (owner only).

Musashi addendum 2026-08-04 §9: the fail-closed hold is cleared only by a
dedicated owner operation consuming a short-lived, nonce-bound capability.
This tool is the ONLY writer of the resume-capability store. It refuses
outside an interactive owner terminal, so no model, LLM, Hermes process or
chat-driven pipeline can mint. The file (with its nonce) never enters Git
or chat.
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
from app.ibkr_l1_capability import capability_digest  # noqa: E402
from app.ibkr_l1_resume import (  # noqa: E402
    MAX_RESUME_VALIDITY_SECONDS,
    RESUME_OPERATION,
    RESUME_SCHEMA_VERSION,
    RESUME_STORE,
    validate_resume_capability,
)

CONFIRMATION_PHRASE = "resume ibkr paper after reconciliation"
DEFAULT_VALIDITY_SECONDS = 600


def mint_payload(profile: L1Profile, *, resume_of_effect_id: str,
                 validity_seconds: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": RESUME_SCHEMA_VERSION,
        "operation": RESUME_OPERATION,
        "venue": "ibkr_paper",
        "host": "127.0.0.1",
        "port": 7497,
        "profile_hash": profile.profile_hash,
        "profile_schema_version": profile.schema_version,
        "account_fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "account_fingerprint": profile.account_fingerprint,
        "asset_id": profile.asset_id,
        "instrument": profile.instrument,
        "resume_of_effect_id": resume_of_effect_id,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=validity_seconds)).isoformat(),
        "nonce": secrets.token_hex(32),
    }


def write_capability(payload: dict, store_dir: Path) -> Path:
    store_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(store_dir, 0o700)
    path = store_dir / f"resume_{payload['nonce'][:8]}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True,
                        help="L1 profile JSON path")
    parser.add_argument("--resume-of-effect-id", required=True,
                        help="the recovery effect this resume closes out")
    parser.add_argument(
        "--validity-seconds", type=int, default=DEFAULT_VALIDITY_SECONDS,
        help=f"expiry window, at most {MAX_RESUME_VALIDITY_SECONDS}")
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
        resume_of_effect_id=args.resume_of_effect_id,
        validity_seconds=args.validity_seconds,
    )
    validate_resume_capability(payload, profile=profile)

    print("About to mint ONE single-use RESUME capability:")
    print(f"  instrument      {payload['instrument']}")
    print(f"  resume of       {payload['resume_of_effect_id']}")
    print(f"  expires         {payload['expires_at']}")
    print("It clears halt=hold ONLY after fresh direct broker evidence")
    print("proves zero positions and zero open orders.")
    typed = input(f"Type the phrase '{CONFIRMATION_PHRASE}' to proceed: ")
    if typed.strip() != CONFIRMATION_PHRASE:
        print("REFUSED: confirmation phrase mismatch.", file=sys.stderr)
        return 3

    path = write_capability(payload, RESUME_STORE)
    print(f"Minted: {path}")
    print(f"Digest: {capability_digest(payload)}")
    print("Now run: python tools/ibkr_resume_after_reconciliation.py "
          "--config <runner config>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
