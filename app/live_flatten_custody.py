"""WP3.3 — deployable flatten custody for a live venue.

The previous return claimed this was covered by loading the accepted
custody module. It was not: loading a module is not materialising a
custody, and nothing in that package opened a store, bound an
obligation to a venue, or required venue evidence to discharge one.
This module is that missing piece.

It does not reimplement the durable protocol. The audited store is
loaded through the authority binding and used as-is, so the
``O_EXCL`` creation, ``0700``/``0600`` modes, symlink refusal,
monotone acknowledgement, digest-verified reads, locked transitions
and episode-identity rule all come from the accepted implementation.

What this module adds is the VENUE contract on top of it:

* an obligation is bound to venue, account fingerprint, symbol,
  position identity, evidence-policy digest, calendar identity and
  authority code identity. Any of them differing refuses;
* a confirmation requires DIRECT venue evidence. The evidence must
  pass the venue policy, must be venue-direct, and must show zero
  positions and zero orders for that exact symbol and account.
  Simulator provenance cannot discharge anything;
* a restart re-reads the store and fails closed;
* several open obligations refuse and wait for an operator.

Read-only in this cycle. Transitions are planned and verified against
the durable store; nothing here sends an effect to a venue, and the
module holds no client and no credential.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from app.venue_direct_evidence import (
    VenueDirectEvidence, VenueEvidenceError, VenueEvidencePolicy,
    require_text, require_venue_direct)


class LiveCustodyError(RuntimeError):
    """The live flatten custody refuses — typed, never a default."""


class LiveCustodyDispositionRequired(LiveCustodyError):
    """Several open obligations: only an operator may dispose."""


@dataclass(frozen=True)
class VenueObligationBinding:
    """Everything an obligation must be tied to, in one value."""

    venue: str
    account_fingerprint: str
    symbol: str
    position_identity: str
    evidence_policy_digest: str
    calendar_identity: str
    authority_code_identity: str

    def __post_init__(self):
        for name in ("venue", "account_fingerprint", "symbol",
                     "position_identity", "evidence_policy_digest",
                     "calendar_identity", "authority_code_identity"):
            require_text(name, getattr(self, name))

    def as_identity(self) -> str:
        return "|".join((self.venue, self.account_fingerprint,
                         self.symbol, self.position_identity))

    def matches(self, record: Mapping[str, Any]) -> tuple:
        """Every field that DISAGREES with a stored record."""
        expected = {
            "venue": self.venue,
            "account_fingerprint": self.account_fingerprint,
            "symbol": self.symbol,
            "position_identity": self.position_identity,
        }
        differing = [name for name, value in expected.items()
                     if record.get(name) != value]
        checkpoint = record.get("checkpoint_identity") or ""
        for label, value in (
                ("evidence_policy_digest",
                 self.evidence_policy_digest),
                ("calendar_identity", self.calendar_identity),
                ("authority_code_identity",
                 self.authority_code_identity)):
            if f"{label}={value}" not in checkpoint:
                differing.append(label)
        return tuple(sorted(differing))

    def checkpoint_identity(self) -> str:
        return ";".join((
            f"evidence_policy_digest={self.evidence_policy_digest}",
            f"calendar_identity={self.calendar_identity}",
            f"authority_code_identity={self.authority_code_identity}",
        ))


def flat_from_direct_evidence(
        positions: VenueDirectEvidence,
        orders: VenueDirectEvidence, *,
        policy: VenueEvidencePolicy, now: Any) -> dict:
    """DIRECT venue proof that nothing is open.

    Both envelopes are re-verified against the venue policy here --
    freshness, source allowlist, account and symbol binding -- so a
    caller cannot hand in evidence that was admitted under some other
    policy. Simulator provenance is refused by name."""
    for evidence in (positions, orders):
        if not isinstance(evidence, VenueDirectEvidence):
            raise LiveCustodyError(
                "direct venue evidence is required; a plain mapping "
                "cannot discharge an obligation")
        evidence.verify(policy, now=now)
        require_venue_direct(evidence.provenance())
    positions_total = positions.facts.get("positions_total")
    orders_total = orders.facts.get("orders_total")
    if positions_total is None or orders_total is None:
        raise LiveCustodyError(
            "the evidence does not state position and order totals")
    flat = int(positions_total) == 0 and int(orders_total) == 0
    return {
        "flat_confirmed": flat,
        "positions": int(positions_total),
        "orders": int(orders_total),
        "venue": policy.venue,
        "symbol": policy.symbol,
        "account_fingerprint": policy.account_fingerprint,
        "evidence_provenance": "venue_direct",
        "venue_direct": True,
        "positions_evidence_id": positions.evidence_id,
        "orders_evidence_id": orders.evidence_id,
        "incident": None if flat else (
            f"FLATTEN_INCOMPLETE: {positions_total} positions and "
            f"{orders_total} orders remain"),
    }


class LiveFlattenCustody:
    """A venue-bound view of the ACCEPTED durable store."""

    def __init__(self, authority, root: Any, *,
                 binding: VenueObligationBinding,
                 episode_identity: str):
        self._custody = authority.flatten_custody
        self._store = self._custody.FlattenObligationStore(root)
        self.binding = binding
        self.episode_identity = require_text("episode_identity",
                                             episode_identity)
        if binding.authority_code_identity != authority.code_identity:
            raise LiveCustodyError(
                "the binding names authority code identity "
                f"{binding.authority_code_identity[:12]}… but the "
                f"loaded authority is {authority.code_identity[:12]}…")

    # -- planning ---------------------------------------------------
    def outstanding(self) -> tuple:
        return self._store.outstanding()

    def recover(self) -> Optional[dict]:
        """Fail-closed restart. Several open obligations refuse; one
        from a foreign binding refuses; one from this binding is
        returned for verification."""
        try:
            record = self._store.require_single_open()
        except self._custody.FlattenDispositionRequired as exc:
            raise LiveCustodyDispositionRequired(str(exc)) from exc
        if record is None:
            return None
        differing = self.binding.matches(record)
        if differing:
            raise LiveCustodyError(
                f"the outstanding obligation "
                f"{record.get('obligation_id')!r} disagrees on "
                f"{list(differing)} — it belongs to a different "
                "venue, account, symbol, position or reviewed "
                "identity and this runner may not discharge it")
        return record

    def open(self, obligation_id: str, *, signed_exposure: float,
             requested_at_bar: int) -> dict:
        return self._store.open_obligation(
            obligation_id, venue=self.binding.venue,
            account_fingerprint=self.binding.account_fingerprint,
            symbol=self.binding.symbol,
            position_identity=self.binding.position_identity,
            episode_identity=self.episode_identity,
            signed_exposure=signed_exposure,
            requested_at_bar=requested_at_bar,
            code_identity=self.binding.authority_code_identity,
            checkpoint_identity=self.binding.checkpoint_identity())

    def mark_in_flight(self, obligation_id: str, *,
                       bar_index: int) -> dict:
        return self._store.mark_in_flight(
            obligation_id, bar_index=bar_index,
            episode_identity=self.episode_identity)

    def confirm_with_direct_evidence(
            self, obligation_id: str, *,
            positions: VenueDirectEvidence,
            orders: VenueDirectEvidence,
            policy: VenueEvidencePolicy, now: Any,
            bar_index: int) -> dict:
        """The ONLY discharge path. Nothing but direct venue evidence
        of zero positions and zero orders may confirm."""
        record = self._store.read(obligation_id)
        differing = self.binding.matches(record)
        if differing:
            raise LiveCustodyError(
                f"{obligation_id} disagrees on {list(differing)} and "
                "may not be confirmed by this runner")
        reconciliation = flat_from_direct_evidence(
            positions, orders, policy=policy, now=now)
        if not reconciliation["flat_confirmed"]:
            raise LiveCustodyError(
                f"{obligation_id}: {reconciliation['incident']} — an "
                "incomplete flatten is never a success")
        return self._store.confirm(
            obligation_id, reconciliation=reconciliation,
            bar_index=bar_index,
            episode_identity=self.episode_identity)

    def interrupt(self, obligation_id: str, *, reason: str) -> dict:
        return self._store.interrupt(
            obligation_id, reason=reason,
            episode_identity=self.episode_identity)

    def read(self, obligation_id: str) -> dict:
        return self._store.read(obligation_id)
