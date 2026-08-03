"""Atomic, filesystem-local heartbeat for continuous model runners."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def write_runner_heartbeat(
    path: str | Path, *, schema: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    destination = Path(os.path.expandvars(str(path))).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema": schema,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        **dict(payload),
    }
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(body, sort_keys=True, indent=1, default=str), encoding="utf-8"
    )
    temporary.replace(destination)
    return body
