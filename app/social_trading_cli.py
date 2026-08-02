"""Command line entry point for the no-order social-trading reality lab."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from app.social_trading_lab import (
    SocialPlatformRegistry,
    SocialTradingLabError,
    SocialTradingOlap,
    SocialTradingScenario,
    run_scenario,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("registry", "run-scenario", "report")
    )
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--scenario", type=Path)
    parser.add_argument("--database", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    store = None
    try:
        if args.command == "registry":
            if args.registry is None:
                raise SocialTradingLabError("--registry is required")
            result = SocialPlatformRegistry.load(args.registry).report()
        elif args.command == "run-scenario":
            if args.scenario is None:
                raise SocialTradingLabError("--scenario is required")
            scenario = SocialTradingScenario.load(args.scenario)
            if args.database is not None:
                scenario = replace(
                    scenario,
                    database_path=args.database.expanduser(),
                )
            registry = SocialPlatformRegistry.load(scenario.registry_path)
            store = SocialTradingOlap(scenario.database_path)
            result = run_scenario(scenario, registry, store)
        else:
            if args.database is None:
                raise SocialTradingLabError("--database is required")
            store = SocialTradingOlap(args.database)
            result = store.report()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (SocialTradingLabError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"status": "blocked", "error": str(exc)}, indent=2),
            file=sys.stderr,
        )
        return 2
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
