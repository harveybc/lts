#!/usr/bin/env python3
"""Store Capital.com Demo credentials outside Git and shell history."""

from __future__ import annotations

import getpass
import os
from pathlib import Path


def main() -> int:
    identifier = input("Capital.com Demo identifier/email: ").strip()
    api_key = getpass.getpass("Capital.com API key (hidden): ").strip()
    password = getpass.getpass(
        "Capital.com custom API-key password (hidden): "
    ).strip()
    if not all((identifier, api_key, password)):
        raise SystemExit("All three values are required")
    target = Path.home() / ".config/lts/capital-demo.env"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            (
                f"CAPITAL_DEMO_IDENTIFIER={identifier}",
                f"CAPITAL_DEMO_API_KEY={api_key}",
                f"CAPITAL_DEMO_PASSWORD={password}",
                "",
            )
        ),
        encoding="utf-8",
    )
    os.chmod(target, 0o600)
    print(f"Credential file stored at {target} with mode 0600")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
