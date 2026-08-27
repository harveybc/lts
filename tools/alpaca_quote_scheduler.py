#!/usr/bin/env python3
"""Scheduled Alpaca crypto quote collector v2 (WP-DATA, agent-multi
order @7886de39; DATA-SOTA-351 corrected).

Reuses the read-only paper-lab plumbing (paper endpoints enforced by
the config loader). Integrity contract:

* terminal status is HONEST: a session starts as
  ``failed_unexpected`` and becomes ``completed`` only after the final
  requested tick; operator interruption records ``interrupted``;
  consecutive-failure aborts record ``aborted_consecutive_failures``;
* storage is GLOBALLY idempotent on the canonical identity
  (venue, symbol, broker_time) via ``record_quote_canonical`` — a
  restarted run never duplicates observations, and the
  session-membership ledger preserves per-run provenance;
* every quote is VALIDATED before storage (finite positive bid/ask,
  ask >= bid, non-negative sizes, parseable broker timestamp); rejects
  are counted by typed reason, never stored;
* bounds validate: ``--max-samples`` is REQUIRED, and
  ``max_consecutive_failures`` must be >= 1.

This tool is NOT activated by merging — it runs only when the operator
invokes it (runbook at the bottom).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.alpaca_paper_lab import (AlpacaPaperClient,  # noqa: E402
                                  AlpacaPaperLabConfig, AlpacaPaperOlap)

ACCOUNT_FINGERPRINT = "market_data_only"
PHASE = "quote_scheduler"
VENUE = "alpaca"


class SchedulerAborted(RuntimeError):
    """Typed abort: too many consecutive fetch failures."""


def validate_quote(quote) -> str | None:
    """DATA-SOTA-351: strict quote schema. Returns a typed rejection
    reason, or None when the quote is storable."""
    if not isinstance(quote, dict):
        return "not_a_mapping"
    try:
        bid = float(quote.get("bp"))
        ask = float(quote.get("ap"))
    except (TypeError, ValueError):
        return "missing_or_non_numeric_bid_ask"
    if not (math.isfinite(bid) and math.isfinite(ask)):
        return "non_finite_bid_ask"
    if bid <= 0 or ask <= 0:
        return "non_positive_bid_ask"
    if ask < bid:
        return "crossed_quote"
    for side in ("bs", "as"):
        size = quote.get(side)
        if size is not None:
            try:
                size = float(size)
            except (TypeError, ValueError):
                return "non_numeric_size"
            if not math.isfinite(size) or size < 0:
                return "negative_or_non_finite_size"
    stamp = quote.get("t")
    if not stamp:
        return "missing_broker_timestamp"
    try:
        datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return "malformed_broker_timestamp"
    return None


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
    until ``max_samples`` ticks, recording each valid quote through the
    globally idempotent canonical store."""
    if max_samples < 1:
        raise ValueError("max_samples must be >= 1")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be > 0")
    if max_consecutive_failures < 1:
        raise ValueError("max_consecutive_failures must be >= 1")
    if not symbols:
        raise ValueError("at least one symbol is required")
    session_id = store.start_session(
        PHASE, ACCOUNT_FINGERPRINT,
        {"symbols": symbols, "interval_seconds": interval_seconds,
         "max_samples": max_samples})
    counters = {"ticks": 0, "quotes_recorded": 0,
                "canonical_new": 0, "canonical_duplicates": 0,
                "symbol_misses": 0, "fetch_failures": 0,
                "rejected_quotes": {}}
    consecutive = 0
    # DATA-SOTA-351: never presume success — the terminal status starts
    # as the worst case and is upgraded only by an honest outcome.
    status = "failed_unexpected"
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
                reason = validate_quote(quote)
                if reason is not None:
                    counters["rejected_quotes"][reason] = \
                        counters["rejected_quotes"].get(reason, 0) + 1
                    log(f"tick {tick}: {symbol} REJECTED ({reason})")
                    continue
                is_new = store.record_quote_canonical(
                    session_id, VENUE, symbol, quote)
                counters["quotes_recorded"] += 1
                counters["canonical_new" if is_new
                         else "canonical_duplicates"] += 1
                bid, ask = float(quote["bp"]), float(quote["ap"])
                mid = (bid + ask) / 2.0
                recorded.append(
                    f"{symbol} {(ask - bid) / mid * 1e4:.2f}bp")
            counters["ticks"] += 1
            log(f"tick {tick}: {' '.join(recorded) or 'no quotes'}")
            if tick + 1 < max_samples:
                elapsed = time.monotonic() - started
                sleeper(max(0.0, interval_seconds - elapsed))
        status = "completed"  # ONLY after every requested tick
    except KeyboardInterrupt:
        status = "interrupted"
        log("operator interruption: session recorded as interrupted")
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
    return 0 if result["status"] in ("completed", "interrupted") else 1


# Runbook (NOT activated by merge):
#   Writes only the configured paper-lab sqlite.
#   One-day ETH+BTC series at one observation per minute:
#     python tools/alpaca_quote_scheduler.py \
#       --config <paper_lab_config.json> --max-samples 1440
#   Connectivity smoke: add --once.
#   Restarts are safe: canonical (venue, symbol, broker_time) identity
#   makes replays idempotent; each run keeps its own session ledger.
#   Long-term operation belongs behind the operator's scheduler
#   (systemd timer/cron) with an explicit --max-samples per invocation;
#   coordinate activation with the trading-front auditor first.

if __name__ == "__main__":
    raise SystemExit(main())
