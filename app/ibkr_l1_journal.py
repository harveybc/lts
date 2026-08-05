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

import json
from typing import Any, Optional

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


class L1ExecutionOlap(DemoExecutionOlap):
    """The accepted L0 ledger plus the L1 effects/capability tables."""

    def __init__(self, path) -> None:
        super().__init__(path)
        self._con.executescript(_L1_SCHEMA)

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

        The outbox is the decisions table itself (finding 066: no parallel
        source of truth): a decision becomes consumed exactly when an
        ``l1_effects`` row exists for its idempotency key.
        """
        rows = self._con.execute(
            "SELECT idempotency_key, intent_json, capability_evidence,"
            " reference_price, quote_time, decided_at FROM decisions "
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
            "SELECT intent_json FROM decisions WHERE idempotency_key=?",
            (idempotency_key,),
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
