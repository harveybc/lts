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


_HEX = frozenset("0123456789abcdef")


def require_digest(name: str, value: Any) -> str:
    """A CANONICAL 64-character lowercase hex digest.

    A digest-shaped field that is not a digest is not a weaker
    identity, it is no identity at all: ``"not-a-digest"``,
    uppercase hex, a 63-character string and a trailing space were
    all accepted before, so a binding could name something that could
    never equal a real digest and the comparison below would be
    vacuous."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise LiveCustodyError(
            f"{name}: a 64-character hex digest is required, got "
            f"{type(value).__name__} {value!r}")
    if len(value) != 64 or any(c not in _HEX for c in value):
        raise LiveCustodyError(
            f"{name}: {value!r} is not a canonical 64-character "
            "lowercase hex digest — no normalisation is applied")
    return value


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
                     "position_identity", "calendar_identity"):
            require_text(name, getattr(self, name))
        # digest-shaped fields are validated as DIGESTS, before any
        # store is touched
        for name in ("evidence_policy_digest",
                     "authority_code_identity"):
            require_digest(name, getattr(self, name))

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


def require_bound_policy(policy: Any, *,
                         binding: "VenueObligationBinding",
                         record: Optional[Mapping[str, Any]] = None
                         ) -> str:
    """WP3-C8: the policy that AUTHORISES the evidence must be the one
    the obligation was opened under.

    Previously the discharge path accepted an independent policy and
    only ran ``evidence.verify(policy)``. An obligation opened under a
    strict policy could therefore be discharged under a looser one for
    the same venue, account and symbol -- a longer freshness horizon
    or a wider source allowlist -- and the record stayed
    self-consistent while the policy actually admitting the evidence
    was not the one it named. The digest is recomputed here and
    required to equal the binding AND the durable record BEFORE any
    freshness or fact is evaluated."""
    if not isinstance(policy, VenueEvidencePolicy):
        raise LiveCustodyError(
            "a validated VenueEvidencePolicy is required to discharge "
            "an obligation")
    supplied = require_digest("policy.policy_digest",
                              policy.policy_digest)
    if supplied != binding.evidence_policy_digest:
        raise LiveCustodyError(
            f"policy substitution refused: the obligation is bound to "
            f"policy {binding.evidence_policy_digest[:12]}… and the "
            f"discharge presents {supplied[:12]}… — a different "
            "freshness horizon, source allowlist, schema or identity "
            "may not authorise this evidence")
    if record is not None:
        stated = record.get("checkpoint_identity") or ""
        if f"evidence_policy_digest={supplied}" not in stated:
            raise LiveCustodyError(
                "policy substitution refused: the durable record does "
                f"not name policy {supplied[:12]}…")
    for name, value in (("venue", policy.venue),
                        ("account_fingerprint",
                         policy.account_fingerprint),
                        ("symbol", policy.symbol)):
        if value != getattr(binding, name):
            raise LiveCustodyError(
                f"the discharge policy names {name} {value!r} but the "
                f"obligation binds {getattr(binding, name)!r}")
    return supplied


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
        "evidence_policy_digest": policy.policy_digest,
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
        # C8: policy identity is checked BEFORE freshness or facts.
        # An unbound policy never gets the chance to admit anything.
        require_bound_policy(policy, binding=self.binding,
                             record=record)
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

    def exists(self, obligation_id: str):
        """The record, or None when no such obligation exists — a
        TYPED distinction, so a caller never matches exception text.
        Integrity failures still raise: an unreadable record is not
        the same fact as an absent one."""
        try:
            return self._store.read(obligation_id)
        except self._custody.FlattenIntegrityError:
            raise
        except self._custody.FlattenObligationError as exc:
            if "no such obligation" in str(exc):
                return None
            raise
