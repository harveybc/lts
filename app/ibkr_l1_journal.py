"""Durable L1 effects journal inside the accepted L0 ledger (finding 063/064).

One database, one truth: this module extends ``DemoExecutionOlap`` with the
L1 tables instead of creating a parallel store, so capability consumption,
effect journaling, lifecycle events, reservations and halt state all commit
through the same SQLite file and the same ``BEGIN IMMEDIATE`` atomic unit.

The effect state machine distinguishes, durably and across restarts:

- ``journaled_pending``   — the intent-to-act is durable; NO broker call has
                            been attempted (zero ``call_attempt`` facts).
- ``effect_unknown``      — at least one broker call was attempted whose
                            outcome is not proven; nothing here is success.
- ``submitted_pending_ack`` — every planned call returned, but protection is
                            NOT yet verified against direct broker facts.
- ``acknowledged``        — exact acknowledgement verified (executor C).
- ``recovering``          — cancel/flatten/hold in progress.
- terminal states         — ``terminal_flat``, ``terminal_cancelled``,
                            ``terminal_rejected``, ``terminal_failed_held``.

A missing fact never becomes zero or success (auditor order 2026-08-02).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Optional

from app import as_of_lineage
from app.as_of_lineage import (
    AsOfLineageContradiction,
    AsOfLineageError,
)
from app.demo_execution_service import DemoExecutionError, DemoExecutionOlap, _utc_now

EFFECT_STATES = frozenset({
    "journaled_pending",
    "effect_unknown",
    "submitted_pending_ack",
    "acknowledged",
    "recovering",
    "terminal_flat",
    "terminal_cancelled",
    "terminal_rejected",
    "terminal_failed_held",
    # finding 073: attempts are journaled BEFORE every broker call, so zero
    # attempt facts PROVE no broker effect; the crash resolves terminally
    # while the consumed capability stays burned.
    "terminal_aborted_no_call",
})

TERMINAL_EFFECT_STATES = frozenset({
    "terminal_flat",
    "terminal_cancelled",
    "terminal_rejected",
    "terminal_failed_held",
    "terminal_aborted_no_call",
})

# Success is only reachable through acknowledged; unknown never jumps to a
# success-like state without passing exact verification again.
LEGAL_EFFECT_TRANSITIONS: dict[str, frozenset[str]] = {
    "journaled_pending": frozenset(
        {"effect_unknown", "submitted_pending_ack", "recovering",
         "terminal_cancelled", "terminal_rejected",
         "terminal_aborted_no_call"}
    ),
    "effect_unknown": frozenset(
        {"recovering", "submitted_pending_ack", "effect_unknown",
         "terminal_failed_held"}
    ),
    "submitted_pending_ack": frozenset(
        {"acknowledged", "effect_unknown", "recovering"}
    ),
    "acknowledged": frozenset(
        {"recovering", "effect_unknown", "terminal_flat"}
    ),
    "recovering": frozenset(
        {"recovering", "effect_unknown", "terminal_flat", "terminal_cancelled",
         "terminal_failed_held"}
    ),
    "terminal_flat": frozenset(),
    "terminal_cancelled": frozenset(),
    "terminal_rejected": frozenset(),
    "terminal_failed_held": frozenset(),
    "terminal_aborted_no_call": frozenset(),
}


_L1_SCHEMA = """
CREATE TABLE IF NOT EXISTS l1_effects (
    effect_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    capability_sha256 TEXT,
    order_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS l1_broker_facts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    effect_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    fact_kind TEXT NOT NULL,
    fact_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS l1_capabilities (
    capability_sha256 TEXT PRIMARY KEY,
    nonce_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    consumed_effect_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS l1_effect_contracts (
    effect_id TEXT PRIMARY KEY,
    contract_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


#: v1 (finding 259): ``input_sha256`` sat inside the uniqueness key, so a
#: changed input hash minted a NEW row instead of contradicting the one due
#: decision. The table is kept readable and migratable — never rewritten or
#: dropped — while v2 below is the authoritative append-only evidence.
_AS_OF_V1_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS as_of_input_bars ("
    " venue TEXT NOT NULL, model_id TEXT NOT NULL,"
    " timeframe TEXT NOT NULL, bar_close TEXT NOT NULL,"
    " input_sha256 TEXT NOT NULL, feature_contract TEXT NOT NULL,"
    " source TEXT NOT NULL, bars_json TEXT NOT NULL,"
    " bars_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL,"
    " UNIQUE(venue, model_id, timeframe, bar_close, input_sha256))"
)

_AS_OF_V1_COLUMNS = (
    "venue", "model_id", "timeframe", "bar_close", "input_sha256",
    "feature_contract", "source", "bars_json", "bars_sha256", "recorded_at",
)

#: v2: one append-only row per (row_state, normalized due-decision identity).
#: ``identity_sha256`` binds venue+account_fingerprint+instrument+decision_id;
#: ``lineage_sha256`` binds that identity together with EVERY lineage and
#: content field, so a contradiction is a hash comparison, not a heuristic.
_AS_OF_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS as_of_input_bars_v2 (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    schema TEXT NOT NULL,
    row_state TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    venue TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    instrument TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_close TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    feature_contract TEXT NOT NULL,
    bars_sha256 TEXT NOT NULL,
    bars_json TEXT NOT NULL,
    source TEXT NOT NULL,
    identity_sha256 TEXT NOT NULL,
    lineage_sha256 TEXT NOT NULL,
    origin TEXT NOT NULL,
    UNIQUE(row_state, identity_sha256)
);
CREATE INDEX IF NOT EXISTS as_of_v2_by_identity
    ON as_of_input_bars_v2(identity_sha256);
CREATE INDEX IF NOT EXISTS as_of_v2_by_route
    ON as_of_input_bars_v2(venue, account_fingerprint, instrument, bar_close);
CREATE TABLE IF NOT EXISTS as_of_lineage_incidents (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    schema TEXT NOT NULL,
    event TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    incident_key TEXT NOT NULL,
    identity_sha256 TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    venue TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    instrument TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE(event, incident_key)
);
"""

_AS_OF_V2_COLUMNS = (
    "schema", "row_state", "recorded_at", "venue", "account_fingerprint",
    "instrument", "decision_id", "model_id", "artifact_sha256",
    "config_sha256", "timeframe", "bar_close", "input_sha256",
    "feature_contract", "bars_sha256", "bars_json", "source",
    "identity_sha256", "lineage_sha256", "origin",
)

_INCIDENT_COLUMNS = (
    "schema", "event", "recorded_at", "incident_key", "identity_sha256",
    "reason_code", "venue", "account_fingerprint", "instrument",
    "decision_id", "detail_json",
)


class _Contradiction(Exception):
    """Internal: unwinds the atomic unit so the refused write leaves nothing
    behind, then becomes a public :class:`AsOfLineageContradiction` once the
    durable incident has been landed in its own transaction."""

    def __init__(self, existing: dict[str, Any], candidate: dict[str, Any],
                 row_state: str) -> None:
        super().__init__("as-of lineage contradiction")
        self.existing = existing
        self.candidate = candidate
        self.row_state = row_state


class L1ExecutionOlap(DemoExecutionOlap):
    """The accepted L0 ledger plus the L1 effects/capability tables."""

    def __init__(self, path) -> None:
        super().__init__(path)
        self._con.executescript(_L1_SCHEMA)
        self.database_path = str(path)
        self.as_of_journal_path = as_of_lineage.journal_path_for(path)
        self._as_of_schema_ready = False
        self._ensure_as_of_schema()

    # -- effects -----------------------------------------------------------
    def create_effect(
        self,
        effect_id: str,
        idempotency_key: str,
        kind: str,
        order_ids: list[Any],
        capability_sha256: Optional[str] = None,
    ) -> None:
        now = _utc_now().isoformat()
        self._con.execute(
            "INSERT INTO l1_effects VALUES (?,?,?,?,?,?,?,?)",
            (
                effect_id,
                idempotency_key,
                kind,
                "journaled_pending",
                capability_sha256,
                json.dumps(order_ids),
                now,
                now,
            ),
        )

    def effect_row(self, effect_id: str) -> Optional[dict[str, Any]]:
        row = self._con.execute(
            "SELECT effect_id, idempotency_key, kind, state, capability_sha256,"
            " order_ids_json, created_at, updated_at "
            "FROM l1_effects WHERE effect_id=?",
            (effect_id,),
        ).fetchone()
        return None if row is None else self._effect_dict(row)

    def effect_exists_with_key_prefix(self, prefix: str) -> bool:
        """True when ANY effect (any state, terminal included) exists whose
        idempotency key starts with ``prefix``. Used by the exactly-once
        retry gate: a bar whose signal already produced an effect is
        satisfied — reconciliation repairs must never mint a new identity
        for it (duplicate-lifecycle defect, 2026-08-04)."""
        escaped = (
            prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        )
        return (
            self._con.execute(
                "SELECT 1 FROM l1_effects WHERE idempotency_key LIKE ? "
                "ESCAPE '\\' LIMIT 1",
                (escaped + "%",),
            ).fetchone()
            is not None
        )

    def record_due_bar_decision(self, fact: dict[str, Any]) -> bool:
        """Record one effective fact per due bar, preserving revisions.

        Normal replay changes nothing. A temporary hold/reconciliation fact
        may be revised after the condition clears; its prior value is appended
        to ``due_bar_decision_revisions`` before the effective row changes.
        Terminal decisions remain immutable.
        """
        required = (
            "venue", "account_fingerprint", "asset_id", "instrument",
            "timeframe", "bar_close", "decided_at", "input_sha256",
            "config_sha256", "model_id", "artifact_sha256", "action",
            "outcome", "decision_id",
        )
        missing = [key for key in required if not fact.get(key)]
        if missing:
            raise DemoExecutionError(
                f"due-bar decision fact missing {missing}")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS due_bar_decisions ("
            " venue TEXT NOT NULL, account_fingerprint TEXT NOT NULL,"
            " asset_id TEXT NOT NULL, instrument TEXT NOT NULL,"
            " timeframe TEXT NOT NULL, bar_close TEXT NOT NULL,"
            " decided_at TEXT NOT NULL, feature_cutoff TEXT,"
            " input_sha256 TEXT NOT NULL, config_sha256 TEXT NOT NULL,"
            " model_id TEXT NOT NULL, artifact_sha256 TEXT NOT NULL,"
            " manifest_sha256 TEXT, action TEXT NOT NULL,"
            " score REAL, outcome TEXT NOT NULL, reason TEXT,"
            " risk_envelope_json TEXT, quote_json TEXT,"
            " decision_id TEXT NOT NULL, effect_or_command_id TEXT,"
            " UNIQUE(venue, model_id, timeframe, bar_close))"
        )
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS due_bar_decision_revisions ("
            " seq INTEGER PRIMARY KEY AUTOINCREMENT, revised_at TEXT NOT NULL,"
            " venue TEXT NOT NULL, model_id TEXT NOT NULL,"
            " timeframe TEXT NOT NULL, bar_close TEXT NOT NULL,"
            " prior_fact_json TEXT NOT NULL, replacement_fact_json TEXT NOT NULL,"
            " prior_sha256 TEXT NOT NULL, replacement_sha256 TEXT NOT NULL)"
        )
        columns = (
            "venue", "account_fingerprint", "asset_id", "instrument",
            "timeframe", "bar_close", "decided_at", "feature_cutoff",
            "input_sha256", "config_sha256", "model_id", "artifact_sha256",
            "manifest_sha256", "action", "score", "outcome", "reason",
            "risk_envelope_json", "quote_json", "decision_id",
            "effect_or_command_id",
        )
        values = (
            fact["venue"], fact["account_fingerprint"], fact["asset_id"],
            fact["instrument"], fact["timeframe"], fact["bar_close"],
            fact["decided_at"], fact.get("feature_cutoff"),
            fact["input_sha256"], fact["config_sha256"], fact["model_id"],
            fact["artifact_sha256"], fact.get("manifest_sha256"),
            fact["action"], fact.get("score"), fact["outcome"],
            fact.get("reason"),
            json.dumps(fact.get("risk_envelope"), sort_keys=True)
            if fact.get("risk_envelope") is not None else None,
            json.dumps(fact.get("quote"), sort_keys=True)
            if fact.get("quote") is not None else None,
            fact["decision_id"], fact.get("effect_or_command_id"),
        )
        key = (fact["venue"], fact["model_id"], fact["timeframe"],
               fact["bar_close"])
        with self.atomic_unit():
            row = self._con.execute(
                "SELECT * FROM due_bar_decisions WHERE venue=? AND model_id=? "
                "AND timeframe=? AND bar_close=?", key,
            ).fetchone()
            if row is None:
                self._con.execute(
                    "INSERT INTO due_bar_decisions VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
                )
                return True

            prior = dict(zip(columns, row))
            same_effective_result = (
                prior["outcome"] == fact["outcome"]
                and prior["reason"] == fact.get("reason")
                and prior["action"] == fact["action"]
                and prior["decision_id"] == fact["decision_id"]
            )
            if same_effective_result:
                return False

            transient = (
                prior["outcome"] == "deferred"
                or prior["reason"] in (
                    "halted:hold",
                    "reconciliation_required_before_new_risk",
                )
            )
            if not transient:
                raise DemoExecutionError(
                    "terminal due-bar decision cannot be revised"
                )
            replacement = dict(zip(columns, values))
            lineage = (
                "venue", "account_fingerprint", "asset_id", "instrument",
                "timeframe", "bar_close", "feature_cutoff", "input_sha256",
                "config_sha256", "model_id", "artifact_sha256",
                "manifest_sha256", "action", "decision_id",
            )
            drift = [name for name in lineage
                     if prior[name] != replacement[name]]
            if drift:
                raise DemoExecutionError(
                    f"due-bar revision lineage drift: {drift}"
                )
            prior_json = json.dumps(prior, sort_keys=True, separators=(",", ":"))
            replacement_json = json.dumps(
                replacement, sort_keys=True, separators=(",", ":")
            )
            self._con.execute(
                "INSERT INTO due_bar_decision_revisions "
                "(revised_at,venue,model_id,timeframe,bar_close,prior_fact_json,"
                "replacement_fact_json,prior_sha256,replacement_sha256) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (_utc_now().isoformat(), *key, prior_json, replacement_json,
                 hashlib.sha256(prior_json.encode()).hexdigest(),
                 hashlib.sha256(replacement_json.encode()).hexdigest()),
            )
            assignments = ",".join(f"{name}=?" for name in columns[1:])
            self._con.execute(
                f"UPDATE due_bar_decisions SET {assignments} "
                "WHERE venue=? AND model_id=? AND timeframe=? AND bar_close=?",
                (*values[1:], *key),
            )
            return True

    def due_bar_decisions(
        self, *, venue: str | None = None, since: str | None = None
    ) -> list[dict[str, Any]]:
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS due_bar_decisions ("
            " venue TEXT NOT NULL, account_fingerprint TEXT NOT NULL,"
            " asset_id TEXT NOT NULL, instrument TEXT NOT NULL,"
            " timeframe TEXT NOT NULL, bar_close TEXT NOT NULL,"
            " decided_at TEXT NOT NULL, feature_cutoff TEXT,"
            " input_sha256 TEXT NOT NULL, config_sha256 TEXT NOT NULL,"
            " model_id TEXT NOT NULL, artifact_sha256 TEXT NOT NULL,"
            " manifest_sha256 TEXT, action TEXT NOT NULL,"
            " score REAL, outcome TEXT NOT NULL, reason TEXT,"
            " risk_envelope_json TEXT, quote_json TEXT,"
            " decision_id TEXT NOT NULL, effect_or_command_id TEXT,"
            " UNIQUE(venue, model_id, timeframe, bar_close))"
        )
        query = "SELECT * FROM due_bar_decisions"
        clauses, params = [], []
        if venue:
            clauses.append("venue=?")
            params.append(venue)
        if since:
            clauses.append("bar_close >= ?")
            params.append(since)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY bar_close"
        columns = [
            "venue", "account_fingerprint", "asset_id", "instrument",
            "timeframe", "bar_close", "decided_at", "feature_cutoff",
            "input_sha256", "config_sha256", "model_id",
            "artifact_sha256", "manifest_sha256", "action", "score",
            "outcome", "reason", "risk_envelope_json", "quote_json",
            "decision_id", "effect_or_command_id",
        ]
        return [dict(zip(columns, row))
                for row in self._con.execute(query, params)]

    # -- as-of input bars, v2 (findings 259/260) ---------------------------

    def _ensure_as_of_schema(self) -> None:
        """Create both tables once per connection.

        ``executescript`` commits any pending transaction, so this must never
        run inside an ``atomic_unit``: the flag keeps it to construction time
        while leaving every call site safe to state its own precondition.
        """
        if self._as_of_schema_ready:
            return
        self._con.execute(_AS_OF_V1_SCHEMA)
        self._con.executescript(_AS_OF_V2_SCHEMA)
        self._as_of_schema_ready = True

    # ---- legacy v1: readable and migratable, never authoritative ----

    def record_as_of_input_bars(self, fact: dict[str, Any]) -> bool:
        """LEGACY v1 writer, retained so an existing ledger stays readable
        and testable. Finding 259: this key admits contradictory rows for one
        due decision — production writes go through
        :meth:`record_due_bar_decision_with_as_of` instead."""
        required = ("venue", "model_id", "timeframe", "bar_close",
                    "input_sha256", "feature_contract", "source", "bars")
        missing = [key for key in required if not fact.get(key)]
        if missing:
            raise DemoExecutionError(f"as-of bars fact missing {missing}")
        bars_json, bars_sha256 = as_of_lineage.canonical_bars(fact["bars"])
        self._ensure_as_of_schema()
        key = (fact["venue"], fact["model_id"], fact["timeframe"],
               fact["bar_close"], fact["input_sha256"])
        with self.atomic_unit():
            row = self._con.execute(
                "SELECT bars_sha256 FROM as_of_input_bars WHERE venue=?"
                " AND model_id=? AND timeframe=? AND bar_close=?"
                " AND input_sha256=?", key,
            ).fetchone()
            if row is not None:
                if row[0] != bars_sha256:
                    raise DemoExecutionError(
                        "as-of input bars are immutable: same decision"
                        " identity, different bar content")
                return False
            self._con.execute(
                "INSERT INTO as_of_input_bars VALUES (?,?,?,?,?,?,?,?,?,?)",
                (*key, fact["feature_contract"], fact["source"], bars_json,
                 bars_sha256, _utc_now().isoformat()),
            )
            return True

    def as_of_input_bars_row(
        self, *, venue: str, model_id: str, timeframe: str, bar_close: str,
        input_sha256: str,
    ) -> Optional[dict[str, Any]]:
        """LEGACY v1 reader (kept so migrated ledgers stay inspectable)."""
        self._ensure_as_of_schema()
        row = self._con.execute(
            "SELECT venue, model_id, timeframe, bar_close, input_sha256,"
            " feature_contract, source, bars_json, bars_sha256, recorded_at"
            " FROM as_of_input_bars WHERE venue=? AND model_id=?"
            " AND timeframe=? AND bar_close=? AND input_sha256=?",
            (venue, model_id, timeframe, bar_close, input_sha256),
        ).fetchone()
        if row is None:
            return None
        fact = dict(zip(_AS_OF_V1_COLUMNS, row))
        fact["bars"] = json.loads(fact["bars_json"])
        return fact

    def legacy_as_of_input_bars(self) -> list[dict[str, Any]]:
        self._ensure_as_of_schema()
        return [dict(zip(_AS_OF_V1_COLUMNS, row)) for row in self._con.execute(
            "SELECT venue, model_id, timeframe, bar_close, input_sha256,"
            " feature_contract, source, bars_json, bars_sha256, recorded_at"
            " FROM as_of_input_bars ORDER BY rowid")]

    # ---- v2 writes ----

    def _as_of_row(self, *, row_state: str, identity_sha256: str,
                   ) -> Optional[dict[str, Any]]:
        row = self._con.execute(
            f"SELECT {','.join(_AS_OF_V2_COLUMNS)} FROM as_of_input_bars_v2"
            " WHERE row_state=? AND identity_sha256=?",
            (row_state, identity_sha256),
        ).fetchone()
        return None if row is None else dict(zip(_AS_OF_V2_COLUMNS, row))

    def _insert_as_of(self, normalized: dict[str, Any], *, row_state: str,
                      origin: str) -> None:
        self._con.execute(
            f"INSERT INTO as_of_input_bars_v2 ({','.join(_AS_OF_V2_COLUMNS)})"
            f" VALUES ({','.join('?' for _ in _AS_OF_V2_COLUMNS)})",
            (as_of_lineage.AS_OF_SCHEMA, row_state, _utc_now().isoformat(),
             *(normalized[key] for key in (
                 "venue", "account_fingerprint", "instrument", "decision_id",
                 "model_id", "artifact_sha256", "config_sha256", "timeframe",
                 "bar_close", "input_sha256", "feature_contract",
                 "bars_sha256", "bars_json", "source", "identity_sha256",
                 "lineage_sha256")),
             origin),
        )

    def _raise_contradiction(self, event: _Contradiction) -> None:
        """Land ONE durable incident (its own transaction, after the refused
        write has already rolled back) and re-raise as the public error."""
        diverging = as_of_lineage.diverging_fields(
            event.existing, event.candidate)
        incident = self.record_as_of_lineage_incident(
            reason_code=as_of_lineage.REASON_CONTRADICTION,
            identity=event.candidate,
            detail={
                "diverging_fields": diverging,
                "row_state": event.row_state,
                "retained_lineage_sha256": event.existing["lineage_sha256"],
                "refused_lineage_sha256": event.candidate["lineage_sha256"],
                "retained_bars_sha256": event.existing["bars_sha256"],
                "refused_bars_sha256": event.candidate["bars_sha256"],
                "retained_recorded_at": event.existing.get("recorded_at"),
            },
        )
        raise AsOfLineageContradiction(
            "as-of lineage contradiction for one due-decision identity:"
            f" {diverging} changed; the retained row stands and this write is"
            " refused",
            identity_sha256=event.candidate["identity_sha256"],
            diverging=diverging, incident=incident,
        ) from None

    def record_as_of_pending(self, fact: dict[str, Any]) -> dict[str, Any]:
        """Explicit, recoverable PENDING linkage written before the risk
        action, carrying the COMPLETE normalized due-decision identity.

        This is never an orphan: it is typed ``pending``, the comparator
        refuses to treat it as evidence, and :meth:`resolve_pending_as_of`
        either binds it to its due-decision fact or reports it. A pending or
        bound row that already contradicts this content refuses here — before
        anything is submitted.
        """
        normalized = as_of_lineage.normalize(fact)
        self._ensure_as_of_schema()
        try:
            with self.atomic_unit():
                bound = self._as_of_row(
                    row_state=as_of_lineage.BOUND,
                    identity_sha256=normalized["identity_sha256"])
                if bound is not None:
                    if bound["lineage_sha256"] != normalized["lineage_sha256"]:
                        raise _Contradiction(bound, normalized,
                                             as_of_lineage.BOUND)
                    return {"state": "already_bound", "appended": False,
                            "identity_sha256": normalized["identity_sha256"]}
                pending = self._as_of_row(
                    row_state=as_of_lineage.PENDING,
                    identity_sha256=normalized["identity_sha256"])
                if pending is not None:
                    if pending["lineage_sha256"] != normalized["lineage_sha256"]:
                        raise _Contradiction(pending, normalized,
                                             as_of_lineage.PENDING)
                    return {"state": as_of_lineage.PENDING, "appended": False,
                            "identity_sha256": normalized["identity_sha256"]}
                self._insert_as_of(normalized, row_state=as_of_lineage.PENDING,
                                   origin="runner_pre_decision")
                return {"state": as_of_lineage.PENDING, "appended": True,
                        "identity_sha256": normalized["identity_sha256"]}
        except _Contradiction as event:
            self._raise_contradiction(event)

    def record_due_bar_decision_with_as_of(
        self, decision: dict[str, Any], as_of: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist the due-decision FACT and its exact as-of BARS as ONE
        logical operation (finding 260's orphan/loss window).

        Both writes share a single ``BEGIN IMMEDIATE`` unit: a crash between
        them rolls both back, so the ledger never holds an as-of row without
        its due-decision fact and never holds a decision whose as-of evidence
        silently vanished. The as-of fact must project the SAME normalized
        identity and lineage as the decision fact, otherwise nothing is
        written at all.
        """
        normalized = as_of_lineage.normalize(as_of)
        projected = as_of_lineage.identity_of_decision(decision)
        mismatched = sorted(
            key for key in as_of_lineage.IDENTITY_FIELDS
            + as_of_lineage.LINEAGE_FIELDS + ("input_sha256",)
            if normalized[key] != projected[key]
        )
        if mismatched:
            raise AsOfLineageError(
                "as-of fact does not describe this due decision:"
                f" {mismatched} disagree")
        self._ensure_as_of_schema()
        try:
            with self.atomic_unit():
                existing = self._as_of_row(
                    row_state=as_of_lineage.BOUND,
                    identity_sha256=normalized["identity_sha256"])
                if existing is not None:
                    if existing["lineage_sha256"] != normalized["lineage_sha256"]:
                        raise _Contradiction(existing, normalized,
                                             as_of_lineage.BOUND)
                    # byte-identical replay: idempotent, no row, no incident
                    result = {
                        "decision_appended": self.record_due_bar_decision(
                            decision),
                        "as_of_appended": False, "as_of_state": "idempotent",
                        "identity_sha256": normalized["identity_sha256"],
                    }
                else:
                    pending = self._as_of_row(
                        row_state=as_of_lineage.PENDING,
                        identity_sha256=normalized["identity_sha256"])
                    if (pending is not None
                            and pending["lineage_sha256"]
                            != normalized["lineage_sha256"]):
                        raise _Contradiction(pending, normalized,
                                             as_of_lineage.PENDING)
                    # the due-decision FACT lands first: an as-of row can
                    # never precede the identity it belongs to
                    decision_appended = self.record_due_bar_decision(decision)
                    self._insert_as_of(
                        normalized, row_state=as_of_lineage.BOUND,
                        origin="runner_atomic_bind")
                    result = {"decision_appended": decision_appended,
                              "as_of_appended": True,
                              "as_of_state": as_of_lineage.BOUND,
                              "identity_sha256": normalized["identity_sha256"]}
        except _Contradiction as event:
            self._raise_contradiction(event)
        # the evidence for this identity is now whole again: close any
        # self-healing incident it carried (a contradiction never lands here)
        for reason in sorted(as_of_lineage.SELF_HEALING_REASONS):
            self.resolve_as_of_lineage_incident(
                identity_sha256=normalized["identity_sha256"],
                reason_code=reason,
                note="as-of evidence bound for this due-decision identity")
        return result

    # ---- v2 reads ----

    def as_of_bound_row(self, decision: dict[str, Any],
                        ) -> Optional[dict[str, Any]]:
        """The bound as-of row for one due-bar decision, joined ONLY on the
        normalized identity and verified against the decision's lineage.

        A row whose lineage disagrees is not returned as evidence — an
        account/route collision or a swapped artifact can therefore never
        lend its bars to another decision.
        """
        self._ensure_as_of_schema()
        projected = as_of_lineage.identity_of_decision(decision)
        row = self._as_of_row(row_state=as_of_lineage.BOUND,
                              identity_sha256=projected["identity_sha256"])
        if row is None:
            return None
        drift = [key for key in as_of_lineage.LINEAGE_FIELDS + ("input_sha256",)
                 if str(row[key]) != projected[key]]
        if drift:
            return None
        row["bars"] = json.loads(row["bars_json"])
        return row

    def as_of_rows(self, *, row_state: Optional[str] = None,
                   venue: Optional[str] = None) -> list[dict[str, Any]]:
        self._ensure_as_of_schema()
        query = (f"SELECT {','.join(_AS_OF_V2_COLUMNS)}"
                 " FROM as_of_input_bars_v2")
        clauses, params = [], []
        if row_state:
            clauses.append("row_state=?")
            params.append(row_state)
        if venue:
            clauses.append("venue=?")
            params.append(venue)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        return [dict(zip(_AS_OF_V2_COLUMNS, row))
                for row in self._con.execute(query + " ORDER BY seq", params)]

    def due_bar_decision_row(self, *, venue: str, model_id: str,
                             timeframe: str, bar_close: str,
                             ) -> Optional[dict[str, Any]]:
        """The single effective due-bar decision fact for one bar identity
        (the C1 table is unique on exactly this key)."""
        columns = (
            "venue", "account_fingerprint", "asset_id", "instrument",
            "timeframe", "bar_close", "decided_at", "feature_cutoff",
            "input_sha256", "config_sha256", "model_id", "artifact_sha256",
            "manifest_sha256", "action", "score", "outcome", "reason",
            "risk_envelope_json", "quote_json", "decision_id",
            "effect_or_command_id",
        )
        try:
            row = self._con.execute(
                f"SELECT {','.join(columns)} FROM due_bar_decisions"
                " WHERE venue=? AND model_id=? AND timeframe=?"
                " AND bar_close=?",
                (venue, model_id, timeframe, bar_close),
            ).fetchone()
        except sqlite3.Error:
            return None
        return None if row is None else dict(zip(columns, row))

    def unresolved_as_of_pendings(self) -> list[dict[str, Any]]:
        """Pending linkages with no bound row — the recoverable state left by
        a crash between the pre-decision write and the atomic bind."""
        self._ensure_as_of_schema()
        return [dict(zip(_AS_OF_V2_COLUMNS, row)) for row in self._con.execute(
            f"SELECT {','.join('p.' + name for name in _AS_OF_V2_COLUMNS)}"
            " FROM as_of_input_bars_v2 AS p WHERE p.row_state=?"
            " AND NOT EXISTS (SELECT 1 FROM as_of_input_bars_v2 AS b"
            "  WHERE b.row_state=? AND b.identity_sha256=p.identity_sha256)"
            " ORDER BY p.seq",
            (as_of_lineage.PENDING, as_of_lineage.BOUND))]

    def resolve_pending_as_of(self) -> dict[str, Any]:
        """Recover every unresolved pending linkage, exactly once.

        - the due-decision fact exists with matching lineage -> append the
          bound row (the crash is repaired, no orphan, no contradiction);
        - the fact exists with DIFFERENT lineage -> refuse and land one
          durable incident; the pending row is never promoted;
        - the fact does not exist -> the decision never happened; the pending
          row stays typed ``pending`` and is reported, never invented into
          evidence.
        """
        self._ensure_as_of_schema()
        outcome = {"bound": [], "contradicted": [], "still_pending": []}
        for pending in self.unresolved_as_of_pendings():
            decision = self.due_bar_decision_row(
                venue=pending["venue"], model_id=pending["model_id"],
                timeframe=pending["timeframe"],
                bar_close=pending["bar_close"])
            if decision is None or as_of_lineage.identity_of_decision(
                    decision)["identity_sha256"] != pending["identity_sha256"]:
                # the due decision never landed under this identity, so no
                # comparison row lost its inputs: typed and counted, never an
                # incident and never promoted into evidence
                outcome["still_pending"].append(pending["identity_sha256"])
                continue
            projected = as_of_lineage.identity_of_decision(decision)
            drift = [key for key
                     in as_of_lineage.LINEAGE_FIELDS + ("input_sha256",)
                     if str(pending[key]) != projected[key]]
            if drift:
                candidate = dict(pending)
                candidate.update({key: projected[key] for key in drift})
                candidate["lineage_sha256"] = as_of_lineage.lineage_digest(
                    candidate)
                try:
                    self._raise_contradiction(
                        _Contradiction(pending, candidate,
                                       as_of_lineage.PENDING))
                except AsOfLineageContradiction:
                    outcome["contradicted"].append(pending["identity_sha256"])
                continue
            with self.atomic_unit():
                if self._as_of_row(
                        row_state=as_of_lineage.BOUND,
                        identity_sha256=pending["identity_sha256"]) is None:
                    self._insert_as_of(pending, row_state=as_of_lineage.BOUND,
                                       origin="pending_recovery")
            self.resolve_as_of_lineage_incident(
                identity_sha256=pending["identity_sha256"],
                reason_code=as_of_lineage.REASON_PENDING_UNRESOLVED,
                note="pending linkage recovered and bound")
            outcome["bound"].append(pending["identity_sha256"])
        return outcome

    # ---- durable incidents ----

    def record_as_of_lineage_incident(
        self, *, reason_code: str, identity: dict[str, Any],
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        """Land ONE durable incident per (identity, typed reason).

        Written twice on purpose: into the ledger's append-only incident
        table AND into a sidecar journal beside the ledger file, because
        finding 260's worst case is the ledger itself refusing writes. Both
        sinks dedup on the same key, so the pair is still exactly one
        incident. Routing/paging stays with the existing fleet incident
        router — this is the durable FACT, not a second alerting stack.
        """
        if reason_code not in as_of_lineage.REASONS:
            raise AsOfLineageError(f"untyped lineage reason {reason_code!r}")
        identity_sha256 = str(
            identity.get("identity_sha256")
            or as_of_lineage.identity_of_decision(identity)["identity_sha256"])
        record = {
            "schema": as_of_lineage.INCIDENT_SCHEMA,
            "event": "opened",
            "recorded_at": _utc_now().isoformat(),
            "incident_key": as_of_lineage.incident_key(
                identity_sha256, reason_code),
            "identity_sha256": identity_sha256,
            "reason_code": reason_code,
            "venue": str(identity.get("venue") or "unknown"),
            "account_fingerprint": str(
                identity.get("account_fingerprint") or "unknown"),
            "instrument": str(identity.get("instrument") or "unknown"),
            "decision_id": str(identity.get("decision_id") or "unknown"),
            "detail_json": json.dumps(detail, sort_keys=True, default=str),
        }
        record["journaled"] = as_of_lineage.append_journal(
            self.as_of_journal_path, record)
        try:
            record["persisted"] = self._insert_incident(record)
        except Exception:
            # recording an incident must never raise a second failure over
            # the first; the sidecar journal already holds the evidence
            record["persisted"] = False
        return record

    def _insert_incident(self, record: dict[str, Any]) -> bool:
        try:
            self._ensure_as_of_schema()
            cursor = self._con.execute(
                "INSERT OR IGNORE INTO as_of_lineage_incidents"
                f" ({','.join(_INCIDENT_COLUMNS)})"
                f" VALUES ({','.join('?' for _ in _INCIDENT_COLUMNS)})",
                tuple(record[name] for name in _INCIDENT_COLUMNS),
            )
            return cursor.rowcount == 1
        except sqlite3.Error:
            # the sidecar journal already holds this incident; a ledger that
            # cannot record its own failure is exactly why it exists
            return False

    def resolve_as_of_lineage_incident(
        self, *, identity_sha256: str, reason_code: str, note: str,
    ) -> bool:
        """Append the ``resolved`` event for one open incident.

        A contradiction is NOT self-healing: only an explicit, recorded call
        closes it, and the closure keeps the original ``opened`` row.
        """
        if reason_code not in as_of_lineage.REASONS:
            raise AsOfLineageError(f"untyped lineage reason {reason_code!r}")
        key = as_of_lineage.incident_key(identity_sha256, reason_code)
        self._ensure_as_of_schema()
        opened = self._con.execute(
            "SELECT venue, account_fingerprint, instrument, decision_id"
            " FROM as_of_lineage_incidents WHERE event='opened'"
            " AND incident_key=?", (key,),
        ).fetchone()
        journal = as_of_lineage.read_journal(self.as_of_journal_path)
        if opened is None:
            match = next((item for item in journal
                          if item.get("event") == "opened"
                          and item.get("incident_key") == key), None)
            if match is None:
                return False
            opened = (match.get("venue"), match.get("account_fingerprint"),
                      match.get("instrument"), match.get("decision_id"))
        record = {
            "schema": as_of_lineage.INCIDENT_SCHEMA,
            "event": "resolved",
            "recorded_at": _utc_now().isoformat(),
            "incident_key": key,
            "identity_sha256": identity_sha256,
            "reason_code": reason_code,
            "venue": str(opened[0]), "account_fingerprint": str(opened[1]),
            "instrument": str(opened[2]), "decision_id": str(opened[3]),
            "detail_json": json.dumps({"note": note}, sort_keys=True),
        }
        journaled = as_of_lineage.append_journal(
            self.as_of_journal_path, record)
        try:
            persisted = self._insert_incident(record)
        except Exception:
            persisted = False
        return bool(journaled or persisted)

    def as_of_lineage_events(self) -> list[dict[str, Any]]:
        """Every incident event from BOTH sinks, merged. Duplicates collapse
        on ``(event, incident_key)`` in :func:`as_of_lineage.open_incidents`."""
        events: list[dict[str, Any]] = []
        try:
            self._ensure_as_of_schema()
            events.extend(
                dict(zip(_INCIDENT_COLUMNS, row)) for row in self._con.execute(
                    f"SELECT {','.join(_INCIDENT_COLUMNS)}"
                    " FROM as_of_lineage_incidents ORDER BY seq"))
        except sqlite3.Error:
            pass
        events.extend(as_of_lineage.read_journal(self.as_of_journal_path))
        return events

    def as_of_lineage_health(self) -> dict[str, Any]:
        """Durable comparison-lineage health for the runner heartbeat.

        Derived from the durable incident events, so a restart cannot wash a
        degradation away and a report can name the same incident.
        """
        try:
            return as_of_lineage.health(self.as_of_lineage_events())
        except Exception as exc:                     # never break a heartbeat
            return {
                "comparison_lineage_state": as_of_lineage.DEGRADED,
                "comparison_lineage_reason":
                    f"health_unreadable: {type(exc).__name__}",
                "comparison_lineage_open_incidents": None,
                "comparison_lineage_last_incident": None,
            }

    def migrate_as_of_v1_to_v2(self) -> dict[str, Any]:
        """Forward-migrate legacy v1 rows that a due-decision fact can bind.

        v1 rows are READ, never altered or dropped. A v1 row is migrated only
        when exactly one due-bar decision supplies the missing identity
        (account fingerprint, instrument, decision id) and its lineage agrees;
        an ambiguous or contradictory v1 row is reported, never guessed into
        evidence.
        """
        self._ensure_as_of_schema()
        report = {"migrated": 0, "already_present": 0, "unbindable": [],
                  "contradictory": []}
        for legacy in self.legacy_as_of_input_bars():
            decision = self.due_bar_decision_row(
                venue=legacy["venue"], model_id=legacy["model_id"],
                timeframe=legacy["timeframe"],
                bar_close=legacy["bar_close"])
            if decision is None:
                report["unbindable"].append({
                    "venue": legacy["venue"], "model_id": legacy["model_id"],
                    "bar_close": legacy["bar_close"],
                    "candidate_decisions": 0})
                continue
            if decision["input_sha256"] != legacy["input_sha256"]:
                report["contradictory"].append({
                    "venue": legacy["venue"], "bar_close": legacy["bar_close"],
                    "decision_input_sha256": decision["input_sha256"],
                    "legacy_input_sha256": legacy["input_sha256"]})
                continue
            normalized = as_of_lineage.normalize({
                **{key: decision[key] for key in
                   as_of_lineage.IDENTITY_FIELDS + as_of_lineage.LINEAGE_FIELDS},
                "input_sha256": legacy["input_sha256"],
                "feature_contract": legacy["feature_contract"],
                "source": legacy["source"],
                "bars_json": legacy["bars_json"],
            })
            with self.atomic_unit():
                existing = self._as_of_row(
                    row_state=as_of_lineage.BOUND,
                    identity_sha256=normalized["identity_sha256"])
                if existing is not None:
                    if existing["lineage_sha256"] != normalized["lineage_sha256"]:
                        report["contradictory"].append({
                            "venue": legacy["venue"],
                            "bar_close": legacy["bar_close"],
                            "reason": "v2 row already binds different content"})
                    else:
                        report["already_present"] += 1
                    continue
                self._insert_as_of(normalized, row_state=as_of_lineage.BOUND,
                                   origin="v1_migration")
                report["migrated"] += 1
        return report

    def effects_with_key_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """All effects whose idempotency key starts with ``prefix``."""
        escaped = (
            prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        )
        rows = self._con.execute(
            "SELECT effect_id, idempotency_key, state FROM l1_effects "
            "WHERE idempotency_key LIKE ? ESCAPE '\\' ORDER BY created_at",
            (escaped + "%",),
        ).fetchall()
        return [{"effect_id": r[0], "idempotency_key": r[1], "state": r[2]}
                for r in rows]

    def reservation_has_open_exposure(self, reservation_id: str) -> bool:
        return (
            self._con.execute(
                "SELECT 1 FROM exposures WHERE source_reservation_id=? "
                "AND state='open' LIMIT 1",
                (reservation_id,),
            ).fetchone()
            is not None
        )

    def reject_decision(self, idempotency_key: str, reason: str) -> bool:
        """Finding 101: terminalize a queued would_be_order decision as a
        durable, lineage-bound rejection (e.g. daily order-budget
        exhaustion) with zero submission — never an exception path."""
        cursor = self._con.execute(
            "UPDATE decisions SET outcome='rejected', reason=? "
            "WHERE idempotency_key=? AND outcome='would_be_order'",
            (reason, idempotency_key),
        )
        return cursor.rowcount == 1

    def supersede_decision(self, idempotency_key: str, reason: str) -> bool:
        """Terminalize a queued would_be_order decision whose signal is
        already satisfied by an existing effect. This is the legitimate
        code path for retiring defect-era duplicates — production state is
        never cleared by direct SQLite manipulation."""
        cursor = self._con.execute(
            "UPDATE decisions SET outcome='superseded', reason=? "
            "WHERE idempotency_key=? AND outcome='would_be_order'",
            (reason, idempotency_key),
        )
        return cursor.rowcount == 1

    def effect_by_key(self, idempotency_key: str) -> Optional[dict[str, Any]]:
        row = self._con.execute(
            "SELECT effect_id, idempotency_key, kind, state, capability_sha256,"
            " order_ids_json, created_at, updated_at "
            "FROM l1_effects WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return None if row is None else self._effect_dict(row)

    def set_effect_order_ids(self, effect_id: str, order_ids: list[Any]) -> None:
        """Bind broker ids before the first call; never rewrite a non-empty set."""
        row = self.effect_row(effect_id)
        if row is None:
            raise DemoExecutionError(f"effect {effect_id} does not exist")
        if row["order_ids"] and row["order_ids"] != order_ids:
            raise DemoExecutionError("effect order ids are immutable once bound")
        self._con.execute(
            "UPDATE l1_effects SET order_ids_json=?, updated_at=? WHERE effect_id=?",
            (json.dumps(order_ids), _utc_now().isoformat(), effect_id),
        )

    def nonterminal_effects(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in TERMINAL_EFFECT_STATES)
        rows = self._con.execute(
            "SELECT effect_id, idempotency_key, kind, state, capability_sha256,"
            " order_ids_json, created_at, updated_at FROM l1_effects "
            f"WHERE state NOT IN ({placeholders}) ORDER BY created_at",
            tuple(sorted(TERMINAL_EFFECT_STATES)),
        ).fetchall()
        return [self._effect_dict(row) for row in rows]

    @staticmethod
    def _effect_dict(row) -> dict[str, Any]:
        return {
            "effect_id": row[0],
            "idempotency_key": row[1],
            "kind": row[2],
            "state": row[3],
            "capability_sha256": row[4],
            "order_ids": json.loads(row[5]),
            "created_at": row[6],
            "updated_at": row[7],
        }

    def advance_effect(self, effect_id: str, target: str) -> None:
        if target not in EFFECT_STATES:
            raise DemoExecutionError(f"unknown effect state {target!r}")
        with self.atomic_unit():
            row = self.effect_row(effect_id)
            if row is None:
                raise DemoExecutionError(f"effect {effect_id} does not exist")
            current = row["state"]
            if target not in LEGAL_EFFECT_TRANSITIONS[current]:
                raise DemoExecutionError(
                    f"illegal effect transition {current!r} -> {target!r}"
                )
            self._con.execute(
                "UPDATE l1_effects SET state=?, updated_at=? WHERE effect_id=?",
                (target, _utc_now().isoformat(), effect_id),
            )

    # -- immutable effect contracts (finding 072) --------------------------
    def store_effect_contract(self, effect_id: str, contract: dict[str, Any]) -> None:
        """Persist the canonical submitted plan and its bindings BEFORE any
        broker call. Immutable: a second store for the same effect refuses."""
        self._con.execute(
            "INSERT INTO l1_effect_contracts VALUES (?,?,?)",
            (
                effect_id,
                json.dumps(contract, sort_keys=True),
                _utc_now().isoformat(),
            ),
        )

    def effect_contract(self, effect_id: str) -> Optional[dict[str, Any]]:
        row = self._con.execute(
            "SELECT contract_json FROM l1_effect_contracts WHERE effect_id=?",
            (effect_id,),
        ).fetchone()
        return None if row is None else json.loads(row[0])

    # -- broker facts (append-only) ----------------------------------------
    def record_broker_fact(
        self, effect_id: str, fact_kind: str, fact: dict[str, Any]
    ) -> None:
        self._con.execute(
            "INSERT INTO l1_broker_facts (effect_id, recorded_at, fact_kind,"
            " fact_json) VALUES (?,?,?,?)",
            (
                effect_id,
                _utc_now().isoformat(),
                fact_kind,
                json.dumps(fact, sort_keys=True, default=str),
            ),
        )

    def broker_facts(
        self, effect_id: str, fact_kind: Optional[str] = None
    ) -> list[dict[str, Any]]:
        if fact_kind is None:
            rows = self._con.execute(
                "SELECT fact_kind, fact_json, recorded_at FROM l1_broker_facts "
                "WHERE effect_id=? ORDER BY seq",
                (effect_id,),
            ).fetchall()
        else:
            rows = self._con.execute(
                "SELECT fact_kind, fact_json, recorded_at FROM l1_broker_facts "
                "WHERE effect_id=? AND fact_kind=? ORDER BY seq",
                (effect_id, fact_kind),
            ).fetchall()
        return [
            {"fact_kind": r[0], "fact": json.loads(r[1]), "recorded_at": r[2]}
            for r in rows
        ]

    def l1_pending_decisions(self, outcome: str) -> list[dict[str, Any]]:
        """Committed L0 decisions of one outcome that have no L1 effect yet.

        The outbox is the effective L0 decision stream (finding 066: no
        parallel source of truth). A narrowly authorized revision supersedes
        a legacy temporary rejection without deleting it; an ``l1_effects``
        row still consumes the idempotency key exactly once.
        """
        rows = self._con.execute(
            "WITH effective AS ("
            " SELECT idempotency_key, intent_json, capability_evidence,"
            " reference_price, quote_time, decided_at, outcome "
            " FROM decision_revisions "
            " UNION ALL "
            " SELECT d.idempotency_key, d.intent_json, d.capability_evidence,"
            " d.reference_price, d.quote_time, d.decided_at, d.outcome "
            " FROM decisions d WHERE d.idempotency_key NOT IN "
            " (SELECT idempotency_key FROM decision_supersessions)"
            ") SELECT idempotency_key, intent_json, capability_evidence,"
            " reference_price, quote_time, decided_at FROM effective "
            "WHERE outcome=? AND intent_json IS NOT NULL "
            "AND idempotency_key NOT IN (SELECT idempotency_key FROM l1_effects) "
            "ORDER BY decided_at",
            (outcome,),
        ).fetchall()
        return [
            {
                "idempotency_key": r[0],
                "intent_json": r[1],
                "capability_evidence": r[2],
                "reference_price": r[3],
                "quote_time": r[4],
                "decided_at": r[5],
            }
            for r in rows
        ]

    def decision_intent_json(self, idempotency_key: str) -> Optional[str]:
        row = self._con.execute(
            "SELECT intent_json FROM decision_revisions "
            "WHERE idempotency_key=? UNION ALL "
            "SELECT d.intent_json FROM decisions d "
            "WHERE d.idempotency_key=? AND d.idempotency_key NOT IN "
            "(SELECT idempotency_key FROM decision_supersessions) LIMIT 1",
            (idempotency_key, idempotency_key),
        ).fetchone()
        return None if row is None else row[0]

    def exposure_reservation(self, order_intent_id: str) -> Optional[str]:
        row = self._con.execute(
            "SELECT source_reservation_id FROM exposures "
            "WHERE order_intent_id=?",
            (order_intent_id,),
        ).fetchone()
        return row[0] if row else None

    def l1_entry_count(self) -> int:
        """Entries that consumed activation budget (refusals do not)."""
        return self._con.execute(
            "SELECT COUNT(*) FROM l1_effects WHERE kind='bracket_entry' "
            "AND state != 'terminal_rejected'"
        ).fetchone()[0]

    def effect_count_since(self, kind: str, since: str) -> int:
        """Count non-rejected effects of one kind from an ISO-8601 boundary."""
        return int(self._con.execute(
            "SELECT COUNT(*) FROM l1_effects WHERE kind=? AND created_at>=? "
            "AND state != 'terminal_rejected'",
            (kind, since),
        ).fetchone()[0])

    def l1_effect_state_counts(self) -> dict[str, int]:
        rows = self._con.execute(
            "SELECT state, COUNT(*) FROM l1_effects GROUP BY state"
        ).fetchall()
        return {state: count for state, count in rows}

    def l1_broker_fact_counts(self) -> dict[str, int]:
        rows = self._con.execute(
            "SELECT fact_kind, COUNT(*) FROM l1_broker_facts GROUP BY fact_kind"
        ).fetchall()
        return {kind: count for kind, count in rows}

    # -- capabilities (single-use, consumed atomically with an effect) -----
    def consume_capability(
        self,
        capability_sha256: str,
        nonce_sha256: str,
        metadata: dict[str, Any],
        effect_id: str,
    ) -> None:
        """Burn a capability digest. UNIQUE constraints make the burn atomic:
        a concurrent second consumer hits IntegrityError inside its own
        transaction and must treat the capability as spent."""
        self._con.execute(
            "INSERT INTO l1_capabilities VALUES (?,?,?,?,?,?)",
            (
                capability_sha256,
                nonce_sha256,
                "consumed",
                _utc_now().isoformat(),
                effect_id,
                json.dumps(metadata, sort_keys=True),
            ),
        )

    def capability_row(self, capability_sha256: str) -> Optional[dict[str, Any]]:
        row = self._con.execute(
            "SELECT capability_sha256, nonce_sha256, state, consumed_at,"
            " consumed_effect_id, metadata_json FROM l1_capabilities "
            "WHERE capability_sha256=?",
            (capability_sha256,),
        ).fetchone()
        if row is None:
            return None
        return {
            "capability_sha256": row[0],
            "nonce_sha256": row[1],
            "state": row[2],
            "consumed_at": row[3],
            "consumed_effect_id": row[4],
            "metadata": json.loads(row[5]),
        }

    def nonce_consumed(self, nonce_sha256: str) -> bool:
        return (
            self._con.execute(
                "SELECT 1 FROM l1_capabilities WHERE nonce_sha256=?",
                (nonce_sha256,),
            ).fetchone()
            is not None
        )
