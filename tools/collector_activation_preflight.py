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
   server fingerprints, expected terminal build
   (trade_allowed is DELIBERATELY not required: the collector is
   read-only and runs under least privilege);
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


class ArtifactPathError(ValueError):
    """C16: a path outside the canonical root, a symlink, an
    irregular file, a foreign owner or a loose mode refuses."""


def _contained_path(root: Path, rel: str) -> Path:
    """C16: only NORMALIZED relative paths contained under a
    canonical, non-symlink root are acceptable — absolute paths,
    parent escapes and denormalized forms refuse before any open."""
    import os as _os
    if not isinstance(rel, str) or not rel:
        raise ArtifactPathError("empty artifact path")
    if _os.path.isabs(rel):
        raise ArtifactPathError(f"absolute path refused: {rel!r}")
    normalized = _os.path.normpath(rel)
    if normalized != rel or normalized.startswith("..") or \
            ".." in Path(normalized).parts:
        raise ArtifactPathError(
            f"path {rel!r} is not a normalized contained relative "
            "path")
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ArtifactPathError(
            "the backup root must be a real, non-symlink directory")
    resolved_root = root.resolve(strict=True)
    candidate = (root / normalized)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise ArtifactPathError(
            f"artifact {rel!r} does not exist under the root")
    if resolved_root not in resolved.parents and \
            resolved != resolved_root:
        raise ArtifactPathError(
            f"artifact {rel!r} escapes the canonical root")
    return candidate


def _read_descriptor_first(root: Path, rel: str) -> tuple:
    """C16/C17: open the artifact descriptor-first with O_NOFOLLOW,
    verify REGULAR / owner-uid / not-group-other-writable from the
    fstat of that very descriptor, and read the FULL bytes FROM that
    descriptor. Returns (bytes, sha256_hex) from ONE verified
    descriptor — the caller parses the RETURNED BYTES and never
    reopens the path, so no substitution between verify and consume
    is possible (C17: the acta TOCTOU seam is closed)."""
    import errno as _errno
    import os as _os
    import stat as _stat
    path = _contained_path(root, rel)
    try:
        fd = _os.open(path, _os.O_RDONLY | _os.O_NOFOLLOW |
                      _os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno in (_errno.ELOOP, _errno.EMLINK):
            raise ArtifactPathError(
                f"{rel!r}: symlink refused — descriptor-bound open "
                "does not follow links") from exc
        raise ArtifactPathError(
            f"{rel!r}: cannot open ({exc})") from exc
    try:
        st = _os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise ArtifactPathError(
                f"{rel!r}: not a regular file")
        if st.st_uid != _os.getuid():
            raise ArtifactPathError(
                f"{rel!r}: foreign owner uid {st.st_uid} refused")
        if _stat.S_IMODE(st.st_mode) & 0o022:
            raise ArtifactPathError(
                f"{rel!r}: group/other-writable mode "
                f"{oct(_stat.S_IMODE(st.st_mode))} refused")
        chunks = []
        while True:
            chunk = _os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        return data, hashlib.sha256(data).hexdigest()
    finally:
        _os.close(fd)


def _sha256_descriptor_first(root: Path, rel: str) -> str:
    """Digest-only wrapper over the single-descriptor reader, for
    artifacts whose CONTENT the judge does not parse (backup files,
    the diff, the rollback script). Structured artifacts the judge
    parses use _read_descriptor_first and consume the returned
    bytes."""
    return _read_descriptor_first(root, rel)[1]


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
    try:
        actual = _sha256_descriptor_first(root, rel)
    except ArtifactPathError as exc:
        failures.append(f"{label}:{name}: {exc}")
        return
    if actual != declared:
        failures.append(f"{label}:{name}: file hashes "
                        f"{actual[:12]}… but the manifest declares "
                        f"{declared[:12]}… — invented or stale")


# Owner ratification 2026-09-04 (agent-multi@bb105fa6, record sha
# 399483a14ab4821a49155afd72d153e870e2f9c051945875ca7fdfb5a5726186):
# terminal build 6140 ACCEPTED as the current expected build for
# collector preflight and operator-kit validation, superseding the
# stale 6090 in this scope only. The judge makes the ratification
# EXECUTABLE: an expected-build argument that differs from the
# ratified value refuses, so neither the old 6090 nor an arbitrary
# build can be smuggled in through the kit. A different observed
# build requires a NEW owner disposition.
OWNER_RATIFIED_TERMINAL_BUILD = 6140


def evaluate(snapshot: dict, *, expected_account_fingerprint: str,
             expected_server_fingerprint: str,
             expected_symbol: str,
             expected_terminal_build: int,
             expected_reviewer_identity: str,
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
        # C16 DECISION, explicit: trade_allowed is NOT a collector
        # requirement. The collector is read-only and must operate
        # under LEAST privilege — requiring trading permission for
        # a component that must never trade would demand more
        # authority than the task needs, so the judge deliberately
        # ignores the field and the precondition text makes no such
        # claim. (The heartbeat schema still carries it for other
        # consumers.)
        if int(expected_terminal_build) != \
                OWNER_RATIFIED_TERMINAL_BUILD:
            failures.append(
                f"P5: expected build {expected_terminal_build} is "
                f"not the owner-ratified "
                f"{OWNER_RATIFIED_TERMINAL_BUILD} (ratification "
                "2026-09-04; a different build needs a new owner "
                "disposition)")
        if beat.terminal_build != OWNER_RATIFIED_TERMINAL_BUILD:
            failures.append(
                f"P5: terminal build {beat.terminal_build} is not "
                f"the owner-ratified "
                f"{OWNER_RATIFIED_TERMINAL_BUILD}")
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

    # -- P3 (C16): the review is a REAL SEALED ACTA artifact -------
    # A textual reference must not authorize: the acta is a file
    # whose digest the caller declares, whose seal verifies, whose
    # reviewer identity must equal the identity FIXED BY THE ORDER,
    # and which itself names the exact diff digest.
    if not ea_diff_review:
        failures.append("P3: no EA diff review")
    else:
        acta_rel = ea_diff_review.get("acta_path")
        acta_declared = ea_diff_review.get("acta_sha256")
        acta = None
        if not isinstance(acta_declared, str) or \
                not _HEX64.match(acta_declared or ""):
            failures.append("P3: acta digest is not canonical "
                            "64-hex")
        elif root is None or not acta_rel:
            failures.append("P3: no acta artifact — a textual "
                            "reference does not authorize")
        else:
            try:
                acta_bytes, actual = _read_descriptor_first(
                    root, acta_rel)
            except ArtifactPathError as exc:
                failures.append(f"P3: acta: {exc}")
                acta_bytes = actual = None
            if actual is not None and actual != acta_declared:
                failures.append("P3: the acta file does not hash "
                                "to its declared digest")
            elif actual is not None:
                # C17: parse the VERIFIED BYTES, never reopen the
                # path — the verified stream is the consumed stream
                try:
                    acta = json.loads(acta_bytes.decode("utf-8"))
                except Exception as exc:
                    failures.append(
                        f"P3: verified acta bytes are malformed: "
                        f"{exc}")
        if acta is not None:
            if not _verify_sealed_manifest(acta, failures,
                                           "P3 acta"):
                acta = None
        if acta is not None:
            if acta.get("differs_only_by") != \
                    "session_evidence_publication":
                failures.append(
                    "P3: the acta does not state the EA differs "
                    "only by session-evidence publication")
            if acta.get("reviewer_identity") != \
                    expected_reviewer_identity:
                failures.append(
                    "P3: the acta's reviewer identity "
                    f"{acta.get('reviewer_identity')!r} is not the "
                    "identity fixed by the order")
            diff_declared = acta.get("diff_sha256")
            diff_rel = acta.get("diff_path")
            if not isinstance(diff_declared, str) or \
                    not _HEX64.match(diff_declared or ""):
                failures.append("P3: the acta's diff digest is not "
                                "canonical 64-hex")
            else:
                try:
                    diff_actual = _sha256_descriptor_first(
                        root, diff_rel)
                except ArtifactPathError as exc:
                    failures.append(f"P3: diff: {exc}")
                    diff_actual = None
                if diff_actual is not None and \
                        diff_actual != diff_declared:
                    failures.append(
                        "P3: the diff file does not hash to the "
                        "EXACT digest the sealed acta names")

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
        elif root is None or not rel:
            failures.append("P4: the rollback script is missing — "
                            "the digest hashes nothing")
        else:
            try:
                actual = _sha256_descriptor_first(root, rel)
                if actual != declared:
                    failures.append(
                        "P4: the rollback script does not hash to "
                        "its declared digest")
            except ArtifactPathError as exc:
                failures.append(f"P4: rollback script: {exc}")
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
    parser.add_argument("--expected-reviewer-identity",
                        required=True)
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
        expected_reviewer_identity=(
            args.expected_reviewer_identity),
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
