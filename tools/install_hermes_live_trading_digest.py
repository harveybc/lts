#!/usr/bin/env python3
"""Install the bounded Hermes Paper/Shadow business-review cron job."""

from __future__ import annotations

import sys
from pathlib import Path


JOB_NAME = "lts-paper-shadow-business-review"
PROMPT = """
Act as a senior trading-operations analyst and data scientist. Analyze only the
sanitized evidence packet injected by the script. Write a concise Spanish
review for Telegram with four labeled parts: system health, business evidence,
anomalies, and at most three bounded offline experiment proposals. State units
and time horizons for every metric. Clearly label insufficient evidence. Do
not place or recommend a trade, access files, run commands, enqueue jobs,
change risk, or promote models. Every proposal requires human review.
""".strip()


def main() -> int:
    home = Path.home()
    hermes_repo = home / ".hermes/hermes-agent"
    context_script = (
        home / "Documents/GitHub/lts/tools/hermes_live_trading_context.py"
    )
    if not hermes_repo.is_dir():
        raise SystemExit("Hermes Agent is not installed")
    if not context_script.is_file():
        raise SystemExit(f"Context script not found: {context_script}")
    sys.path.insert(0, str(hermes_repo))
    from cron.jobs import create_job, load_jobs, update_job

    matches = [job for job in load_jobs() if job.get("name") == JOB_NAME]
    values = {
        "prompt": PROMPT,
        "script": str(context_script),
        "deliver": "telegram",
        "enabled_toolsets": ["todo"],
        "workdir": None,
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
    }
    if matches:
        job = update_job(matches[0]["id"], {**values, "schedule": "every 12h"})
        if job is None:
            raise SystemExit("Could not update the Hermes cron job")
        print(f"updated {job['id']} {job['schedule_display']}")
        return 0
    job = create_job(
        prompt=PROMPT,
        schedule="every 12h",
        name=JOB_NAME,
        deliver="telegram",
        script=str(context_script),
        enabled_toolsets=["todo"],
    )
    print(f"created {job['id']} {job['schedule_display']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
