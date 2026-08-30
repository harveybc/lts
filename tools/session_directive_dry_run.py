"""WP3.5 — the weekly session directive DRY RUN.

Produces the command that WOULD have been sent to a venue, and the
reason, from sanitized captured payloads. It cannot send anything, and
that is a structural property rather than a promise:

* the only input is a directory of capture files on disk;
* no credential is read, accepted as an argument, or looked up in the
  environment;
* the only venue-facing object this tool constructs is
  :class:`NoWriteVenueInterface`, which has no client, no session, no
  host, and whose every mutating method name raises;
* nothing here imports ``requests``, ``socket``, or any venue client
  module.

``python -m tools.session_directive_dry_run --captures DIR`` prints one
JSON document: the directive, its four separate action records, the
effects it would have authorised, and the provenance of every fact it
rested on.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.session_authority_adapter import (  # noqa: E402
    AdapterRefusal, AuthorityUnavailable, derive_directive,
    load_authority)
from app.session_watchdog import classify  # noqa: E402
from app.venue_direct_evidence import (  # noqa: E402
    VenueDirectEvidence, VenueEvidencePolicy, VenueEvidenceError,
    require_utc)

CAPTURE_FILES = {
    "account_session": "account_session.json",
    "positions": "positions.json",
    "open_orders": "open_orders.json",
    "market_clock": "market_clock.json",
}


class WriteAttempted(RuntimeError):
    """A dry run tried to mutate a venue — always a defect."""


class NoWriteVenueInterface:
    """The only venue-facing object this tool has.

    It holds no client and no credential, and every mutating name
    raises. A dry run that could write would not be a dry run, so the
    inability is expressed in the type rather than in a comment."""

    __slots__ = ()

    credentials = None
    client = None
    base_url = None

    def _refuse(self, what: str):
        raise WriteAttempted(
            f"{what} was attempted from the dry run; this interface "
            "has no client and no credential and can never mutate a "
            "venue")

    def submit_order(self, *args, **kwargs):
        self._refuse("submit_order")

    def cancel_order(self, *args, **kwargs):
        self._refuse("cancel_order")

    def close_position(self, *args, **kwargs):
        self._refuse("close_position")

    def replace_order(self, *args, **kwargs):
        self._refuse("replace_order")

    def enqueue(self, *args, **kwargs):
        self._refuse("enqueue")

    def order_send(self, *args, **kwargs):
        self._refuse("order_send")


def load_capture(directory: Path, name: str) -> dict:
    path = directory / CAPTURE_FILES[name]
    if not path.is_file():
        raise VenueEvidenceError(f"capture {path} is missing")
    envelope = json.loads(path.read_text())
    for field in ("venue", "account_fingerprint", "symbol",
                  "evidence_type", "schema_version", "source",
                  "evidence_id", "observed_at", "payload"):
        if field not in envelope:
            raise VenueEvidenceError(
                f"capture {path.name}: missing field {field!r}")
    return envelope


def evidence_from(envelope: Mapping[str, Any]) -> VenueDirectEvidence:
    """The capture stores the payload as TEXT so the original bytes
    survive the round trip; parsing a re-serialized object would lose
    exactly the duplicates the parser must refuse."""
    payload = envelope["payload"]
    if not isinstance(payload, str):
        raise VenueEvidenceError(
            "capture payload must be the ORIGINAL text, not a "
            "re-serialized object")
    return VenueDirectEvidence.parse(
        venue=envelope["venue"],
        account_fingerprint=envelope["account_fingerprint"],
        symbol=envelope["symbol"],
        evidence_type=envelope["evidence_type"],
        schema_version=envelope["schema_version"],
        source=envelope["source"],
        evidence_id=envelope["evidence_id"],
        observed_at=envelope["observed_at"],
        raw_bytes=payload.encode("utf-8"))


def run(captures: Path, *, authority_root: Path, now: Any,
        raw_model_output: float, mapped_command: int,
        state_block: Mapping[str, Any], policy: Mapping[str, Any],
        evidence_policy: VenueEvidencePolicy,
        recovery: Mapping[str, Any] | None = None) -> dict:
    interface = NoWriteVenueInterface()
    assert interface.client is None and interface.credentials is None

    authority = load_authority(authority_root)
    moment = require_utc("now", now)

    facts = {}
    provenance = {}
    for name in ("account_session", "positions", "open_orders"):
        envelope = load_capture(captures, name)
        evidence = evidence_from(envelope).verify(evidence_policy,
                                                  now=moment)
        facts[name] = evidence.facts
        provenance[name] = evidence.provenance()

    clock_path = captures / CAPTURE_FILES["market_clock"]
    if clock_path.is_file():
        clock = evidence_from(load_capture(captures, "market_clock"))
        clock.verify(evidence_policy, now=moment)
        facts["market_clock"] = clock.facts
        provenance["market_clock"] = clock.provenance()

    directive = derive_directive(
        authority, policy=dict(policy), state_block=dict(state_block),
        venue=evidence_policy.venue,
        account_fingerprint=evidence_policy.account_fingerprint,
        symbol=evidence_policy.symbol,
        raw_model_output=raw_model_output,
        mapped_command=mapped_command,
        positions=facts["positions"], orders=facts["open_orders"],
        provenance=provenance["positions"], recovery=recovery)

    account = facts["account_session"]
    bar_age = 0.0
    if "market_clock" in facts:
        bar_age = (moment - require_utc(
            "bar_time", facts["market_clock"]["bar_time"])
        ).total_seconds()
    verdict = classify(
        session_state=directive.session_state,
        session_connected=account["session_connected"],
        trading_enabled=account["trading_enabled"],
        bar_age_seconds=max(0.0, bar_age),
        max_bar_age_seconds=evidence_policy.max_age_seconds,
        positions_total=facts["positions"]["positions_total"],
        orders_total=facts["open_orders"]["orders_total"],
        flatten_requested=directive.requires_direct_confirmation,
        flatten_confirmed=False,
        recovery_active=bool(recovery and recovery.get(
            "blocks_risk_increase")),
        provenance=provenance["positions"])

    return {
        "schema": "lts.session_directive_dry_run.v1",
        "dry_run": True,
        "writes_performed": 0,
        "interface": type(interface).__name__,
        "authority_code_identity": authority.code_identity,
        "evidence_policy_digest": evidence_policy.policy_digest,
        "would_send": directive.as_dict(),
        "watchdog": verdict.as_event(venue=evidence_policy.venue,
                                     symbol=evidence_policy.symbol),
        "provenance": provenance,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the venue command a weekly session "
                    "directive WOULD produce. Sends nothing.")
    parser.add_argument("--captures", required=True, type=Path)
    parser.add_argument("--authority-root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path,
                        help="dry-run configuration: policies, the "
                             "state block and the model output")
    args = parser.parse_args(argv)

    config = json.loads(args.config.read_text())
    evidence_policy = VenueEvidencePolicy.build(
        **config["evidence_policy"])
    try:
        report = run(args.captures,
                     authority_root=args.authority_root,
                     now=config["now"],
                     raw_model_output=config["raw_model_output"],
                     mapped_command=config["mapped_command"],
                     state_block=config["state_block"],
                     policy=config["session_policy"],
                     evidence_policy=evidence_policy,
                     recovery=config.get("recovery"))
    except (VenueEvidenceError, AdapterRefusal,
            AuthorityUnavailable) as exc:
        print(json.dumps({
            "schema": "lts.session_directive_dry_run.v1",
            "dry_run": True, "writes_performed": 0,
            "outcome": "REFUSED",
            "reason": f"{type(exc).__name__}: {exc}"}, indent=1))
        return 2
    print(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
