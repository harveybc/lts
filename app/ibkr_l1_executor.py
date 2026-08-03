"""L1 bracket executor: journal first, broker second, truth from facts only.

Corrects findings 063/064 mechanics:

- an effect is durable in the L0 ledger BEFORE any broker call;
- the single-use capability is consumed in the SAME atomic unit as that
  first durable effect record (finding 064);
- each broker call is preceded by a ``call_attempt`` fact and followed by a
  ``call_result`` fact, so a crash between them is provably ambiguous and
  resumes as ``effect_unknown``, never as success;
- the return value after all three placements is ``submitted_pending_ack``:
  this module has NO code path that returns a submitted/acknowledged claim
  without the corresponding broker calls having been made and journaled;
- duplicate intents and restarts replay the journal and never repeat an
  acknowledged effect.

Acknowledgement verification and recovery are the C milestone and live in
``ibkr_l1_recovery``; nothing here marks an effect ``acknowledged``.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from trading_contracts import OrderIntentV2

from app.ibkr_l1_adapter import (
    BracketPlan,
    L1AuthorizationError,
    L1ExecutionError,
)
from app.ibkr_l1_broker import IbkrClientProtocol, translate_bracket
from app.ibkr_l1_journal import L1ExecutionOlap


class L1EffectUnknown(L1ExecutionError):
    """A broker call was attempted and its outcome is not proven."""

    def __init__(self, effect_id: str, message: str) -> None:
        super().__init__(message)
        self.effect_id = effect_id


@dataclass(frozen=True)
class CapabilityRecord:
    """The consumable identity of one owner-issued Paper capability.

    Only digests and bounded metadata: the capability file itself never
    enters the ledger, Git or chat. ``metadata`` carries the enforcement
    ceilings (quantity ceiling, risk ceiling, expiry) copied from the
    validated capability document.
    """

    capability_sha256: str
    nonce_sha256: str
    metadata: dict[str, Any]


def effect_id_for(idempotency_key: str) -> str:
    return "l1e-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]


EFFECT_CONTRACT_SCHEMA = "lts.ibkr_l1.effect_contract.v1"


def _redacted_leg(leg: dict[str, Any]) -> dict[str, Any]:
    """The stored plan carries the account FINGERPRINT, never the raw id."""
    redacted = dict(leg)
    redacted["account"] = None
    return redacted


def build_effect_contract(
    intent: OrderIntentV2,
    plan: BracketPlan,
    *,
    expected_con_id: Optional[int],
    price_decimals: int,
    quantity_decimals: int,
) -> dict[str, Any]:
    """Finding 072: everything acknowledgement and recovery may ever need,
    frozen before the first broker call. Resume reads THIS, not current
    configuration."""
    return {
        "schema": EFFECT_CONTRACT_SCHEMA,
        "kind": "bracket_entry",
        "trace_id": intent.trace_id,
        "intent_object_id": intent.object_id,
        "instrument": intent.instrument,
        "asset_id": intent.asset_id,
        "venue": intent.venue,
        "delta_units": intent.delta_units,
        "reservation_id": None if intent.risk is None else intent.risk.reservation_id,
        "account_fingerprint": hashlib.sha256(
            str(plan.parent["account"]).encode()
        ).hexdigest()[:16],
        "expected_con_id": expected_con_id,
        "price_decimals": price_decimals,
        "quantity_decimals": quantity_decimals,
        "plan": {
            "parent": _redacted_leg(plan.parent),
            "take_profit": _redacted_leg(plan.take_profit),
            "stop_loss": _redacted_leg(plan.stop_loss),
        },
    }


def plan_from_contract(contract: dict[str, Any], *, account: str) -> BracketPlan:
    """Rebuild the exact submitted plan from the immutable record, injecting
    the account only after the caller verified it against the stored
    fingerprint. This is the ONLY legal resume source (finding 072)."""
    if contract.get("schema") != EFFECT_CONTRACT_SCHEMA:
        raise L1ExecutionError("unknown effect contract schema")
    expected = contract["account_fingerprint"]
    observed = hashlib.sha256(account.encode()).hexdigest()[:16]
    if observed != expected:
        raise L1ExecutionError(
            "connected account does not match the immutable effect contract"
        )
    legs = {}
    for name in ("parent", "take_profit", "stop_loss"):
        leg = dict(contract["plan"][name])
        leg["account"] = account
        legs[name] = leg
    return BracketPlan(
        parent=legs["parent"],
        take_profit=legs["take_profit"],
        stop_loss=legs["stop_loss"],
    )


class BracketExecutor:
    """Executes exactly one protected bracket per consumed capability."""

    def __init__(self, olap: L1ExecutionOlap, client: IbkrClientProtocol) -> None:
        self.olap = olap
        self.client = client

    # -- submission --------------------------------------------------------
    def submit_bracket(
        self,
        intent: OrderIntentV2,
        plan: BracketPlan,
        capability: CapabilityRecord,
        *,
        expected_con_id: Optional[int] = None,
        price_decimals: int = 5,
        quantity_decimals: int = 0,
    ) -> dict[str, Any]:
        existing = self.olap.effect_by_key(intent.idempotency_key)
        if existing is not None:
            existing["replayed"] = True
            return existing

        halt = self.olap.get_state("halt", "none")
        if halt != "none":
            raise L1ExecutionError(
                f"global hold active ({halt}); no new risk may be submitted"
            )
        if intent.intent_class != "risk_increasing" or intent.protection is None:
            raise L1ExecutionError("executor only submits protected entries")
        ceiling = float(capability.metadata.get("quantity_ceiling", 0.0))
        if not ceiling > 0.0:
            raise L1AuthorizationError("capability quantity ceiling missing")
        if abs(intent.delta_units) > ceiling:
            raise L1AuthorizationError(
                f"intent quantity {abs(intent.delta_units)} exceeds the "
                f"capability ceiling {ceiling}; the executor never resizes"
            )
        account = self.client.connected_account()
        if account is None or account != plan.parent["account"]:
            raise L1ExecutionError(
                "connected account does not match the planned bracket account"
            )

        translated = translate_bracket(plan, instrument=intent.instrument)
        effect_id = effect_id_for(intent.idempotency_key)
        order_ids = [
            plan.parent["orderId"],
            plan.take_profit["orderId"],
            plan.stop_loss["orderId"],
        ]
        # Finding 064: capability burn and first durable effect are ONE
        # serialized transaction; a concurrent twin or a reused capability
        # fails here, before any broker call.
        try:
            with self.olap.atomic_unit():
                self.olap.create_effect(
                    effect_id,
                    intent.idempotency_key,
                    "bracket_entry",
                    order_ids,
                    capability.capability_sha256,
                )
                self.olap.store_effect_contract(
                    effect_id,
                    build_effect_contract(
                        intent, plan,
                        expected_con_id=expected_con_id,
                        price_decimals=price_decimals,
                        quantity_decimals=quantity_decimals,
                    ),
                )
                self.olap.consume_capability(
                    capability.capability_sha256,
                    capability.nonce_sha256,
                    capability.metadata,
                    effect_id,
                )
        except sqlite3.IntegrityError:
            twin = self.olap.effect_by_key(intent.idempotency_key)
            if twin is not None:
                twin["replayed"] = True
                return twin
            raise L1AuthorizationError(
                "capability already consumed; a new owner-issued capability "
                "is required"
            ) from None

        for leg_name, order in translated.legs():
            self.olap.record_broker_fact(
                effect_id,
                "call_attempt",
                {"leg": leg_name, "orderId": int(order.orderId)},
            )
            try:
                result = self.client.place_order(translated.contract, order)
            except Exception as error:  # noqa: BLE001 — every failure journals
                with self.olap.atomic_unit():
                    self.olap.record_broker_fact(
                        effect_id,
                        "call_failure",
                        {
                            "leg": leg_name,
                            "orderId": int(order.orderId),
                            "error": f"{type(error).__name__}: {error}",
                        },
                    )
                    self.olap.advance_effect(effect_id, "effect_unknown")
                raise L1EffectUnknown(
                    effect_id,
                    f"broker call for {leg_name} failed with unproven outcome; "
                    "effect journaled as unknown and requires reconciliation",
                ) from error
            self.olap.record_broker_fact(
                effect_id,
                "call_result",
                {"leg": leg_name, **{k: result[k] for k in sorted(result)}},
            )
        self.olap.advance_effect(effect_id, "submitted_pending_ack")
        return {
            "state": "submitted_pending_ack",
            "effect_id": effect_id,
            "order_ids": order_ids,
            "acknowledged": False,
            "replayed": False,
        }

    # -- restart classification -------------------------------------------
    def resume_report(self) -> list[dict[str, Any]]:
        """Classify every non-terminal effect from durable facts.

        ``journaled_pending`` with zero ``call_attempt`` facts is provably
        pre-effect (``consumed_before_effect``). Any attempt without a
        matching result is ambiguous and is durably demoted to
        ``effect_unknown``. Nothing is ever promoted here.
        """
        report = []
        for effect in self.olap.nonterminal_effects():
            attempts = self.olap.broker_facts(effect["effect_id"], "call_attempt")
            results = self.olap.broker_facts(effect["effect_id"], "call_result")
            classification = effect["state"]
            if effect["state"] == "journaled_pending":
                if not attempts:
                    # finding 073: zero journaled attempts PROVE no broker
                    # call happened; resolve terminally and visibly. The
                    # consumed capability stays burned by design.
                    with self.olap.atomic_unit():
                        self.olap.record_broker_fact(
                            effect["effect_id"], "no_call_abort",
                            {"proof": "zero call_attempt facts",
                             "capability_sha256": effect["capability_sha256"]},
                        )
                        self.olap.advance_effect(
                            effect["effect_id"], "terminal_aborted_no_call"
                        )
                    classification = "aborted_no_call"
                else:
                    self.olap.advance_effect(effect["effect_id"], "effect_unknown")
                    classification = "effect_unknown"
            elif effect["state"] == "submitted_pending_ack":
                classification = "awaiting_acknowledgement"
            report.append(
                {
                    "effect_id": effect["effect_id"],
                    "idempotency_key": effect["idempotency_key"],
                    "state": self.olap.effect_row(effect["effect_id"])["state"],
                    "classification": classification,
                    "call_attempts": len(attempts),
                    "call_results": len(results),
                }
            )
        return report
