"""Run or inspect the authenticated LTS MT5 demo bridge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

import uvicorn

from app.mt5_bridge_lab import (
    Mt5BridgeConfig,
    Mt5BridgeStore,
    create_mt5_bridge_app,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("command", choices=("serve", "report"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    config = Mt5BridgeConfig.load(args.config)
    store = Mt5BridgeStore(config.database_path)
    if args.command == "report":
        try:
            print(
                json.dumps(
                    store.report(config.stale_heartbeat_seconds),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            store.close()

    app = create_mt5_bridge_app(config, store, config.secret())
    uvicorn.run(
        app,
        host=config.bind_host,
        port=config.port,
        access_log=False,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
