#!/usr/bin/env python3
"""Mint ONE single-use Paper promotion capability (owner only, WO3).

Mirror of ``tools/mint_resume_capability.py`` (findings 094/227): this
tool is the ONLY writer of the promotion-capability store. The minted
file is INERT until the owner signs it with the passphrase-protected
Ed25519 key::

    ssh-keygen -Y sign -f ~/.ssh/owner_promotion_key \
        -n lts-paper-promotion <capability>.json

That signature over the exact capability bytes is the human
authentication boundary; the interactive-terminal check and the typed
confirmation phrase below are ergonomic guards against accidental
invocation only — they authenticate nobody. The file (with its nonce)
never enters Git or chat. Promotion additionally requires the seat to
be flat and every WO3 stage (compatibility proof, shadow evidence,
native protection, drain/carry) to pass at consumption time.
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

from app.champion_succession import (  # noqa: E402
    MAX_PROMOTION_VALIDITY_SECONDS,
    PROMOTION_OPERATION,
    PROMOTION_SCHEMA_VERSION,
    PROMOTION_STORE,
    PROMOTION_VENUES,
)

CONFIRMATION_PHRASE = "promote paper champion"
DEFAULT_VALIDITY_SECONDS = 900


def mint_payload(args: argparse.Namespace) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": PROMOTION_SCHEMA_VERSION,
        "operation": PROMOTION_OPERATION,
        "venue": args.venue,
        "asset_id": args.asset_id,
        "instrument": args.instrument,
        "timeframe": args.timeframe,
        "incumbent_model_id": args.incumbent_model_id,
        "incumbent_artifact_sha256": args.incumbent_artifact_sha256,
        "candidate_model_id": args.candidate_model_id,
        "candidate_artifact_sha256": args.candidate_artifact_sha256,
        "candidate_config_sha256": args.candidate_config_sha256,
        "compatibility_report_sha256": args.compatibility_report_sha256,
        "shadow_report_sha256": args.shadow_report_sha256,
        "issued_at": now.isoformat(),
        "expires_at": (
            now + timedelta(seconds=args.validity_seconds)
        ).isoformat(),
        "nonce": secrets.token_hex(32),
    }


def write_capability(payload: dict, store_dir: Path) -> Path:
    store_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(store_dir, 0o700)
    path = store_dir / f"promotion_{payload['nonce'][:8]}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return path


def _sha(value: str, name: str) -> str:
    value = str(value).lower()
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise SystemExit(f"{name} must be a 64-hex sha256 digest")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", required=True, choices=sorted(
        PROMOTION_VENUES))
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--incumbent-model-id", required=True)
    parser.add_argument("--incumbent-artifact-sha256", required=True)
    parser.add_argument("--candidate-model-id", required=True)
    parser.add_argument("--candidate-artifact-sha256", required=True)
    parser.add_argument("--candidate-config-sha256", required=True)
    parser.add_argument("--compatibility-report-sha256", required=True)
    parser.add_argument("--shadow-report-sha256", required=True)
    parser.add_argument("--validity-seconds", type=int,
                        default=DEFAULT_VALIDITY_SECONDS)
    parser.add_argument("--store-dir", type=Path, default=PROMOTION_STORE)
    args = parser.parse_args()

    for name in ("incumbent_artifact_sha256", "candidate_artifact_sha256",
                 "candidate_config_sha256", "compatibility_report_sha256",
                 "shadow_report_sha256"):
        setattr(args, name, _sha(getattr(args, name), name))
    if not 0 < args.validity_seconds <= MAX_PROMOTION_VALIDITY_SECONDS:
        raise SystemExit(
            f"validity must be in (0, {MAX_PROMOTION_VALIDITY_SECONDS}]s")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit(
            "minting requires an interactive owner terminal (ergonomic"
            " guard; the owner SIGNATURE is the real boundary)")
    print("This mints a SINGLE-USE Paper promotion capability for"
          f" {args.venue}/{args.instrument}: {args.incumbent_model_id}"
          f" -> {args.candidate_model_id}.")
    typed = input(f"Type the confirmation phrase ({CONFIRMATION_PHRASE!r})"
                  " to continue: ").strip()
    if typed != CONFIRMATION_PHRASE:
        raise SystemExit("confirmation phrase mismatch; nothing minted")

    payload = mint_payload(args)
    path = write_capability(payload, args.store_dir)
    print(f"minted (INERT until owner-signed): {path}")
    print("sign it with: ssh-keygen -Y sign -f <owner_key>"
          f" -n lts-paper-promotion {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
