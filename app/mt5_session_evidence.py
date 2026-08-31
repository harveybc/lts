"""F7 (order agent-multi@a678fd55): MT5 ETHUSD session-evidence
collector schema — READ-ONLY by construction, and NOT YET ACTIVE.

Inventory finding, recorded here so nobody re-derives it: the MT5
bridge payload schemas (``app/mt5_bridge_lab.py`` — Heartbeat,
Snapshot with positions/orders/symbols/bars, TradeEvent) carry NO
session or calendar fields. The EA has never published
``SymbolInfoSessionTrade``/``SymbolInfoSessionQuote``, so
historical-time MT5 ETHUSD session evidence DOES NOT EXIST anywhere
downstream of the EA, and no store can contain what was never sent.
Work plan 42 §3 requires exactly that evidence for the live session
authority, and WP4 economic calibration therefore remains
``VENUE_SESSION_HISTORY_UNAVAILABLE`` until this collector is
activated and its output independently accepted.

Activation status: ``COORDINATED_WINDOW_REQUIRED``. Publishing
sessions requires an EA change, and the running EA currently
protects an open position. Replacing or restarting it outside a
coordinated operator window is forbidden — metadata is never worth
risking the position. The runbook below is the activation contract.

This module holds NO trading command capability: it validates and
sanitizes what an EA would POST. It cannot send orders, modify
positions, or write anything toward a terminal — a structural test
asserts the absence of every such surface.

Coordinated activation runbook (READ-ONLY addition):

1. OWNER opens a coordinated window (position flat, or the operator
   explicitly accepts the EA restart with native SL/TP verified on
   the venue side first).
2. The EA gains an additive, read-only publisher: once per session
   change and once per heartbeat cycle it POSTs
   ``lts.mt5_session_evidence.v1`` built from
   ``SymbolInfoSessionTrade``/``SymbolInfoSessionQuote`` loops
   (day 0-6, session index until empty), trade-server time.
3. No other EA behaviour changes; no trading path is touched.
4. The collector validates, digest-binds and stores; the sanitized
   export is reviewed before any WP4 use.
5. Historical backfill does NOT exist and is never fabricated: the
   dataset is authoritative only from activation forward, and any
   gap in collection is recorded as a gap, separated from authority.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

SESSION_EVIDENCE_SCHEMA = "lts.mt5_session_evidence.v1"
ACTIVATION_STATUS = "COORDINATED_WINDOW_REQUIRED"
MAX_EVIDENCE_AGE_SECONDS = 900.0


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionInterval(StrictModel):
    """One venue session interval as the terminal reports it:
    minutes from midnight, trade-server time."""

    day_of_week: int = Field(ge=0, le=6)
    from_minute: int = Field(ge=0, le=1440)
    to_minute: int = Field(ge=0, le=1440)

    def model_post_init(self, _ctx) -> None:
        if self.from_minute == self.to_minute:
            raise ValueError(
                "empty session interval — the EA must omit empty "
                "sessions rather than publish zero-length ones")


class SessionEvidencePayload(StrictModel):
    schema_name: str = Field(alias="schema")
    account_fingerprint: str = Field(min_length=12, max_length=64)
    server_fingerprint: str = Field(min_length=12, max_length=64)
    symbol: str = Field(min_length=1, max_length=32)
    ea_version: str = Field(min_length=1, max_length=64)
    terminal_build: int = Field(ge=0)
    server_gmt_offset_minutes: int = Field(ge=-14 * 60, le=14 * 60)
    observed_at: datetime
    quote_sessions: list[SessionInterval]
    trade_sessions: list[SessionInterval]
    acquisition_source: str

    def model_post_init(self, _ctx) -> None:
        if self.schema_name != SESSION_EVIDENCE_SCHEMA:
            raise ValueError(
                f"schema {self.schema_name!r} is not "
                f"{SESSION_EVIDENCE_SCHEMA!r}")
        if self.acquisition_source != \
                "SymbolInfoSessionTrade/SymbolInfoSessionQuote":
            raise ValueError(
                "acquisition_source must name the terminal API the "
                "sessions came from — nothing else is session "
                "authority")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        for name in ("quote_sessions", "trade_sessions"):
            intervals = getattr(self, name)
            if not intervals:
                raise ValueError(
                    f"{name} is empty — a symbol with no sessions "
                    "is contradictory evidence, refused")
            seen = sorted(intervals, key=lambda s: (
                s.day_of_week, s.from_minute))
            for a, b in zip(seen, seen[1:]):
                if a.day_of_week == b.day_of_week and \
                        b.from_minute < a.to_minute:
                    raise ValueError(
                        f"{name}: overlapping intervals on day "
                        f"{a.day_of_week} — refused")


def verify_freshness_and_identity(
        payload: SessionEvidencePayload, *,
        expected_account_fingerprint: str,
        expected_server_fingerprint: str,
        expected_symbol: str,
        now: Optional[datetime] = None) -> None:
    """Identity and freshness fail closed; nothing is defaulted."""
    if payload.account_fingerprint != expected_account_fingerprint:
        raise ValueError("foreign account fingerprint refused")
    if payload.server_fingerprint != expected_server_fingerprint:
        raise ValueError("foreign server fingerprint refused")
    if payload.symbol != expected_symbol:
        raise ValueError(
            f"symbol {payload.symbol!r} is not the bound "
            f"{expected_symbol!r} — refused")
    now = now or datetime.now(timezone.utc)
    age = (now - payload.observed_at).total_seconds()
    if age < 0:
        raise ValueError("observed_at is in the future — refused")
    if age > MAX_EVIDENCE_AGE_SECONDS:
        raise ValueError(
            f"session evidence is {age:.0f}s old and the policy "
            f"allows {MAX_EVIDENCE_AGE_SECONDS:.0f}s — stale "
            "evidence is refused")


def sanitized_export(payloads: list[SessionEvidencePayload]) -> dict:
    """The digest-bound, read-only dataset WP4 would consume:
    session intervals, symbol, server timezone/version facts and
    acquisition provenance — with collection GAPS separated
    explicitly from authority. Fingerprints are already one-way
    fingerprints; no account number, host, path or operator
    identifier exists anywhere in the schema."""
    if not payloads:
        return {"schema": SESSION_EVIDENCE_SCHEMA + ".export",
                "status": "VENUE_SESSION_HISTORY_UNAVAILABLE",
                "activation": ACTIVATION_STATUS,
                "records": [], "gaps": [],
                "authority_note": "no evidence exists; nothing is "
                                  "fabricated in its absence"}
    ordered = sorted(payloads, key=lambda p: p.observed_at)
    records = []
    gaps = []
    previous = None
    for payload in ordered:
        records.append(json.loads(payload.model_dump_json(
            by_alias=True)))
        if previous is not None:
            silent = (payload.observed_at
                      - previous.observed_at).total_seconds()
            if silent > MAX_EVIDENCE_AGE_SECONDS:
                gaps.append({
                    "after": previous.observed_at.isoformat(),
                    "before": payload.observed_at.isoformat(),
                    "silent_seconds": silent,
                    "meaning": "COLLECTION gap — evidence absence, "
                               "never venue-closure authority"})
        previous = payload
    body = {"schema": SESSION_EVIDENCE_SCHEMA + ".export",
            "status": "COLLECTED",
            "activation": ACTIVATION_STATUS,
            "symbol": ordered[0].symbol,
            "server_gmt_offset_minutes":
                ordered[0].server_gmt_offset_minutes,
            "terminal_build": ordered[0].terminal_build,
            "ea_version": ordered[0].ea_version,
            "acquisition_provenance":
                ordered[0].acquisition_source,
            "records": records,
            "gaps": gaps,
            "authority_note": "gaps are evidence about COLLECTION, "
                              "explicitly separated from session "
                              "authority; observed historical gaps "
                              "never override fresh venue session "
                              "evidence (work plan 42 §3)"}
    digest = hashlib.sha256(json.dumps(
        body, sort_keys=True, default=str).encode()).hexdigest()
    return {**body, "export_sha256": digest}
