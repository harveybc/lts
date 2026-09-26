"""The MT5 Demo lane's POLICY and RISK interfaces: the refusals we make before any venue is asked.

RP149 names two interfaces, and they are not the taxonomy next door.
``app/broker_refusal.py`` and ``app/mt5_refusal.py`` type what a VENUE refused
and carry the venue's own words verbatim. This module types what WE refuse,
before a terminal is contacted at all, and it must therefore never carry a
venue's words: no venue spoke. Its refusals name OUR rule, in our own declared
sentence, and that distinction is structural rather than documented —
``PolicyRefusal`` has no ``venue_reason`` field to put borrowed prose in.

Both interfaces answer the same shape of question and share the recovery
vocabulary of ``broker_refusal`` (``RECOVERIES``), so a runner has one thing to
switch on whether the refusal came from us or from the terminal.

What each one is for
--------------------
``OrderPolicy`` — is this order ALLOWED to exist? Demo account only, a symbol
inside the mandate, model evidence attached, a protective bracket present, a
decision that is not stale, a client order id that is not already live, and no
unreconciled effect outstanding.

``RiskMandate`` — is it allowed to be this BIG, now? Volume cap, open positions
per route, the account-wide daily entry budget, the daily loss limit, and the
route's unresolved-command rule.

Five rules the whole module exists to keep
------------------------------------------
1. **An unknown state is never assumed flat.** ``PositionState`` has a
   ``certainty``, a missing units fact CONSTRUCTS as ``UNKNOWN`` rather than as
   zero, and an unknown state refuses with ``RECOVERY_RECONCILE_UNKNOWN_FILL``
   and blocks new orders. ``PositionState(units=None, certainty=KNOWN)`` cannot
   be constructed.
2. **A duplicate client order id never places a second order.** The policy asks
   the journal first, and the declared recovery is to resume from the persisted
   id. There is no recovery in the vocabulary that means "submit again".
3. **An entitlement failure is never retried as transient.** No policy or risk
   rule may declare ``RECOVERY_RETRY_WITH_BOUND`` at all: a rule of ours is not
   a blip, and waiting does not make an order permissible.
4. **The read/mutating distinction is honoured.** Every policy and risk decision
   is about placing or changing something, so every refusal here is
   ``OPERATION_MUTATING`` and every one of them blocks new orders. A refusal
   that did not block would be a silent no-op with a name.
5. **Fail closed, and once.** ``evaluate_order`` runs the policy first and the
   risk mandate only if the policy admitted; the first refusal is raised and the
   remaining rules are not consulted, so a refused order never produces two
   competing reasons.

Demo only, and structurally: ``OrderPolicy`` refuses to be CONSTRUCTED for any
account kind but ``demo``. ``Mt5ExecutionConfig.load`` already refuses a config
that is not ``environment=demo``; this is the same rule one layer up, where the
decision is made.

Offline by construction: no socket, no credential, no terminal, no order. The
journal and exposure facts are passed in as plain mappings by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from app.broker_refusal import (
    OPERATION_MUTATING,
    RECOVERY_RECONCILE_UNKNOWN_FILL,
    RECOVERY_REFUSE_TO_PROCEED,
    RECOVERY_RESUME_FROM_PERSISTED_CLIENT_ORDER_ID,
    RECOVERIES,
)

POLICY_REFUSAL_SCHEMA = "lts.mt5_policy_risk.refusal.v1"

#: The only account kind this lane can be built for.
ACCOUNT_KIND_DEMO = "demo"

# ------------------------------------------------------------- policy rules
# There is deliberately no rule for "the account is live". A live account cannot
# reach a rule, because no ``OrderPolicy`` can be CONSTRUCTED for one: the
# refusal happens at construction and there is no code path past it.
RULE_ACCOUNT_NOT_THE_MANDATED_ONE = "account_not_the_mandated_one"
RULE_SYMBOL_OUTSIDE_MANDATE = "symbol_outside_mandate"
RULE_ACTION_NOT_DECLARED = "action_not_declared"
RULE_MODEL_EVIDENCE_MISSING = "model_evidence_missing"
RULE_PROTECTIVE_BRACKET_MISSING = "protective_bracket_missing"
RULE_PROTECTIVE_BRACKET_INVERTED = "protective_bracket_inverted"
RULE_DECISION_STALE = "decision_stale"
RULE_CLIENT_ORDER_ID_MISSING = "client_order_id_missing"
RULE_DUPLICATE_CLIENT_ORDER_ID = "duplicate_client_order_id"
RULE_EFFECT_UNKNOWN_OUTSTANDING = "effect_unknown_outstanding"

POLICY_RULES = frozenset({
    RULE_ACCOUNT_NOT_THE_MANDATED_ONE,
    RULE_SYMBOL_OUTSIDE_MANDATE, RULE_ACTION_NOT_DECLARED,
    RULE_MODEL_EVIDENCE_MISSING, RULE_PROTECTIVE_BRACKET_MISSING,
    RULE_PROTECTIVE_BRACKET_INVERTED, RULE_DECISION_STALE,
    RULE_CLIENT_ORDER_ID_MISSING, RULE_DUPLICATE_CLIENT_ORDER_ID,
    RULE_EFFECT_UNKNOWN_OUTSTANDING,
})

# --------------------------------------------------------------- risk rules
RULE_VOLUME_ABOVE_CAP = "volume_above_cap"
RULE_VOLUME_NOT_POSITIVE = "volume_not_positive"
RULE_OPEN_POSITIONS_AT_CAP = "open_positions_at_cap"
RULE_DAILY_ENTRY_BUDGET_EXHAUSTED = "daily_entry_budget_exhausted"
RULE_UNRESOLVED_COMMAND_ON_ROUTE = "unresolved_command_on_route"
RULE_POSITION_STATE_UNKNOWN = "position_state_unknown"
RULE_DAILY_LOSS_LIMIT_REACHED = "daily_loss_limit_reached"

RISK_RULES = frozenset({
    RULE_VOLUME_ABOVE_CAP, RULE_VOLUME_NOT_POSITIVE,
    RULE_OPEN_POSITIONS_AT_CAP, RULE_DAILY_ENTRY_BUDGET_EXHAUSTED,
    RULE_UNRESOLVED_COMMAND_ON_ROUTE, RULE_POSITION_STATE_UNKNOWN,
    RULE_DAILY_LOSS_LIMIT_REACHED,
})

RULES = POLICY_RULES | RISK_RULES

#: Rules whose subject is an UNPROVEN effect rather than a forbidden order. They
#: are the only rules that may declare reconciliation, and they always block.
_UNKNOWN_STATE_RULES = frozenset({
    RULE_EFFECT_UNKNOWN_OUTSTANDING, RULE_POSITION_STATE_UNKNOWN,
})

#: The one rule whose recovery is to resume from what is already persisted.
_RESUME_RULES = frozenset({RULE_DUPLICATE_CLIENT_ORDER_ID})

#: The actions this lane can express, matching ``mt5_execution_bridge._ACTIONS``.
ACTION_OPEN_LONG = "open_long"
ACTION_OPEN_SHORT = "open_short"
ACTION_CLOSE = "close"
ACTIONS = frozenset({ACTION_OPEN_LONG, ACTION_OPEN_SHORT, ACTION_CLOSE})
OPENING_ACTIONS = frozenset({ACTION_OPEN_LONG, ACTION_OPEN_SHORT})

#: Certainty of a position fact. There is no third value: a fact is either
#: measured or it is not, and "probably flat" is not a state.
CERTAINTY_KNOWN = "known"
CERTAINTY_UNKNOWN = "unknown"


@dataclass(frozen=True)
class PositionState:
    """What we know about a route's position, and whether we know it.

    ``units`` is meaningless without ``certainty``. A fact with no units is
    UNKNOWN by construction, and an UNKNOWN state carries no units at all, so no
    caller can read a number out of one by accident.
    """

    certainty: str
    units: Optional[float] = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.certainty not in (CERTAINTY_KNOWN, CERTAINTY_UNKNOWN):
            raise ValueError(f"unknown certainty: {self.certainty!r}")
        if self.certainty == CERTAINTY_KNOWN:
            if self.units is None:
                raise ValueError(
                    "a KNOWN position state must carry its units; a missing "
                    "fact is UNKNOWN, never zero"
                )
            if isinstance(self.units, bool) or not isinstance(self.units, (int, float)):
                raise ValueError("position units must be a number")
        elif self.units is not None:
            raise ValueError(
                "an UNKNOWN position state carries no units, so that nothing "
                "can read a quantity out of it"
            )
        elif not self.reason:
            raise ValueError("an UNKNOWN position state must name why it is unknown")

    @property
    def is_known(self) -> bool:
        return self.certainty == CERTAINTY_KNOWN

    @classmethod
    def known(cls, units: float) -> "PositionState":
        return cls(certainty=CERTAINTY_KNOWN, units=float(units))

    @classmethod
    def unknown(cls, reason: str) -> "PositionState":
        return cls(certainty=CERTAINTY_UNKNOWN, reason=reason)

    @classmethod
    def from_fact(cls, fact: Optional[Mapping[str, Any]]) -> "PositionState":
        """Read a position fact. An absent fact, an absent ``units`` key, or a
        non-numeric one is UNKNOWN — never zero. This is the constructor the
        "never assumed flat" rule lives in, because a caller reading a mapping
        is exactly where a missing key becomes a silent 0.0."""
        if fact is None:
            return cls.unknown("no position fact was supplied for this route")
        if "units" not in fact:
            return cls.unknown("the position fact carries no units key")
        value = fact.get("units")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return cls.unknown(
                f"the position fact's units are {type(value).__name__}, not a number"
            )
        return cls.known(float(value))


@dataclass(frozen=True)
class PolicyRefusal:
    """One typed refusal of OURS, with its declared recovery.

    There is deliberately no ``venue_reason``: no venue was asked. ``reason`` is
    this repository's own declared sentence for the rule, and ``measured`` and
    ``limit`` carry the numbers a cap was breached by, because "over the cap" is
    not a finding and "0.9 against a cap of 0.5" is.
    """

    rule: str
    reason: str
    recovery: str
    interface: str
    operation: str = OPERATION_MUTATING
    measured: Optional[Any] = None
    limit: Optional[Any] = None
    client_order_id: Optional[str] = None
    state_is_unknown: bool = False
    blocks_new_orders: bool = True
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.rule not in RULES:
            raise ValueError(f"unknown rule: {self.rule!r}")
        if self.recovery not in RECOVERIES:
            raise ValueError(f"unknown recovery: {self.recovery!r}")
        if self.recovery not in _allowed_recoveries(self.rule):
            raise ValueError(f"{self.rule} may not declare recovery {self.recovery}")
        if self.interface not in ("policy", "risk"):
            raise ValueError(f"unknown interface: {self.interface!r}")
        if self.operation != OPERATION_MUTATING:
            raise ValueError(
                "every policy and risk decision is about placing or changing "
                "something; there is no read-only refusal here"
            )
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError(
                "a refusal must state our own reason; an empty reason would be "
                "a silent no-op with a name"
            )
        if not self.blocks_new_orders:
            raise ValueError(
                "a refusal that does not block is not a refusal; it is a "
                "warning nobody reads"
            )
        if self.state_is_unknown and self.recovery != RECOVERY_RECONCILE_UNKNOWN_FILL:
            raise ValueError("an unknown state is reconciled, never anything else")
        if (self.rule in _UNKNOWN_STATE_RULES) != self.state_is_unknown:
            raise ValueError(
                f"{self.rule} and state_is_unknown={self.state_is_unknown} "
                f"disagree about whether an effect is unproven"
            )

    @property
    def is_transient(self) -> bool:
        """Never. A rule of ours is not a blip, and waiting does not make an
        order permissible."""
        return False

    def as_fact(self) -> dict[str, Any]:
        return {
            "schema": POLICY_REFUSAL_SCHEMA,
            "rule": self.rule,
            "reason": self.reason,
            "recovery": self.recovery,
            "interface": self.interface,
            "operation": self.operation,
            "measured": self.measured,
            "limit": self.limit,
            "client_order_id": self.client_order_id,
            "state_is_unknown": bool(self.state_is_unknown),
            "blocks_new_orders": True,
            "detail": dict(self.detail),
        }


def _allowed_recoveries(rule: str) -> frozenset[str]:
    """Which recoveries a rule may ever declare. ``RECOVERY_RETRY_WITH_BOUND``
    appears for no rule at all: see the module docstring, rule 3."""
    if rule in _UNKNOWN_STATE_RULES:
        return frozenset({RECOVERY_RECONCILE_UNKNOWN_FILL})
    if rule in _RESUME_RULES:
        return frozenset({RECOVERY_RESUME_FROM_PERSISTED_CLIENT_ORDER_ID})
    return frozenset({RECOVERY_REFUSE_TO_PROCEED})


class PolicyRefusalError(RuntimeError):
    """Raised by the policy or the risk mandate, carrying its typed refusal."""

    def __init__(self, refusal: PolicyRefusal) -> None:
        super().__init__(f"{refusal.rule}: {refusal.reason}")
        self.refusal = refusal


def _refuse(rule, reason, interface, **over) -> "PolicyRefusalError":
    unknown = rule in _UNKNOWN_STATE_RULES
    return PolicyRefusalError(PolicyRefusal(
        rule=rule, reason=reason, interface=interface,
        recovery=sorted(_allowed_recoveries(rule))[0] if not unknown
        else RECOVERY_RECONCILE_UNKNOWN_FILL,
        state_is_unknown=unknown, **over,
    ))


@dataclass(frozen=True)
class OrderRequest:
    """One order this lane might place. Plain values only — nothing in here can
    reach a terminal, and nothing in here is a credential."""

    client_order_id: str
    account_fingerprint: str
    symbol: str
    action: str
    volume: float
    decided_at: datetime
    model_id: str = ""
    artifact_sha256: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None

    @property
    def is_opening(self) -> bool:
        return self.action in OPENING_ACTIONS


@dataclass(frozen=True)
class AdmittedOrder:
    """What the policy returns when every rule passed. It is a separate type on
    purpose: a caller cannot mistake ``None`` for permission."""

    request: OrderRequest
    rules_checked: tuple[str, ...]


@dataclass(frozen=True)
class MandateGrant:
    """What the risk mandate returns, with the headroom it measured."""

    request: OrderRequest
    rules_checked: tuple[str, ...]
    volume_headroom: float
    entries_remaining: int


@dataclass(frozen=True)
class OrderPolicy:
    """Is this order allowed to exist at all?"""

    account_fingerprint: str
    allowed_symbols: frozenset[str]
    max_decision_age_seconds: float
    account_kind: str = ACCOUNT_KIND_DEMO

    def __post_init__(self) -> None:
        if self.account_kind != ACCOUNT_KIND_DEMO:
            raise ValueError(
                "this lane is Demo-only: an OrderPolicy cannot be constructed "
                "for any other account kind, so no code path exists that could "
                "admit a live order"
            )
        if not self.allowed_symbols:
            raise ValueError("a mandate with no symbols admits nothing; declare it")
        if not self.max_decision_age_seconds > 0:
            raise ValueError("a decision age bound must be positive")
        if not self.account_fingerprint:
            raise ValueError("the mandate must name the account fingerprint it covers")

    #: The order the rules are applied in. It is part of the contract: the
    #: cheapest structural refusals come first, and the journal is consulted
    #: only for a request that is otherwise well formed.
    RULE_ORDER = (
        RULE_ACCOUNT_NOT_THE_MANDATED_ONE, RULE_ACTION_NOT_DECLARED,
        RULE_SYMBOL_OUTSIDE_MANDATE, RULE_CLIENT_ORDER_ID_MISSING,
        RULE_MODEL_EVIDENCE_MISSING, RULE_PROTECTIVE_BRACKET_MISSING,
        RULE_PROTECTIVE_BRACKET_INVERTED, RULE_DECISION_STALE,
        RULE_DUPLICATE_CLIENT_ORDER_ID, RULE_EFFECT_UNKNOWN_OUTSTANDING,
    )

    def admit(
        self,
        request: OrderRequest,
        *,
        now: datetime,
        already_submitted: Optional[Callable[[str], bool]] = None,
        unknown_effects: int = 0,
    ) -> AdmittedOrder:
        """Admit the order, or raise ``PolicyRefusalError`` with a typed refusal.

        ``already_submitted`` is the journal predicate. It is REQUIRED for an
        order that would open risk: without it there is no way to know whether
        this client order id is already live, and an unanswerable question is
        refused rather than assumed to be "no".
        """
        if request.account_fingerprint != self.account_fingerprint:
            raise _refuse(
                RULE_ACCOUNT_NOT_THE_MANDATED_ONE,
                "the request names an account this mandate does not cover; a "
                "mandate is for one account and is never widened at the call site",
                "policy",
            )
        if request.action not in ACTIONS:
            raise _refuse(
                RULE_ACTION_NOT_DECLARED,
                f"{request.action!r} is not one of the declared actions "
                f"{sorted(ACTIONS)}",
                "policy",
            )
        if request.symbol not in self.allowed_symbols:
            raise _refuse(
                RULE_SYMBOL_OUTSIDE_MANDATE,
                "the symbol is outside the declared mandate; a symbol is added "
                "by declaring it, never by an order arriving for it",
                "policy", measured=request.symbol,
                limit=sorted(self.allowed_symbols),
            )
        if not request.client_order_id:
            raise _refuse(
                RULE_CLIENT_ORDER_ID_MISSING,
                "an order with no client order id cannot be recognised on a "
                "retry, so a second one could be placed for the same decision",
                "policy",
            )
        if request.is_opening and not (request.model_id and request.artifact_sha256):
            raise _refuse(
                RULE_MODEL_EVIDENCE_MISSING,
                "an opening order must name the model and the exact artifact "
                "digest it came from; an entry nobody can attribute is not one "
                "this lane places",
                "policy", client_order_id=request.client_order_id,
            )
        if request.is_opening and (request.stop_loss is None or request.take_profit is None):
            raise _refuse(
                RULE_PROTECTIVE_BRACKET_MISSING,
                "an opening order carries its protective bracket or it is not "
                "placed; an unprotected entry is refused, never placed and "
                "protected afterwards",
                "policy", client_order_id=request.client_order_id,
            )
        if request.is_opening:
            long_ok = request.action == ACTION_OPEN_LONG and request.stop_loss < request.take_profit
            short_ok = request.action == ACTION_OPEN_SHORT and request.take_profit < request.stop_loss
            if not (long_ok or short_ok):
                raise _refuse(
                    RULE_PROTECTIVE_BRACKET_INVERTED,
                    "the bracket's geometry contradicts the order's direction, "
                    "so the stop would be a target and the target a stop",
                    "policy", client_order_id=request.client_order_id,
                    measured={"stop_loss": request.stop_loss,
                              "take_profit": request.take_profit},
                )
        age = _age_seconds(request.decided_at, now)
        if age > self.max_decision_age_seconds:
            raise _refuse(
                RULE_DECISION_STALE,
                "the decision is older than the mandate allows; a stale "
                "decision is re-made, never placed late",
                "policy", client_order_id=request.client_order_id,
                measured=age, limit=self.max_decision_age_seconds,
            )
        if already_submitted is None:
            if request.is_opening:
                raise _refuse(
                    RULE_DUPLICATE_CLIENT_ORDER_ID,
                    "whether this client order id is already live cannot be "
                    "answered without the journal, and an unanswerable question "
                    "is refused rather than answered 'no'",
                    "policy", client_order_id=request.client_order_id,
                )
        elif already_submitted(request.client_order_id):
            raise _refuse(
                RULE_DUPLICATE_CLIENT_ORDER_ID,
                "this client order id is already persisted; the recovery is to "
                "resume from it and reconcile, and placing a second order is "
                "not an option this interface can express",
                "policy", client_order_id=request.client_order_id,
            )
        if int(unknown_effects) > 0:
            raise _refuse(
                RULE_EFFECT_UNKNOWN_OUTSTANDING,
                "an earlier effect on this account has never been proven one "
                "way or the other; new risk waits for it to be reconciled, "
                "because an unknown position is not a flat one",
                "policy", client_order_id=request.client_order_id,
                measured=int(unknown_effects), limit=0,
            )
        return AdmittedOrder(request=request, rules_checked=self.RULE_ORDER)


@dataclass(frozen=True)
class RiskMandate:
    """Is the order allowed to be this big, now?"""

    max_volume: float
    max_open_positions_per_symbol: int
    max_open_commands_per_day: int
    max_daily_loss: float

    def __post_init__(self) -> None:
        if not 0 < self.max_volume:
            raise ValueError("a volume cap must be positive")
        if self.max_open_positions_per_symbol < 1 or self.max_open_commands_per_day < 1:
            raise ValueError("a mandate that permits nothing is declared as no mandate")
        if not self.max_daily_loss > 0:
            raise ValueError(
                "a daily loss limit must be a positive magnitude; its sign is "
                "not the caller's to choose"
            )

    RULE_ORDER = (
        RULE_POSITION_STATE_UNKNOWN, RULE_UNRESOLVED_COMMAND_ON_ROUTE,
        RULE_VOLUME_NOT_POSITIVE, RULE_VOLUME_ABOVE_CAP,
        RULE_OPEN_POSITIONS_AT_CAP, RULE_DAILY_ENTRY_BUDGET_EXHAUSTED,
        RULE_DAILY_LOSS_LIMIT_REACHED,
    )

    def evaluate(
        self,
        request: OrderRequest,
        *,
        position: PositionState,
        unresolved_commands_on_route: int = 0,
        open_positions_on_symbol: int = 0,
        entries_today: int = 0,
        realized_loss_today: float = 0.0,
    ) -> MandateGrant:
        """Grant the order, or raise ``PolicyRefusalError``.

        ``position`` is a ``PositionState`` and not a number, so an unknown
        position cannot be passed in as ``0.0``. It is checked FIRST: every
        other number below is meaningless while the current exposure is unknown.
        """
        if not position.is_known:
            raise _refuse(
                RULE_POSITION_STATE_UNKNOWN,
                f"the route's position is unknown ({position.reason}); it is "
                f"reconciled before any order, and it is never read as flat",
                "risk", client_order_id=request.client_order_id,
            )
        if int(unresolved_commands_on_route) > 0:
            raise _refuse(
                RULE_UNRESOLVED_COMMAND_ON_ROUTE,
                "a command on this route has not resolved yet; the route "
                "carries one at a time by declared concurrency, so a second "
                "would race the first",
                "risk", client_order_id=request.client_order_id,
                measured=int(unresolved_commands_on_route), limit=0,
            )
        if request.is_opening:
            if not request.volume > 0:
                raise _refuse(
                    RULE_VOLUME_NOT_POSITIVE,
                    "an opening order with no volume is not an order; it is "
                    "refused rather than sent as a no-op",
                    "risk", client_order_id=request.client_order_id,
                    measured=request.volume,
                )
            if request.volume > self.max_volume:
                raise _refuse(
                    RULE_VOLUME_ABOVE_CAP,
                    "the requested volume is above the mandate's cap; it is "
                    "refused, never silently reduced to the cap",
                    "risk", client_order_id=request.client_order_id,
                    measured=request.volume, limit=self.max_volume,
                )
            if int(open_positions_on_symbol) >= self.max_open_positions_per_symbol:
                raise _refuse(
                    RULE_OPEN_POSITIONS_AT_CAP,
                    "the route already holds as many positions as the mandate "
                    "permits",
                    "risk", client_order_id=request.client_order_id,
                    measured=int(open_positions_on_symbol),
                    limit=self.max_open_positions_per_symbol,
                )
            if int(entries_today) >= self.max_open_commands_per_day:
                raise _refuse(
                    RULE_DAILY_ENTRY_BUDGET_EXHAUSTED,
                    "the account-wide daily entry budget is spent; a command "
                    "whose outcome is unknown still counts against it, because "
                    "it may have opened a position",
                    "risk", client_order_id=request.client_order_id,
                    measured=int(entries_today),
                    limit=self.max_open_commands_per_day,
                )
        loss = abs(float(realized_loss_today))
        if loss >= self.max_daily_loss and request.is_opening:
            raise _refuse(
                RULE_DAILY_LOSS_LIMIT_REACHED,
                "the day's realized loss has reached the mandate's limit; only "
                "risk-reducing orders remain permitted",
                "risk", client_order_id=request.client_order_id,
                measured=loss, limit=self.max_daily_loss,
            )
        return MandateGrant(
            request=request,
            rules_checked=self.RULE_ORDER,
            volume_headroom=float(self.max_volume - request.volume),
            entries_remaining=int(self.max_open_commands_per_day - int(entries_today)),
        )


def evaluate_order(
    request: OrderRequest,
    *,
    policy: OrderPolicy,
    mandate: RiskMandate,
    now: datetime,
    position: PositionState,
    already_submitted: Optional[Callable[[str], bool]] = None,
    unknown_effects: int = 0,
    unresolved_commands_on_route: int = 0,
    open_positions_on_symbol: int = 0,
    entries_today: int = 0,
    realized_loss_today: float = 0.0,
) -> MandateGrant:
    """Policy, then risk. The first refusal is raised and the rest is not run.

    Order matters and is part of the contract: an order that is not ALLOWED to
    exist is never measured against a cap, so a refused order has exactly one
    reason and that reason is the earliest true one.
    """
    policy.admit(request, now=now, already_submitted=already_submitted,
                 unknown_effects=unknown_effects)
    return mandate.evaluate(
        request, position=position,
        unresolved_commands_on_route=unresolved_commands_on_route,
        open_positions_on_symbol=open_positions_on_symbol,
        entries_today=entries_today,
        realized_loss_today=realized_loss_today,
    )


def _age_seconds(decided_at: datetime, now: datetime) -> float:
    """Age in seconds, refusing a naive timestamp on either side.

    A naive timestamp names no instant, so an age computed from one is not a
    duration. The refusal is a ``ValueError`` rather than a ``PolicyRefusal``
    because it is a programming fault in the caller, not a rule about an order.
    """
    for label, value in (("decided_at", decided_at), ("now", now)):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                f"{label} must be an aware datetime; a naive one names no "
                f"instant and an age computed from it is not a duration"
            )
    return (now.astimezone(timezone.utc) - decided_at.astimezone(timezone.utc)).total_seconds()
