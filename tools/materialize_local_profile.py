"""AUD-SEC-20260823-305: render tracked placeholder templates into
usable EFFECTIVE profiles under ~/.config/lts — operational
fingerprints never live in the public tree.

Placeholders (exact tokens) come from the environment:
- <ACCOUNT_FINGERPRINT_24HEX>  <- LTS_MT5_ACCOUNT_FINGERPRINT

The tool refuses unreplaced placeholders, malformed values, and
writing anywhere outside ~/.config/lts. It prints the destination
path and sha256 — never the substituted values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

_FP_RE = re.compile(r"^[0-9a-f]{24}$")
PLACEHOLDERS = {
    "<ACCOUNT_FINGERPRINT_24HEX>": ("LTS_MT5_ACCOUNT_FINGERPRINT",
                                    _FP_RE),
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     allow_abbrev=False)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--name", required=True,
                        help="output filename under ~/.config/lts")
    args = parser.parse_args(argv)
    if "/" in args.name or args.name.startswith("."):
        print("REFUSED: output name must be a bare filename")
        return 2
    text = args.template.read_text()
    for token, (env, pattern) in PLACEHOLDERS.items():
        if token in text:
            value = os.environ.get(env, "").strip().lower()
            if not pattern.fullmatch(value):
                print(f"REFUSED: {env} missing or malformed for "
                      f"placeholder {token}")
                return 2
            text = text.replace(token, value)
    leftovers = re.findall(r"<[A-Z0-9_]+>", text)
    if leftovers:
        print(f"REFUSED: unreplaced placeholders {sorted(set(leftovers))}")
        return 2
    json.loads(text)  # must stay valid JSON
    dest_dir = Path.home() / ".config" / "lts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / args.name
    dest.write_text(text)
    os.chmod(dest, 0o600)
    print(json.dumps({
        "written": str(dest),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
