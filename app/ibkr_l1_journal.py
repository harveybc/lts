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
})

TERMINAL_EFFECT_STATES = frozenset({
    "terminal_flat",
    "terminal_cancelled",
    "terminal_rejected",
    "terminal_failed_held",
})

# Success is only reachable through acknowledged; unknown never jumps to a
# success-like state without passing exact verification again.
LEGAL_EFFECT_TRANSITIONS: dict[str, frozenset[str]] = {
    "journaled_pending": frozenset(
        {"effect_unknown", "submitted_pending_ack", "recovering",
         "terminal_cancelled", "terminal_rejected"}
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
        order_ids: list[int],
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

    def effect_by_key(self, idempotency_key: str) -> Optional[dict[str, Any]]:
        row = self._con.execute(
            "SELECT effect_id, idempotency_key, kind, state, capability_sha256,"
            " order_ids_json, created_at, updated_at "
            "FROM l1_effects WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return None if row is None else self._effect_dict(row)

    def nonterminal_effects(self) -> list[dict[str, Any]]:
        rows = self._con.execute(
            "SELECT effect_id, idempotency_key, kind, state, capability_sha256,"
            " order_ids_json, created_at, updated_at FROM l1_effects "
            "WHERE state NOT IN ('terminal_flat','terminal_cancelled',"
            "'terminal_rejected','terminal_failed_held') ORDER BY created_at"
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
