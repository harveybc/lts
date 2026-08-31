"""Collector activation preflight (order agent-multi@22218df1).

The owner authorizes activating the READ-ONLY MT5 session-evidence
collector at the first coordinated safe window. This tool evaluates
the six executable preconditions MECHANICALLY over evidence the
operator supplies and answers GO or COORDINATED_WINDOW_REQUIRED —
it performs no venue read itself, holds no credential, and cannot
send anything: it is a judge, not an actor.

Preconditions (all must hold):
1. fresh direct evidence shows ZERO MT5 positions and ZERO pending
   orders (a validated SnapshotPayload, bound identity, fresh);
2. current EA/bridge artifacts and configs are backed up and
   digest-bound (a backup manifest listing sha256 per artifact);
3. the updated EA differs ONLY by session-evidence publication —
   a reviewed diff statement naming the reviewer;
4. rollback is prepared and TESTED without order effects;
5. terminal connection, account identity, symbol and native
   protection checks pass (from the same fresh evidence);
6. no order/close/cancel API exists in the collector path —
   verified structurally against app/mt5_session_evidence.py.

If any position or order exists the answer is
COORDINATED_WINDOW_REQUIRED: keep monitoring, and never restart or
replace the protecting EA. This tool never weakens a precondition
to reach GO, and a GO from this tool still activates ONLY the
read-only collector — never weekly-flat trading logic.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_SNAPSHOT_AGE_SECONDS = 300.0


def evaluate(snapshot: dict, *, expected_account_fingerprint: str,
             backup_manifest: dict | None,
             ea_diff_review: dict | None,
             rollback_evidence: dict | None,
             now: datetime | None = None) -> dict:
    """Pure evaluation. Every failed precondition is NAMED; the
    verdict is GO only when every one holds."""
    from app.mt5_bridge_lab import SnapshotPayload
    failures = []
    payload = None
    try:
        payload = SnapshotPayload(**snapshot)
    except Exception as exc:
        failures.append(f"P1: snapshot does not validate against "
                        f"the strict bridge schema: {exc}")
    now = now or datetime.now(timezone.utc)
    if payload is not None:
        if payload.account_fingerprint != \
                expected_account_fingerprint:
            failures.append("P5: foreign account fingerprint")
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
                    "P5: an open position lacks native protection "
                    "— nothing may be touched")
    if not backup_manifest or not isinstance(
            backup_manifest.get("artifacts"), list) or not all(
            isinstance(a.get("sha256"), str) and
            len(a.get("sha256", "")) == 64
            for a in backup_manifest.get("artifacts", [])):
        failures.append(
            "P2: no digest-bound backup manifest for the current "
            "EA/bridge artifacts and configs")
    if not ea_diff_review or \
            ea_diff_review.get("differs_only_by") != \
            "session_evidence_publication" or \
            not ea_diff_review.get("reviewed_by"):
        failures.append(
            "P3: no reviewed statement that the updated EA differs "
            "only by session-evidence publication")
    if not rollback_evidence or \
            rollback_evidence.get("tested") is not True or \
            rollback_evidence.get("order_effects") != 0:
        failures.append(
            "P4: rollback is not prepared and tested without order "
            "effects")
    # P6: structural — the collector path has no trading surface
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
        "schema": "lts.collector_activation_preflight.v1",
        "verdict": verdict,
        "failures": failures,
        "scope": "a GO activates ONLY the read-only session "
                 "collector; weekly-flat trading logic stays "
                 "blocked regardless",
        "evaluated_at": now.isoformat(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path,
                        help="fresh SnapshotPayload JSON from the "
                             "bridge (operator-supplied)")
    parser.add_argument("--expected-account-fingerprint",
                        required=True)
    parser.add_argument("--backup-manifest", type=Path)
    parser.add_argument("--ea-diff-review", type=Path)
    parser.add_argument("--rollback-evidence", type=Path)
    args = parser.parse_args(argv)
    result = evaluate(
        json.loads(args.snapshot.read_text()),
        expected_account_fingerprint=(
            args.expected_account_fingerprint),
        backup_manifest=(json.loads(
            args.backup_manifest.read_text())
            if args.backup_manifest else None),
        ea_diff_review=(json.loads(args.ea_diff_review.read_text())
                        if args.ea_diff_review else None),
        rollback_evidence=(json.loads(
            args.rollback_evidence.read_text())
            if args.rollback_evidence else None))
    print(json.dumps(result, indent=1))
    return 0 if result["verdict"].startswith("GO") else 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
