#!/usr/bin/env python3
"""Scheduled Alpaca crypto quote collector (WP-DATA, agent-multi order
@7886de39): turns the session-scoped preflight sampling into a
training-grade continuous spread/depth series.

Reuses the existing paper-lab plumbing unchanged: the read-only
``AlpacaPaperClient`` (market data only, paper endpoints enforced by the
config loader) and ``AlpacaPaperOlap.record_quote`` into
``quote_observations`` (deduplicated by (session, symbol, broker_time)).

Bounded by construction: ``--max-samples`` is REQUIRED (no unbounded
default), consecutive fetch failures abort the run with a typed status,
and every run opens/finishes its own lab session with counts in the
detail. This tool is NOT activated by merging — it runs only when the
operator invokes it (see the runbook note at the bottom).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.alpaca_paper_lab import (AlpacaPaperClient,  # noqa: E402
                                  AlpacaPaperLabConfig, AlpacaPaperOlap)

ACCOUNT_FINGERPRINT = "market_data_only"
PHASE = "quote_scheduler"


class SchedulerAborted(RuntimeError):
    """Typed abort: too many consecutive fetch failures."""


def run_scheduler(
    *,
    client,
    store,
    symbols: list[str],
    interval_seconds: float,
    max_samples: int,
    max_consecutive_failures: int,
    sleeper=time.sleep,
    log=print,
) -> dict:
    """Sample latest quotes for ``symbols`` every ``interval_seconds``
    until ``max_samples`` ticks, recording each quote. Returns run
    counters; raises SchedulerAborted after
    ``max_consecutive_failures`` consecutive fetch failures."""
    if max_samples < 1:
        raise ValueError("max_samples must be >= 1")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be > 0")
    if not symbols:
        raise ValueError("at least one symbol is required")
    session_id = store.start_session(
        PHASE, ACCOUNT_FINGERPRINT,
        {"symbols": symbols, "interval_seconds": interval_seconds,
         "max_samples": max_samples})
    counters = {"ticks": 0, "quotes_recorded": 0, "symbol_misses": 0,
                "fetch_failures": 0}
    consecutive = 0
    status = "completed"
    try:
        for tick in range(max_samples):
            started = time.monotonic()
            try:
                quotes = client.latest_crypto_quotes(symbols)
                consecutive = 0
            except Exception as exc:  # noqa: BLE001 — journal + bound
                counters["fetch_failures"] += 1
                consecutive += 1
                log(f"tick {tick}: fetch failure ({exc}); "
                    f"{consecutive}/{max_consecutive_failures} "
                    f"consecutive")
                if consecutive >= max_consecutive_failures:
                    status = "aborted_consecutive_failures"
                    raise SchedulerAborted(
                        f"{consecutive} consecutive fetch failures"
                    ) from exc
                quotes = {}
            recorded = []
            for symbol in symbols:
                quote = quotes.get(symbol)
                if not quote:
                    counters["symbol_misses"] += 1
                    continue
                store.record_quote(session_id, symbol, quote)
                counters["quotes_recorded"] += 1
                bid, ask = quote.get("bp"), quote.get("ap")
                if bid and ask:
                    mid = (float(bid) + float(ask)) / 2.0
                    recorded.append(
                        f"{symbol} "
                        f"{(float(ask) - float(bid)) / mid * 1e4:.2f}bp")
            counters["ticks"] += 1
            log(f"tick {tick}: {' '.join(recorded) or 'no quotes'}")
            if tick + 1 < max_samples:
                elapsed = time.monotonic() - started
                sleeper(max(0.0, interval_seconds - elapsed))
    finally:
        store.finish_session(session_id, status, counters)
    return {"session_id": session_id, "status": status, **counters}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path,
                        help="lts.alpaca.paper_lab_config.v1 file "
                             "(paper-only, read-only enforced)")
    parser.add_argument("--symbols", default="ETH/USD,BTC/USD")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--max-samples", type=int, required=True,
                        help="REQUIRED bound: number of sampling ticks "
                             "(e.g. 1440 = one day at 60s)")
    parser.add_argument("--max-consecutive-failures", type=int,
                        default=5)
    parser.add_argument("--once", action="store_true",
                        help="single tick (connectivity smoke)")
    args = parser.parse_args()

    config = AlpacaPaperLabConfig.load(args.config)
    key = os.environ.get(config.api_key_env)
    secret = os.environ.get(config.api_secret_env)
    if not key or not secret:
        raise SystemExit(f"REFUSED: credentials absent from env "
                         f"({config.api_key_env}/"
                         f"{config.api_secret_env})")
    client = AlpacaPaperClient(
        key, secret, timeout_seconds=config.timeout_seconds)
    store = AlpacaPaperOlap(config.database_path)
    result = run_scheduler(
        client=client, store=store,
        symbols=[s.strip() for s in args.symbols.split(",")
                 if s.strip()],
        interval_seconds=args.interval_seconds,
        max_samples=1 if args.once else args.max_samples,
        max_consecutive_failures=args.max_consecutive_failures)
    print(json.dumps(result, indent=1))
    return 0


# Runbook (NOT activated by merge):
#   PGDATA-free; writes only the configured paper-lab sqlite.
#   One-day ETH+BTC series at one observation per minute:
#     python tools/alpaca_quote_scheduler.py \
#       --config <paper_lab_config.json> --max-samples 1440
#   Connectivity smoke: add --once.
#   Long-term operation belongs behind the operator's scheduler
#   (systemd timer/cron) with an explicit --max-samples per invocation;
#   coordinate activation with the trading-front auditor first.

if __name__ == "__main__":
    raise SystemExit(main())
