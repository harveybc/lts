#!/usr/bin/env python3
"""WO1 direct seat truth: typed fleet inventory from DIRECT venue/runner facts.

Binding order: MUSASHI_RESPONSE_TO_RETSU_FULL_AUDIT_AND_CORRECTED_SATOSHI_ORDER
_2026_08_15.md SWO1 (agent-multi authoring worktree), findings F-P1-01/03/04.

Doctrine enforced by this tool:

* DIRECT SOURCES ONLY. Every fact names the venue endpoint, runner state
  file, or runner execution ledger it came from. The consolidated
  paper-execution watchdog, the paper-execution-monitor store, the TWS
  continuity monitor and the incident router are FORBIDDEN as sources;
  assembly refuses (raises) if any fact cites one.
* READ-ONLY. No order is submitted/cancelled/modified, no hold cleared,
  no runner touched. The IBKR probe connects with ``readonly=True`` on a
  dedicated, unused clientId and never calls ``reqAllOpenOrders`` (it can
  rebind order ownership); that limitation is emitted as a typed fact.
* TYPED ``unavailable``. Any fact that cannot be observed directly is
  reported as ``{"unavailable": <reason_code>, ...}`` - never imputed.
* REDACTION. Raw account identifiers and secrets never appear in output.
  Accounts are shown as sha256-16 fingerprints; the assembler scans the
  final serialization against every raw identifier it transiently held
  and against broker id patterns, and refuses on any leak.
* FRESHNESS. Every time-bearing fact carries ``observed_at``,
  ``age_seconds``, ``freshness_budget_seconds`` and ``fresh``.

MT5 (F-P1-03): Dragon-local evidence is made fleet-readable by pulling
dragon's runner heartbeat JSON and DIRECT rows from dragon's
``mt5-bridge.sqlite`` (account_snapshots/position_snapshots/heartbeats,
read via ``ssh -o BatchMode=yes`` + sqlite ``file:...?mode=ro`` URI) onto
an omega-readable audit path, labeled direct-source with freshness.

IBKR (F-P1-04): the order/exposure mismatch is resolved from direct TWS
facts (positions()/portfolio() account-scoped are authoritative for
flatness) joined to the runner's own execution ledger; the venue-scoped
open-order set is typed unavailable with the reqAllOpenOrders-rebinding
reason, and the heartbeat model-artifact proof gap is typed explicitly.

PRIVATE/PUBLIC SPLIT (correction of AUD-SEC-20260816-255): the typed
inventory this tool assembles is genuinely private -- it carries account
balances/equity/margin, exact sizes and prices, broker tickets/order ids
and stable account/server fingerprints. It is therefore written ONLY to
the local evidence store ``~/.local/state/lts/evidence/`` with 0700
directories and 0600 files (modes are re-applied and then verified with
``os.stat``; the run fails closed otherwise). What goes to git is a
*derived* public document built by ``tools/seat_truth_public_evidence.py``
from an explicit field allowlist: typed availability/freshness states,
counts, booleans, model/config/artifact hashes and the SHA-256 of the
private packet -- never the packet, never a fingerprint value.

Usage (CPU only; never touches GPU work):
    python tools/seat_truth_inventory.py \
        --output-dir docs/evidence/seat_truth [--skip-tws] [--skip-dragon]
        [--skip-alpaca-rest] [--tws-client-id 179]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.seat_truth_public_evidence import (  # noqa: E402
    PRIVATE_SCHEMA,
    assert_public_document,
    render_public_table,
    sanitize_inventory,
)

SCHEMA = "lts.seat_truth_inventory.v1"
STATE = Path.home() / ".local/state/lts"
EVIDENCE_ROOT = STATE / "evidence"
PRIVATE_SEAT_TRUTH_DIR = EVIDENCE_ROOT / "seat_truth"
MANIFEST_ROOT = Path.home() / ".local/share/prediction-provider/live"

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

# Consolidated/derived stores that must NEVER be cited as a source.
FORBIDDEN_SOURCE_TOKENS = (
    "paper_execution_watchdog",
    "paper-execution-watchdog",
    "paper-execution-monitor",
    "tws-continuity-monitor",
    "tws_continuity_monitor",
    "incident_ledger",
    "multi-venue-shadow",
)

# Broker account-id shapes that must never appear in output.
ACCOUNT_ID_PATTERNS = (
    re.compile(r"\bDU[A-Z]?\d{5,}\b"),        # IBKR paper accounts
    re.compile(r"\bU\d{7,}\b"),               # IBKR live accounts
    re.compile(r"\bPA[0-9A-Z]{8,}\b"),        # Alpaca paper account numbers
)

HEARTBEAT_BUDGET_S = 300.0          # runner loops are 60s/60s/15s
VENUE_PROBE_BUDGET_S = 120.0        # direct API probe, observed at collection
BRIDGE_SNAPSHOT_BUDGET_S = 120.0    # mt5 runner snapshot_max_age_seconds
SSH_TIMEOUT_S = 30.0

# Seat registry. The EXPECTED ACCOUNT FINGERPRINT IS DELIBERATELY ABSENT:
# a fingerprint is a stable account identifier, so this tool never carries
# a second copy of one and never emits one. The expected binding is read at
# run time from the deployed runner config (the single source of truth) and
# only the boolean comparison result is reported.
CONFIG_ROOT = Path.home() / "Documents/GitHub/lts/examples/configs"

EXPECTED = {
    "alpaca_paper": {"symbol": "SPY", "timeframe": "1d",
                     "model_id": "spy-daily-linear-live-v1",
                     "binding_config": "alpaca_spy_model_runner_v1.json"},
    "ibkr_paper": {"symbol": "USD.CAD", "timeframe": "4h",
                   "model_id": "usdcad-4h-linear-live-v1",
                   "binding_config": "ibkr_usdcad_model_runner_v1.json"},
    "mt5_demo": {"symbol": "ETHUSD", "timeframe": "4h",
                 "model_id": "ethusdt-4h-linear-live-v1",
                 "binding_config": "mt5_eth_model_runner_v1.json"},
}

TIMEFRAME_SECONDS = {"1d": 86400.0, "4h": 14400.0}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


# --------------------------------------------------------------------------
# private evidence store (0700 dirs / 0600 files, verified programmatically)
# --------------------------------------------------------------------------

class PrivateStoreError(RuntimeError):
    """The local evidence store is not in its required private mode."""


def secure_mkdir(path: Path) -> None:
    """Create (or tighten) a directory to 0700 and verify the result."""
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    os.chmod(path, PRIVATE_DIR_MODE)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != PRIVATE_DIR_MODE:
        raise PrivateStoreError(
            f"{path} is mode {mode:04o}, required {PRIVATE_DIR_MODE:04o}")


def secure_write_text(path: Path, text: str) -> tuple[str, int]:
    """Write private text 0600-only. Returns (sha256, byte_length).

    The file is created with 0600 by ``os.open`` (never briefly wider),
    re-chmod'd in case it already existed, and the resulting mode is read
    back with ``os.stat`` -- the caller fails closed if it is not 0600.
    """
    secure_mkdir(path.parent)
    data = (text if text.endswith("\n") else text + "\n").encode("utf-8")
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                         PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.chmod(path, PRIVATE_FILE_MODE)
    verify_private_mode(path)
    return hashlib.sha256(data).hexdigest(), len(data)


def verify_private_mode(path: Path) -> int:
    """Read the on-disk mode back and refuse anything but 0600/0700."""
    mode = stat.S_IMODE(path.stat().st_mode)
    required = PRIVATE_DIR_MODE if path.is_dir() else PRIVATE_FILE_MODE
    if mode != required:
        raise PrivateStoreError(
            f"{path} is mode {mode:04o}, required {required:04o}")
    return mode


def harden_evidence_tree(root: Path) -> list[str]:
    """Tighten every pre-existing file/dir under the local evidence root.

    Earlier WO1 runs left direct venue evidence world-readable; this makes
    the whole store private again and reports what it changed.
    """
    changed: list[str] = []
    if not root.exists():
        return changed
    for path in [root, *sorted(root.rglob("*"))]:
        required = PRIVATE_DIR_MODE if path.is_dir() else PRIVATE_FILE_MODE
        current = stat.S_IMODE(path.stat().st_mode)
        if current != required:
            os.chmod(path, required)
            changed.append(f"{path} {current:04o} -> {required:04o}")
        verify_private_mode(path)
    return changed


# --------------------------------------------------------------------------
# typed fact model
# --------------------------------------------------------------------------

def fact(value: Any, *, source: str, observed_at: str | None = None,
         budget_seconds: float | None = None, now: datetime | None = None,
         note: str | None = None) -> dict:
    """A typed direct fact: value + source + freshness envelope."""
    now = now or utcnow()
    entry: dict[str, Any] = {"value": value, "source": source,
                             "collected_at": iso(now)}
    if observed_at is not None:
        entry["observed_at"] = observed_at
        try:
            age = (now - datetime.fromisoformat(observed_at)).total_seconds()
        except ValueError:
            age = None
        entry["age_seconds"] = round(age, 3) if age is not None else None
        if budget_seconds is not None:
            entry["freshness_budget_seconds"] = budget_seconds
            entry["fresh"] = (age is not None and age <= budget_seconds)
    elif budget_seconds is not None:
        entry["freshness_budget_seconds"] = budget_seconds
    if note:
        entry["note"] = note
    return entry


def unavailable(reason_code: str, *, detail: str | None = None,
                source_attempted: str | None = None) -> dict:
    """A typed absent fact. Never imputed, never silently dropped."""
    entry: dict[str, Any] = {"unavailable": reason_code}
    if detail:
        entry["detail"] = detail
    if source_attempted:
        entry["source_attempted"] = source_attempted
    return entry


def is_unavailable(entry: Any) -> bool:
    return isinstance(entry, dict) and "unavailable" in entry


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

class RedactionLeak(RuntimeError):
    pass


class Redactor:
    """Holds raw identifiers transiently; guarantees they never surface."""

    def __init__(self) -> None:
        self._forbidden: set[str] = set()

    def register(self, raw: str | None) -> None:
        if raw and len(str(raw)) >= 4:
            self._forbidden.add(str(raw))

    def fingerprint(self, raw: str, *, length: int = 16) -> str:
        self.register(raw)
        return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:length]

    def assert_clean(self, text: str) -> None:
        for raw in self._forbidden:
            if raw in text:
                raise RedactionLeak(
                    "raw identifier would leak into output (redacted"
                    f" fingerprint {self.fingerprint(raw)})")
        for pattern in ACCOUNT_ID_PATTERNS:
            match = pattern.search(text)
            if match:
                raise RedactionLeak(
                    "account-id-shaped token would leak into output"
                    f" (pattern {pattern.pattern})")


# --------------------------------------------------------------------------
# assembly refusal logic (pure; unit-tested with fakes)
# --------------------------------------------------------------------------

def iter_sources(node: Any):
    if isinstance(node, dict):
        if "source" in node and isinstance(node["source"], str):
            yield node["source"]
        if "source_attempted" in node and isinstance(
                node["source_attempted"], str):
            yield node["source_attempted"]
        for value in node.values():
            yield from iter_sources(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_sources(value)


def assert_direct_sources_only(seats: dict) -> None:
    """Refuse any fact sourced from a consolidated watchdog/alert store."""
    for source in iter_sources(seats):
        lowered = source.lower()
        for token in FORBIDDEN_SOURCE_TOKENS:
            if token in lowered:
                raise ValueError(
                    f"forbidden consolidated source cited: {source!r}"
                    f" (token {token!r}); WO1 requires direct venue or"
                    " runner facts only")


def index_unavailable(node: Any, path: str = "") -> list[dict]:
    """Collect every typed unavailable with its JSON path."""
    found: list[dict] = []
    if isinstance(node, dict):
        if "unavailable" in node:
            found.append({"path": path, "reason": node["unavailable"],
                          "detail": node.get("detail")})
        else:
            for key, value in node.items():
                found.extend(index_unavailable(
                    value, f"{path}.{key}" if path else key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found.extend(index_unavailable(value, f"{path}[{i}]"))
    return found


def assemble_inventory(seats: dict, *, collector: dict,
                       ibkr_resolution: dict | None = None,
                       now: datetime | None = None) -> dict:
    """Pure assembly: validates sources, indexes unavailability."""
    assert_direct_sources_only(seats)
    inventory = {
        "schema": SCHEMA,
        "schema_version": 1,
        "generated_at": iso(now or utcnow()),
        "collector": collector,
        "doctrine": {
            "direct_sources_only": True,
            "forbidden_sources": list(FORBIDDEN_SOURCE_TOKENS),
            "read_only": True,
            "no_reqAllOpenOrders": True,
            "redaction": ("account identifiers appear only as sha256"
                          " fingerprints; output scanned before emission"),
        },
        "seats": seats,
    }
    if ibkr_resolution is not None:
        inventory["ibkr_order_exposure_resolution"] = ibkr_resolution
    inventory["unavailable_index"] = index_unavailable(seats)
    if ibkr_resolution is not None:
        inventory["unavailable_index"].extend(index_unavailable(
            ibkr_resolution, "ibkr_order_exposure_resolution"))
    return inventory


# --------------------------------------------------------------------------
# IBKR order/exposure resolution (pure over gathered typed facts)
# --------------------------------------------------------------------------

def resolve_ibkr_order_exposure(*, direct_positions: dict,
                                direct_portfolio: dict,
                                heartbeat_position: dict,
                                heartbeat_orders: dict,
                                ledger_open_exposure: dict,
                                stuck_order: dict,
                                venue_open_orders: dict) -> dict:
    """Join direct TWS flatness with runner-ledger order state.

    Direct positions()/portfolio() (account-scoped) are authoritative for
    flatness. The venue-scoped open-order set is typed unavailable
    (reqAllOpenOrders rebinding risk), so the single runner-tracked open
    order is identified from the runner's own execution ledger.
    """
    verdicts: list[str] = []
    flat_direct: Any = "unavailable"
    position_rows: int | None = None
    portfolio_rows: int | None = None
    if not is_unavailable(direct_positions):
        rows = direct_positions.get("value") or []
        position_rows = len(rows)
        flat_direct = all(
            float(row.get("position", 0.0)) == 0.0 for row in rows)
        verdicts.append(
            "direct TWS positions() account-scoped shows"
            f" {'FLAT' if flat_direct else 'NON-FLAT'}"
            f" ({len(rows)} position rows)")
    else:
        verdicts.append("direct TWS positions unavailable: "
                        + str(direct_positions.get("unavailable")))
    if not is_unavailable(direct_portfolio):
        rows = direct_portfolio.get("value") or []
        portfolio_rows = len(rows)
        port_flat = all(
            float(row.get("position", 0.0)) == 0.0 for row in rows)
        verdicts.append(
            "direct TWS portfolio() account-scoped shows"
            f" {'FLAT' if port_flat else 'NON-FLAT'}"
            f" ({len(rows)} portfolio rows)")
        if flat_direct != "unavailable":
            flat_direct = flat_direct and port_flat
        else:
            flat_direct = port_flat

    exposure_open = (not is_unavailable(ledger_open_exposure)
                     and ledger_open_exposure.get("value") is not None)
    orders_tracked = (heartbeat_orders.get("value")
                      if not is_unavailable(heartbeat_orders) else None)

    if flat_direct is True and exposure_open:
        state = "flat_at_venue__ledger_exposure_row_stale_open"
        explanation = (
            "The account is FLAT by direct TWS positions()/portfolio()"
            " (authoritative). The runner ledger still carries an OPEN"
            " exposure row because reconciliation is pending: the runner"
            " tracks one non-terminal open order (the bracket exit leg"
            " below) and refuses to close the exposure until the venue"
            " order set is clean. Exposure is therefore an accounting"
            " residue, not market risk.")
    elif flat_direct is True and not exposure_open:
        state = "flat_and_reconciled"
        explanation = "Direct flat and no open ledger exposure."
    elif flat_direct is False:
        state = "venue_position_open"
        explanation = ("Direct TWS shows a non-flat position; ledger and"
                       " heartbeat must be read against it.")
    else:
        state = "unresolved_direct_unavailable"
        explanation = ("Direct TWS facts unavailable; no flatness verdict"
                       " is issued (never inferred from one field).")

    return {
        "state": state,
        "explanation": explanation,
        "direct_flatness": flat_direct,
        "direct_position_rows": position_rows,
        "direct_portfolio_rows": portfolio_rows,
        "verdicts": verdicts,
        "heartbeat_position": heartbeat_position,
        "heartbeat_open_order_count": heartbeat_orders,
        "ledger_open_exposure": ledger_open_exposure,
        "runner_tracked_open_order": stuck_order,
        "venue_scoped_open_orders": venue_open_orders,
        "residual": ("orders_tracked=" + str(orders_tracked)
                     + "; venue-side confirmation of that order requires"
                       " reqAllOpenOrders or an owner-authorized order-"
                       "history query - both outside this read-only"
                       " mandate."),
    }


# --------------------------------------------------------------------------
# real IO (kept thin; every function returns typed facts)
# --------------------------------------------------------------------------

def read_json_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def sqlite_rows(path: Path, sql: str, params: tuple = ()) -> list[dict]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head(path: Path) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15)
        return done.stdout.strip() if done.returncode == 0 else None
    except Exception:
        return None


def ssh_python_json(host: str, script: str,
                    timeout: float = SSH_TIMEOUT_S) -> tuple[dict | None, str]:
    """Run a python3 script on the remote host (BatchMode ssh), parse the
    single JSON object it prints. Returns (payload|None, error)."""
    try:
        done = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             host, "python3", "-"],
            input=script, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return None, f"ssh transport failure: {exc}"
    if done.returncode != 0:
        return None, f"remote exit {done.returncode}: {done.stderr[:300]}"
    try:
        return json.loads(done.stdout), ""
    except json.JSONDecodeError as exc:
        return None, f"remote output not JSON: {exc}"


def http_get_json(url: str, headers: dict[str, str],
                  timeout: float = 20.0) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def find_first_key(node: Any, key: str) -> Any:
    """Depth-first lookup of the first occurrence of ``key``."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = find_first_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_first_key(value, key)
            if found is not None:
                return found
    return None


def expected_account_fingerprint(seat_key: str) -> str | None:
    """Read the seat's expected binding from its DEPLOYED runner config.

    Returns None when the config or the field is absent; callers must then
    type the comparison ``unavailable`` instead of reporting a mismatch.
    The value is used only for an equality test -- it is never emitted.
    """
    name = EXPECTED[seat_key].get("binding_config")
    if not name:
        return None
    config = read_json_file(CONFIG_ROOT / name)
    value = find_first_key(config, "account_fingerprint")
    return value if isinstance(value, str) and value else None


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


# --------------------------------------------------------------------------
# shared seat pieces
# --------------------------------------------------------------------------

def heartbeat_fact(payload: dict | None, path: str, key: str,
                   now: datetime, budget: float = HEARTBEAT_BUDGET_S,
                   note: str | None = None) -> dict:
    source = f"direct_runner:{path}"
    if payload is None:
        return unavailable("heartbeat_unreadable", source_attempted=source)
    if key not in payload:
        return unavailable("field_absent_in_heartbeat",
                           detail=key, source_attempted=source)
    return fact(payload[key], source=source,
                observed_at=payload.get("observed_at"),
                budget_seconds=budget, now=now, note=note)


def manifest_join(*, heartbeat: dict | None, manifest: dict | None,
                  manifest_path: str, manifest_bytes_sha256: str | None,
                  artifact_recomputed_sha256: str | None,
                  now: datetime) -> dict:
    """Finding-241 join: heartbeat hashes vs manifest vs recomputed bytes."""
    source = f"direct_file:{manifest_path}"
    if manifest is None:
        return {"join": unavailable("manifest_unreadable",
                                    source_attempted=source)}
    if heartbeat is None:
        return {"join": unavailable("heartbeat_unreadable_for_join",
                                    source_attempted=source)}
    hb_artifact = heartbeat.get("artifact_sha256")
    mf_artifact = manifest.get("artifact_sha256")
    checks = {
        "heartbeat_artifact_equals_manifest_artifact":
            (hb_artifact == mf_artifact) if hb_artifact and mf_artifact
            else "unavailable",
        "manifest_artifact_equals_file_bytes":
            (mf_artifact == artifact_recomputed_sha256)
            if mf_artifact and artifact_recomputed_sha256 else "unavailable",
        "heartbeat_manifest_sha_equals_manifest_bytes":
            (heartbeat.get("manifest_sha256") == manifest_bytes_sha256)
            if heartbeat.get("manifest_sha256") and manifest_bytes_sha256
            else "unavailable",
        "heartbeat_config_equals_manifest_config":
            (heartbeat.get("config_sha256") == manifest.get("config_sha256"))
            if heartbeat.get("config_sha256") and manifest.get("config_sha256")
            else "unavailable",
    }
    gaps = [name for name, value in checks.items() if value is not True]
    eligibility = {
        "live_inference_eligible": manifest.get("live_inference_eligible"),
        "live_execution_eligible": manifest.get("live_execution_eligible"),
        "research_validated": manifest.get("research_validated"),
    }
    proof_notes = []
    if all(v is True for v in checks.values()):
        proof_notes.append(
            "artifact identity is provable: heartbeat hash == manifest"
            " hash == recomputed file bytes; load path"
            " (app/live_model_selection.py) refuses on hash mismatch")
    if eligibility["live_inference_eligible"] is not True \
            or eligibility["live_execution_eligible"] is not True:
        proof_notes.append(
            "F-P1-04 gap remains: the manifest declares the artifact NOT"
            " live-eligible (live_inference_eligible="
            f"{eligibility['live_inference_eligible']},"
            " live_execution_eligible="
            f"{eligibility['live_execution_eligible']}); the heartbeat"
            " therefore proves the identity of an integration-baseline"
            " artifact, not a validated live champion; the heartbeat also"
            " carries no signature and no continuous re-attestation of"
            " loaded bytes")
    return {
        "join": fact({"checks": checks, "gaps": gaps},
                     source=source, now=now),
        "manifest_schema": manifest.get("schema"),
        "eligibility": fact(eligibility, source=source, now=now),
        "proof_notes": proof_notes,
    }


def decision_block(ledger: Path, model_id: str, timeframe: str,
                   now: datetime) -> dict:
    src = f"direct_runner_ledger:{ledger}"
    budget = 2 * TIMEFRAME_SECONDS.get(timeframe, 86400.0)
    block: dict[str, Any] = {}
    try:
        inference = sqlite_rows(
            ledger, "SELECT observed_at,last_closed_bar,action,"
                    "probability_up,input_sha256 FROM live_model_inferences"
                    " ORDER BY observed_at DESC LIMIT 1")
        decision = sqlite_rows(
            ledger, "SELECT bar_close,decided_at,action,outcome,reason,"
                    "decision_id,artifact_sha256,input_sha256"
                    " FROM due_bar_decisions ORDER BY bar_close DESC LIMIT 1")
        deferral = sqlite_rows(
            ledger, "SELECT * FROM decision_deferrals"
                    " ORDER BY rowid DESC LIMIT 1")
    except sqlite3.Error as exc:
        return {"last_closed_input_bar": unavailable(
            "ledger_unreadable", detail=str(exc), source_attempted=src)}
    if inference:
        row = inference[0]
        block["last_closed_input_bar"] = fact(
            row["last_closed_bar"], source=f"{src}#live_model_inferences",
            observed_at=row["observed_at"], budget_seconds=budget, now=now)
        block["due_decision_identity"] = fact(
            f"{model_id}:{row['last_closed_bar']}",
            source=f"{src}#live_model_inferences",
            now=now, note="derived as model_id:last_closed_bar, the"
                          " ledger's decision_id convention")
    else:
        block["last_closed_input_bar"] = unavailable(
            "no_inference_rows", source_attempted=src)
        block["due_decision_identity"] = unavailable(
            "no_inference_rows", source_attempted=src)
    if decision:
        row = decision[0]
        block["last_recorded_decision"] = fact(
            {k: row[k] for k in ("decision_id", "bar_close", "action",
                                 "outcome", "reason")},
            source=f"{src}#due_bar_decisions",
            observed_at=row["decided_at"], budget_seconds=budget, now=now)
        due = block.get("due_decision_identity", {})
        if not is_unavailable(due):
            block["decision_current_for_last_closed_bar"] = fact(
                row["decision_id"] == due.get("value"),
                source=f"{src}#due_bar_decisions", now=now)
    else:
        block["last_recorded_decision"] = unavailable(
            "no_decision_rows", source_attempted=src)
    if deferral:
        block["latest_deferral"] = fact(
            deferral[0], source=f"{src}#decision_deferrals", now=now)
    return block


def control_block(ledger: Path, now: datetime) -> dict:
    src = f"direct_runner_ledger:{ledger}#service_state"
    try:
        rows = sqlite_rows(ledger, "SELECT key,value FROM service_state")
    except sqlite3.Error as exc:
        return {"halt": unavailable("ledger_unreadable", detail=str(exc),
                                    source_attempted=src)}
    state = {row["key"]: row["value"] for row in rows}
    block = {
        "halt": (fact(state["halt"], source=src, now=now)
                 if "halt" in state else
                 unavailable("no_halt_row", source_attempted=src,
                             detail="service_state has no halt key")),
    }
    if "last_resume" in state:
        try:
            resume = json.loads(state["last_resume"])
            resume = {k: resume.get(k) for k in
                      ("at", "effect_id", "evidence_sha256")}
        except json.JSONDecodeError:
            resume = state["last_resume"][:120]
        block["last_resume"] = fact(resume, source=src, now=now)
    return block


def open_exposure_fact(ledger: Path, now: datetime) -> dict:
    src = f"direct_runner_ledger:{ledger}#exposures"
    try:
        rows = sqlite_rows(
            ledger, "SELECT exposure_id,instrument,units_open,state,"
                    "opened_at FROM exposures WHERE state='open'"
                    " ORDER BY opened_at DESC")
    except sqlite3.Error as exc:
        return unavailable("ledger_unreadable", detail=str(exc),
                           source_attempted=src)
    return fact(rows[0] if rows else None, source=src, now=now,
                note=f"{len(rows)} open exposure rows")


def latest_effect_fact(ledger: Path, now: datetime) -> dict:
    src = f"direct_runner_ledger:{ledger}#l1_effects"
    try:
        rows = sqlite_rows(
            ledger, "SELECT effect_id,idempotency_key,kind,state,"
                    "order_ids_json,created_at,updated_at FROM l1_effects"
                    " ORDER BY created_at DESC LIMIT 1")
    except sqlite3.Error as exc:
        return unavailable("ledger_unreadable", detail=str(exc),
                           source_attempted=src)
    if not rows:
        return unavailable("no_effect_rows", source_attempted=src)
    return fact(rows[0], source=src, observed_at=rows[0]["updated_at"],
                budget_seconds=7 * 86400.0, now=now)


# --------------------------------------------------------------------------
# IBKR direct TWS probe (readonly, unused clientId, NO reqAllOpenOrders)
# --------------------------------------------------------------------------

def tws_direct_probe(host: str, port: int, client_id: int,
                     redactor: Redactor) -> dict:
    """Read-only TWS session. connectAsync with readonly=True requests
    positions + account updates + executions ONLY (verified against the
    installed ib_async source: open/completed-order requests are gated on
    ``not readonly``, and reqAutoOpenOrders binds only for clientId 0).
    reqAllOpenOrders is NEVER called.

    ``IB.connectAsync`` only LOGS a startup request that times out
    (``ib.py``: the initializing requests are gathered with
    ``return_exceptions=True``), so ``ib.positions()`` can return an empty
    list because the request never completed rather than because the
    account is flat. An empty list from a failed request must never be
    read as FLAT, so this probe re-issues the position and execution
    requests explicitly and records whether each round trip completed in
    ``evidence_completeness``; the seat builder types the corresponding
    facts ``unavailable`` when it did not.
    """
    import asyncio

    from ib_async import IB, util  # imported lazily; probe is optional
    import ib_async

    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=25.0, readonly=True)
    try:
        def completed(factory: Callable[[], Any],
                      timeout: float = 20.0) -> bool:
            """Await one read-only request; True only on a full round trip."""
            try:
                util.run(asyncio.wait_for(factory(), timeout))
                return True
            except Exception:
                return False

        accounts = list(ib.managedAccounts())
        payload: dict[str, Any] = {
            "endpoint": f"tws://{host}:{port} clientId={client_id}"
                        " readonly=True",
            "server_version": ib.client.serverVersion(),
            "ib_async_version": ib_async.__version__,
            "managed_account_count": len(accounts),
        }
        payload["evidence_completeness"] = {
            "positions_request_verified": completed(ib.reqPositionsAsync),
            "executions_request_verified": completed(ib.reqExecutionsAsync),
            "account_updates_received": bool(ib.accountValues()),
        }
        if len(accounts) == 1:
            raw = accounts[0]
            payload["account_fingerprint"] = redactor.fingerprint(raw)
            payload["paper_account_prefix"] = raw.startswith("DU")
        payload["positions"] = [
            {"con_id": p.contract.conId,
             "local_symbol": p.contract.localSymbol or p.contract.symbol,
             "sec_type": p.contract.secType,
             "position": p.position, "avg_cost": p.avgCost}
            for p in ib.positions()
        ]
        payload["portfolio"] = [
            {"con_id": item.contract.conId,
             "local_symbol": (item.contract.localSymbol
                              or item.contract.symbol),
             "position": item.position,
             "market_value": item.marketValue,
             "unrealized_pnl": item.unrealizedPNL}
            for item in ib.portfolio()
        ]
        values = {v.tag: v.value for v in ib.accountValues()
                  if v.tag in ("NetLiquidation", "TotalCashValue",
                               "AvailableFunds", "InitMarginReq")
                  and v.currency in ("USD", "BASE", "")}
        payload["account_values"] = values
        payload["fills_today"] = [
            {"exec_id": f.execution.execId,
             "order_id": f.execution.orderId,
             "side": f.execution.side,
             "shares": f.execution.shares,
             "price": f.execution.price,
             "time": str(f.execution.time)}
            for f in ib.fills()
        ]
        return payload
    finally:
        ib.disconnect()


def typed_tws_facts(probe: dict | None, probe_error: str, tws_src: str,
                    expected_fingerprint: str, now: datetime) -> dict:
    """Pure: raw TWS probe payload -> typed facts (socket-free, testable).

    A request that did not complete yields a typed ``unavailable`` rather
    than an empty list: an empty ``positions()`` after a timed-out
    ``reqPositions`` is NOT evidence of a flat account.
    """
    if probe is None:
        gap = unavailable("tws_probe_unavailable", detail=probe_error,
                          source_attempted=tws_src)
        return {"positions": gap, "portfolio": gap, "fills_today": gap,
                "account": gap}
    complete = probe.get("evidence_completeness") or {}

    def gated(key: str, flag: str, reason: str, source: str,
              note: str | None = None) -> dict:
        if not complete.get(flag):
            return unavailable(
                reason, detail=("the request did not complete; an empty"
                                " result is not evidence of absence"),
                source_attempted=source)
        return fact(probe.get(key), source=source, observed_at=iso(now),
                    budget_seconds=VENUE_PROBE_BUDGET_S, now=now, note=note)

    return {
        "positions": gated("positions", "positions_request_verified",
                           "positions_request_incomplete",
                           f"{tws_src} positions()"),
        "portfolio": gated("portfolio", "account_updates_received",
                           "account_updates_incomplete",
                           f"{tws_src} portfolio()"),
        "fills_today": gated("fills_today", "executions_request_verified",
                             "executions_request_incomplete",
                             f"{tws_src} fills()/reqExecutions",
                             note="current-day executions only"),
        "account": fact(
            {"fingerprint_redacted": probe.get("account_fingerprint"),
             "matches_expected": (probe.get("account_fingerprint")
                                  == expected_fingerprint),
             "paper_account_prefix": probe.get("paper_account_prefix"),
             "managed_account_count": probe.get("managed_account_count"),
             "account_values": probe.get("account_values"),
             "evidence_completeness": complete,
             "server_version": probe.get("server_version")},
            source=tws_src, observed_at=iso(now),
            budget_seconds=VENUE_PROBE_BUDGET_S, now=now),
    }


def ibkr_stuck_order_from_ledger(ledger: Path, now: datetime) -> dict:
    """Identify the runner-tracked open order from the runner's OWN
    ledger (direct runner facts, client-scoped by construction)."""
    src = f"direct_runner_ledger:{ledger}#l1_broker_facts"
    try:
        legs = sqlite_rows(
            ledger,
            "SELECT recorded_at,fact_json FROM l1_broker_facts"
            " WHERE fact_kind='call_result' AND effect_id="
            " (SELECT effect_id FROM l1_effects WHERE state NOT LIKE"
            "  'terminal%' ORDER BY created_at DESC LIMIT 1)"
            " ORDER BY seq")
        cancels = sqlite_rows(
            ledger,
            "SELECT json_extract(fact_json,'$.orderId') AS order_id,"
            " COUNT(*) AS attempts, MIN(recorded_at) AS first_attempt,"
            " MAX(recorded_at) AS last_attempt"
            " FROM l1_broker_facts"
            " WHERE fact_kind='protective_exit_cancel_attempt'"
            " GROUP BY order_id ORDER BY attempts DESC")
        last_results = sqlite_rows(
            ledger,
            "SELECT json_extract(fact_json,'$.orderId') AS order_id,"
            " json_extract(fact_json,'$.status') AS status,"
            " MAX(recorded_at) AS at FROM l1_broker_facts"
            " WHERE fact_kind='protective_exit_cancel_result'"
            " GROUP BY order_id")
    except sqlite3.Error as exc:
        return unavailable("ledger_unreadable", detail=str(exc),
                           source_attempted=src)
    leg_map = {}
    for row in legs:
        body = json.loads(row["fact_json"])
        leg_map[body.get("orderId")] = {
            "leg": body.get("leg"), "action": body.get("action"),
            "order_type": body.get("orderType"),
            "lmt_price": body.get("lmtPrice"),
            "aux_price": body.get("auxPrice"), "tif": body.get("tif"),
            "quantity": body.get("totalQuantity"),
        }
    if not cancels:
        return unavailable("no_protective_cancel_history",
                           source_attempted=src)
    stuck = max(cancels, key=lambda row: row["attempts"])
    order_id = stuck["order_id"]
    status = next((r for r in last_results
                   if r["order_id"] == order_id), None)
    value = {
        "order_id": order_id,
        "leg_identity": leg_map.get(order_id),
        "cancel_attempts": stuck["attempts"],
        "first_cancel_attempt": stuck["first_attempt"],
        "last_cancel_attempt": stuck["last_attempt"],
        "last_runner_observed_cancel_status":
            (status or {}).get("status"),
        "last_runner_observed_at": (status or {}).get("at"),
    }
    return fact(value, source=src,
                observed_at=stuck["last_attempt"],
                budget_seconds=7 * 86400.0, now=now,
                note="runner-tracked; venue-current status is typed"
                     " unavailable separately")


# --------------------------------------------------------------------------
# seat builders
# --------------------------------------------------------------------------

def identity_block(config: dict, heartbeat: dict | None, hb_path: str,
                   timeframe: str, code_rev: str | None, code_src: str,
                   now: datetime) -> dict:
    return {
        "symbol": heartbeat_fact(heartbeat, hb_path, "instrument", now),
        "timeframe": fact(timeframe,
                          source=f"direct_file:{config['_path']}"),
        "model_id": heartbeat_fact(heartbeat, hb_path, "model_id", now),
        "artifact_sha256": heartbeat_fact(heartbeat, hb_path,
                                          "artifact_sha256", now),
        "config_sha256": heartbeat_fact(heartbeat, hb_path,
                                        "config_sha256", now),
        "code_revision": (fact(code_rev, source=code_src, now=now)
                          if code_rev else
                          unavailable("git_head_unreadable",
                                      source_attempted=code_src)),
        "strategy": fact(config.get("strategy"),
                         source=f"direct_file:{config['_path']}", now=now),
        "execution_tier": fact(
            (config.get("model") or {}).get("execution_tier"),
            source=f"direct_file:{config['_path']}", now=now),
    }


def account_block(heartbeat: dict | None, hb_path: str,
                  expected_fp: str | None, environment: str,
                  now: datetime, direct_fp: dict | None = None) -> dict:
    hb_fp = heartbeat_fact(heartbeat, hb_path, "account_fingerprint", now)
    if is_unavailable(hb_fp):
        match = unavailable("heartbeat_fingerprint_absent",
                            source_attempted=hb_path)
    elif not expected_fp:
        match = unavailable(
            "expected_binding_unknown",
            detail="the deployed runner config declares no expected"
                   " account binding; a mismatch is never assumed",
            source_attempted=hb_path)
    else:
        match = fact(hb_fp.get("value") == expected_fp,
                     source=hb_fp.get("source", hb_path), now=now,
                     note="equality against the deployed runner config;"
                          " neither identifier is emitted")
    block = {
        "fingerprint_redacted": hb_fp,
        "fingerprint_matches_expected": match,
        "environment_class": heartbeat_fact(
            heartbeat, hb_path, "environment", now,
            note=f"expected {environment}"),
        "binding_verified_by_runner": heartbeat_fact(
            heartbeat, hb_path, "account_binding_verified", now),
        "write_enabled": _write_enabled_fact(heartbeat, hb_path, now),
    }
    if direct_fp is not None:
        block["direct_venue_fingerprint_matches"] = direct_fp
    return block


def _write_enabled_fact(heartbeat: dict | None, hb_path: str,
                        now: datetime) -> dict:
    entry = heartbeat_fact(heartbeat, hb_path, "read_only", now)
    if is_unavailable(entry):
        return entry
    entry["value"] = not entry["value"]
    entry["note"] = "negation of the runner heartbeat read_only flag"
    return entry


def normalize_bar_ts(value: Any) -> str:
    """Normalize an ISO-8601 UTC timestamp for identity comparison only
    (explicit rule: trailing 'Z' means '+00:00'; same instant)."""
    text = str(value)
    return text[:-1] + "+00:00" if text.endswith("Z") else text


def build_alpaca_seat(now: datetime, redactor: Redactor,
                      skip_rest: bool) -> dict:
    hb_path = str(STATE / "alpaca-model-runner-heartbeat.json")
    heartbeat = read_json_file(Path(hb_path))
    config_path = Path.home() / (
        "Documents/GitHub/lts/examples/configs/alpaca_spy_model_runner_v1"
        ".json")
    config = read_json_file(config_path) or {}
    config["_path"] = str(config_path)
    ledger = STATE / "alpaca-model-execution.sqlite"
    manifest_dir = MANIFEST_ROOT / "spy_daily_linear_v1"
    manifest = read_json_file(manifest_dir / "manifest.json")
    exp = EXPECTED["alpaca_paper"]
    expected_fp = expected_account_fingerprint("alpaca_paper")

    # direct venue probe (read-only REST GETs on the paper endpoint)
    venue: dict[str, Any]
    direct_fp_fact: dict | None = None
    env_file = Path.home() / ".config/lts/alpaca-paper.env"
    if skip_rest:
        venue = {"probe": unavailable(
            "probe_skipped_by_flag", source_attempted="alpaca_rest")}
    else:
        try:
            env = parse_env_file(env_file)
            base = env.get("ALPACA_PAPER_BASE_URL",
                           "https://paper-api.alpaca.markets").rstrip("/")
            headers = {
                "APCA-API-KEY-ID": env["ALPACA_PAPER_API_KEY_ID"],
                "APCA-API-SECRET-KEY": env["ALPACA_PAPER_API_SECRET_KEY"],
            }
            redactor.register(env.get("ALPACA_PAPER_API_KEY_ID"))
            redactor.register(env.get("ALPACA_PAPER_API_SECRET_KEY"))
            account = http_get_json(f"{base}/v2/account", headers)
            positions = http_get_json(f"{base}/v2/positions", headers)
            orders = http_get_json(
                f"{base}/v2/orders?status=open&nested=true", headers)
            recent = http_get_json(
                f"{base}/v2/orders?status=all&limit=5&nested=true",
                headers)
            identity = str(account.get("id")
                           or account.get("account_number") or "")
            redactor.register(account.get("account_number"))
            fp = redactor.fingerprint(identity) if identity else None
            src = f"direct_venue:{base}/v2"
            keep_order = ("side", "type", "order_class", "status", "qty",
                          "filled_qty", "limit_price", "stop_price",
                          "symbol", "submitted_at", "time_in_force")
            def type_orders(raw_orders):
                typed = []
                for order in raw_orders:
                    entry = {k: order.get(k) for k in keep_order}
                    entry["legs"] = [
                        {k: leg.get(k) for k in keep_order}
                        for leg in (order.get("legs") or [])]
                    typed.append(entry)
                return typed

            typed_orders = type_orders(orders)
            typed_recent = type_orders(recent)
            protection = [
                {"symbol": order.get("symbol"), "type": order.get("type"),
                 "side": order.get("side"), "status": order.get("status"),
                 "limit_price": order.get("limit_price"),
                 "stop_price": order.get("stop_price"),
                 "time_in_force": order.get("time_in_force")}
                for order in typed_orders + [
                    leg for order in typed_recent
                    for leg in order["legs"]]
                if order.get("type") in ("limit", "stop", "stop_limit")
                and order.get("status") in ("new", "held", "accepted",
                                            "open")]
            venue = {
                "endpoint_class": fact(
                    "paper" if "paper-api" in base else "UNEXPECTED",
                    source=src, now=now),
                "account_status": fact(
                    {"status": account.get("status"),
                     "trading_blocked": account.get("trading_blocked"),
                     "account_blocked": account.get("account_blocked"),
                     "equity": account.get("equity"),
                     "currency": account.get("currency")},
                    source=f"{src}/account", observed_at=iso(now),
                    budget_seconds=VENUE_PROBE_BUDGET_S, now=now),
                "positions": fact(
                    [{k: p.get(k) for k in
                      ("symbol", "qty", "side", "avg_entry_price",
                       "market_value", "unrealized_pl")}
                     for p in positions],
                    source=f"{src}/positions", observed_at=iso(now),
                    budget_seconds=VENUE_PROBE_BUDGET_S, now=now),
                "open_orders": fact(
                    typed_orders, source=f"{src}/orders?status=open"
                    "&nested=true", observed_at=iso(now),
                    budget_seconds=VENUE_PROBE_BUDGET_S, now=now),
                "recent_orders_with_legs": fact(
                    typed_recent, source=f"{src}/orders?status=all"
                    "&limit=5&nested=true", observed_at=iso(now),
                    budget_seconds=VENUE_PROBE_BUDGET_S, now=now,
                    note="bracket legs are the native SL/TP evidence"),
                "native_protection_evidence": fact(
                    protection, source=f"{src}/orders (open + recent"
                    " bracket legs)", observed_at=iso(now),
                    budget_seconds=VENUE_PROBE_BUDGET_S, now=now,
                    note="working protective limit/stop orders at the"
                         " venue"),
            }
            direct_fp_fact = (
                fact(fp == expected_fp, source=f"{src}/account", now=now,
                     note="venue account identity vs the deployed runner"
                          " config binding; neither value is emitted")
                if expected_fp and fp else
                unavailable("expected_binding_unknown",
                            source_attempted=f"{src}/account"))
        except KeyError as exc:
            venue = {"probe": unavailable(
                "credentials_absent", detail=str(exc),
                source_attempted=str(env_file))}
        except Exception as exc:
            venue = {"probe": unavailable(
                "venue_unreachable", detail=str(exc)[:200],
                source_attempted="alpaca paper REST /v2")}

    code_rev = git_head(Path.home() / "Documents/GitHub/lts")
    seat = {
        "venue": fact("alpaca_paper", source=f"direct_runner:{hb_path}"),
        "account": account_block(heartbeat, hb_path, expected_fp,
                                 "paper", now, direct_fp_fact),
        "identity": identity_block(config, heartbeat, hb_path, "1d",
                                   code_rev,
                                   "git:~/Documents/GitHub/lts (deployed"
                                   " WorkingDirectory of"
                                   " lts-alpaca-model-runner.service)",
                                   now),
        "model_artifact_join": manifest_join(
            heartbeat=heartbeat, manifest=manifest,
            manifest_path=str(manifest_dir / "manifest.json"),
            manifest_bytes_sha256=_safe_sha(manifest_dir / "manifest.json"),
            artifact_recomputed_sha256=_safe_sha(
                Path(manifest.get("artifact_file", ""))
                if manifest else None),
            now=now),
        "bars_and_decisions": decision_block(
            ledger, exp["model_id"], "1d", now),
        "broker_state": {
            "heartbeat_positions": heartbeat_fact(
                heartbeat, hb_path, "positions", now),
            "heartbeat_orders": heartbeat_fact(
                heartbeat, hb_path, "orders", now),
            "open_exposure": open_exposure_fact(ledger, now),
            "latest_effect": latest_effect_fact(ledger, now),
            "direct_venue": venue,
        },
        "control_state": control_block(ledger, now),
        "runner_state": heartbeat_fact(heartbeat, hb_path, "state", now),
    }
    return seat


def _safe_sha(path: Path | None) -> str | None:
    try:
        if path and path.is_file():
            return file_sha256(path)
    except Exception:
        pass
    return None


def build_ibkr_seat(now: datetime, redactor: Redactor,
                    skip_tws: bool, client_id: int) -> tuple[dict, dict]:
    hb_path = str(STATE / "ibkr-model-runner-heartbeat.json")
    heartbeat = read_json_file(Path(hb_path))
    config_path = Path.home() / (
        "Documents/GitHub/lts/examples/configs/ibkr_usdcad_model_runner_v1"
        ".json")
    config = read_json_file(config_path) or {}
    config["_path"] = str(config_path)
    profile = read_json_file(Path.home() / (
        "Documents/GitHub/lts/examples/configs/"
        "ibkr_usdcad_model_profile_v1.json")) or {}
    ledger = STATE / "ibkr-model-execution.sqlite"
    manifest_dir = MANIFEST_ROOT / "usdcad_4h_linear_v1"
    manifest = read_json_file(manifest_dir / "manifest.json")
    exp = EXPECTED["ibkr_paper"]
    expected_fp = expected_account_fingerprint("ibkr_paper")

    # --- direct TWS probe (readonly, unused clientId, no order requests)
    tws_src = (f"direct_venue:tws://{profile.get('host')}:"
               f"{profile.get('port')} clientId={client_id} readonly")
    if skip_tws:
        probe: dict | None = None
        probe_error = "probe_skipped_by_flag"
    else:
        try:
            probe = tws_direct_probe(profile.get("host", "127.0.0.1"),
                                     int(profile.get("port", 7497)),
                                     client_id, redactor)
            probe_error = ""
        except Exception as exc:
            probe = None
            probe_error = f"{type(exc).__name__}: {exc}"[:200]

    typed_probe = typed_tws_facts(probe, probe_error, tws_src,
                                  expected_fp, now)
    direct_positions = typed_probe["positions"]
    direct_portfolio = typed_probe["portfolio"]
    direct_fills = typed_probe["fills_today"]
    direct_account = typed_probe["account"]

    venue_open_orders = unavailable(
        "reqAllOpenOrders_rebinding_risk",
        detail=("the venue-scoped open-order set is only observable via"
                " reqAllOpenOrders, which can rebind order ownership away"
                " from the runner's clientId"
                f" {profile.get('client_id')}; a fresh read-only clientId"
                " receives no orders bound to other clients, so the set"
                " is typed unavailable instead of imputed"),
        source_attempted=tws_src)

    stuck_order = ibkr_stuck_order_from_ledger(ledger, now)
    hb_position = heartbeat_fact(heartbeat, hb_path, "position", now)
    hb_orders = heartbeat_fact(heartbeat, hb_path, "orders", now)
    open_exposure = open_exposure_fact(ledger, now)

    resolution = resolve_ibkr_order_exposure(
        direct_positions=direct_positions,
        direct_portfolio=direct_portfolio,
        heartbeat_position=hb_position,
        heartbeat_orders=hb_orders,
        ledger_open_exposure=open_exposure,
        stuck_order=stuck_order,
        venue_open_orders=venue_open_orders)

    # reconciliation trail (runner ledger, typed)
    src = f"direct_runner_ledger:{ledger}#l1_broker_facts"
    try:
        pending = sqlite_rows(
            ledger, "SELECT recorded_at,fact_json FROM l1_broker_facts"
                    " WHERE fact_kind='position_reconciliation_pending'"
                    " ORDER BY seq DESC LIMIT 1")
        last_reconciled = sqlite_rows(
            ledger, "SELECT MAX(recorded_at) AS at FROM l1_broker_facts"
                    " WHERE fact_kind='position_reconciled'")
        first_flat = sqlite_rows(
            ledger, "SELECT MIN(recorded_at) AS at FROM l1_broker_facts"
                    " WHERE fact_kind='flat_convergence_sample'")
        reconciliation = fact(
            {"latest_pending": (
                {"recorded_at": pending[0]["recorded_at"],
                 **json.loads(pending[0]["fact_json"])}
                if pending else None),
             "last_position_reconciled_at":
                 (last_reconciled[0]["at"] if last_reconciled else None),
             "first_flat_convergence_at":
                 (first_flat[0]["at"] if first_flat else None)},
            source=src,
            observed_at=pending[0]["recorded_at"] if pending else None,
            budget_seconds=HEARTBEAT_BUDGET_S, now=now)
    except sqlite3.Error as exc:
        reconciliation = unavailable("ledger_unreadable", detail=str(exc),
                                     source_attempted=src)

    code_rev = git_head(Path.home() / "Documents/GitHub/lts")
    seat = {
        "venue": fact("ibkr_paper", source=f"direct_runner:{hb_path}"),
        "account": account_block(heartbeat, hb_path, expected_fp,
                                 "paper", now),
        "identity": identity_block(config, heartbeat, hb_path, "4h",
                                   code_rev,
                                   "git:~/Documents/GitHub/lts (deployed"
                                   " WorkingDirectory of"
                                   " lts-ibkr-model-runner.service)",
                                   now),
        "model_artifact_join": manifest_join(
            heartbeat=heartbeat, manifest=manifest,
            manifest_path=str(manifest_dir / "manifest.json"),
            manifest_bytes_sha256=_safe_sha(manifest_dir / "manifest.json"),
            artifact_recomputed_sha256=_safe_sha(
                Path(manifest.get("artifact_file", ""))
                if manifest else None),
            now=now),
        "bars_and_decisions": decision_block(
            ledger, exp["model_id"], "4h", now),
        "broker_state": {
            "heartbeat_position": hb_position,
            "heartbeat_orders": hb_orders,
            "open_exposure": open_exposure,
            "latest_effect": latest_effect_fact(ledger, now),
            "direct_venue": {
                "account": direct_account,
                "positions": direct_positions,
                "portfolio": direct_portfolio,
                "fills_today": direct_fills,
                "open_orders_venue_scoped": venue_open_orders,
            },
            "runner_tracked_open_order": stuck_order,
            "native_protection_evidence": fact(
                "bracket exits are runner-placed native GTC orders (see"
                " runner_tracked_open_order leg_identity and"
                " l1_effects order_ids_json)",
                source=f"direct_runner_ledger:{ledger}#l1_broker_facts",
                now=now),
        },
        "control_state": {**control_block(ledger, now),
                          "reconciliation": reconciliation},
        "runner_state": heartbeat_fact(heartbeat, hb_path, "state", now),
    }
    return seat, resolution


DRAGON_COLLECT_SCRIPT = r"""
import hashlib, json, os, sqlite3, subprocess
from datetime import datetime, timezone

out = {"host": "dragon",
       "collected_at": datetime.now(timezone.utc).isoformat()}
home = os.path.expanduser("~")
hb_path = home + "/.local/state/lts/mt5-model-runner-heartbeat.json"
try:
    with open(hb_path) as fh:
        out["heartbeat_file"] = json.load(fh)
    out["heartbeat_path"] = hb_path
except Exception as exc:
    out["heartbeat_error"] = str(exc)

db = home + "/.local/state/lts/mt5-bridge.sqlite"
try:
    con = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    bridge = {"path": db}
    row = con.execute(
        "SELECT account_fingerprint,server_fingerprint,adapter_version,"
        "environment,connected,trade_allowed,terminal_build,"
        "terminal_ping_ms,terminal_observed_at,received_at"
        " FROM heartbeats ORDER BY id DESC LIMIT 1").fetchone()
    bridge["heartbeat_row"] = dict(row) if row else None
    row = con.execute(
        "SELECT id,account_fingerprint,terminal_observed_at,received_at,"
        "currency,balance,equity,margin,free_margin,positions_total,"
        "orders_total,symbols_total FROM account_snapshots"
        " ORDER BY id DESC LIMIT 1").fetchone()
    bridge["account_snapshot"] = dict(row) if row else None
    if row:
        bridge["position_rows"] = [
            dict(r) for r in con.execute(
                "SELECT ticket,symbol,side,volume,price_open,stop_loss,"
                "take_profit,profit FROM position_snapshots"
                " WHERE snapshot_id=?", (row["id"],))]
        bridge["order_rows"] = [
            dict(r) for r in con.execute(
                "SELECT ticket,symbol,order_type,volume,price_open,"
                "stop_loss,take_profit,state FROM order_snapshots"
                " WHERE snapshot_id=?", (row["id"],))]
    row = con.execute(
        "SELECT bar_time,open,high,low,close,volume FROM bar_snapshots"
        " WHERE symbol='ETHUSD' AND timeframe='4h'"
        " ORDER BY bar_time DESC LIMIT 1").fetchone()
    bridge["latest_bar"] = dict(row) if row else None
    row = con.execute(
        "SELECT bar_close,decided_at,action,outcome,reason,decision_id,"
        "model_id,artifact_sha256,input_sha256 FROM due_bar_decisions"
        " ORDER BY bar_close DESC LIMIT 1").fetchone()
    bridge["due_bar_decision"] = dict(row) if row else None
    row = con.execute(
        "SELECT observed_at,last_closed_bar,action,probability_up,"
        "input_sha256 FROM live_model_inferences"
        " ORDER BY observed_at DESC LIMIT 1").fetchone()
    bridge["latest_inference"] = dict(row) if row else None
    row = con.execute(
        "SELECT session_id,venue,account_fingerprint,symbol,model_id,"
        "artifact_sha256,config_sha256,started_at,state"
        " FROM live_model_sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    bridge["live_model_session"] = dict(row) if row else None
    bridge["service_state_rows"] = [
        dict(r) for r in con.execute("SELECT key,value FROM service_state")]
    con.close()
    out["bridge"] = bridge
except Exception as exc:
    out["bridge_error"] = str(exc)

manifest_path = (home + "/.local/share/prediction-provider/live/"
                 "ethusdt_4h_linear_v1/manifest.json")
try:
    with open(manifest_path, "rb") as fh:
        raw = fh.read()
    out["manifest_bytes_sha256"] = hashlib.sha256(raw).hexdigest()
    manifest = json.loads(raw)
    out["manifest"] = {k: manifest.get(k) for k in (
        "schema", "model_id", "artifact_sha256", "config_sha256",
        "artifact_file", "live_inference_eligible",
        "live_execution_eligible", "research_validated", "timeframe")}
    out["manifest_path"] = manifest_path
    artifact = os.path.expanduser(manifest.get("artifact_file", ""))
    with open(artifact, "rb") as fh:
        out["artifact_recomputed_sha256"] = hashlib.sha256(
            fh.read()).hexdigest()
except Exception as exc:
    out["manifest_error"] = str(exc)

try:
    out["code_revision"] = subprocess.run(
        ["git", "-C", home + "/Documents/GitHub/lts", "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10).stdout.strip()
except Exception as exc:
    out["code_revision_error"] = str(exc)

print(json.dumps(out, default=str))
"""


def build_mt5_seat(now: datetime, redactor: Redactor, skip_dragon: bool,
                   audit_dir: Path) -> dict:
    """Fleet-readable DIRECT MT5 evidence (F-P1-03 closure): pull dragon's
    runner heartbeat + direct bridge rows over BatchMode ssh and persist
    them on an omega-readable audit path."""
    exp = EXPECTED["mt5_demo"]
    expected_fp = expected_account_fingerprint("mt5_demo")
    ssh_src = ("direct_runner:ssh://dragon"
               "/~/.local/state/lts/mt5-model-runner-heartbeat.json")
    bridge_src = ("direct_runner_ledger:ssh://dragon"
                  "/~/.local/state/lts/mt5-bridge.sqlite")

    if skip_dragon:
        payload, error = None, "probe_skipped_by_flag"
    else:
        payload, error = ssh_python_json("dragon", DRAGON_COLLECT_SCRIPT)

    if payload is None:
        gap = unavailable("dragon_unreachable", detail=error,
                          source_attempted=ssh_src)
        return {
            "venue": fact("mt5_demo", source="expected_registry",
                          note="seat registry entry; live facts below are"
                               " unavailable"),
            "account": {"fingerprint_redacted": gap},
            "identity": {"symbol": gap},
            "model_artifact_join": {"join": gap},
            "bars_and_decisions": {"last_closed_input_bar": gap},
            "broker_state": {"direct_venue": {"probe": gap}},
            "control_state": {"halt": gap},
            "fleet_readable_evidence": gap,
        }

    # persist the direct evidence bundle in the PRIVATE local store only
    # (0700 dir / 0600 files, verified); it carries account and position
    # detail and must never reach a public repository.
    secure_mkdir(audit_dir)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    evidence_path = audit_dir / f"mt5_direct_evidence_{stamp}.json"
    bundle = {
        "schema": "lts.mt5.direct_evidence.v1",
        "source_label": "direct-source (dragon runner heartbeat + "
                        "mt5-bridge.sqlite ro rows); consolidated"
                        " watchdog NOT consulted",
        "transport": "ssh -o BatchMode=yes dragon python3 - "
                     "(sqlite file:...?mode=ro URI)",
        "collected_at": iso(now),
        "payload": payload,
    }
    text = json.dumps(bundle, indent=1, sort_keys=True, default=str)
    redactor.assert_clean(text)
    secure_write_text(evidence_path, text)
    secure_write_text(audit_dir / "latest.json", text)

    heartbeat = payload.get("heartbeat_file")
    hb_path = "ssh://dragon" + (payload.get("heartbeat_path")
                                or "~/.local/state/lts/"
                                   "mt5-model-runner-heartbeat.json")
    bridge = payload.get("bridge") or {}
    bridge_hb = bridge.get("heartbeat_row")
    snapshot = bridge.get("account_snapshot")

    def bridge_fact(value: Any, table: str, observed: str | None) -> dict:
        return fact(value, source=f"{bridge_src}#{table}",
                    observed_at=observed,
                    budget_seconds=BRIDGE_SNAPSHOT_BUDGET_S, now=now)

    seat: dict[str, Any] = {
        "venue": heartbeat_fact(heartbeat, hb_path, "venue", now),
        "account": {
            "fingerprint_redacted": heartbeat_fact(
                heartbeat, hb_path, "account_fingerprint", now),
            "fingerprint_matches_expected": (
                fact((heartbeat or {}).get("account_fingerprint")
                     == expected_fp, source=hb_path, now=now,
                     note="equality against the deployed runner config;"
                          " neither identifier is emitted")
                if expected_fp and (heartbeat or {}).get(
                    "account_fingerprint") else
                unavailable("expected_binding_unknown",
                            source_attempted=hb_path)),
            "bridge_fingerprint_matches_heartbeat": (
                bridge_fact(
                    bridge_hb.get("account_fingerprint")
                    == (heartbeat or {}).get("account_fingerprint"),
                    "heartbeats", bridge_hb.get("received_at"))
                if bridge_hb else unavailable(
                    "no_bridge_heartbeat_rows",
                    source_attempted=f"{bridge_src}#heartbeats")),
            "environment_class": (
                bridge_fact(bridge_hb.get("environment"), "heartbeats",
                            bridge_hb.get("received_at"))
                if bridge_hb else unavailable(
                    "no_bridge_heartbeat_rows",
                    source_attempted=f"{bridge_src}#heartbeats")),
            "write_enabled": (
                bridge_fact({"connected": bridge_hb.get("connected"),
                             "trade_allowed": bridge_hb.get(
                                 "trade_allowed")},
                            "heartbeats", bridge_hb.get("received_at"))
                if bridge_hb else unavailable(
                    "no_bridge_heartbeat_rows",
                    source_attempted=f"{bridge_src}#heartbeats")),
        },
        "identity": {
            "symbol": heartbeat_fact(heartbeat, hb_path, "instrument",
                                     now),
            "timeframe": fact("4h", source="ssh://dragon/~/Documents/"
                              "GitHub/lts/examples/configs/"
                              "mt5_eth_model_runner_v1.json"),
            "model_id": heartbeat_fact(heartbeat, hb_path, "model_id",
                                       now),
            "artifact_sha256": heartbeat_fact(
                heartbeat, hb_path, "artifact_sha256", now),
            "config_sha256": heartbeat_fact(
                heartbeat, hb_path, "config_sha256", now),
            "code_revision": (
                fact(payload["code_revision"],
                     source="ssh://dragon git:~/Documents/GitHub/lts",
                     now=now)
                if payload.get("code_revision") else
                unavailable("git_head_unreadable",
                            detail=payload.get("code_revision_error"),
                            source_attempted="ssh://dragon git")),
            "adapter_version": (
                bridge_fact(bridge_hb.get("adapter_version"),
                            "heartbeats", bridge_hb.get("received_at"))
                if bridge_hb else unavailable(
                    "no_bridge_heartbeat_rows",
                    source_attempted=f"{bridge_src}#heartbeats")),
        },
        "model_artifact_join": manifest_join(
            heartbeat=heartbeat, manifest=payload.get("manifest"),
            manifest_path="ssh://dragon"
                          + (payload.get("manifest_path") or ""),
            manifest_bytes_sha256=payload.get("manifest_bytes_sha256"),
            artifact_recomputed_sha256=payload.get(
                "artifact_recomputed_sha256"),
            now=now),
        "bars_and_decisions": {},
        "broker_state": {
            "heartbeat_positions": heartbeat_fact(
                heartbeat, hb_path, "positions", now),
            "heartbeat_orders": heartbeat_fact(
                heartbeat, hb_path, "orders", now),
            "account_snapshot": (
                bridge_fact({k: snapshot.get(k) for k in
                             ("terminal_observed_at", "received_at",
                              "currency", "balance", "equity", "margin",
                              "free_margin", "positions_total",
                              "orders_total")},
                            "account_snapshots",
                            snapshot.get("received_at"))
                if snapshot else unavailable(
                    "no_account_snapshots",
                    source_attempted=f"{bridge_src}#account_snapshots")),
            "positions": (
                bridge_fact(bridge.get("position_rows"),
                            "position_snapshots",
                            snapshot.get("received_at"))
                if snapshot is not None else unavailable(
                    "no_account_snapshots",
                    source_attempted=f"{bridge_src}#position_snapshots")),
            "pending_orders": (
                bridge_fact(bridge.get("order_rows"), "order_snapshots",
                            snapshot.get("received_at"))
                if snapshot is not None else unavailable(
                    "no_account_snapshots",
                    source_attempted=f"{bridge_src}#order_snapshots")),
            "native_protection_evidence": (
                bridge_fact(
                    [{"ticket": p.get("ticket"),
                      "stop_loss": p.get("stop_loss"),
                      "take_profit": p.get("take_profit")}
                     for p in (bridge.get("position_rows") or [])],
                    "position_snapshots", snapshot.get("received_at"))
                if snapshot is not None else unavailable(
                    "no_account_snapshots",
                    source_attempted=f"{bridge_src}#position_snapshots")),
        },
        "control_state": {
            "halt": (fact(
                dict((r["key"], r["value"])
                     for r in bridge.get("service_state_rows") or [])
                .get("halt", "no_halt_row_recorded"),
                source=f"{bridge_src}#service_state", now=now)
                if bridge.get("service_state_rows") is not None else
                unavailable("service_state_unreadable",
                            source_attempted=f"{bridge_src}"
                                             "#service_state")),
            "kill_switch_trade_allowed": (
                bridge_fact(bridge_hb.get("trade_allowed"), "heartbeats",
                            bridge_hb.get("received_at"))
                if bridge_hb else unavailable(
                    "no_bridge_heartbeat_rows",
                    source_attempted=f"{bridge_src}#heartbeats")),
        },
        "runner_state": heartbeat_fact(heartbeat, hb_path, "state", now),
        "fleet_readable_evidence": fact(
            str(evidence_path), source=bridge_src, now=now,
            note="private 0600 direct-source bundle in the local evidence"
                 " store; latest.json alongside; never committed"),
    }
    bars = seat["bars_and_decisions"]
    latest_bar = bridge.get("latest_bar")
    inference = bridge.get("latest_inference")
    decision = bridge.get("due_bar_decision")
    tf_budget = 2 * TIMEFRAME_SECONDS["4h"]
    if latest_bar:
        bars["last_closed_input_bar"] = fact(
            latest_bar, source=f"{bridge_src}#bar_snapshots",
            observed_at=latest_bar.get("bar_time"),
            budget_seconds=tf_budget, now=now,
            note="latest ETHUSD 4h bar row in the bridge store")
    else:
        bars["last_closed_input_bar"] = unavailable(
            "no_bar_rows", source_attempted=f"{bridge_src}#bar_snapshots")
    if inference:
        bars["runner_last_closed_bar"] = fact(
            inference, source=f"{bridge_src}#live_model_inferences",
            observed_at=inference.get("observed_at"),
            budget_seconds=tf_budget, now=now)
        bars["due_decision_identity"] = fact(
            f"{exp['model_id']}:{inference.get('last_closed_bar')}",
            source=f"{bridge_src}#live_model_inferences", now=now,
            note="derived as model_id:last_closed_bar")
    else:
        bars["runner_last_closed_bar"] = unavailable(
            "no_inference_rows",
            source_attempted=f"{bridge_src}#live_model_inferences")
        bars["due_decision_identity"] = unavailable(
            "no_inference_rows",
            source_attempted=f"{bridge_src}#live_model_inferences")
    if decision:
        bars["last_recorded_decision"] = fact(
            {k: decision.get(k) for k in
             ("decision_id", "bar_close", "action", "outcome", "reason")},
            source=f"{bridge_src}#due_bar_decisions",
            observed_at=decision.get("decided_at"),
            budget_seconds=tf_budget, now=now)
        if inference:
            expected_id = normalize_bar_ts(
                f"{exp['model_id']}:{inference.get('last_closed_bar')}")
            bars["decision_current_for_last_closed_bar"] = fact(
                normalize_bar_ts(decision.get("decision_id") or "")
                == expected_id,
                source=f"{bridge_src}#due_bar_decisions", now=now,
                note="Z suffix normalized to +00:00 for identity"
                     " comparison")
    else:
        bars["last_recorded_decision"] = unavailable(
            "no_decision_rows",
            source_attempted=f"{bridge_src}#due_bar_decisions")
    return seat


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _cell(entry: Any) -> str:
    if is_unavailable(entry):
        return f"UNAVAILABLE({entry['unavailable']})"
    if isinstance(entry, dict) and "value" in entry:
        value = entry["value"]
        suffix = ""
        if "fresh" in entry:
            suffix = " [fresh]" if entry["fresh"] else (
                f" [STALE {round((entry.get('age_seconds') or 0)/3600, 1)}h]")
        if isinstance(value, (dict, list)):
            return json.dumps(value, default=str)[:76] + suffix
        return f"{value}{suffix}"
    return str(entry)[:76]


def render_table(inventory: dict) -> str:
    seats = inventory["seats"]
    names = list(seats)
    rows: list[tuple[str, list[str]]] = []

    def row(label: str, getter: Callable[[dict], Any]) -> None:
        cells = []
        for name in names:
            try:
                cells.append(_cell(getter(seats[name])))
            except (KeyError, TypeError):
                cells.append("UNAVAILABLE(path_absent)")
        rows.append((label, cells))

    row("venue", lambda s: s["venue"])
    row("account fingerprint (redacted)",
        lambda s: s["account"]["fingerprint_redacted"])
    row("fingerprint matches expected",
        lambda s: s["account"]["fingerprint_matches_expected"])
    row("environment class", lambda s: s["account"]["environment_class"])
    row("write-enabled", lambda s: s["account"]["write_enabled"])
    row("symbol", lambda s: s["identity"]["symbol"])
    row("timeframe", lambda s: s["identity"]["timeframe"])
    row("model id", lambda s: s["identity"]["model_id"])
    row("artifact sha256", lambda s: s["identity"]["artifact_sha256"])
    row("config sha256", lambda s: s["identity"]["config_sha256"])
    row("code revision", lambda s: s["identity"]["code_revision"])
    row("manifest join", lambda s: s["model_artifact_join"]["join"])
    row("live-eligible (manifest)",
        lambda s: s["model_artifact_join"]["eligibility"])
    row("last closed input bar",
        lambda s: s["bars_and_decisions"]["last_closed_input_bar"])
    row("due-decision identity",
        lambda s: s["bars_and_decisions"]["due_decision_identity"])
    row("last recorded decision",
        lambda s: s["bars_and_decisions"]["last_recorded_decision"])

    def first_path(seat: dict, *paths: tuple[str, ...]) -> Any:
        for path in paths:
            node: Any = seat
            try:
                for key in path:
                    node = node[key]
                return node
            except (KeyError, TypeError):
                continue
        raise KeyError(paths)

    row("direct positions (venue)", lambda s: first_path(
        s, ("broker_state", "direct_venue", "positions"),
        ("broker_state", "positions")))
    row("direct open orders (venue)", lambda s: first_path(
        s, ("broker_state", "direct_venue", "open_orders"),
        ("broker_state", "direct_venue", "open_orders_venue_scoped"),
        ("broker_state", "pending_orders")))
    row("native SL/TP evidence", lambda s: first_path(
        s, ("broker_state", "direct_venue", "native_protection_evidence"),
        ("broker_state", "native_protection_evidence")))
    row("halt/hold state", lambda s: s["control_state"]["halt"])
    row("runner state", lambda s: s["runner_state"])

    width = max(len(label) for label, _ in rows) + 2
    lines = ["SEAT TRUTH INVENTORY (direct sources only) "
             + inventory["generated_at"],
             "=" * 100]
    header = " " * width + " | ".join(f"{name:<40}" for name in names)
    lines.append(header)
    lines.append("-" * len(header))
    for label, cells in rows:
        lines.append(f"{label:<{width}}"
                     + " | ".join(f"{cell:<40}" for cell in cells))
    resolution = inventory.get("ibkr_order_exposure_resolution")
    if resolution:
        lines += ["", "IBKR ORDER/EXPOSURE RESOLUTION: "
                  + resolution["state"],
                  "  " + resolution["explanation"]]
        for verdict in resolution["verdicts"]:
            lines.append("  * " + verdict)
        stuck = resolution.get("runner_tracked_open_order")
        if stuck and not is_unavailable(stuck):
            lines.append("  * runner-tracked open order: "
                         + json.dumps(stuck["value"], default=str))
        lines.append("  * venue-scoped open orders: "
                     + _cell(resolution["venue_scoped_open_orders"]))
    unavailable_index = inventory.get("unavailable_index") or []
    lines += ["", f"TYPED UNAVAILABLE FACTS: {len(unavailable_index)}"]
    for entry in unavailable_index:
        lines.append(f"  - {entry['path']}: {entry['reason']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def relative_to_home(path: Path) -> str:
    try:
        return "~/" + str(path.resolve().relative_to(Path.home().resolve()))
    except ValueError:
        return str(path)


def emit_public_evidence(inventory: dict, private_path: Path,
                         output_dir: Path, stamp: str,
                         redactor: "Redactor | None" = None
                         ) -> tuple[Path, Path, str, str]:
    """Derive, validate and write the committed public artifacts.

    Pure with respect to the venues: it reads the private packet's bytes
    only to bind its digest, so an auditor can re-derive the committed
    summary offline from the 0600 packet
    (``--rebuild-public-from``) and compare bytes.
    """
    private_bytes = private_path.read_bytes()
    packet_reference = {
        "sha256": hashlib.sha256(private_bytes).hexdigest(),
        "byte_length": len(private_bytes),
        "path": relative_to_home(private_path),
        # verify_private_mode raises unless the packet really is 0600 in a
        # 0700 directory, so this flag can only ever be written as True
        "mode_verified": (
            verify_private_mode(private_path) == PRIVATE_FILE_MODE
            and verify_private_mode(private_path.parent)
            == PRIVATE_DIR_MODE),
    }
    public = sanitize_inventory(inventory, private_packet=packet_reference)
    assert_public_document(public)          # fail closed before writing
    public_text = json.dumps(public, indent=1, sort_keys=True) + "\n"
    public_table = render_public_table(public) + "\n"
    if redactor is not None:
        redactor.assert_clean(public_text)
        redactor.assert_clean(public_table)

    output_dir.mkdir(parents=True, exist_ok=True)
    public_path = output_dir / f"seat_truth_public_{stamp}.json"
    public_path.write_text(public_text, encoding="utf-8")
    table_path = output_dir / f"seat_truth_public_table_{stamp}.txt"
    table_path.write_text(public_table, encoding="utf-8")
    return (public_path, table_path,
            hashlib.sha256(public_path.read_bytes()).hexdigest(),
            public_table)


def rebuild_public_evidence(packet_path: Path, output_dir: Path) -> int:
    """Re-derive the committed summary from a stored private packet.

    Touches no venue, no runner and no network: the private packet already
    holds the typed inventory. Used to verify that a committed public
    artifact really is the sanitisation of the packet whose digest it
    names.
    """
    verify_private_mode(packet_path)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    if packet.get("schema") != PRIVATE_SCHEMA:
        raise ValueError(f"{packet_path} is not a {PRIVATE_SCHEMA} packet")
    stamp = packet_path.stem.replace("seat_truth_private_", "")
    public_path, table_path, public_sha, table = emit_public_evidence(
        packet["inventory"], packet_path, output_dir, stamp)
    print(table)
    print()
    print(f"re-derived from PRIVATE packet: {packet_path}")
    print(f"PUBLIC packet (git):    {public_path}")
    print(f"  sha256:               {public_sha}")
    print(f"public table:           {table_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path,
                        default=Path("docs/evidence/seat_truth"),
                        help="PUBLIC, committed: sanitized summary only")
    parser.add_argument("--private-dir", type=Path,
                        default=PRIVATE_SEAT_TRUTH_DIR,
                        help="PRIVATE 0700/0600 local store for the full"
                             " typed inventory (never committed)")
    parser.add_argument("--mt5-audit-dir", type=Path,
                        default=EVIDENCE_ROOT / "mt5-direct",
                        help="private 0700/0600 path for the direct MT5"
                             " evidence bundle")
    parser.add_argument("--tws-client-id", type=int, default=179,
                        help="dedicated UNUSED read-only clientId"
                             " (runner owns 78; resume CLI uses 169)")
    parser.add_argument("--skip-tws", action="store_true")
    parser.add_argument("--skip-dragon", action="store_true")
    parser.add_argument("--skip-alpaca-rest", action="store_true")
    parser.add_argument("--rebuild-public-from", type=Path, default=None,
                        help="re-derive the public summary from a stored"
                             " private packet; touches no venue, runner or"
                             " network")
    args = parser.parse_args()

    if args.rebuild_public_from is not None:
        return rebuild_public_evidence(args.rebuild_public_from,
                                       args.output_dir)

    now = utcnow()
    redactor = Redactor()

    # any evidence left world-readable by an earlier run is tightened first
    for line in harden_evidence_tree(EVIDENCE_ROOT):
        print(f"hardened private evidence store: {line}")

    alpaca = build_alpaca_seat(now, redactor, args.skip_alpaca_rest)
    ibkr, resolution = build_ibkr_seat(now, redactor, args.skip_tws,
                                       args.tws_client_id)
    mt5 = build_mt5_seat(now, redactor, args.skip_dragon,
                         args.mt5_audit_dir)

    collector = {
        "host": os.uname().nodename,
        "tool": "tools/seat_truth_inventory.py",
        "tool_code_revision": git_head(Path(__file__).resolve().parents[1]),
        "python": sys.version.split()[0],
        "read_only": True,
    }
    inventory = assemble_inventory(
        {"alpaca_paper_spy_1d": alpaca,
         "ibkr_paper_usdcad_4h": ibkr,
         "mt5_demo_ethusd_4h": mt5},
        collector=collector, ibkr_resolution=resolution, now=now)

    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    # ---- PRIVATE packet: full typed inventory, local 0600 store only ----
    packet = {
        "schema": PRIVATE_SCHEMA,
        "schema_version": 1,
        "generated_at": iso(now),
        "warning": ("PRIVATE: account balances/equity/margin, exact sizes"
                    " and prices, broker order ids and account/server"
                    " fingerprints. Never commit, never paste."),
        "inventory": inventory,
    }
    private_text = json.dumps(packet, indent=1, sort_keys=True, default=str)
    redactor.assert_clean(private_text)
    private_table = render_table(inventory)
    redactor.assert_clean(private_table)

    secure_mkdir(args.private_dir)
    private_path = args.private_dir / f"seat_truth_private_{stamp}.json"
    private_sha, private_bytes = secure_write_text(private_path,
                                                   private_text)
    private_table_path = (args.private_dir
                          / f"seat_truth_private_table_{stamp}.txt")
    secure_write_text(private_table_path, private_table)
    private_mode = verify_private_mode(private_path)
    dir_mode = verify_private_mode(args.private_dir)

    # ---- PUBLIC packet: sanitized summary, safe to commit ----
    public_path, public_table_path, public_sha, public_table = \
        emit_public_evidence(inventory, private_path, args.output_dir,
                             stamp, redactor)

    print(public_table)
    print()
    print(f"PRIVATE packet (0600):  {private_path}")
    print(f"  mode:                 {private_mode:04o}"
          f" (dir {dir_mode:04o})")
    print(f"  sha256:               {private_sha}")
    print(f"  bytes:                {private_bytes}")
    print(f"PUBLIC packet (git):    {public_path}")
    print(f"  sha256:               {public_sha}")
    print(f"public table:           {public_table_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
