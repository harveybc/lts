"""CLI for the OANDA Practice execution-observation laboratory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from app.oanda_practice_lab import (
    ORDER_CONFIRMATION,
    OandaPracticeClient,
    OandaPracticeLab,
    PracticeLabConfig,
    PracticeOlap,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight")

    observe = subparsers.add_parser("observe")
    observe.add_argument("--hours", required=True, type=float)

    subparsers.add_parser("report")

    canary = subparsers.add_parser("protected-canary")
    canary.add_argument("--instrument", required=True)
    canary.add_argument("--side", required=True, choices=("buy", "sell"))
    canary.add_argument("--units", required=True, type=float)
    canary.add_argument("--stop-distance-pips", required=True, type=float)
    canary.add_argument("--reward-risk-ratio", default=2.0, type=float)
    canary.add_argument(
        "--confirmation",
        required=True,
        help=f"Must equal {ORDER_CONFIRMATION}",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    config = PracticeLabConfig.load(args.config)
    store = PracticeOlap(config.database_path)
    try:
        if args.command == "report":
            result = store.report()
        else:
            account_id, access_token = config.credentials()
            client = OandaPracticeClient(account_id, access_token)
            lab = OandaPracticeLab(config, client, store)
            if args.command == "preflight":
                result = lab.preflight()
            elif args.command == "observe":
                result = lab.observe(args.hours * 3600.0)
            else:
                result = lab.protected_market_canary(
                    instrument=args.instrument,
                    side=args.side,
                    units=args.units,
                    stop_distance_pips=args.stop_distance_pips,
                    reward_risk_ratio=args.reward_risk_ratio,
                    confirmation=args.confirmation,
                )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
