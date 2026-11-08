"""Recoverable offline step for the forward pool.

The process writes only under --state-dir. It does not mint a capability,
open a broker connection, or start a service unit. A failure is a reason code
in FAILURE.json and a non-zero exit. PAPER_DEMO_EXECUTING stays false.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.forward_pool.cohort import PoolService
from app.forward_pool.core import PoolRefusal
from app.forward_pool.snapshot import HourlyBar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="offline forward-pool step")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--fixture", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        payload["columns"] = tuple(payload["columns"])
        payload["values"] = tuple(payload["values"])
        payload["published_utc_ns"] = tuple(payload["published_utc_ns"])
        bar = HourlyBar(**payload)
        service = PoolService(args.state_dir)
        try:
            result = service.step(bar)
        finally:
            service.close()
    except PoolRefusal as exc:
        sys.stdout.write(json.dumps({"ok": False, "code": exc.code, "paper_demo_executing": False}) + "\n")
        return 2
    public = {
        "ok": True,
        "replayed": result["replayed"],
        "submitted": result["submitted"],
        "paper_demo_executing": False,
        "shadow_observing": False,
        "evidence_class": "OFFLINE_FIXTURE_SNAPSHOT",
        "decisions": [
            {
                "command": item.get("command"),
                "intent": item.get("intent"),
                "outcome": item.get("outcome"),
                "refusal": item.get("refusal"),
                "model_hash": item.get("model_hash"),
            }
            for item in result["decisions"]
        ],
    }
    sys.stdout.write(json.dumps(public, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
