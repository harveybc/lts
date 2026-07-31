"""CLI for the GET-only Capital.com Demo laboratory."""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from app.capital_demo_lab import (
    CapitalDemoClient,
    CapitalDemoConfig,
    CapitalDemoError,
    CapitalDemoOlap,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("command", choices=("observe", "report"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    store = None
    lock = None
    try:
        config = CapitalDemoConfig.load(args.config)
        if args.command == "observe":
            lock_path = config.database_path.with_suffix(".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = lock_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CapitalDemoError(
                    "Another Capital.com Demo observer is running"
                ) from exc
        store = CapitalDemoOlap(config.database_path)
        if args.command == "report":
            result = store.report()
        else:
            api_key, identifier, password = config.credentials()
            client = CapitalDemoClient(
                api_key,
                identifier,
                password,
                timeout_seconds=config.timeout_seconds,
            )
            result = store.record(
                client.snapshot(config.search_terms),
                client.probes,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (CapitalDemoError, ValueError, json.JSONDecodeError) as exc:
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
