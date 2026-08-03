#!/usr/bin/env python3
"""Fetch a normalized, same-source closed-bar history from Alpaca IEX."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from app.alpaca_paper_lab import AlpacaPaperClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if config.get("schema") != "lts.alpaca.closed_bar_fetch.v1":
        raise ValueError("unsupported Alpaca bar-fetch config")
    import os
    key = os.environ[config["api_key_env"]]
    secret = os.environ[config["api_secret_env"]]
    client = AlpacaPaperClient(key, secret, timeout_seconds=config.get("timeout_seconds", 30))
    bars = []
    token = None
    while True:
        page = client.stock_bars(
            config["symbol"], timeframe=config["timeframe"],
            start=config["start"], end=config.get("end"), feed="iex",
            page_token=token,
        )
        bars.extend(page.get("bars", []))
        token = page.get("next_page_token")
        if not token:
            break
    now = datetime.now(timezone.utc)
    normalized = []
    for bar in bars:
        timestamp = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
        if config["timeframe"] in {"1Day", "1D"}:
            complete = timestamp.date() < now.date()
        else:
            seconds = int(config["bar_seconds"])
            complete = timestamp.timestamp() + seconds <= now.timestamp()
        if complete:
            normalized.append({
                "DateTime": timestamp.isoformat(), "Open": bar["o"],
                "High": bar["h"], "Low": bar["l"], "Close": bar["c"],
                "Volume": bar["v"],
            })
    output = Path(config["output_file"]).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["DateTime", "Open", "High", "Low", "Close", "Volume"])
        writer.writeheader()
        writer.writerows(normalized)
    print(json.dumps({
        "schema": "lts.alpaca.closed_bar_fetch_result.v1",
        "symbol": config["symbol"], "feed": "iex",
        "timeframe": config["timeframe"], "bars": len(normalized),
        "first": normalized[0]["DateTime"] if normalized else None,
        "last": normalized[-1]["DateTime"] if normalized else None,
        "output_file": str(output),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
