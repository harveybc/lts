"""Normalized as-of lineage identity for the sim-vs-live comparator (v2).

Corrects AUD-F2-20260816-259 and AUD-F2-20260816-260.

**259** — the v1 ``as_of_input_bars`` table put ``input_sha256`` INSIDE its
uniqueness key, so changing the input hash changed the purported decision
identity and one due decision could retain two contradictory as-of rows.
Here the row binds to the ALREADY NORMALIZED due-decision identity

``venue + account_fingerprint + instrument + decision_id``

and every lineage/content field is bound to that identity by content hash:

``model_id + artifact_sha256 + config_sha256 + timeframe + bar_close``
``input_sha256 + feature_contract + bars_sha256``

The same identity with ANY changed lineage field or changed bars is a
CONTRADICTION: it is refused and lands one durable incident. A byte
identical replay is idempotent (no row, no incident).

**260** — losing as-of evidence used to be one transient stdout line. Every
refusal or persistence failure now lands a durable, deduplicated incident
(ledger table plus a ledger-independent sidecar journal, so a failure of
the ledger itself is still recorded), the runner heartbeat exposes
``comparison_lineage_state=degraded`` with a typed reason, and the
sim-vs-live report names the incident instead of reporting generic
missing data.

Nothing here decides trading. Trading safety stays exactly as designed:
these facts are evidence about comparability, never a risk gate.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

AS_OF_SCHEMA = "lts.as_of_input_bars.v2"
INCIDENT_SCHEMA = "lts.as_of_lineage_incident.v1"

#: the normalized due-decision identity — nothing else may key an as-of row
IDENTITY_FIELDS = ("venue", "account_fingerprint", "instrument", "decision_id")
#: lineage bound to that identity; any change is a contradiction
LINEAGE_FIELDS = ("model_id", "artifact_sha256", "config_sha256",
                  "timeframe", "bar_close")
#: as-of content bound to that identity; any change is a contradiction
CONTENT_FIELDS = ("input_sha256", "feature_contract", "bars_sha256")

BOUND = "bound"
PENDING = "pending"

# typed incident reasons — never invent an untyped one
REASON_CONTRADICTION = "as_of_lineage_contradiction"
REASON_PERSISTENCE_FAILURE = "as_of_persistence_failure"
REASON_PENDING_UNRESOLVED = "as_of_pending_unresolved"
REASONS = frozenset({
    REASON_CONTRADICTION,
    REASON_PERSISTENCE_FAILURE,
    REASON_PENDING_UNRESOLVED,
})

HEALTHY = "healthy"
DEGRADED = "degraded"

#: reasons that a later successful bind of the SAME identity clears; a
#: contradiction is never auto-cleared — the evidence is irreconcilable and
#: only an explicit, recorded operator resolution closes it.
SELF_HEALING_REASONS = frozenset({REASON_PERSISTENCE_FAILURE,
                                  REASON_PENDING_UNRESOLVED})


class AsOfLineageError(RuntimeError):
    """Any refusal of the as-of lineage contract."""


class AsOfLineageContradiction(AsOfLineageError):
    """Same due-decision identity, different lineage or different bars.

    Carries the exact diverging field names and the durable incident that
    was landed for the identity, so no caller has to re-derive them.
    """

    def __init__(self, message: str, *, identity_sha256: str,
                 diverging: list[str], incident: Optional[dict[str, Any]] = None
                 ) -> None:
        super().__init__(message)
        self.identity_sha256 = identity_sha256
        self.diverging = list(diverging)
        self.incident = incident


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":"),
                   default=str).encode()
    ).hexdigest()


def canonical_bars(bars: Any) -> tuple[str, str]:
    """Exact, order-preserving canonical form of the closed-bar window and
    its content hash. The bar LIST order is evidence, so it is preserved;
    only each bar's key order is normalized."""
    if not isinstance(bars, (list, tuple)) or not bars:
        raise AsOfLineageError("as-of bars must be a non-empty sequence")
    body = json.dumps(list(bars), sort_keys=True, separators=(",", ":"),
                      default=str)
    return body, hashlib.sha256(body.encode()).hexdigest()


def normalize(fact: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and validate one as-of fact into its identity, lineage and
    content triple plus the two binding digests.

    A missing or blank field is refused here, before any write: an as-of
    row whose identity is not fully known must never reach the table.
    """
    required = IDENTITY_FIELDS + LINEAGE_FIELDS + (
        "input_sha256", "feature_contract", "source")
    values: dict[str, Any] = {}
    missing = []
    for key in required:
        value = fact.get(key)
        if value is None or str(value).strip() == "":
            missing.append(key)
        else:
            values[key] = str(value).strip()
    if missing:
        raise AsOfLineageError(f"as-of lineage fact missing {sorted(missing)}")
    if "bars_json" in fact and "bars" not in fact:
        bars_json = str(fact["bars_json"])
        bars_sha256 = hashlib.sha256(bars_json.encode()).hexdigest()
    else:
        bars_json, bars_sha256 = canonical_bars(fact.get("bars"))
    values["bars_json"] = bars_json
    values["bars_sha256"] = bars_sha256
    values["identity_sha256"] = _digest(
        {key: values[key] for key in IDENTITY_FIELDS})
    values["lineage_sha256"] = lineage_digest(values)
    return values


def lineage_digest(values: Mapping[str, Any]) -> str:
    """Binding digest over identity + lineage + content — the single value
    that decides idempotent replay versus contradiction."""
    return _digest({
        key: str(values.get(key, ""))
        for key in IDENTITY_FIELDS + LINEAGE_FIELDS + CONTENT_FIELDS
    })


def identity_of_decision(decision: Mapping[str, Any]) -> dict[str, str]:
    """The identity/lineage projection of a normalized due-bar decision
    fact — the only sanctioned way to look an as-of row up."""
    projected = {}
    for key in IDENTITY_FIELDS + LINEAGE_FIELDS + ("input_sha256",):
        value = decision.get(key)
        projected[key] = "" if value is None else str(value).strip()
    projected["identity_sha256"] = _digest(
        {key: projected[key] for key in IDENTITY_FIELDS})
    return projected


def diverging_fields(existing: Mapping[str, Any],
                     candidate: Mapping[str, Any]) -> list[str]:
    """Exact list of bound fields whose content differs. ``bars_sha256``
    standing alone means the lineage agrees but the BARS changed."""
    return sorted(
        key for key in LINEAGE_FIELDS + CONTENT_FIELDS
        if str(existing.get(key, "")) != str(candidate.get(key, ""))
    )


def incident_key(identity_sha256: str, reason_code: str) -> str:
    """One durable incident per (identity, typed reason) — the dedup key."""
    return hashlib.sha256(
        f"{identity_sha256}:{reason_code}".encode()).hexdigest()


# --------------------------------------------------------------- sidecar

def journal_path_for(database_path: str | os.PathLike[str]) -> Path:
    """Sidecar incident journal beside the ledger.

    Deliberately a separate FILE: finding 260's worst case is the ledger
    itself refusing writes, and an incident that can only live inside the
    failing store is not durable evidence.
    """
    path = Path(os.path.expandvars(str(database_path))).expanduser()
    return path.with_name(path.name + ".as_of_incidents.jsonl")


def append_journal(path: str | os.PathLike[str],
                   record: Mapping[str, Any]) -> bool:
    """Append one incident event, deduplicated by ``(event, incident_key)``.

    Returns True only when a NEW event line was written. Never raises: the
    caller is already on a failure path and a failed journal write must not
    replace the original failure.
    """
    try:
        destination = Path(os.path.expandvars(str(path))).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        seen = {(item.get("event"), item.get("incident_key"))
                for item in read_journal(destination)}
        if (record.get("event"), record.get("incident_key")) in seen:
            return False
        line = json.dumps(dict(record), sort_keys=True, default=str)
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except Exception:
        return False


def read_journal(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every readable event line; an unreadable/partial line is skipped,
    never guessed."""
    try:
        destination = Path(os.path.expandvars(str(path))).expanduser()
        text = destination.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


# ---------------------------------------------------------------- health

def build_as_of_fact(*, venue: str, account_fingerprint: str, instrument: str,
                     model_id: str, artifact_sha256: str, config_sha256: str,
                     timeframe: str, source: str,
                     observation: Mapping[str, Any], bars: Any,
                     ) -> dict[str, Any]:
    """The as-of fact for one due decision, keyed by the SAME normalized
    identity the due-bar decision fact uses (``model_id:bar_close``)."""
    bar_close = str(observation["last_closed_bar"])
    return {
        "venue": venue, "account_fingerprint": account_fingerprint,
        "instrument": instrument, "decision_id": f"{model_id}:{bar_close}",
        "model_id": model_id, "artifact_sha256": artifact_sha256,
        "config_sha256": config_sha256, "timeframe": timeframe,
        "bar_close": bar_close,
        "input_sha256": observation["input_sha256"],
        "feature_contract": observation["feature_contract"],
        "source": source, "bars": bars,
    }


def begin_as_of(olap: Any, fact: Mapping[str, Any]) -> dict[str, Any]:
    """Open the explicit, recoverable PENDING linkage before the risk action.

    Never raises into a tick. A contradiction discovered here is already
    durable (the journal recorded it) and is reported as a typed outcome.
    """
    try:
        return {"ok": True, **olap.record_as_of_pending(dict(fact))}
    except AsOfLineageContradiction as exc:
        return {"ok": False, "reason": REASON_CONTRADICTION,
                "diverging": exc.diverging,
                "identity_sha256": exc.identity_sha256}
    except Exception as exc:
        incident = _persistence_incident(olap, fact, exc, phase="pending")
        return {"ok": False, "reason": REASON_PERSISTENCE_FAILURE,
                "error": f"{type(exc).__name__}: {exc}"[:200],
                "incident_key": (incident or {}).get("incident_key")}


def persist_due_bar(olap: Any, decision: Mapping[str, Any],
                    as_of: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Persist the due-decision FACT and its exact as-of BARS as one logical
    operation. Never raises into a tick; trading safety is unchanged.

    A contradiction refuses the as-of bind (the retained evidence stands and
    one durable incident is already landed) but still records the C1 decision
    fact, which is separate trading evidence. A persistence failure lands one
    durable incident so the loss is visible in health and in the report
    instead of scrolling past as one stdout line.
    """
    if as_of is None:
        try:
            appended = olap.record_due_bar_decision(dict(decision))
            return {"ok": True, "as_of_state": "absent",
                    "decision_appended": appended}
        except Exception as exc:
            return {"ok": False, "reason": REASON_PERSISTENCE_FAILURE,
                    "error": f"{type(exc).__name__}: {exc}"[:200]}
    try:
        return {"ok": True,
                **olap.record_due_bar_decision_with_as_of(dict(decision),
                                                          dict(as_of))}
    except AsOfLineageContradiction as exc:
        outcome = {"ok": False, "reason": REASON_CONTRADICTION,
                   "diverging": exc.diverging,
                   "identity_sha256": exc.identity_sha256,
                   "incident_key": (exc.incident or {}).get("incident_key")}
        try:    # the trading fact is separate evidence and must still land
            outcome["decision_appended"] = olap.record_due_bar_decision(
                dict(decision))
        except Exception as nested:
            outcome["decision_error"] = \
                f"{type(nested).__name__}: {nested}"[:200]
        return outcome
    except Exception as exc:
        incident = _persistence_incident(olap, as_of, exc, phase="bind")
        return {"ok": False, "reason": REASON_PERSISTENCE_FAILURE,
                "error": f"{type(exc).__name__}: {exc}"[:200],
                "incident_key": (incident or {}).get("incident_key")}


def _persistence_incident(olap: Any, fact: Mapping[str, Any],
                          error: BaseException, *, phase: str,
                          ) -> Optional[dict[str, Any]]:
    try:
        return olap.record_as_of_lineage_incident(
            reason_code=REASON_PERSISTENCE_FAILURE,
            identity=dict(fact),
            detail={"phase": phase,
                    "error": f"{type(error).__name__}: {error}"[:400]})
    except Exception:
        return None


def open_incidents(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse append-only ``opened``/``resolved`` events into the set of
    still-open incidents, newest first. Both the ledger table and the
    sidecar journal produce these events, so they merge by construction."""
    opened: dict[str, dict[str, Any]] = {}
    resolved: set[str] = set()
    for event in events:
        key = str(event.get("incident_key") or "")
        if not key:
            continue
        if event.get("event") == "resolved":
            resolved.add(key)
        elif event.get("event") == "opened":
            opened.setdefault(key, dict(event))
    still_open = [value for key, value in opened.items()
                  if key not in resolved]
    still_open.sort(key=lambda item: str(item.get("recorded_at") or ""),
                    reverse=True)
    return still_open


def health(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The durable comparison-lineage health published in the heartbeat.

    ``degraded`` persists across restarts because it is derived from the
    durable incident events, not from an in-memory flag.
    """
    incidents = open_incidents(events)
    if not incidents:
        return {
            "comparison_lineage_state": HEALTHY,
            "comparison_lineage_reason": None,
            "comparison_lineage_open_incidents": 0,
            "comparison_lineage_last_incident": None,
        }
    newest = incidents[0]
    reasons = sorted({str(item.get("reason_code")) for item in incidents})
    return {
        "comparison_lineage_state": DEGRADED,
        "comparison_lineage_reason": ",".join(reasons),
        "comparison_lineage_open_incidents": len(incidents),
        "comparison_lineage_last_incident": {
            "incident_key": newest.get("incident_key"),
            "identity_sha256": newest.get("identity_sha256"),
            "reason_code": newest.get("reason_code"),
            "recorded_at": newest.get("recorded_at"),
            "venue": newest.get("venue"),
            "instrument": newest.get("instrument"),
            "decision_id": newest.get("decision_id"),
        },
    }
