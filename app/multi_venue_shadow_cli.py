"""CLI for the continuous no-order multi-venue shadow portfolio."""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from app.multi_venue_shadow import (
    MultiVenueShadow,
    MultiVenueShadowConfig,
    MultiVenueShadowError,
    MultiVenueShadowOlap,
    QuoteReader,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("command", choices=("snapshot", "report"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    store = None
    lock = None
    try:
        config = MultiVenueShadowConfig.load(args.config)
        if args.command == "snapshot":
            lock_path = config.database_path.with_suffix(".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = lock_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MultiVenueShadowError(
                    "Another multi-venue shadow snapshot is running"
                ) from exc
        store = MultiVenueShadowOlap(config.database_path)
        if args.command == "report":
            result = store.report()
        else:
            result = MultiVenueShadow(
                config,
                QuoteReader(config.source_databases),
                store,
            ).snapshot()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (MultiVenueShadowError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": str(exc)},
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if store is not None:
            store.close()
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
