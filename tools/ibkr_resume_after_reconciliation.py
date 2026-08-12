#!/usr/bin/env python3
"""Owner CLI: execute the bounded resume_after_reconciliation transition.

Musashi addendum 2026-08-04 §9, corrected per finding 227. Flow (all
fail-closed):

1. the passphrase-protected owner SIGNATURE over the capability bytes is
   the human-authentication boundary (findings 094/227); the interactive
   terminal check below is ergonomic confirmation only — a TTY never
   authenticates anyone;
2. loads the runner config, its L1 profile and the durable execution
   ledger; Paper-only bindings are enforced by the profile and the
   capability schema;
3. selects the owner's capability: preferably the file the owner names
   with ``--capability PATH`` (must be inside the protected store,
   signed, current, profile-bound, unconsumed); otherwise the single
   VALID entry after typed classification of every store file —
   unsigned/malformed/expired/consumed side files are logged and
   ignored, never counted, and can never deny a valid signed capability;
   two valid signed current capabilities still refuse (ambiguity);
4. gathers FRESH direct broker evidence over a read-only TWS session on a
   dedicated client id (positions, open orders, connected account
   fingerprint) immediately before the transition;
5. queries the fleet incident ledger; any active P0/P1 for this venue —
   or an unanswerable ledger — refuses;
6. runs the atomic core (burn + evidence + halt->none in one BEGIN
   IMMEDIATE unit);
7. reports the transition into the incident ledger history and prints a
   sanitized result (hashes and fingerprints only).

``--archive-invalid`` is a SEPARATE explicit operation: it moves typed
expired/consumed files into ``<store>/archive/`` and exits without
resuming. The default flow moves nothing and never deletes evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ibkr_l1_adapter import L1AuthorizationError  # noqa: E402
from app.ibkr_l1_journal import L1ExecutionOlap  # noqa: E402
from app.ibkr_model_authority import ContinuousPaperProfile  # noqa: E402
from app.ibkr_l1_resume import (  # noqa: E402
    ResumeGate,
    archive_invalid_capabilities,
    resume_after_reconciliation,
    select_resume_capability,
)

DEFAULT_INCIDENT_REPO = Path.home() / "Documents/GitHub/agent-multi"
EVIDENCE_CLIENT_ID_OFFSET = 91
VENUE_EVENT_CODES = {"tws_unavailable", "decision_clock_stale"}


def gather_broker_evidence(profile: ContinuousPaperProfile) -> dict:
    """Read-only TWS session: positions, open orders, account identity."""
    import ib_async
    from ib_async import IB

    ib = IB()
    ib.connect(
        profile.host, profile.port,
        clientId=profile.client_id + EVIDENCE_CLIENT_ID_OFFSET,
        timeout=20.0, readonly=True, raiseSyncErrors=True,
    )
    try:
        accounts = ib.managedAccounts()
        if len(accounts) != 1:
            raise L1AuthorizationError(
                f"expected exactly one managed account, saw {len(accounts)}")
        fingerprint = hashlib.sha256(
            accounts[0].encode()).hexdigest()[:16]
        positions = [
            {"conId": p.contract.conId, "position": p.position}
            for p in ib.positions()
        ]
        open_orders = [
            {"orderId": t.order.orderId, "status": t.orderStatus.status}
            for t in ib.reqAllOpenOrders()
        ]
        return {
            "source": "direct_tws",
            "gathered_at": datetime.now(timezone.utc).isoformat(),
            "account_fingerprint": fingerprint,
            "positions": positions,
            "open_orders": open_orders,
            "server_version": ib.client.serverVersion(),
            "ib_async_version": ib_async.__version__,
        }
    finally:
        ib.disconnect()


def active_venue_incidents(repo: Path, venue_object: str):
    """Return active P0/P1 incidents for this venue, or None when the
    ledger cannot be queried (None makes the core refuse)."""
    script = repo / "tools" / "incident_ledger.py"
    try:
        result = subprocess.run(
            [sys.executable, str(script), "status", "--active",
             "--severity", "P0,P1", "--json"],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        rows = json.loads(result.stdout or "[]")
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    return [
        row for row in rows
        if row.get("affected_object") == venue_object
        or row.get("event_code") in VENUE_EVENT_CODES
    ]


def report_transition(repo: Path, result: dict, machine: str) -> None:
    script = repo / "tools" / "incident_ledger.py"
    identity = ["--source", "ibkr_resume_cli", "--front", "front2",
                "--machine", machine, "--event-code", "ibkr_hold_cleared",
                "--object", "ibkr_paper"]
    payload = {
        "summary": "owner cleared the reconciliation hold",
        "effect_id": result["effect_id"],
        "evidence_sha256": result["evidence_sha256"],
    }
    subprocess.run(
        [sys.executable, str(script), "observe", *identity,
         "--severity", "P3",
         "--evidence-at", datetime.now(timezone.utc).isoformat(),
         "--payload-json", json.dumps(payload, sort_keys=True)],
        capture_output=True, text=True, timeout=30)
    subprocess.run(
        [sys.executable, str(script), "recover", *identity,
         "--state-json", json.dumps(
             {"summary": "transition committed",
              "evidence_sha256": result["evidence_sha256"]},
             sort_keys=True)],
        capture_output=True, text=True, timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True,
                        help="IBKR model-runner config JSON")
    parser.add_argument("--capability", type=Path, default=None,
                        help="explicit capability file selected by the"
                             " owner; must live inside the protected store"
                             " and be signed, current, profile-bound and"
                             " unconsumed (finding 227)")
    parser.add_argument("--archive-invalid", action="store_true",
                        help="separate archival operation: move typed"
                             " expired/consumed capability files (with"
                             " their .sig) into <store>/archive, then exit"
                             " without resuming; never deletes, never"
                             " touches valid/unsigned/malformed files")
    parser.add_argument("--incident-repo", type=Path,
                        default=DEFAULT_INCIDENT_REPO)
    parser.add_argument("--machine", default=os.uname().nodename)
    args = parser.parse_args()

    # Ergonomic confirmation ONLY — never authentication (finding 227).
    # The human-authority boundary is the owner's passphrase-protected
    # Ed25519 signature, verified during store classification below.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("REFUSED: resume requires an interactive owner terminal"
              " (ergonomic confirmation; the owner signature is the actual"
              " authentication).",
              file=sys.stderr)
        return 2

    config = json.loads(Path(os.path.expanduser(args.config)).read_text())
    if config.get("schema") != "lts.ibkr.model_runner.v1":
        print("REFUSED: runner config schema mismatch", file=sys.stderr)
        return 2
    profile = ContinuousPaperProfile.load(
        os.path.expanduser(config["profile_file"]))
    ledger_path = Path(os.path.expanduser(
        config["service"]["database_path"]))
    olap = L1ExecutionOlap(ledger_path)

    try:
        gate = ResumeGate()
        if args.archive_invalid:
            # Separate explicit archival operation (finding 227 §4): only
            # typed expired/consumed files move; nothing is deleted; the
            # valid capability and any unsigned/malformed evidence stay.
            report = archive_invalid_capabilities(
                gate.store_dir, profile=profile, olap=olap)
            print(json.dumps(report, sort_keys=True))
            return 0
        # Finding 094/227: the owner's detached Ed25519 signature over the
        # exact capability bytes, verified against the root-pinned
        # allowed-signers file, is the human-authentication boundary.
        # Classification types every store file BEFORE any ambiguity
        # check, so unsigned/malformed/expired/consumed side files are
        # logged and ignored — they can never deny the owner's valid
        # signed capability. Two valid signed capabilities still refuse.
        chosen, ignored = select_resume_capability(
            gate.store_dir, profile=profile, olap=olap,
            explicit_path=args.capability)
        for entry in ignored:
            print(f"IGNORED ({entry.kind}) {entry.path.name}:"
                  f" {entry.detail}", file=sys.stderr)
        payload, record = chosen.payload, chosen.record
        evidence = gather_broker_evidence(profile)
        result = resume_after_reconciliation(
            olap=olap, profile=profile, payload=payload, record=record,
            broker_evidence=evidence,
            # Finding 093: re-queried inside the serialized transaction.
            incident_probe=lambda: active_venue_incidents(
                args.incident_repo, "ibkr_paper"),
        )
    except L1AuthorizationError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    finally:
        olap.close()

    if result["applied"]:
        report_transition(args.incident_repo, result, args.machine)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
