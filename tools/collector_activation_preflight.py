"""Collector activation preflight (orders agent-multi@22218df1 +
C15 hardening, owner-relayed 2026-08-31).

The owner authorizes activating the READ-ONLY MT5 session-evidence
collector at the first coordinated safe window. This tool evaluates
the preconditions MECHANICALLY and answers GO or
COORDINATED_WINDOW_REQUIRED — it performs no venue read itself,
holds no credential and cannot act: it is a judge, not an actor.

C15: a GO can no longer be obtained with invented JSON. Every
artifact behind a digest must EXIST and hash to it; manifests are
SEALED (self-digest verified); the review carries an identity AND a
digest binding the exact EA diff content; a fresh HeartbeatPayload
must bind account, server, connection and terminal build; the
expected symbol must be published by the terminal; and the rollback
evidence is digest-bound to its script and to the sealed backup
manifest. The frozen counterexample (bare dicts, digests hashing
nothing, no heartbeat) is a permanent refusal regression.

Preconditions (ALL must hold):
1. fresh strict SnapshotPayload: ZERO positions, ZERO pending
   orders, bound account, fresh, expected symbol published;
2. sealed backup manifest: the REQUIRED artifact set, non-empty
   names, canonical digests, every file present and hash-matching;
3. reviewed EA diff: reviewer identity, differs-only-by statement,
   and the diff file present and matching its declared digest;
4. rollback: tested, zero order effects, script present and
   hash-matching, bound to the backup manifest seal;
5. fresh strict HeartbeatPayload: connected, bound account AND
   server fingerprints, expected terminal build;
6. no order/close/cancel API in the collector path (structural).

Any open position or pending order answers
COORDINATED_WINDOW_REQUIRED — keep monitoring, never restart or
replace the protecting EA. A GO covers ONLY the read-only
collector; weekly-flat trading logic stays blocked regardless.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_SNAPSHOT_AGE_SECONDS = 300.0
MAX_HEARTBEAT_AGE_SECONDS = 120.0
REQUIRED_BACKUP_ARTIFACTS = ("ea_source", "ea_compiled",
                             "bridge_config")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical_digest(payload) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        default=str).encode()).hexdigest()


def _verify_sealed_manifest(manifest: dict, failures: list,
                            label: str) -> bool:
    """C15: manifests are SEALED — the self-digest covers the body
    and must verify, so a hand-typed manifest without its seal, or
    an edited one, refuses."""
    if not isinstance(manifest, dict) or "seal_sha256" not in \
            manifest:
        failures.append(f"{label}: not a SEALED manifest "
                        "(seal_sha256 missing)")
        return False
    body = {k: v for k, v in manifest.items() if k != "seal_sha256"}
    if manifest["seal_sha256"] != _canonical_digest(body):
        failures.append(f"{label}: seal digest does not match the "
                        "manifest body — edited or invented")
        return False
    return True


def _verify_artifact(entry, root: Path, failures: list,
                     label: str) -> None:
    name = entry.get("name") if isinstance(entry, dict) else None
    declared = entry.get("sha256") if isinstance(entry, dict) \
        else None
    rel = entry.get("path") if isinstance(entry, dict) else None
    if not name or not isinstance(name, str) or not name.strip():
        failures.append(f"{label}: artifact with an empty name")
        return
    if not isinstance(declared, str) or not _HEX64.match(declared):
        failures.append(f"{label}:{name}: digest is not canonical "
                        "lowercase 64-hex")
        return
    if not rel or not isinstance(rel, str):
        failures.append(f"{label}:{name}: no artifact path — a "
                        "digest that hashes NOTHING binds nothing")
        return
    path = (root / rel).resolve()
    if not path.is_file():
        failures.append(f"{label}:{name}: artifact file missing — "
                        "the digest hashes nothing")
        return
    actual = _sha256_file(path)
    if actual != declared:
        failures.append(f"{label}:{name}: file hashes "
                        f"{actual[:12]}… but the manifest declares "
                        f"{declared[:12]}… — invented or stale")


def evaluate(snapshot: dict, *, expected_account_fingerprint: str,
             expected_server_fingerprint: str,
             expected_symbol: str,
             expected_terminal_build: int,
             heartbeat: dict | None,
             backup_manifest: dict | None,
             backup_root: Path | str | None,
             ea_diff_review: dict | None,
             rollback_evidence: dict | None,
             now: datetime | None = None) -> dict:
    """Pure evaluation. Every failed precondition is NAMED; the
    verdict is GO only when every one holds against REAL artifacts."""
    from app.mt5_bridge_lab import HeartbeatPayload, SnapshotPayload
    failures: list = []
    now = now or datetime.now(timezone.utc)
    root = Path(backup_root) if backup_root else None

    # -- P1: fresh zero/zero snapshot, bound, symbol published -----
    payload = None
    try:
        payload = SnapshotPayload(**snapshot)
    except Exception as exc:
        failures.append(f"P1: snapshot does not validate against "
                        f"the strict bridge schema: {exc}")
    if payload is not None:
        if payload.account_fingerprint != \
                expected_account_fingerprint:
            failures.append("P1: foreign account fingerprint in "
                            "the snapshot")
        age = (now - payload.observed_at).total_seconds()
        if age < 0 or age > MAX_SNAPSHOT_AGE_SECONDS:
            failures.append(
                f"P1: snapshot is not fresh ({age:.0f}s old, "
                f"{MAX_SNAPSHOT_AGE_SECONDS:.0f}s allowed)")
        if payload.positions:
            failures.append(
                f"P1: {len(payload.positions)} open position(s) — "
                "COORDINATED_WINDOW_REQUIRED, keep monitoring, and "
                "never restart or replace the protecting EA")
        if payload.orders:
            failures.append(
                f"P1: {len(payload.orders)} pending order(s)")
        for position in payload.positions:
            if position.stop_loss <= 0 or position.take_profit <= 0:
                failures.append(
                    "P1: an open position lacks native protection "
                    "— nothing may be touched")
        published = {s.symbol for s in payload.symbols}
        if expected_symbol not in published:
            failures.append(
                f"P1: the terminal snapshot does not publish "
                f"{expected_symbol!r} — symbol identity unverified")

    # -- P5: fresh bound heartbeat, terminal identity --------------
    beat = None
    if heartbeat is None:
        failures.append("P5: no heartbeat — terminal, account and "
                        "build identity unverified")
    else:
        try:
            beat = HeartbeatPayload(**heartbeat)
        except Exception as exc:
            failures.append(f"P5: heartbeat does not validate "
                            f"against the strict schema: {exc}")
    if beat is not None:
        if beat.account_fingerprint != expected_account_fingerprint:
            failures.append("P5: foreign account fingerprint in "
                            "the heartbeat")
        if beat.server_fingerprint != expected_server_fingerprint:
            failures.append("P5: foreign server fingerprint in "
                            "the heartbeat")
        if not beat.connected:
            failures.append("P5: the terminal is not connected")
        if beat.terminal_build != int(expected_terminal_build):
            failures.append(
                f"P5: terminal build {beat.terminal_build} is not "
                f"the expected {expected_terminal_build}")
        beat_age = (now - beat.observed_at).total_seconds()
        if beat_age < 0 or beat_age > MAX_HEARTBEAT_AGE_SECONDS:
            failures.append(
                f"P5: heartbeat is not fresh ({beat_age:.0f}s old, "
                f"{MAX_HEARTBEAT_AGE_SECONDS:.0f}s allowed)")

    # -- P2: sealed backup manifest over REAL artifacts ------------
    manifest_seal = None
    if not backup_manifest:
        failures.append("P2: no backup manifest")
    elif _verify_sealed_manifest(backup_manifest, failures,
                                 "P2 backup manifest"):
        manifest_seal = backup_manifest["seal_sha256"]
        artifacts = backup_manifest.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            failures.append("P2: the backup manifest lists no "
                            "artifacts")
        else:
            if root is None:
                failures.append("P2: no backup root to resolve "
                                "artifacts against")
            else:
                names = set()
                for entry in artifacts:
                    _verify_artifact(entry, root, failures,
                                     "P2 backup")
                    if isinstance(entry, dict) and \
                            entry.get("name"):
                        names.add(entry["name"])
                missing = sorted(set(REQUIRED_BACKUP_ARTIFACTS)
                                 - names)
                if missing:
                    failures.append(
                        f"P2: required artifacts missing from the "
                        f"backup manifest: {missing}")

    # -- P3: reviewed EA diff, identity + content digest -----------
    if not ea_diff_review:
        failures.append("P3: no EA diff review")
    else:
        if ea_diff_review.get("differs_only_by") != \
                "session_evidence_publication":
            failures.append("P3: the review does not state the EA "
                            "differs only by session-evidence "
                            "publication")
        reviewer = ea_diff_review.get("reviewed_by")
        if not isinstance(reviewer, dict) or \
                not reviewer.get("identity") or \
                not reviewer.get("review_reference"):
            failures.append(
                "P3: reviewer identity must carry both an identity "
                "and a review reference — a bare self-declared "
                "name binds nothing")
        declared = ea_diff_review.get("diff_sha256")
        rel = ea_diff_review.get("diff_path")
        if not isinstance(declared, str) or \
                not _HEX64.match(declared or ""):
            failures.append("P3: diff digest is not canonical "
                            "64-hex")
        elif root is None or not rel or \
                not (Path(root) / rel).is_file():
            failures.append("P3: the reviewed diff file is missing "
                            "— the digest hashes nothing")
        elif _sha256_file(Path(root) / rel) != declared:
            failures.append("P3: the diff file does not hash to "
                            "the reviewed digest")

    # -- P4: rollback digest-bound and tested ----------------------
    if not rollback_evidence:
        failures.append("P4: no rollback evidence")
    else:
        if rollback_evidence.get("tested") is not True or \
                rollback_evidence.get("order_effects") != 0:
            failures.append("P4: rollback is not tested with zero "
                            "order effects")
        declared = rollback_evidence.get("script_sha256")
        rel = rollback_evidence.get("script_path")
        if not isinstance(declared, str) or \
                not _HEX64.match(declared or ""):
            failures.append("P4: rollback script digest is not "
                            "canonical 64-hex")
        elif root is None or not rel or \
                not (Path(root) / rel).is_file():
            failures.append("P4: the rollback script is missing — "
                            "the digest hashes nothing")
        elif _sha256_file(Path(root) / rel) != declared:
            failures.append("P4: the rollback script does not hash "
                            "to its declared digest")
        bound = rollback_evidence.get("backup_manifest_sha256")
        if manifest_seal is None or bound != manifest_seal:
            failures.append(
                "P4: rollback evidence is not bound to the sealed "
                "backup manifest — it could roll back to anything")

    # -- P6: structural — the collector path cannot act ------------
    import app.mt5_session_evidence as collector
    source = inspect.getsource(collector)
    for forbidden in ("OrderSend", "PositionClose", "TradeReq",
                      "requests.", "urllib", "socket",
                      "subprocess", "connect("):
        if forbidden in source:
            failures.append(
                f"P6: forbidden surface {forbidden!r} in the "
                "collector path")

    verdict = "GO_READ_ONLY_COLLECTOR_ONLY" if not failures else \
        "COORDINATED_WINDOW_REQUIRED"
    return {
        "schema": "lts.collector_activation_preflight.v2",
        "verdict": verdict,
        "failures": failures,
        "scope": "a GO activates ONLY the read-only session "
                 "collector; weekly-flat trading logic stays "
                 "blocked regardless",
        "evaluated_at": now.isoformat(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--heartbeat", required=True, type=Path)
    parser.add_argument("--expected-account-fingerprint",
                        required=True)
    parser.add_argument("--expected-server-fingerprint",
                        required=True)
    parser.add_argument("--expected-symbol", required=True)
    parser.add_argument("--expected-terminal-build", required=True,
                        type=int)
    parser.add_argument("--backup-manifest", required=True,
                        type=Path)
    parser.add_argument("--backup-root", required=True, type=Path)
    parser.add_argument("--ea-diff-review", required=True,
                        type=Path)
    parser.add_argument("--rollback-evidence", required=True,
                        type=Path)
    args = parser.parse_args(argv)
    result = evaluate(
        json.loads(args.snapshot.read_text()),
        expected_account_fingerprint=(
            args.expected_account_fingerprint),
        expected_server_fingerprint=(
            args.expected_server_fingerprint),
        expected_symbol=args.expected_symbol,
        expected_terminal_build=args.expected_terminal_build,
        heartbeat=json.loads(args.heartbeat.read_text()),
        backup_manifest=json.loads(
            args.backup_manifest.read_text()),
        backup_root=args.backup_root,
        ea_diff_review=json.loads(args.ea_diff_review.read_text()),
        rollback_evidence=json.loads(
            args.rollback_evidence.read_text()))
    print(json.dumps(result, indent=1))
    return 0 if result["verdict"].startswith("GO") else 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
