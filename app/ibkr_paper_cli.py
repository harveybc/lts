"""CLI for the read-only IBKR TWS Paper capability laboratory."""

from __future__ import annotations

import argparse
import fcntl
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
    lock = None
    store = None
    try:
        try:
            if args.command == "preflight":
                lock_path = config.database_path.with_name(
                    config.database_path.name + ".lock"
                )
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock = lock_path.open("a+", encoding="utf-8")
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise IbkrPaperError(
                        "Another IBKR Paper preflight is already running"
                    ) from exc
            store = IbkrPaperOlap(config.database_path)
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
        if store is not None:
            store.close()
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
