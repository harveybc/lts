#!/usr/bin/env python3
"""Public-safe seat-truth evidence: schema, sanitizer, validator, renderer.

Correction of AUD-SEC-20260816-255. The WO1 collector
(``tools/seat_truth_inventory.py``) gathers DIRECT venue/runner facts that
are genuinely private: balances, equity, margin/free margin, exact
position sizes and prices, broker tickets/order ids and stable
account/server fingerprints. None of that may enter a public git
repository, even fingerprinted -- a hash of a stable identifier is still
a stable identifier.

Split enforced here:

* PRIVATE PACKET -- the full typed inventory, written only under
  ``~/.local/state/lts/evidence/`` with 0700 directories and 0600 files.
* PUBLIC PACKET -- a *derived* summary carrying only typed
  availability/freshness states, counts, booleans, model/config/artifact
  hashes and the SHA-256 of the private packet. Never the packet.

The public document is not "the private document with fields removed": it
is rebuilt field by field from an explicit allowlist
(:data:`PUBLIC_DOCUMENT_SPEC`). A field that nobody declared cannot
appear, and every emitted scalar is normalised to a typed token, hash,
timestamp, boolean or count, so free prose cannot smuggle a private fact
through a string field.

Two independent, structural gates run over the public document (both at
emit time in the collector and in the repository test
``tests/test_seat_truth_public_evidence.py``):

1. :func:`validate_public_document` -- allowlist validation of the whole
   structure: unknown keys are errors, declared keys must match their
   declared type/shape.
2. :func:`scan_forbidden_keys` -- a denylist applied to every dict key in
   the tree (nested values included), catching the private field names
   directly even if a future edit also widens the schema by mistake.

Neither gate is a regex over prose; both walk the parsed structure.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

PUBLIC_SCHEMA = "lts.seat_truth_public.v1"
PUBLIC_SCHEMA_VERSION = 1
PRIVATE_SCHEMA = "lts.seat_truth_private.v1"
SOURCE_INVENTORY_SCHEMA = "lts.seat_truth_inventory.v1"

# Committed evidence file names. Anything else under the public evidence
# directory is a repository-test failure (fail closed on unknown files).
PUBLIC_JSON_NAME_RE = re.compile(r"^seat_truth_public_\d{8}T\d{6}Z\.json$")
PUBLIC_TABLE_NAME_RE = re.compile(
    r"^seat_truth_public_table_\d{8}T\d{6}Z\.txt$")

PUBLIC_EVIDENCE_DIRNAME = "docs/evidence/seat_truth"


# --------------------------------------------------------------------------
# denylist: private field names that must never appear as a key, anywhere
# --------------------------------------------------------------------------

def normalize_key(key: str) -> str:
    """Lowercase and strip separators so ``free_margin``/``freeMargin``/
    ``free-margin`` all normalise to the same token."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


# Substring matches: any key containing one of these is forbidden.
FORBIDDEN_KEY_SUBSTRINGS = (
    "balance", "equity", "margin", "buyingpower", "netliquidation",
    "availablefunds", "cashvalue", "notional",
    "price", "avgcost", "avgentry", "marketvalue", "unrealized", "realized",
    "stoploss", "takeprofit",
    "ticket", "orderid", "execid", "permid",
    "fingerprint", "accountnumber", "accountid", "apikey", "secret",
    "credential", "password",
)

# Exact matches (short/ambiguous names that would over-match as substrings,
# plus the private fact envelope itself: a public document never carries a
# raw ``value``/``source``/``detail``/``note`` field).
FORBIDDEN_KEY_EXACT = (
    "qty", "filledqty", "quantity", "volume", "units", "unitsopen", "size",
    "shares", "position", "positions", "pnl", "pl", "profit", "cash",
    "login", "account", "server", "serverversion", "avgprice",
    "value", "source", "sourceattempted", "detail", "note", "payload",
    "raw", "rows", "explanation",
)


def iter_keys(node: Any, path: str = "") -> Iterable[tuple[str, str]]:
    """Yield ``(json_path, key)`` for every dict key in the tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            yield here, str(key)
            yield from iter_keys(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_keys(value, f"{path}[{index}]")


def key_is_forbidden(key: str) -> str | None:
    """Return the matched denylist token, or None."""
    norm = normalize_key(key)
    if norm in FORBIDDEN_KEY_EXACT:
        return norm
    for token in FORBIDDEN_KEY_SUBSTRINGS:
        if token in norm:
            return token
    return None


def scan_forbidden_keys(document: Any) -> list[str]:
    """Structural denylist scan over every key of a parsed document."""
    errors = []
    for path, key in iter_keys(document):
        token = key_is_forbidden(key)
        if token:
            errors.append(
                f"{path}: forbidden private field name {key!r}"
                f" (denylist token {token!r})")
    return errors


# Account-id shapes must never survive into a public document even inside
# an allowlisted string field.
ACCOUNT_SHAPE_PATTERNS = (
    re.compile(r"\bDU[A-Z]?\d{5,}\b"),
    re.compile(r"\bU\d{7,}\b"),
    re.compile(r"\bPA[0-9A-Z]{8,}\b"),
)


def iter_strings(node: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from iter_strings(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def scan_account_shapes(document: Any) -> list[str]:
    errors = []
    for path, text in iter_strings(document):
        for pattern in ACCOUNT_SHAPE_PATTERNS:
            if pattern.search(text):
                errors.append(
                    f"{path}: account-id-shaped token matches"
                    f" {pattern.pattern}")
    return errors


# --------------------------------------------------------------------------
# allowlist schema (a tiny structural spec language, no dependencies)
# --------------------------------------------------------------------------

TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,64}$")
RELPATH_RE = re.compile(r"^[A-Za-z0-9_./-]{1,80}$")
NODE_PATH_RE = re.compile(r"^[A-Za-z0-9_.\[\]-]{1,160}$")
TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
PRIVATE_PATH_RE = re.compile(
    r"^~/\.local/state/lts/evidence/[A-Za-z0-9_./-]{1,120}$")


def obj(fields: dict[str, dict], *, required: Iterable[str] = ()) -> dict:
    return {"kind": "object", "fields": fields, "required": set(required)}


def mapping(key_re: re.Pattern, value: dict) -> dict:
    return {"kind": "map", "key_re": key_re, "value": value}


def arr(item: dict, *, max_items: int = 4096) -> dict:
    return {"kind": "array", "item": item, "max_items": max_items}


def scalar(check: Callable[[Any], bool], name: str,
           *, nullable: bool = True) -> dict:
    return {"kind": "scalar", "check": check, "name": name,
            "nullable": nullable}


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_token(value: Any) -> bool:
    return isinstance(value, str) and bool(TOKEN_RE.match(value))


def _is_timestamp(value: Any) -> bool:
    return isinstance(value, str) and bool(TIMESTAMP_RE.match(value))


def _hex(length: int) -> Callable[[Any], bool]:
    pattern = re.compile(rf"^[0-9a-f]{{{length}}}$")

    def check(value: Any) -> bool:
        return isinstance(value, str) and bool(pattern.match(value))
    return check


def enum(values: Iterable[str], *, nullable: bool = True) -> dict:
    allowed = frozenset(values)
    return scalar(lambda v: isinstance(v, str) and v in allowed,
                  f"enum{sorted(allowed)}", nullable=nullable)


BOOL = scalar(_is_bool, "bool")
BOOL_REQ = scalar(_is_bool, "bool", nullable=False)
COUNT = scalar(_is_count, "count>=0")
NUMBER = scalar(_is_number, "number")
TOKEN = scalar(_is_token, "token[A-Za-z0-9_.:+-]{1,64}")
TOKEN_REQ = scalar(_is_token, "token", nullable=False)
TIMESTAMP = scalar(_is_timestamp, "iso8601")
TIMESTAMP_REQ = scalar(_is_timestamp, "iso8601", nullable=False)
SHA256 = scalar(_hex(64), "sha256-hex")
SHA256_REQ = scalar(_hex(64), "sha256-hex", nullable=False)
GITREV = scalar(_hex(40), "git-sha1-hex")
NODE_PATH = scalar(
    lambda v: isinstance(v, str) and bool(NODE_PATH_RE.match(v)),
    "json-node-path")
RELPATH = scalar(
    lambda v: isinstance(v, str) and bool(RELPATH_RE.match(v)),
    "repository-relative path")
TRIBOOL = scalar(
    lambda v: isinstance(v, bool) or v == "unavailable", "bool|'unavailable'")

AVAILABILITY_STATES = ("available", "unavailable", "absent")
SOURCE_CLASSES = ("direct_venue", "direct_runner", "direct_runner_remote",
                  "direct_runner_ledger", "direct_file", "expected_registry",
                  "git", "unknown", "none")

AVAILABILITY = obj({
    "state": enum(AVAILABILITY_STATES, nullable=False),
    "reason": TOKEN,
    "source_class": enum(SOURCE_CLASSES, nullable=False),
    "fresh": BOOL,
    "age_seconds": NUMBER,
    "freshness_budget_seconds": NUMBER,
}, required=("state", "reason", "source_class", "fresh", "age_seconds",
             "freshness_budget_seconds"))

JOIN_CHECK_NAMES = (
    "heartbeat_artifact_equals_manifest_artifact",
    "manifest_artifact_equals_file_bytes",
    "heartbeat_manifest_sha_equals_manifest_bytes",
    "heartbeat_config_equals_manifest_config",
)

RESOLUTION_STATES = (
    "flat_at_venue__ledger_exposure_row_stale_open",
    "flat_and_reconciled",
    "venue_position_open",
    "unresolved_direct_unavailable",
)

RESOLUTION_EXPLANATIONS = {
    "flat_at_venue__ledger_exposure_row_stale_open": (
        "Direct TWS positions()/portfolio() (authoritative, account-scoped)"
        " report FLAT while the runner ledger still carries an open"
        " exposure row: reconciliation is pending behind one non-terminal"
        " runner-tracked order, so the exposure row is an accounting"
        " residue, not market risk."),
    "flat_and_reconciled": (
        "Direct venue facts report FLAT and the runner ledger carries no"
        " open exposure row."),
    "venue_position_open": (
        "Direct venue facts report a non-flat position; ledger and"
        " heartbeat must be read against it."),
    "unresolved_direct_unavailable": (
        "Direct venue facts were unavailable; no flatness verdict is"
        " issued (flatness is never inferred from one field)."),
}

SEAT_SPEC = obj({
    "venue": TOKEN,
    "environment_class": TOKEN,
    "availability": obj({
        "runner_heartbeat": AVAILABILITY,
        "runner_ledger": AVAILABILITY,
        "direct_venue_probe": AVAILABILITY,
        "manifest_join": AVAILABILITY,
    }, required=("runner_heartbeat", "runner_ledger", "direct_venue_probe",
                 "manifest_join")),
    "identity": obj({
        "symbol": TOKEN,
        "timeframe": TOKEN,
        "model_id": TOKEN,
        "artifact_sha256": SHA256,
        "config_sha256": SHA256,
        "code_revision": GITREV,
        "adapter_version": TOKEN,
        "execution_tier": TOKEN,
    }),
    "account_binding": obj({
        "matches_expected_seat_binding": BOOL,
        "direct_venue_binding_matches": BOOL,
        "verified_by_runner": BOOL,
        "write_enabled": BOOL,
        "identifier_disclosed_in_git": BOOL_REQ,
    }, required=("matches_expected_seat_binding",
                 "identifier_disclosed_in_git")),
    "model_artifact_join": obj({
        "checks": obj({name: TRIBOOL for name in JOIN_CHECK_NAMES}),
        "gap_count": COUNT,
        "live_inference_eligible": BOOL,
        "live_execution_eligible": BOOL,
        "research_validated": BOOL,
    }),
    "bars_and_decisions": obj({
        "last_closed_input_bar": AVAILABILITY,
        "last_closed_input_bar_time": TIMESTAMP,
        "due_decision_identity_present": BOOL,
        "decision_current_for_last_closed_bar": BOOL,
        "last_decision": AVAILABILITY,
        "last_decision_action": TOKEN,
        "last_decision_outcome": TOKEN,
        "last_decision_reason": TOKEN,
        "last_decision_bar_close": TIMESTAMP,
    }),
    "counts": obj({
        "venue_position_rows": COUNT,
        "venue_portfolio_rows": COUNT,
        "venue_open_orders": COUNT,
        "venue_open_orders_availability": AVAILABILITY,
        "venue_native_protection_orders": COUNT,
        "venue_fills_today": COUNT,
        "runner_tracked_open_orders": COUNT,
        "runner_tracked_cancel_attempts": COUNT,
        "heartbeat_open_orders": COUNT,
        "ledger_open_exposure_rows_present": BOOL,
        "heartbeat_reports_no_exposure": BOOL,
        "venue_reports_flat": BOOL,
    }),
    "control_state": obj({
        "halt": AVAILABILITY,
        "halt_active": BOOL,
        "trade_allowed": BOOL,
        "last_resume_present": BOOL,
        "runner_state": TOKEN,
    }),
}, required=("venue", "availability", "identity", "account_binding",
             "model_artifact_join", "bars_and_decisions", "counts",
             "control_state"))

PUBLIC_DOCUMENT_SPEC = obj({
    "schema": enum((PUBLIC_SCHEMA,), nullable=False),
    "schema_version": scalar(lambda v: v == PUBLIC_SCHEMA_VERSION,
                             f"=={PUBLIC_SCHEMA_VERSION}", nullable=False),
    "source_schema": enum((SOURCE_INVENTORY_SCHEMA,), nullable=False),
    "generated_at": TIMESTAMP_REQ,
    "collector": obj({
        "host": TOKEN,
        "tool": RELPATH,
        "tool_code_revision": GITREV,
        "python": TOKEN,
        "read_only": BOOL,
    }),
    "doctrine": obj({
        "direct_sources_only": BOOL_REQ,
        "read_only": BOOL_REQ,
        "no_reqAllOpenOrders": BOOL_REQ,
        "private_detail_excluded": BOOL_REQ,
        "private_store_mode": enum(("0600",), nullable=False),
        "private_store_dir_mode": enum(("0700",), nullable=False),
    }, required=("direct_sources_only", "read_only", "no_reqAllOpenOrders",
                 "private_detail_excluded", "private_store_mode",
                 "private_store_dir_mode")),
    "private_packet": obj({
        "schema": enum((PRIVATE_SCHEMA,), nullable=False),
        "sha256": SHA256_REQ,
        "byte_length": scalar(_is_count, "count>=0", nullable=False),
        "path": scalar(
            lambda v: isinstance(v, str) and bool(PRIVATE_PATH_RE.match(v)),
            "~/.local/state/lts/evidence/... path", nullable=False),
        "mode_verified": BOOL_REQ,
    }, required=("schema", "sha256", "byte_length", "path",
                 "mode_verified")),
    "seat_count": scalar(_is_count, "count>=0", nullable=False),
    "seats": mapping(re.compile(r"^[a-z0-9_]{1,64}$"), SEAT_SPEC),
    "ibkr_order_exposure_resolution": obj({
        "state": enum(RESOLUTION_STATES, nullable=False),
        "direct_flatness": TRIBOOL,
        "venue_position_rows": COUNT,
        "venue_portfolio_rows": COUNT,
        "venue_scoped_open_orders": AVAILABILITY,
        "ledger_open_exposure_present": BOOL,
        "runner_tracked_open_order_present": BOOL,
        "runner_tracked_cancel_attempts": COUNT,
        "heartbeat_open_orders": COUNT,
        "heartbeat_reports_no_exposure": BOOL,
    }, required=("state",)),
    "unavailable_count": scalar(_is_count, "count>=0", nullable=False),
    "unavailable_index": arr(obj({
        "path": NODE_PATH,
        "reason": TOKEN,
    }, required=("path", "reason"))),
}, required=("schema", "schema_version", "source_schema", "generated_at",
             "collector", "doctrine", "private_packet", "seat_count",
             "seats", "unavailable_count", "unavailable_index"))


def validate_public_document(document: Any) -> list[str]:
    """Allowlist validation. Returns a list of human-readable errors."""
    errors: list[str] = []
    _validate(document, PUBLIC_DOCUMENT_SPEC, "", errors)
    return errors


def _validate(node: Any, spec: dict, path: str, errors: list[str]) -> None:
    kind = spec["kind"]
    where = path or "<root>"
    if kind == "object":
        if not isinstance(node, dict):
            errors.append(f"{where}: expected object, got"
                          f" {type(node).__name__}")
            return
        for key in spec["required"]:
            if key not in node:
                errors.append(f"{where}: missing required field {key!r}")
        for key, value in node.items():
            child = spec["fields"].get(key)
            if child is None:
                errors.append(
                    f"{where}: field {key!r} is not in the public-evidence"
                    " allowlist")
                continue
            _validate(value, child, f"{path}.{key}" if path else str(key),
                      errors)
    elif kind == "map":
        if not isinstance(node, dict):
            errors.append(f"{where}: expected object map, got"
                          f" {type(node).__name__}")
            return
        for key, value in node.items():
            if not spec["key_re"].match(str(key)):
                errors.append(f"{where}: map key {key!r} does not match"
                              f" {spec['key_re'].pattern}")
            _validate(value, spec["value"],
                      f"{path}.{key}" if path else str(key), errors)
    elif kind == "array":
        if not isinstance(node, list):
            errors.append(f"{where}: expected array, got"
                          f" {type(node).__name__}")
            return
        if len(node) > spec["max_items"]:
            errors.append(f"{where}: array longer than"
                          f" {spec['max_items']}")
        for index, value in enumerate(node):
            _validate(value, spec["item"], f"{path}[{index}]", errors)
    else:                                          # scalar
        if node is None:
            if not spec["nullable"]:
                errors.append(f"{where}: null is not allowed"
                              f" (expected {spec['name']})")
            return
        if isinstance(node, (dict, list)):
            errors.append(f"{where}: expected scalar {spec['name']}, got"
                          f" {type(node).__name__}")
            return
        if not spec["check"](node):
            errors.append(f"{where}: value does not satisfy"
                          f" {spec['name']}")


def iter_spec_field_names(spec: dict) -> Iterable[str]:
    """Every field name the public schema declares (for self-checks)."""
    kind = spec["kind"]
    if kind == "object":
        for name, child in spec["fields"].items():
            yield name
            yield from iter_spec_field_names(child)
    elif kind == "map":
        yield from iter_spec_field_names(spec["value"])
    elif kind == "array":
        yield from iter_spec_field_names(spec["item"])


def assert_public_document(document: Any) -> None:
    """Fail closed: raise unless the document passes both gates."""
    errors = (validate_public_document(document)
              + scan_forbidden_keys(document)
              + scan_account_shapes(document))
    if errors:
        raise PublicEvidenceViolation(
            "public evidence rejected:\n  - " + "\n  - ".join(errors))


class PublicEvidenceViolation(RuntimeError):
    pass


# --------------------------------------------------------------------------
# sanitizer: rebuild the public document from the private inventory
# --------------------------------------------------------------------------

def _is_unavailable(entry: Any) -> bool:
    return isinstance(entry, dict) and "unavailable" in entry


def _token(value: Any, *, limit: int = 64) -> str | None:
    """Normalise any scalar into a typed token (prose cannot survive)."""
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = re.sub(r"[^A-Za-z0-9_.:+-]+", "_", str(value)).strip("_")
    return text[:limit] or None


def _relpath(value: Any) -> str | None:
    """Normalise a repository-relative path (no spaces, no prose)."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"[^A-Za-z0-9_./-]+", "_", value).lstrip("/")[:80]
    return text if RELPATH_RE.match(text) else None


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().replace(" ", "T")
    return text if TIMESTAMP_RE.match(text) else None


def _sha(value: Any, length: int = 64) -> str | None:
    if isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}",
                                               value.strip().lower()):
        return value.strip().lower()
    return None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in (
            "true", "false", "1", "0", "yes", "no"):
        return value.strip().lower() in ("true", "1", "yes")
    return None


def _source_class(source: Any) -> str:
    if not isinstance(source, str) or not source:
        return "none"
    head = source.split(":", 1)[0].strip().lower()
    if head == "ssh":
        return "direct_runner_remote"
    return head if head in SOURCE_CLASSES else "unknown"


def availability(entry: Any) -> dict:
    """Typed availability/freshness record for any fact-or-unavailable."""
    if entry is None:
        return {"state": "absent", "reason": None, "source_class": "none",
                "fresh": None, "age_seconds": None,
                "freshness_budget_seconds": None}
    if _is_unavailable(entry):
        return {"state": "unavailable",
                "reason": _token(entry.get("unavailable")),
                "source_class": _source_class(entry.get("source_attempted")),
                "fresh": None, "age_seconds": None,
                "freshness_budget_seconds": None}
    if not isinstance(entry, dict):
        return {"state": "absent", "reason": None, "source_class": "none",
                "fresh": None, "age_seconds": None,
                "freshness_budget_seconds": None}
    age = entry.get("age_seconds")
    budget = entry.get("freshness_budget_seconds")
    return {
        "state": "available",
        "reason": None,
        "source_class": _source_class(entry.get("source")),
        "fresh": entry.get("fresh") if isinstance(entry.get("fresh"), bool)
        else None,
        "age_seconds": round(float(age), 1) if isinstance(
            age, (int, float)) and not isinstance(age, bool) else None,
        "freshness_budget_seconds": float(budget) if isinstance(
            budget, (int, float)) and not isinstance(budget, bool) else None,
    }


def _value(entry: Any) -> Any:
    if isinstance(entry, dict) and not _is_unavailable(entry):
        return entry.get("value")
    return None


def _count_rows(entry: Any) -> int | None:
    value = _value(entry)
    if isinstance(value, list):
        return len(value)
    return None


def _first(node: Any, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current = node
        ok = True
        for key in path:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                ok = False
                break
        if ok:
            return current
    return None


SIZE_KEYS = ("position", "qty", "volume", "units_open", "quantity")


def _rows_are_flat(entry: Any) -> bool | None:
    """True when every venue row reports zero size. Only the boolean is
    emitted; the sizes themselves stay in the private packet."""
    value = _value(entry)
    if not isinstance(value, list):
        return None
    for row in value:
        if not isinstance(row, dict):
            return None
        sizes = [row.get(key) for key in SIZE_KEYS if key in row]
        if not sizes:
            return False
        for size in sizes:
            try:
                if float(size) != 0.0:
                    return False
            except (TypeError, ValueError):
                return False
    return True


HALT_INACTIVE_TOKENS = frozenset(
    {"", "none", "no", "false", "0", "no_halt_row_recorded", "null",
     "inactive", "clear", "cleared"})


def _halt_active(entry: Any) -> bool | None:
    value = _value(entry)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in HALT_INACTIVE_TOKENS


def sanitize_seat(seat: dict) -> dict:
    """Rebuild one seat as public-safe typed states, counts and booleans."""
    account = seat.get("account") or {}
    identity = seat.get("identity") or {}
    join = seat.get("model_artifact_join") or {}
    bars = seat.get("bars_and_decisions") or {}
    broker = seat.get("broker_state") or {}
    control = seat.get("control_state") or {}
    direct = broker.get("direct_venue") or {}

    positions = _first(seat, ("broker_state", "direct_venue", "positions"),
                       ("broker_state", "positions"))
    open_orders = _first(seat, ("broker_state", "direct_venue",
                                "open_orders"),
                         ("broker_state", "pending_orders"))
    venue_scoped = _first(seat, ("broker_state", "direct_venue",
                                 "open_orders_venue_scoped"))
    protection = _first(seat, ("broker_state", "direct_venue",
                               "native_protection_evidence"),
                        ("broker_state", "native_protection_evidence"))
    heartbeat_exposure = _first(seat, ("broker_state", "heartbeat_positions"),
                                ("broker_state", "heartbeat_position"))
    probe = _first(seat, ("broker_state", "direct_venue", "positions"),
                   ("broker_state", "direct_venue", "account"),
                   ("broker_state", "direct_venue", "probe"),
                   ("broker_state", "account_snapshot"))
    tracked = broker.get("runner_tracked_open_order")
    tracked_value = _value(tracked)
    exposure = broker.get("open_exposure")
    eligibility = _value(join.get("eligibility")) or {}
    checks = (_value(join.get("join")) or {}).get("checks") or {}
    decision = bars.get("last_recorded_decision")
    decision_value = _value(decision) or {}
    bar_entry = bars.get("last_closed_input_bar")
    bar_value = _value(bar_entry)
    heartbeat_value = _value(heartbeat_exposure)

    binding_direct = _first(
        seat, ("account", "direct_venue_fingerprint_matches"),
        ("account", "bridge_fingerprint_matches_heartbeat"))
    direct_account_value = _value(direct.get("account")) or {}
    if binding_direct is None and direct_account_value:
        direct_match = _bool(direct_account_value.get("matches_expected"))
    else:
        direct_match = _bool(_value(binding_direct))

    return {
        "venue": _token(_value(seat.get("venue"))),
        "environment_class": _token(_value(account.get("environment_class"))),
        "availability": {
            "runner_heartbeat": availability(seat.get("runner_state")),
            "runner_ledger": availability(bar_entry),
            "direct_venue_probe": availability(probe),
            "manifest_join": availability(join.get("join")),
        },
        "identity": {
            "symbol": _token(_value(identity.get("symbol"))),
            "timeframe": _token(_value(identity.get("timeframe"))),
            "model_id": _token(_value(identity.get("model_id"))),
            "artifact_sha256": _sha(_value(identity.get("artifact_sha256"))),
            "config_sha256": _sha(_value(identity.get("config_sha256"))),
            "code_revision": _sha(_value(identity.get("code_revision")), 40),
            "adapter_version": _token(_value(identity.get(
                "adapter_version"))),
            "execution_tier": _token(_value(identity.get("execution_tier"))),
        },
        "account_binding": {
            "matches_expected_seat_binding": _bool(
                _value(account.get("fingerprint_matches_expected"))),
            "direct_venue_binding_matches": direct_match,
            "verified_by_runner": _bool(
                _value(account.get("binding_verified_by_runner"))),
            "write_enabled": _bool(_value(account.get("write_enabled"))),
            "identifier_disclosed_in_git": False,
        },
        "model_artifact_join": {
            "checks": {name: (checks[name]
                              if isinstance(checks.get(name), bool)
                              else "unavailable")
                       for name in JOIN_CHECK_NAMES if name in checks},
            "gap_count": len((_value(join.get("join")) or {}).get("gaps")
                             or []) if _value(join.get("join")) else None,
            "live_inference_eligible": _bool(
                eligibility.get("live_inference_eligible")),
            "live_execution_eligible": _bool(
                eligibility.get("live_execution_eligible")),
            "research_validated": _bool(
                eligibility.get("research_validated")),
        },
        "bars_and_decisions": {
            "last_closed_input_bar": availability(bar_entry),
            "last_closed_input_bar_time": _timestamp(
                bar_value.get("bar_time") if isinstance(bar_value, dict)
                else bar_value),
            "due_decision_identity_present": not _is_unavailable(
                bars.get("due_decision_identity", {"unavailable": "absent"})),
            "decision_current_for_last_closed_bar": _bool(
                _value(bars.get("decision_current_for_last_closed_bar"))),
            "last_decision": availability(decision),
            "last_decision_action": _token(decision_value.get("action")),
            "last_decision_outcome": _token(decision_value.get("outcome")),
            "last_decision_reason": _token(decision_value.get("reason")),
            "last_decision_bar_close": _timestamp(
                decision_value.get("bar_close")),
        },
        "counts": {
            "venue_position_rows": _count_rows(positions),
            "venue_portfolio_rows": _count_rows(direct.get("portfolio")),
            "venue_open_orders": _count_rows(open_orders),
            "venue_open_orders_availability": availability(
                venue_scoped if venue_scoped is not None else open_orders),
            "venue_native_protection_orders": _count_rows(protection),
            "venue_fills_today": _count_rows(direct.get("fills_today")),
            "runner_tracked_open_orders": (
                1 if isinstance(tracked_value, dict) else
                (0 if _is_unavailable(tracked) else None)),
            "runner_tracked_cancel_attempts": (
                tracked_value.get("cancel_attempts")
                if isinstance(tracked_value, dict)
                and _is_count(tracked_value.get("cancel_attempts"))
                else None),
            "heartbeat_open_orders": (
                _value(broker.get("heartbeat_orders"))
                if _is_count(_value(broker.get("heartbeat_orders")))
                else None),
            "ledger_open_exposure_rows_present": (
                None if exposure is None or _is_unavailable(exposure)
                else _value(exposure) is not None),
            "heartbeat_reports_no_exposure": (
                float(heartbeat_value) == 0.0
                if isinstance(heartbeat_value, (int, float))
                and not isinstance(heartbeat_value, bool) else None),
            "venue_reports_flat": _rows_are_flat(positions),
        },
        "control_state": {
            "halt": availability(control.get("halt")),
            "halt_active": _halt_active(control.get("halt")),
            "trade_allowed": _bool(
                _value(control.get("kill_switch_trade_allowed"))),
            "last_resume_present": ("last_resume" in control
                                    and not _is_unavailable(
                                        control.get("last_resume"))),
            "runner_state": _token(_value(seat.get("runner_state"))),
        },
    }


def sanitize_resolution(resolution: dict | None) -> dict | None:
    if not resolution:
        return None
    tracked = resolution.get("runner_tracked_open_order")
    tracked_value = _value(tracked)
    exposure = resolution.get("ledger_open_exposure")
    heartbeat_value = _value(resolution.get("heartbeat_position"))
    flat = resolution.get("direct_flatness")
    position_rows = resolution.get("direct_position_rows")
    portfolio_rows = resolution.get("direct_portfolio_rows")
    return {
        "state": resolution.get("state"),
        "direct_flatness": flat if isinstance(flat, bool) else "unavailable",
        "venue_position_rows": (position_rows if _is_count(position_rows)
                                else None),
        "venue_portfolio_rows": (portfolio_rows if _is_count(portfolio_rows)
                                 else None),
        "venue_scoped_open_orders": availability(
            resolution.get("venue_scoped_open_orders")),
        "ledger_open_exposure_present": (
            None if exposure is None or _is_unavailable(exposure)
            else _value(exposure) is not None),
        "runner_tracked_open_order_present": isinstance(tracked_value, dict),
        "runner_tracked_cancel_attempts": (
            tracked_value.get("cancel_attempts")
            if isinstance(tracked_value, dict)
            and _is_count(tracked_value.get("cancel_attempts")) else None),
        "heartbeat_open_orders": (
            _value(resolution.get("heartbeat_open_order_count"))
            if _is_count(_value(resolution.get("heartbeat_open_order_count")))
            else None),
        "heartbeat_reports_no_exposure": (
            float(heartbeat_value) == 0.0
            if isinstance(heartbeat_value, (int, float))
            and not isinstance(heartbeat_value, bool) else None),
    }


def sanitize_inventory(inventory: dict, *, private_packet: dict) -> dict:
    """Build the public document from the private inventory.

    ``private_packet`` must carry ``sha256``/``byte_length``/``path``/
    ``mode_verified`` for the 0600 local packet this summary refers to.
    """
    seats = inventory.get("seats") or {}
    collector = inventory.get("collector") or {}
    document = {
        "schema": PUBLIC_SCHEMA,
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "source_schema": inventory.get("schema", SOURCE_INVENTORY_SCHEMA),
        "generated_at": inventory.get("generated_at"),
        "collector": {
            "host": _token(collector.get("host")),
            "tool": _relpath(collector.get("tool")),
            "tool_code_revision": _sha(collector.get("tool_code_revision"),
                                       40),
            "python": _token(collector.get("python")),
            "read_only": _bool(collector.get("read_only")),
        },
        "doctrine": {
            "direct_sources_only": True,
            "read_only": True,
            "no_reqAllOpenOrders": True,
            "private_detail_excluded": True,
            "private_store_mode": "0600",
            "private_store_dir_mode": "0700",
        },
        "private_packet": {
            "schema": PRIVATE_SCHEMA,
            "sha256": private_packet["sha256"],
            "byte_length": private_packet["byte_length"],
            "path": private_packet["path"],
            "mode_verified": private_packet["mode_verified"],
        },
        "seat_count": len(seats),
        "seats": {name: sanitize_seat(seat) for name, seat in seats.items()},
        "unavailable_index": [
            {"path": entry.get("path"), "reason": _token(entry.get("reason"))}
            for entry in (inventory.get("unavailable_index") or [])],
    }
    document["unavailable_count"] = len(document["unavailable_index"])
    resolution = sanitize_resolution(
        inventory.get("ibkr_order_exposure_resolution"))
    if resolution is not None:
        document["ibkr_order_exposure_resolution"] = resolution
    return document


# --------------------------------------------------------------------------
# rendering: a pure function of the public document (nothing else)
# --------------------------------------------------------------------------

def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _fmt_avail(record: Any) -> str:
    if not isinstance(record, dict):
        return "-"
    if record.get("state") != "available":
        return f"{record.get('state')}({record.get('reason') or '-'})"
    text = f"available[{record.get('source_class')}]"
    if record.get("fresh") is True:
        text += " fresh"
    elif record.get("fresh") is False:
        age = record.get("age_seconds")
        text += f" STALE({round((age or 0) / 3600.0, 1)}h)"
    return text


def render_public_table(document: dict) -> str:
    """Render the committed table. Pure function of ``document`` -- the
    repository test re-renders and compares bytes, so the table cannot
    carry a fact the validated JSON does not."""
    seats = document.get("seats") or {}
    names = list(seats)
    rows: list[tuple[str, list[str]]] = []

    def row(label: str, getter: Callable[[dict], Any],
            formatter: Callable[[Any], str] = _fmt) -> None:
        rows.append((label, [formatter(getter(seats[name]))
                             for name in names]))

    def path(*keys: str) -> Callable[[dict], Any]:
        def getter(seat: dict) -> Any:
            node: Any = seat
            for key in keys:
                node = node.get(key) if isinstance(node, dict) else None
            return node
        return getter

    row("venue", path("venue"))
    row("environment class", path("environment_class"))
    row("symbol", path("identity", "symbol"))
    row("timeframe", path("identity", "timeframe"))
    row("model id", path("identity", "model_id"))
    row("artifact sha256", path("identity", "artifact_sha256"))
    row("config sha256", path("identity", "config_sha256"))
    row("code revision", path("identity", "code_revision"))
    row("runner heartbeat", path("availability", "runner_heartbeat"),
        _fmt_avail)
    row("runner ledger", path("availability", "runner_ledger"), _fmt_avail)
    row("direct venue probe", path("availability", "direct_venue_probe"),
        _fmt_avail)
    row("manifest join", path("availability", "manifest_join"), _fmt_avail)
    row("seat binding matches",
        path("account_binding", "matches_expected_seat_binding"))
    row("direct venue binding matches",
        path("account_binding", "direct_venue_binding_matches"))
    row("write-enabled", path("account_binding", "write_enabled"))
    row("join gaps", path("model_artifact_join", "gap_count"))
    row("live-inference eligible",
        path("model_artifact_join", "live_inference_eligible"))
    row("live-execution eligible",
        path("model_artifact_join", "live_execution_eligible"))
    row("last closed input bar",
        path("bars_and_decisions", "last_closed_input_bar_time"))
    row("decision current for bar",
        path("bars_and_decisions", "decision_current_for_last_closed_bar"))
    row("last decision action",
        path("bars_and_decisions", "last_decision_action"))
    row("last decision outcome",
        path("bars_and_decisions", "last_decision_outcome"))
    row("venue position rows", path("counts", "venue_position_rows"))
    row("venue reports flat", path("counts", "venue_reports_flat"))
    row("venue open orders", path("counts", "venue_open_orders"))
    row("venue open orders state",
        path("counts", "venue_open_orders_availability"), _fmt_avail)
    row("native protection orders",
        path("counts", "venue_native_protection_orders"))
    row("runner-tracked open orders",
        path("counts", "runner_tracked_open_orders"))
    row("runner-tracked cancel attempts",
        path("counts", "runner_tracked_cancel_attempts"))
    row("heartbeat open orders", path("counts", "heartbeat_open_orders"))
    row("ledger open exposure row",
        path("counts", "ledger_open_exposure_rows_present"))
    row("halt active", path("control_state", "halt_active"))
    row("runner state", path("control_state", "runner_state"))

    width = max([len(label) for label, _ in rows] + [32]) + 2
    lines = [
        "SEAT TRUTH -- PUBLIC-SAFE SUMMARY  " + str(
            document.get("generated_at")),
        f"schema {document.get('schema')} (from"
        f" {document.get('source_schema')})",
        "private packet sha256 "
        + str((document.get("private_packet") or {}).get("sha256")),
        "private packet path   "
        + str((document.get("private_packet") or {}).get("path"))
        + "  (0600 file / 0700 dir, never committed)",
        "balances, equity, margin, sizes, prices, tickets, broker order ids"
        " and account/server fingerprints are NOT in this file",
        "=" * 110,
    ]
    header = " " * width + " | ".join(f"{name:<38}" for name in names)
    lines.append(header)
    lines.append("-" * len(header))
    for label, cells in rows:
        lines.append(f"{label:<{width}}"
                     + " | ".join(f"{cell:<38}" for cell in cells))
    resolution = document.get("ibkr_order_exposure_resolution")
    if resolution:
        state = resolution.get("state")
        lines += ["", f"IBKR ORDER/EXPOSURE RESOLUTION: {state}",
                  "  " + RESOLUTION_EXPLANATIONS.get(state, "-"),
                  f"  * direct flatness: {_fmt(resolution.get('direct_flatness'))}",
                  f"  * venue position rows:"
                  f" {_fmt(resolution.get('venue_position_rows'))};"
                  f" portfolio rows:"
                  f" {_fmt(resolution.get('venue_portfolio_rows'))}",
                  f"  * ledger open exposure present:"
                  f" {_fmt(resolution.get('ledger_open_exposure_present'))}",
                  f"  * runner-tracked open order present:"
                  f" {_fmt(resolution.get('runner_tracked_open_order_present'))}"
                  f" (cancel attempts"
                  f" {_fmt(resolution.get('runner_tracked_cancel_attempts'))})",
                  "  * venue-scoped open orders: "
                  + _fmt_avail(resolution.get("venue_scoped_open_orders"))]
    index = document.get("unavailable_index") or []
    lines += ["", f"TYPED UNAVAILABLE FACTS: {len(index)}"]
    for entry in index:
        lines.append(f"  - {entry.get('path')}: {entry.get('reason')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# repository scan (used by the fail-closed test)
# --------------------------------------------------------------------------

def scan_public_evidence_dir(directory: Path) -> list[str]:
    """Validate every artifact under the committed evidence directory.

    Fail closed: unknown file names are errors, JSON artifacts must pass
    the allowlist schema plus the structural denylist, and each table must
    be byte-identical to ``render_public_table`` of its JSON sibling.
    """
    errors: list[str] = []
    if not directory.is_dir():
        return errors
    for entry in sorted(directory.rglob("*")):
        if entry.is_dir():
            errors.append(f"{entry}: unexpected subdirectory in public"
                          " evidence")
            continue
        name = entry.name
        if PUBLIC_JSON_NAME_RE.match(name):
            try:
                document = json.loads(entry.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                errors.append(f"{entry}: not valid JSON ({exc})")
                continue
            errors += [f"{name}: {err}" for err in
                       validate_public_document(document)]
            errors += [f"{name}: {err}" for err in
                       scan_forbidden_keys(document)]
            errors += [f"{name}: {err}" for err in
                       scan_account_shapes(document)]
        elif PUBLIC_TABLE_NAME_RE.match(name):
            stamp = name[len("seat_truth_public_table_"):-len(".txt")]
            sibling = directory / f"seat_truth_public_{stamp}.json"
            if not sibling.is_file():
                errors.append(f"{name}: no validated JSON sibling"
                              f" seat_truth_public_{stamp}.json")
                continue
            document = json.loads(sibling.read_text(encoding="utf-8"))
            expected = render_public_table(document) + "\n"
            if entry.read_text(encoding="utf-8") != expected:
                errors.append(
                    f"{name}: table is not the rendering of its validated"
                    " JSON sibling (hand-edited or stale)")
        else:
            errors.append(
                f"{entry}: file name is not an allowed public evidence"
                " artifact (expected seat_truth_public_<stamp>.json or"
                " seat_truth_public_table_<stamp>.txt)")
    return errors
