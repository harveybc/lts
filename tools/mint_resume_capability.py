#!/usr/bin/env python3
"""Mint ONE single-use resume_after_reconciliation capability (owner only).

Musashi addendum 2026-08-04 §9: the fail-closed hold is cleared only by a
dedicated owner operation consuming a short-lived, nonce-bound capability.
This tool is the ONLY writer of the resume-capability store. The minted
file is INERT until the owner signs it with the passphrase-protected
Ed25519 key — that signature is the human-authentication boundary
(findings 094/227). The interactive-terminal check and the typed
confirmation phrase below are ergonomic guards against accidental
invocation only; they authenticate nobody. The file (with its nonce)
never enters Git or chat.
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

from app.ibkr_l1_adapter import FINGERPRINT_ALGORITHM  # noqa: E402
from app.ibkr_l1_capability import capability_digest  # noqa: E402
from app.ibkr_model_authority import ContinuousPaperProfile  # noqa: E402
from app.ibkr_l1_resume import (  # noqa: E402
    MAX_RESUME_VALIDITY_SECONDS,
    RESUME_OPERATION,
    RESUME_SCHEMA_VERSION,
    RESUME_STORE,
    validate_resume_capability,
)

CONFIRMATION_PHRASE = "resume ibkr paper after reconciliation"
DEFAULT_VALIDITY_SECONDS = 600


def mint_payload(profile: ContinuousPaperProfile, *, resume_of_effect_id: str,
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

    # Ergonomic confirmation ONLY — never authentication (finding 227).
    # The minted file stays inert until signed with the owner's
    # passphrase-protected key.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "REFUSED: minting requires an interactive owner terminal "
            "(ergonomic guard against accidental invocation; the owner "
            "signature is the actual authentication).",
            file=sys.stderr,
        )
        return 2

    profile = ContinuousPaperProfile.load(args.profile)
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
    print("")
    print("Finding 094: this capability is INERT until you sign it with")
    print("your passphrase-protected owner key (see")
    print("docs/security/OWNER_RESUME_SIGNER_SETUP_2026_08_05.md):")
    print(f"  ssh-keygen -Y sign -f ~/.ssh/lts_owner_resume "
          f"-n lts-ibkr-resume {path}")
    print("Then run (naming the file explicitly, finding 227):")
    print(f"  python tools/ibkr_resume_after_reconciliation.py "
          f"--config <runner config> --capability {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
