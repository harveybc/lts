"""CLI for the read-only IBKR TWS Paper capability laboratory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from app.ibkr_paper_lab import (
    IbkrPaperLab,
    IbkrPaperLabConfig,
    IbkrPaperError,
    IbkrPaperOlap,
    IbkrTwsPaperClient,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("command", choices=("preflight", "report"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    config = IbkrPaperLabConfig.load(args.config)
    store = IbkrPaperOlap(config.database_path)
    try:
        try:
            if args.command == "report":
                result = store.report()
            else:
                client = IbkrTwsPaperClient(
                    config.host,
                    config.port,
                    config.client_id,
                    timeout_seconds=config.timeout_seconds,
                )
                result = IbkrPaperLab(config, client, store).preflight()
        except IbkrPaperError as exc:
            print(
                json.dumps(
                    {"status": "blocked", "error": str(exc)},
                    indent=2,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
