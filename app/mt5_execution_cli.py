"""Serve or inspect the MT5 Demo execution bridge v2."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn

from app.mt5_execution_bridge import (
    Mt5ExecutionConfig,
    Mt5ExecutionStore,
    create_mt5_execution_app,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("command", choices=("serve", "report"))
    args = parser.parse_args()
    config = Mt5ExecutionConfig.load(args.config)
    store = Mt5ExecutionStore(config.database_path)
    if args.command == "report":
        try:
            report = store.report(config.stale_heartbeat_seconds)
            report["command_counts"] = store.command_counts()
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        finally:
            store.close()
    app = create_mt5_execution_app(config, store, config.secret())
    uvicorn.run(app, host=config.bind_host, port=config.port,
                access_log=False, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
