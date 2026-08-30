"""WP3.2 — venue decision adapter over the ACCEPTED session authority.

This module does not contain a session state machine. It loads the one
already audited and accepted in the simulator repository, verifies the
identity of the code it loaded, and TRANSLATES its verdicts into typed
venue directives. Reimplementing the states here would create a second
authority that could drift from the first, which is exactly what the
order forbids.

Loading is explicit and identity-checked. The authority path is
configuration, never a search: an absent or mismatched authority is a
typed refusal, and no fallback local implementation exists to quietly
take over.

The translation is total over the five states plus recovery, and each
directive says what may be sent and what may not:

===========================  ==========================================
state                        directive
===========================  ==========================================
``NORMAL_TRADING``           the ordinary decision passes through
``WIND_DOWN``                new entries blocked; ONLY pending entry
                             orders may be cancelled; protection is
                             preserved
``FORCED_FLATTEN``           pending entries cancelled, protection kept
                             until the close completes, close requested,
                             success only on DIRECT zero/zero evidence
``EXPECTED_MARKET_CLOSED``   no actionable step exists at all
``REOPEN_BLACKOUT``          entries blocked until causal evidence is
                             sufficient
``RECOVERY``                 every risk increase blocked until an
                             operator disposition or valid direct
                             evidence discharges the obligation
===========================  ==========================================

The raw model output, the mapped command, the overlay decision and the
final command are published as four separate values. Nothing here
sends anything: a directive is a description of what WOULD be sent.
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from app.venue_direct_evidence import (
    VenueEvidenceError, require_enum, require_text,
    require_venue_direct)

AUTHORITY_MODULES = ("app.session_exposure", "app.flatten_custody")

STATES = ("NORMAL_TRADING", "WIND_DOWN", "FORCED_FLATTEN",
          "EXPECTED_MARKET_CLOSED", "REOPEN_BLACKOUT", "RECOVERY")

# What a directive may authorise. Every one of these is DESCRIPTIVE:
# this package has no client that could perform any of them.
EFFECTS = ("none", "submit_decision", "cancel_pending_entries",
           "request_close")


class AuthorityUnavailable(RuntimeError):
    """The accepted authority could not be loaded or verified."""


class AdapterRefusal(RuntimeError):
    """A venue directive could not be derived — typed refusal."""


# ---------------------------------------------------------------- #
# loading the accepted authority                                    #
# ---------------------------------------------------------------- #

@dataclass(frozen=True)
class AuthorityBinding:
    """A verified handle on the accepted session authority."""

    root: str
    code_identity: str
    session_exposure: Any = field(repr=False)
    flatten_custody: Any = field(repr=False)

    @property
    def states(self) -> tuple:
        return tuple(self.session_exposure.STATES)


def _module_identity(modules) -> str:
    material = []
    for module in modules:
        source = inspect.getsource(module).encode("utf-8")
        material.append(
            f"{module.__name__}={hashlib.sha256(source).hexdigest()}")
    return hashlib.sha256(
        "|".join(sorted(material)).encode("utf-8")).hexdigest()


def load_authority(root: Any, *,
                   expected_code_identity: Optional[str] = None
                   ) -> AuthorityBinding:
    """Load the accepted authority from an EXPLICIT checkout root.

    ``expected_code_identity`` is the digest of the exact authority
    source this deployment was reviewed against. When it is supplied
    and does not match, the load refuses: running a venue adapter
    against an unreviewed copy of the state machine is precisely the
    drift this binding exists to prevent."""
    base = Path(root)
    if not base.is_dir():
        raise AuthorityUnavailable(
            f"authority root {base} does not exist — the accepted "
            "session authority is configuration, not a search path, "
            "and there is no local reimplementation to fall back to")
    loaded = []
    for dotted in AUTHORITY_MODULES:
        relative = Path(*dotted.split(".")).with_suffix(".py")
        path = base / relative
        if not path.is_file():
            raise AuthorityUnavailable(
                f"{dotted} not found at {path}")
        name = f"_lts_authority_{dotted.replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:  # pragma: no cover
            raise AuthorityUnavailable(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        # flatten_custody imports migration_custody by dotted name, so
        # the authority checkout must be importable while it loads
        inserted = str(base) not in sys.path
        if inserted:
            sys.path.insert(0, str(base))
        # dataclass resolution looks the defining module up in
        # sys.modules by __module__, so it must be registered BEFORE
        # execution; and flatten_custody imports migration_custody by
        # dotted name, so the authority checkout must be importable
        # while it loads.
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(name, None)
            raise AuthorityUnavailable(
                f"{dotted} at {path} failed to load: {exc}") from exc
        finally:
            if inserted and str(base) in sys.path:
                sys.path.remove(str(base))
        loaded.append(module)

    identity = _module_identity(loaded)
    if expected_code_identity is not None and \
            identity != expected_code_identity:
        raise AuthorityUnavailable(
            f"authority code identity {identity[:16]}… does not match "
            f"the reviewed {expected_code_identity[:16]}… — refusing "
            "to run a venue adapter against unreviewed policy")
    session_exposure, flatten_custody = loaded
    for required in ("session_state", "overlay_action",
                     "classify_discrete_command", "ExposureFacts",
                     "SessionCalendar", "validate_policy",
                     "reconciliation_gate", "STATES"):
        if not hasattr(session_exposure, required):
            raise AuthorityUnavailable(
                f"the loaded authority has no {required!r}; it is not "
                "the accepted session_exposure module")
    return AuthorityBinding(root=str(base), code_identity=identity,
                            session_exposure=session_exposure,
                            flatten_custody=flatten_custody)


# ---------------------------------------------------------------- #
# the typed venue directive                                         #
# ---------------------------------------------------------------- #

@dataclass(frozen=True)
class VenueDirective:
    """What WOULD be sent, and why. Never a transmission."""

    venue: str
    account_fingerprint: str
    symbol: str
    session_state: str
    raw_model_output: Any
    mapped_command: Optional[int]
    mapped_action: Optional[dict]
    overlay: str
    final_command: Optional[int]
    effects: tuple
    cancel_order_identities: tuple
    blocks_risk_increase: bool
    requires_direct_confirmation: bool
    preserve_protection: bool
    reason: str
    evidence_provenance: dict

    def __post_init__(self):
        require_enum("session_state", self.session_state, STATES)
        for effect in self.effects:
            require_enum("effect", effect, EFFECTS)
        if self.requires_direct_confirmation and \
                "request_close" not in self.effects:
            raise AdapterRefusal(
                "direct confirmation is only required by a close")

    def as_dict(self) -> dict:
        return {
            "venue": self.venue,
            "account_fingerprint": self.account_fingerprint,
            "symbol": self.symbol,
            "session_state": self.session_state,
            # the four separate records the contract requires
            "raw_model_output": self.raw_model_output,
            "mapped_command": self.mapped_command,
            "mapped_action": (None if self.mapped_action is None
                              else dict(self.mapped_action)),
            "overlay": self.overlay,
            "final_command": self.final_command,
            "effects": list(self.effects),
            "cancel_order_identities": list(
                self.cancel_order_identities),
            "blocks_risk_increase": self.blocks_risk_increase,
            "requires_direct_confirmation":
                self.requires_direct_confirmation,
            "preserve_protection": self.preserve_protection,
            "reason": self.reason,
            "evidence_provenance": dict(self.evidence_provenance),
        }


# ---------------------------------------------------------------- #
# translation                                                       #
# ---------------------------------------------------------------- #

def build_exposure_facts(authority: AuthorityBinding, *,
                         positions: Mapping[str, Any],
                         orders: Mapping[str, Any]):
    """Signed exposure and the entry/protective split, both taken from
    DIRECT venue facts. No coercion: a missing or contradictory fact
    refuses instead of reading as a flat account."""
    rows = positions.get("positions")
    if rows is None:
        raise AdapterRefusal(
            "positions evidence carries no positions field")
    signed = 0.0
    for row in rows:
        signed += float(row["signed_quantity"])
    entry_orders = orders.get("entry_orders")
    protective_orders = orders.get("protective_orders")
    if entry_orders is None or protective_orders is None:
        raise AdapterRefusal(
            "order evidence does not state the entry/protective split")
    return authority.session_exposure.ExposureFacts.build(
        signed_exposure=signed,
        pending_orders=int(entry_orders) + int(protective_orders),
        protective_orders=int(protective_orders),
        action_mapping="discrete_command_v1")


def derive_directive(authority: AuthorityBinding, *, policy: dict,
                     state_block: Mapping[str, Any],
                     venue: str, account_fingerprint: str,
                     symbol: str, raw_model_output: Any,
                     mapped_command: int,
                     positions: Mapping[str, Any],
                     orders: Mapping[str, Any],
                     provenance: Mapping[str, Any],
                     recovery: Optional[Mapping[str, Any]] = None
                     ) -> VenueDirective:
    """Translate ONE authority verdict into ONE venue directive.

    ``provenance`` must be direct venue evidence; simulator provenance
    is refused here, not merely discouraged."""
    require_venue_direct(provenance)
    require_text("venue", venue)
    require_text("account_fingerprint", account_fingerprint)
    require_text("symbol", symbol)
    session = authority.session_exposure

    exposure = build_exposure_facts(authority, positions=positions,
                                    orders=orders)
    entry_identities = tuple(
        row["order_identity"] for row in orders.get("orders", ())
        if row.get("role") == "entry")

    state = require_enum("state_block.state",
                         state_block.get("state"), STATES)

    # RECOVERY is an adapter-level state: the authority never emits it,
    # because it is a fact about a durable obligation rather than about
    # the calendar. It takes precedence over everything else.
    if recovery and recovery.get("blocks_risk_increase"):
        return VenueDirective(
            venue=venue, account_fingerprint=account_fingerprint,
            symbol=symbol, session_state="RECOVERY",
            raw_model_output=raw_model_output,
            mapped_command=mapped_command,
            mapped_action=session.classify_discrete_command(
                mapped_command, exposure),
            overlay="blocked_by_flatten_recovery",
            final_command=session.HOLD_COMMAND,
            effects=("none",), cancel_order_identities=(),
            blocks_risk_increase=True,
            requires_direct_confirmation=False,
            preserve_protection=True,
            reason=str(recovery.get("reason",
                                    "outstanding_obligation")),
            evidence_provenance=dict(provenance))

    if state == "EXPECTED_MARKET_CLOSED":
        return VenueDirective(
            venue=venue, account_fingerprint=account_fingerprint,
            symbol=symbol, session_state=state,
            raw_model_output=raw_model_output,
            mapped_command=None, mapped_action=None,
            overlay="no_actionable_step", final_command=None,
            effects=("none",), cancel_order_identities=(),
            blocks_risk_increase=True,
            requires_direct_confirmation=False,
            preserve_protection=True,
            reason="the venue calendar declares the market closed; no "
                   "actionable step exists",
            evidence_provenance=dict(provenance))

    classification = session.classify_discrete_command(mapped_command,
                                                       exposure)
    decision = session.overlay_action(policy, state_block, exposure,
                                      float(mapped_command),
                                      classification=classification)
    overlay = decision["overlay"]

    if overlay == "forced_close":
        return VenueDirective(
            venue=venue, account_fingerprint=account_fingerprint,
            symbol=symbol, session_state=state,
            raw_model_output=raw_model_output,
            mapped_command=mapped_command,
            mapped_action=classification, overlay=overlay,
            final_command=session.CLOSE_COMMAND,
            effects=("cancel_pending_entries", "request_close")
            if entry_identities else ("request_close",),
            cancel_order_identities=entry_identities,
            blocks_risk_increase=True,
            requires_direct_confirmation=True,
            preserve_protection=True,
            reason="forced flatten: cancel pending entries, keep "
                   "protection until the close completes, and confirm "
                   "only on direct zero-position zero-order evidence",
            evidence_provenance=dict(provenance))

    if overlay in ("masked_risk_increase",
                   "masked_entry_during_blackout"):
        cancel = entry_identities if decision.get(
            "cancel_pending") else ()
        return VenueDirective(
            venue=venue, account_fingerprint=account_fingerprint,
            symbol=symbol, session_state=state,
            raw_model_output=raw_model_output,
            mapped_command=mapped_command,
            mapped_action=classification, overlay=overlay,
            final_command=session.HOLD_COMMAND,
            effects=("cancel_pending_entries",) if cancel
            else ("none",),
            cancel_order_identities=cancel,
            blocks_risk_increase=True,
            requires_direct_confirmation=False,
            preserve_protection=True,
            reason=("new entries are blocked; only pending ENTRY "
                    "orders may be cancelled and native protection is "
                    "preserved"),
            evidence_provenance=dict(provenance))

    return VenueDirective(
        venue=venue, account_fingerprint=account_fingerprint,
        symbol=symbol, session_state=state,
        raw_model_output=raw_model_output,
        mapped_command=mapped_command, mapped_action=classification,
        overlay=overlay, final_command=mapped_command,
        effects=("submit_decision",), cancel_order_identities=(),
        blocks_risk_increase=False,
        requires_direct_confirmation=False,
        preserve_protection=True,
        reason="ordinary decision", evidence_provenance=dict(
            provenance))
