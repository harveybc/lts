"""CLI for the read-only Alpaca Paper capability laboratory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from app.alpaca_paper_lab import (
    AlpacaPaperClient,
    AlpacaPaperLab,
    AlpacaPaperLabConfig,
    AlpacaPaperOlap,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("command", choices=("preflight", "report"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    config = AlpacaPaperLabConfig.load(args.config)
    store = AlpacaPaperOlap(config.database_path)
    try:
        if args.command == "report":
            result = store.report()
        else:
            api_key, api_secret = config.credentials()
            client = AlpacaPaperClient(
                api_key,
                api_secret,
                timeout_seconds=config.timeout_seconds,
            )
            result = AlpacaPaperLab(config, client, store).preflight()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
