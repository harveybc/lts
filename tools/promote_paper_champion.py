#!/usr/bin/env python3
"""Paper-only champion succession entry point (findings 257/258).

This is the NON-TEST inbound call path to ``promote_paper_champion``.
Everything it needs about the venue it observes itself; everything it
needs about the candidate it verifies against artifact bytes.

    tools/promote_paper_champion.py --runner-config <the seat's own
    runner config> --candidate-descriptor <candidate.json> --action promote

Stages (all fail-closed, every refusal typed and named):

 1. seat contract built from the seat's OWN runner config and manifest;
 2. compatibility PRE-GATE — a candidate that cannot possibly run on this
    seat is refused before any broker session is opened at all;
 3. seat exclusivity — a promotion never runs beside a live runner that
    holds the same seat and the same ledger;
 4. DIRECT venue facts through the real adapter (REST account/orders/
    positions, TWS session, the terminal's own posted snapshot);
 5. the REAL venue executor is constructed and bound;
 6. shadow replay — the candidate re-decides the incumbent's own due bars
    from the venue's own bar history, through the real policy;
 7. ``app.champion_succession.promote_paper_champion`` — which re-runs
    compatibility, re-verifies the shadow evidence, requires the owner's
    signed single-use capability, drains, RE-OBSERVES the venue and then
    runs the resumable manifest saga.

Broker truth is never operator-supplied. There is no ``--facts-file``,
``--account-json``, ``--orders-json`` or ``--positions-json``: those flags
exist only to refuse, loudly, with exit code 2. Account, order, position,
balance and equity facts can only come from :mod:`app.succession_venue`.

Promotion additionally requires an owner-minted, owner-SIGNED, unconsumed
capability in the protected store (``tools/mint_promotion_capability.py``)
bound to this exact seat, candidate, incumbent and report digests. As of
2026-08-16 no such promotion is possible: all three seats refuse at stage
2 with named incompatibilities.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.champion_succession import (  # noqa: E402
    OUTGOING_SHADOW_DEFAULT_DAYS,
    PROMOTION_ALLOWED_SIGNERS,
    PROMOTION_STORE,
    VERDICT_COMPATIBLE,
    SuccessionError,
    candidate_shadow_replay,
    ensure_saga_schema,
    open_promotion_saga,
    outgoing_shadow_status,
    preflight_candidate,
    promote_paper_champion,
    resume_promotion_saga,
    succession_pending,
)
from app.succession_venue import (  # noqa: E402
    build_successor_manifest,
    build_venue,
    linear_shadow_inference,
    load_candidate_descriptor,
    load_runner_config,
    seat_contract_from_runner_config,
    venue_of,
)

REPORT_SCHEMA = "lts.succession.cli_run.v1"

#: A live runner holds the seat's ledger and its venue session. A
#: promotion is an exclusive operation on that seat.
LIVE_RUNNER_GRACE_SECONDS = 180.0

REFUSED_FACT_FLAGS = (
    "--facts-file", "--account-json", "--orders-json", "--positions-json",
    "--balance", "--equity", "--account-fingerprint",
)


class _RefuseBrokerTruth(argparse.Action):
    """Operator-supplied broker truth is structurally impossible here."""

    def __call__(self, parser, namespace, values, option_string=None):
        parser.exit(2, json.dumps({
            "schema": REPORT_SCHEMA,
            "state": "refused",
            "code": "OPERATOR_SUPPLIED_BROKER_TRUTH",
            "detail": (
                f"{option_string} is refused by design: account, order,"
                " position, balance and equity facts may only come from"
                " the venue's own fact interface"
                " (app.succession_venue). A promotion authorized by"
                " operator-supplied JSON would authorize nothing."),
        }, indent=1) + "\n")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _live_runner_holding_seat(
    config: dict[str, Any], *, now: datetime,
) -> Optional[dict[str, Any]]:
    """Direct evidence that the seat's own runner is alive right now."""
    heartbeat_path = config.get("heartbeat_path")
    if not heartbeat_path:
        return None
    path = Path(str(heartbeat_path)).expanduser()
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    observed_at = payload.get("observed_at")
    if not observed_at:
        return None
    try:
        observed = datetime.fromisoformat(str(observed_at))
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    budget = max(LIVE_RUNNER_GRACE_SECONDS,
                 3.0 * float(config.get("loop_seconds", 60.0)))
    age = (now - observed).total_seconds()
    if age > budget:
        return None
    return {"heartbeat_path": str(path), "observed_at": observed.isoformat(),
            "age_seconds": round(age, 1), "budget_seconds": budget,
            "runner_state": payload.get("state")}


def _refusal(code: str, detail: str, **facts: Any) -> dict[str, Any]:
    return {"state": "refused", "code": code, "detail": detail, **facts}


def _volatile(doc: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in doc.items()
            if key not in ("checked_at", "generated_at", "report_sha256")}


def _stable_evidence(fresh: dict[str, Any], path: Path, kind: str
                     ) -> dict[str, Any]:
    """Evidence digests must be STABLE, or no capability could ever bind
    them: the owner mints against the digests one run prints and spends
    them on the next run.

    So the report is persisted once and reused — but only after the fresh
    recomputation is proven identical in everything except its timestamp.
    Any real change (a new due bar, a changed live contract, a different
    artifact) invalidates the stored evidence and refuses, loudly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        stored = json.loads(path.read_bytes())
        if _volatile(stored) == _volatile(fresh):
            return stored
        path.write_text(
            json.dumps(fresh, indent=1, sort_keys=True, default=str) + "\n")
        path.chmod(0o600)
        raise SuccessionError(
            f"the persisted {kind} evidence at {path} no longer matches"
            " the seat and candidate as they are NOW; it has been"
            " replaced with the current evidence — mint a capability"
            " against the new digest")
    path.write_text(
        json.dumps(fresh, indent=1, sort_keys=True, default=str) + "\n")
    path.chmod(0o600)
    return fresh


def run(args: argparse.Namespace) -> dict[str, Any]:
    """The whole entry point, as data. ``main`` only prints it."""
    now = _utc_now()
    config = load_runner_config(args.runner_config)
    venue_id = venue_of(config)
    seat = seat_contract_from_runner_config(config)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "action": args.action,
        "venue": venue_id,
        "instrument": seat.instrument,
        "timeframe": seat.timeframe,
        "manifest_file": seat.manifest_file,
        "runner_config": str(args.runner_config),
        "at": now.isoformat(),
        "stages": [],
    }

    def stage(name: str, **facts: Any) -> None:
        report["stages"].append({"stage": name, **facts})

    if args.action in ("status", "resume-complete", "resume-rollback"):
        return _run_saga_action(args, config, seat, report, stage, now=now)

    if not args.candidate_descriptor:
        raise SuccessionError(
            "--candidate-descriptor is required for"
            f" --action {args.action}")
    candidate = load_candidate_descriptor(args.candidate_descriptor)
    report["candidate"] = {
        "model_id": candidate.model_id,
        "model_kind": candidate.model_kind,
        "artifact_sha256": candidate.artifact_sha256,
        "config_sha256": candidate.config_sha256,
        "observation_dim": candidate.observation_dim,
        "feature_count": len(candidate.feature_names),
    }
    stage("seat_and_candidate_loaded",
          seat_provisioning=seat.provisioning.contract_id,
          seat_observation_dim=seat.provisioning.observation_dim)

    evidence_dir = Path(args.evidence_dir).expanduser() / venue_id / (
        seat.instrument.replace("/", "_")) / candidate.model_id

    # -- stage 2: compatibility pre-gate (no broker session yet) --------
    compatibility = preflight_candidate(seat, candidate, now=now)
    report["compatibility"] = compatibility
    codes = [item["code"] for item in compatibility["incompatibilities"]]
    stage("compatibility_pre_gate", verdict=compatibility["verdict"],
          codes=codes, report_sha256=compatibility["report_sha256"])
    if compatibility["verdict"] != VERDICT_COMPATIBLE:
        report.update(_refusal(
            "CANDIDATE_INCOMPATIBLE",
            "the candidate cannot run on this seat; no broker session was"
            " opened and nothing was consumed",
            incompatibility_codes=codes))
        return report
    if args.action == "preflight":
        report["state"] = "compatible_preflight_only"
        report["detail"] = ("compatibility only; no venue facts were"
                            " obtained and nothing was promoted")
        return report

    # -- stage 3: seat exclusivity --------------------------------------
    holder = _live_runner_holding_seat(config, now=now)
    if holder is not None:
        report.update(_refusal(
            "SEAT_HELD_BY_LIVE_RUNNER",
            "the seat's own runner is alive and owns this venue session"
            " and ledger; stop it before promoting so a succession never"
            " races the runner it is replacing",
            live_runner=holder))
        return report
    stage("seat_exclusivity", live_runner=None)

    # -- stages 4-5: direct facts and the REAL executor -----------------
    compatibility = _stable_evidence(
        compatibility, evidence_dir / "compatibility_report.json",
        "compatibility")
    report["compatibility"] = compatibility

    venue = build_venue(config)
    try:
        facts = venue.fetch_facts()
        stage("direct_venue_facts", **facts.summary())
        if facts.account_fingerprint != config["service"][
                "account_fingerprint"]:
            report.update(_refusal(
                "ACCOUNT_FINGERPRINT_MISMATCH",
                "the observed account is not the account this seat is"
                " configured for"))
            return report
        executor = venue.bind_executor()
        stage("executor_bound", **executor)

        store = _ledger_of(venue)
        ensure_saga_schema(store)
        pending = succession_pending(
            store, venue=venue_id, instrument=seat.instrument,
            account_fingerprint=facts.account_fingerprint)
        if pending is not None:
            report.update(_refusal(
                "PROMOTION_SAGA_OPEN",
                "a promotion saga is already open for this seat; resume"
                " or roll it back first", succession=pending))
            return report

        # -- stage 6: shadow replay on the incumbent's own due bars -----
        infer = linear_shadow_inference(
            candidate, venue.historical_closed_bars())
        shadow = candidate_shadow_replay(
            store, seat=seat, candidate=candidate, infer=infer,
            since=args.shadow_since, now=now)
        shadow = _stable_evidence(
            shadow, evidence_dir / "shadow_report.json", "shadow")
        report["shadow"] = {
            "counts": shadow["counts"],
            "coverage_fraction": shadow["coverage_fraction"],
            "report_sha256": shadow["report_sha256"],
        }
        stage("shadow_replay", **report["shadow"])

        # -- stage 7: the owner-gated succession ------------------------
        successor_manifest = build_successor_manifest(seat, candidate)
        result = promote_paper_champion(
            store=store, venue=venue, seat=seat, candidate=candidate,
            compatibility_report=compatibility, shadow_report=shadow,
            strategy_config=config["strategy"],
            capability_store_dir=Path(args.capability_store),
            new_manifest=successor_manifest,
            allowed_signers=Path(args.allowed_signers),
            require_root_pin=not args.no_root_pin,
            explicit_capability=(Path(args.capability)
                                 if args.capability else None),
            outgoing_shadow_days=float(args.outgoing_shadow_days),
            now=now)
        report["promotion"] = result
        report["state"] = result.get("state", "unknown")
        stage("promotion", state=report["state"],
              saga_id=result.get("saga_id"))
        return report
    except SuccessionError as error:
        # A typed refusal keeps the report: the owner needs the evidence
        # digests and the incumbent identity printed here to mint the
        # capability this very run refused for lack of.
        report.update(_refusal("SUCCESSION_REFUSED", str(error)))
        report["mint_binding"] = {
            "venue": venue_id, "instrument": seat.instrument,
            "timeframe": seat.timeframe, "asset_id": seat.asset_id,
            "candidate_model_id": candidate.model_id,
            "candidate_artifact_sha256": candidate.artifact_sha256,
            "candidate_config_sha256": candidate.config_sha256,
            "compatibility_report_sha256":
                compatibility.get("report_sha256"),
            "shadow_report_sha256":
                (report.get("shadow") or {}).get("report_sha256"),
        }
        return report
    finally:
        venue.close()


def _ledger_of(venue: Any) -> Any:
    """The L1 ledger the seat's runner already owns."""
    for attribute in ("store", "olap", "l0"):
        ledger = getattr(venue.runner, attribute, None)
        if ledger is not None:
            return ledger
    raise SuccessionError("the venue runner exposes no L1 ledger")


def _run_saga_action(args, config, seat, report, stage, *, now):
    """status / resume-complete / resume-rollback.

    Resume deliberately does NOT observe the venue and does NOT select a
    capability: an interrupted promotion is finished from its own durable
    saga row or it is not finished at all.
    """
    fingerprint = str(config["service"]["account_fingerprint"])
    venue_id = venue_of(config)
    from app.ibkr_l1_journal import L1ExecutionOlap

    database = Path(str(config["service"]["database_path"])).expanduser()
    store = L1ExecutionOlap(database)
    try:
        ensure_saga_schema(store)
        saga = open_promotion_saga(
            store, venue=venue_id, account_fingerprint=fingerprint,
            instrument=seat.instrument)
        if args.action == "status":
            report["state"] = "ok"
            report["succession_pending"] = succession_pending(
                store, venue=venue_id, instrument=seat.instrument,
                account_fingerprint=fingerprint)
            report["outgoing_shadow"] = outgoing_shadow_status(
                store, seat=seat, now=now)
            last = store.get_state(
                f"last_promotion:{venue_id}:{seat.instrument}", "")
            report["last_promotion"] = json.loads(last) if last else None
            stage("status", open_saga=(saga or {}).get("saga_id"))
            return report
        if saga is None:
            report.update(_refusal(
                "NO_OPEN_SAGA",
                "there is no interrupted promotion to resume for this"
                " seat"))
            return report
        action = ("complete" if args.action == "resume-complete"
                  else "rollback")
        result = resume_promotion_saga(
            store, venue=venue_id, account_fingerprint=fingerprint,
            instrument=seat.instrument, action=action,
            reason=args.reason or f"operator {args.action}", now=now)
        report["promotion"] = result
        report["state"] = result.get("state", "unknown")
        stage("resume", action=action, saga_id=result.get("saga_id"),
              saga_state=result.get("saga_state"))
        return report
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="promote_paper_champion.py",
        description=(
            "Paper-only champion succession: obtain DIRECT venue facts,"
            " construct the real venue executor, re-run compatibility and"
            " shadow checks and invoke the owner-gated, crash-resumable"
            " promotion saga. Broker truth is never operator-supplied."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "actions:\n"
            "  promote          full succession (owner capability"
            " required)\n"
            "  preflight        compatibility only; opens no broker"
            " session\n"
            "  status           saga / outgoing-shadow / last promotion\n"
            "  resume-complete  finish an interrupted saga forward\n"
            "  resume-rollback  undo an interrupted saga coherently\n"
            "\nexit codes: 0 ok, 2 forbidden input, 3 typed refusal\n"))
    parser.add_argument(
        "--runner-config", required=True, type=Path,
        help="the seat's OWN runner config (alpaca/ibkr/mt5); it names"
             " the profile, ledger and manifest this succession acts on")
    parser.add_argument(
        "--candidate-descriptor", type=Path,
        help="candidate MODEL descriptor (hashes and contracts only;"
             " never broker facts)")
    parser.add_argument(
        "--action", default="promote",
        choices=["promote", "preflight", "status", "resume-complete",
                 "resume-rollback"])
    parser.add_argument(
        "--capability-store", type=Path, default=PROMOTION_STORE,
        help="protected owner capability store (mode 0700)")
    parser.add_argument(
        "--capability", type=Path,
        help="name ONE capability inside the protected store")
    parser.add_argument(
        "--allowed-signers", type=Path, default=PROMOTION_ALLOWED_SIGNERS,
        help="root-pinned owner allowed-signers file")
    parser.add_argument(
        "--no-root-pin", action="store_true",
        help="accept a non-root-owned signer pin (test rigs only)")
    parser.add_argument(
        "--outgoing-shadow-days", type=float,
        default=OUTGOING_SHADOW_DEFAULT_DAYS,
        help="displaced-champion shadow window (>= 7 days)")
    parser.add_argument(
        "--evidence-dir", type=Path,
        default=Path.home() / ".local/state/lts/succession",
        help="where the compatibility and shadow reports the owner mints"
             " against are persisted (0600)")
    parser.add_argument("--shadow-since", default=None,
                        help="replay only due bars at/after this ISO time")
    parser.add_argument("--reason", default="",
                        help="reason recorded with a resume/rollback")
    parser.add_argument("--json", type=Path,
                        help="write the full typed run report here")
    for flag in REFUSED_FACT_FLAGS:
        parser.add_argument(
            flag, action=_RefuseBrokerTruth, nargs="?", default=None,
            help=argparse.SUPPRESS)
    return parser


def _print_human(report: dict[str, Any]) -> None:
    print(f"[{report['venue']}/{report['instrument']}@"
          f"{report['timeframe']}] action={report['action']}")
    for entry in report.get("stages", []):
        facts = {k: v for k, v in entry.items() if k != "stage"}
        print(f"  · {entry['stage']}: "
              + json.dumps(facts, sort_keys=True, default=str)[:400])
    compatibility = report.get("compatibility")
    if compatibility:
        print(f"  verdict: {compatibility['verdict']}")
        for item in compatibility["incompatibilities"]:
            print(f"    - {item['code']}: {item['detail']}")
            missing = (item.get("facts") or {}).get("missing_features")
            if missing:
                print(f"      missing ({len(missing)}):"
                      f" {', '.join(missing[:12])}"
                      + (" …" if len(missing) > 12 else ""))
    if report.get("state") == "refused":
        print(f"  REFUSED {report['code']}: {report['detail']}")
        binding = report.get("mint_binding")
        if binding:
            print("  mint binding (tools/mint_promotion_capability.py):")
            for key, value in sorted(binding.items()):
                print(f"    {key}: {value}")
    else:
        print(f"  state: {report.get('state')}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except SuccessionError as error:
        report = {"schema": REPORT_SCHEMA, "state": "refused",
                  "code": "SUCCESSION_REFUSED", "detail": str(error),
                  "venue": "unknown", "instrument": "unknown",
                  "timeframe": "unknown", "action": args.action,
                  "stages": []}
    if args.json:
        target = Path(args.json).expanduser()
        target.write_text(
            json.dumps(report, indent=1, sort_keys=True, default=str)
            + "\n")
        target.chmod(0o600)
    _print_human(report)
    return 3 if report.get("state") == "refused" else 0


if __name__ == "__main__":
    raise SystemExit(main())
