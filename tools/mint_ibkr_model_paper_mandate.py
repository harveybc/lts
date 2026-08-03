#!/usr/bin/env python3
"""Create a revocable continuous-model mandate for one IBKR Paper route."""
from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.ibkr_model_authority import ContinuousPaperProfile, MANDATE_SCHEMA


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-risk-fraction", type=float, default=0.0000625)
    parser.add_argument("--quantity-ceiling", type=float, default=20000.0)
    parser.add_argument("--yes-i-authorize-paper-model-execution", action="store_true")
    args = parser.parse_args()
    if not args.yes_i_authorize_paper_model_execution:
        parser.error("explicit Paper model-execution authorization flag is required")
    if not 1 <= args.days <= 90:
        parser.error("--days must be in [1, 90]")
    profile = ContinuousPaperProfile.load(args.profile.expanduser())
    if not 0 < args.max_risk_fraction <= 0.01:
        parser.error("risk fraction must be in (0, 0.01]")
    if not 0 < args.quantity_ceiling <= profile.quantity_ceiling:
        parser.error("quantity ceiling exceeds the Paper profile")
    now = datetime.now(timezone.utc)
    payload = {
        "schema": MANDATE_SCHEMA,
        "environment": "paper",
        "venue": "ibkr_paper",
        "profile_hash": profile.profile_hash,
        "asset_id": profile.asset_id,
        "instrument": profile.instrument,
        "execution_tier": "demo_research_canary",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(days=args.days)).isoformat(),
        "max_risk_fraction_at_stop": args.max_risk_fraction,
        "quantity_ceiling": args.quantity_ceiling,
        "max_entries_per_day": profile.max_entries_per_day,
        "mandate_id": str(uuid.uuid4()),
    }
    output = args.output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    os.chmod(output, 0o600)
    print(json.dumps({
        "created": str(output), "environment": "paper",
        "profile_hash": profile.profile_hash, "expires_at": payload["expires_at"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
