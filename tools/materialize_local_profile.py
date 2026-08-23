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
    # AUD-SEC-20260823-314: no-follow, atomic, fail-closed install.
    # A pre-existing symlink (or any non-regular destination) refuses;
    # the payload is written to an O_EXCL|O_NOFOLLOW 0600 temporary in
    # the SAME 0700 directory, fsynced, then atomically renamed over a
    # verified-regular destination; the directory is fsynced before
    # success is acknowledged. Substituted values are never printed.
    dest_dir = Path.home() / ".config" / "lts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    if dest_dir.is_symlink():
        print("REFUSED: ~/.config/lts is a symlink")
        return 2
    os.chmod(dest_dir, 0o700)
    dest = dest_dir / args.name
    if dest.is_symlink() or (dest.exists() and not dest.is_file()):
        print("REFUSED: destination exists and is not a regular "
              "local file (symlink or special); refusing to follow "
              "or replace it")
        return 2
    tmp = dest_dir / (args.name + ".tmp")
    try:
        fd = os.open(str(tmp),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY
                     | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        print("REFUSED: temporary file already exists (concurrent "
              "materialization or stale race artifact); remove it "
              "deliberately and retry")
        return 2
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # re-verify the destination immediately before the atomic
        # replace (rename replaces a symlink ENTRY, never follows it,
        # but a race to a special file still refuses)
        if dest.is_symlink() or (dest.exists() and not dest.is_file()):
            print("REFUSED: destination changed during write")
            return 2
        os.replace(tmp, dest)
        dfd = os.open(str(dest_dir), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if tmp.exists():
            tmp.unlink()
    print(json.dumps({
        "written": str(dest),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
