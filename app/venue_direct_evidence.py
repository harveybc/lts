"""WP3.1 — strict DIRECT venue evidence for Alpaca and MT5.

Read-only by construction. Nothing in this module opens a socket, holds
a credential or can reach a broker: it takes the RAW bytes a venue
already produced and turns them into typed facts, or it refuses.

The discipline is the one already accepted for the simulator custody,
applied to real venue payloads:

* authority begins at the ORIGINAL BYTES. A pre-parsed mapping is
  refused, because by then duplicate keys have already been collapsed
  and the payload can no longer be checked;
* duplicate keys, non-finite JSON constants, unknown fields and
  missing fields all refuse — nothing is coerced and nothing is
  defaulted;
* the PARSER is chosen from a sealed allowlist keyed by venue,
  evidence type and schema version, and its identity is recomputed
  from the executing source and compared against a COMMITTED constant,
  so neither a substituted parser nor an unreviewed edit can sign
  evidence;
* the POLICY -- never the payload -- fixes the allowed sources and the
  maximum age. Evidence that declares its own freshness is refused;
* ``venue_direct`` is a property of the SOURCE, not a field a payload
  may assert. ``simulator_bar_local`` and anything carrying
  ``venue_direct=false`` are refused by name.

Order roles are derived from facts the venue itself states, never from
geometry. Alpaca states them structurally: a bracket order's parent is
the entry and each leg is protection typed by its own ``type``. MT5
states them differently and the difference is preserved rather than
flattened: protection lives on the POSITION as ``stop_loss`` /
``take_profit``, so a pending MT5 order is an entry. An order whose
role cannot be established from the payload is AMBIGUOUS and refuses.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence

SCHEMA_VERSION = "lts.venue_direct_evidence.v1"

VENUES = ("alpaca_paper", "mt5_demo")
EVIDENCE_TYPES = ("account_session", "positions", "open_orders",
                  "native_protection", "market_clock",
                  "terminal_orders")
ORDER_ROLES = ("entry", "protective_stop", "protective_take_profit",
               "close")
SIDES = ("long", "short", "flat")

# Provenance that may NEVER stand in for a venue fact. The simulator's
# own cycle evidence is fine for the simulator and is refused here by
# name, together with any envelope that admits it is not venue-direct.
REFUSED_PROVENANCE = ("simulator_bar_local", "simulator", "replay",
                      "backtest", "shadow")


class VenueEvidenceError(ValueError):
    """Venue evidence is unusable — typed refusal, never a default."""


class VenuePolicyError(ValueError):
    """The evidence policy itself is invalid — refused, not defaulted."""


# ---------------------------------------------------------------- #
# strict primitives                                                 #
# ---------------------------------------------------------------- #

def require_text(name: str, value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or \
            not value.strip():
        raise VenueEvidenceError(
            f"{name}: a nonempty string is required, got "
            f"{type(value).__name__} {value!r}")
    return value


def require_real(name: str, value: Any, *, positive: bool = False,
                 nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VenueEvidenceError(
            f"{name}: a finite real number is required, got "
            f"{type(value).__name__} {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise VenueEvidenceError(f"{name}: non-finite value {value!r}")
    if positive and number <= 0.0:
        raise VenueEvidenceError(f"{name}: must be > 0, got {number}")
    if nonnegative and number < 0.0:
        raise VenueEvidenceError(f"{name}: must be >= 0, got {number}")
    return number


_DECIMAL_GRAMMAR = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")


def require_json_number(name: str, value: Any) -> float:
    """A JSON NUMBER, and nothing that merely converts to one.

    ``float(value)`` before a strict check is not a strict check: it
    turns ``True`` into 1.0 and accepts any numeric string, which is
    exactly how a boolean became a 1.0-lot position. MT5 emits real
    JSON numbers, so a string here is a contract violation, not a
    value to be rescued."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VenueEvidenceError(
            f"{name}: a JSON number is required, got "
            f"{type(value).__name__} {value!r} — no coercion")
    number = float(value)
    if not math.isfinite(number):
        raise VenueEvidenceError(f"{name}: non-finite value {value!r}")
    return number


def require_decimal_string(name: str, value: Any) -> float:
    """A finite decimal STRING in the grammar the venue documents.

    Alpaca returns quantities and prices as strings. They are read
    through an explicit grammar -- optional sign, no leading zeros, an
    optional fractional part -- so leading or trailing whitespace,
    exponents, ``NaN``, ``Infinity``, ``0x`` forms and bare booleans
    all refuse instead of being handed to ``float()``."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise VenueEvidenceError(
            f"{name}: a decimal string is required, got "
            f"{type(value).__name__} {value!r}")
    if not _DECIMAL_GRAMMAR.match(value):
        raise VenueEvidenceError(
            f"{name}: {value!r} is not a finite decimal in the "
            "documented grammar (no whitespace, no exponent, no "
            "non-finite words)")
    number = float(value)
    if not math.isfinite(number):  # pragma: no cover - grammar bars it
        raise VenueEvidenceError(f"{name}: non-finite value {value!r}")
    return number


def bounded(name: str, number: float, *, positive: bool = False,
            nonnegative: bool = False) -> float:
    if positive and number <= 0.0:
        raise VenueEvidenceError(f"{name}: must be > 0, got {number}")
    if nonnegative and number < 0.0:
        raise VenueEvidenceError(f"{name}: must be >= 0, got {number}")
    return number


def require_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise VenueEvidenceError(
            f"{name}: a bool is required, got "
            f"{type(value).__name__} {value!r}")
    return value


def require_enum(name: str, value: Any,
                 allowed: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise VenueEvidenceError(
            f"{name}: {value!r} is not one of {list(allowed)}")
    return value


def require_utc(name: str, value: Any) -> datetime:
    """A timezone-aware UTC instant. Naive stamps refuse: a venue
    timestamp without an offset cannot be aged."""
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError as exc:
            raise VenueEvidenceError(
                f"{name}: {value!r} is not an ISO-8601 instant") \
                from exc
    else:
        raise VenueEvidenceError(
            f"{name}: an ISO-8601 UTC instant is required, got "
            f"{type(value).__name__} {value!r}")
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise VenueEvidenceError(
            f"{name}: timezone-aware UTC required; a naive venue "
            "timestamp cannot be aged")
    return stamp.astimezone(timezone.utc)


def require_fields(name: str, payload: Mapping[str, Any],
                   expected: Sequence[str]) -> None:
    present, wanted = set(payload), set(expected)
    missing = sorted(wanted - present)
    unknown = sorted(present - wanted)
    if missing:
        raise VenueEvidenceError(f"{name}: missing fields {missing}")
    if unknown:
        raise VenueEvidenceError(
            f"{name}: unknown fields {unknown} — an unrecognised "
            "field may carry meaning this parser does not model")


def decode_payload_bytes(raw: bytes, *, what: str) -> Any:
    """Authority begins at the ORIGINAL BYTES."""
    if not isinstance(raw, (bytes, bytearray)):
        raise VenueEvidenceError(
            f"{what}: original payload bytes are required, not a "
            f"pre-parsed {type(raw).__name__} — duplicate keys would "
            "already have been collapsed")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VenueEvidenceError(f"{what}: invalid encoding: {exc}") \
            from exc

    def _no_duplicates(pairs):
        seen: dict = {}
        for key, value in pairs:
            if key in seen:
                raise VenueEvidenceError(
                    f"{what}: duplicate key {key!r} in the raw venue "
                    "payload — refused")
            seen[key] = value
        return seen

    def _no_constants(token):
        raise VenueEvidenceError(
            f"{what}: non-finite JSON constant {token!r} refused")

    try:
        return json.loads(text, object_pairs_hook=_no_duplicates,
                          parse_constant=_no_constants)
    except VenueEvidenceError:
        raise
    except json.JSONDecodeError as exc:
        raise VenueEvidenceError(f"{what}: invalid JSON: {exc}") \
            from exc


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- #
# policy — the POLICY owns freshness and sources, not the payload    #
# ---------------------------------------------------------------- #

@dataclass(frozen=True)
class VenueEvidencePolicy:
    venue: str
    account_fingerprint: str
    symbol: str
    allowed_sources: tuple
    max_age_seconds: float
    schema_version: str
    calendar_identity: str
    # E11: the POLICY names the collector whose receipts it accepts.
    # An arbitrary nonempty string was authority for nobody.
    collector_source: str
    collector_code_identity: str

    def __post_init__(self):
        require_enum("venue", self.venue, VENUES)
        for name in ("account_fingerprint", "symbol",
                     "schema_version", "calendar_identity",
                     "collector_source", "collector_code_identity"):
            require_text(name, getattr(self, name))
        require_real("max_age_seconds", self.max_age_seconds,
                     positive=True)
        if not isinstance(self.allowed_sources, tuple) or \
                not self.allowed_sources:
            raise VenuePolicyError(
                "allowed_sources must be a non-empty tuple")
        for source in self.allowed_sources:
            require_text("allowed_source", source)
            if source in REFUSED_PROVENANCE:
                raise VenuePolicyError(
                    f"allowed_sources may not contain {source!r}: "
                    "that provenance is not venue-direct evidence")

    @staticmethod
    def build(**kwargs) -> "VenueEvidencePolicy":
        sources = kwargs.pop("allowed_sources")
        return VenueEvidencePolicy(
            allowed_sources=tuple(sources),
            schema_version=kwargs.pop("schema_version",
                                      SCHEMA_VERSION),
            **kwargs)

    @property
    def policy_digest(self) -> str:
        material = canonical_bytes({
            "venue": self.venue,
            "account_fingerprint": self.account_fingerprint,
            "symbol": self.symbol,
            "allowed_sources": sorted(self.allowed_sources),
            "max_age_seconds": self.max_age_seconds,
            "schema_version": self.schema_version,
            "calendar_identity": self.calendar_identity,
            "collector_source": self.collector_source,
            "collector_code_identity": self.collector_code_identity,
            "parsers": sorted(
                f"{'|'.join(key)}={identity}"
                for key, identity in SEALED_PARSER_IDENTITIES.items()),
        })
        return sha256_hex(material)


# ---------------------------------------------------------------- #
# parsers — one per (venue, evidence type, schema version)           #
# ---------------------------------------------------------------- #

def _parse_alpaca_account_session_v1(payload: Any) -> dict:
    """Alpaca ``GET /v2/account`` joined with ``GET /v2/clock``.

    Numeric strings go through the documented decimal grammar; the
    account fingerprint is DERIVED here so it can be bound to the
    policy rather than merely asserted by the envelope."""
    if not isinstance(payload, dict):
        raise VenueEvidenceError("account session must be an object")
    require_fields("alpaca.account_session", payload,
                   ("account", "clock"))
    account, clock = payload["account"], payload["clock"]
    if not isinstance(account, dict) or not isinstance(clock, dict):
        raise VenueEvidenceError(
            "account and clock must both be objects")
    require_fields("alpaca.account", account,
                   ("id", "account_number", "status",
                    "trading_blocked", "cash", "equity"))
    require_fields("alpaca.clock", clock,
                   ("timestamp", "is_open", "next_open", "next_close"))
    identity = require_text("account.id", account["id"])
    return {
        "account_identity": identity,
        "account_fingerprint": sha256_hex(
            identity.encode("utf-8"))[:16],
        "session_connected": True,
        "trading_enabled": not require_bool(
            "account.trading_blocked", account["trading_blocked"]),
        "account_status": require_text("account.status",
                                       account["status"]),
        "market_open": require_bool("clock.is_open", clock["is_open"]),
        "cash": require_decimal_string("account.cash",
                                       account["cash"]),
        "equity": require_decimal_string("account.equity",
                                         account["equity"]),
        "observed_at": require_utc("clock.timestamp",
                                   clock["timestamp"]).isoformat(),
        "internal_symbols": (),
    }


def _parse_alpaca_positions_v1(payload: Any) -> dict:
    """Alpaca ``GET /v2/positions``. Signed quantity comes from the
    venue's own redundant qty/side pair, and a contradiction between
    them refuses rather than picking a winner. Every symbol is
    reported so the caller can BIND it to the policy."""
    if not isinstance(payload, dict):
        raise VenueEvidenceError("positions payload must be an object")
    require_fields("alpaca.positions", payload,
                   ("positions", "observed_at"))
    rows = payload["positions"]
    if not isinstance(rows, list):
        raise VenueEvidenceError("positions must be a list")
    parsed, identities = [], set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise VenueEvidenceError(f"position[{index}] not an object")
        require_fields(f"alpaca.position[{index}]", row,
                       ("asset_id", "symbol", "qty", "side",
                        "avg_entry_price"))
        quantity = require_decimal_string(f"position[{index}].qty",
                                          row["qty"])
        side = require_enum(f"position[{index}].side", row["side"],
                            ("long", "short"))
        if side == "long" and quantity > 0.0:
            signed = quantity
        elif side == "short" and quantity > 0.0:
            signed = -quantity
        elif side == "short" and quantity < 0.0:
            signed = quantity
        else:
            raise VenueEvidenceError(
                f"position[{index}]: qty {quantity} contradicts side "
                f"{side!r} — the venue's redundant facts disagree")
        identity = require_text(f"position[{index}].asset_id",
                                row["asset_id"])
        if identity in identities:
            raise VenueEvidenceError(
                f"position identity {identity!r} appears twice — "
                "duplicate identities refuse")
        identities.add(identity)
        parsed.append({
            "position_identity": identity,
            # E7: Alpaca's asset_id names the ASSET, not the position
            # instance — a closed and reopened position carries the
            # SAME value. Stated here so no consumer can mistake it
            # for an instance identity.
            "identity_kind": "asset_identity_only",
            "symbol": require_text(f"position[{index}].symbol",
                                   row["symbol"]),
            "side": side,
            "signed_quantity": signed,
            "entry_price": bounded(
                f"position[{index}].avg_entry_price",
                require_decimal_string(
                    f"position[{index}].avg_entry_price",
                    row["avg_entry_price"]), positive=True),
        })
    return {"positions": tuple(parsed),
            "positions_total": len(parsed),
            "internal_symbols": tuple(sorted(
                {row["symbol"] for row in parsed})),
            "observed_at": require_utc(
                "positions.observed_at",
                payload["observed_at"]).isoformat()}


# Alpaca states what an order is FOR. These are its own values, not
# an interpretation of side and quantity.
_ALPACA_OPENING_INTENTS = ("buy_to_open", "sell_to_open")
_ALPACA_CLOSING_INTENTS = ("buy_to_close", "sell_to_close")
_ALPACA_INTENTS = _ALPACA_OPENING_INTENTS + _ALPACA_CLOSING_INTENTS
_ALPACA_PROTECTIVE_TYPE_ROLE = {
    "limit": "protective_take_profit",
    "stop": "protective_stop",
    "stop_limit": "protective_stop",
}


def _alpaca_intent_side(intent: str) -> str:
    return "buy" if intent.startswith("buy_") else "sell"


def _parse_alpaca_open_orders_v1(payload: Any) -> dict:
    """Alpaca ``GET /v2/orders?status=open&nested=true``.

    The role comes from what the venue DECLARES the order is for --
    ``position_intent`` together with the order type -- and never from
    side and quantity. An owner-authorized read-only capture disproved
    the earlier assumption that a top-level ``order_class=bracket``
    object is always an entry: once the parent has filled, the
    endpoint returns the resting protective child at the top level
    with ``legs`` null, and Alpaca declares it plainly as
    ``position_intent=buy_to_close``. Reading that as an entry made a
    protective take-profit a cancellation candidate during WIND_DOWN,
    and geometry could not have rescued it either -- a BUY while SHORT
    looks exactly like a reversal.

    Exactly two shapes are supported, and anything else refuses:

    * an OPENING intent with a non-empty ``legs`` list: the object is
      the entry and each leg is protection typed by its own ``type``;
    * a CLOSING intent with ``legs`` null or empty: the object is
      itself the protective child, typed by its own ``type``.

    ``legs`` is validated as the venue sent it. Turning null into an
    empty list before the contract runs is precisely the
    normalisation that produced the misclassification."""
    if not isinstance(payload, dict):
        raise VenueEvidenceError("orders payload must be an object")
    require_fields("alpaca.open_orders", payload,
                   ("orders", "observed_at"))
    rows = payload["orders"]
    if not isinstance(rows, list):
        raise VenueEvidenceError("orders must be a list")
    parsed, identities = [], set()

    def _remember(identity: str) -> str:
        if identity in identities:
            raise VenueEvidenceError(
                f"order identity {identity!r} appears twice — "
                "duplicate identities refuse")
        identities.add(identity)
        return identity

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise VenueEvidenceError(f"order[{index}] not an object")
        require_fields(f"alpaca.order[{index}]", row,
                       ("id", "symbol", "side", "qty", "status",
                        "order_class", "type", "position_intent",
                        "legs"))
        order_class = require_text(f"order[{index}].order_class",
                                   row["order_class"])
        if order_class != "bracket":
            raise VenueEvidenceError(
                f"order[{index}]: order_class {order_class!r} does "
                "not state a role; only a bracket states one, and a "
                "role may never be inferred from side or size")
        intent = require_enum(f"order[{index}].position_intent",
                              row["position_intent"],
                              _ALPACA_INTENTS)
        side = require_enum(f"order[{index}].side", row["side"],
                            ("buy", "sell"))
        if side != _alpaca_intent_side(intent):
            raise VenueEvidenceError(
                f"order[{index}]: side {side!r} contradicts "
                f"position_intent {intent!r} — the venue's own facts "
                "disagree")
        order_type = require_text(f"order[{index}].type", row["type"])
        symbol = require_text(f"order[{index}].symbol", row["symbol"])
        quantity = bounded(
            f"order[{index}].qty",
            require_decimal_string(f"order[{index}].qty", row["qty"]),
            positive=True)
        status = require_text(f"order[{index}].status", row["status"])

        # legs EXACTLY as the venue sent them: null and [] are the
        # same shape here, and a list is a different one. Nothing is
        # normalised before the contract decides.
        legs = row["legs"]
        if legs is not None and not isinstance(legs, list):
            raise VenueEvidenceError(
                f"order[{index}].legs must be null or a list, got "
                f"{type(legs).__name__}")
        has_legs = bool(legs)

        if intent in _ALPACA_OPENING_INTENTS:
            if not has_legs:
                raise VenueEvidenceError(
                    f"order[{index}]: an opening bracket with no legs "
                    "is not a shape this parser models; a bracket "
                    "parent carries its protection")
            parsed.append({
                "order_identity": _remember(
                    require_text(f"order[{index}].id", row["id"])),
                "symbol": symbol, "side": side, "quantity": quantity,
                "order_type": order_type,
                "position_intent": intent,
                "role": "entry", "status": status,
            })
            for leg_index, leg in enumerate(legs):
                if not isinstance(leg, dict):
                    raise VenueEvidenceError(
                        f"order[{index}].legs[{leg_index}] not an "
                        "object")
                require_fields(
                    f"alpaca.order[{index}].leg[{leg_index}]", leg,
                    ("id", "side", "type", "qty", "status"))
                leg_type = require_text("leg.type", leg["type"])
                role = _ALPACA_PROTECTIVE_TYPE_ROLE.get(leg_type)
                if role is None:
                    raise VenueEvidenceError(
                        f"order[{index}].leg[{leg_index}]: type "
                        f"{leg_type!r} does not state a protective "
                        "role")
                leg_side = require_enum(
                    f"order[{index}].leg[{leg_index}].side",
                    leg["side"], ("buy", "sell"))
                # C10-A: a protective leg CLOSES what the parent
                # opened, so it must oppose it. Validating the side
                # only as buy|sell accepted a buy_to_open parent
                # carrying BUY protection, which cannot close a long
                # position: the venue's own facts contradict the
                # claimed role, and the whole order population is
                # refused rather than partially derived.
                if leg_side == side:
                    raise VenueEvidenceError(
                        f"order[{index}].leg[{leg_index}]: a "
                        f"protective leg on side {leg_side!r} cannot "
                        f"close a parent opened on side {side!r} — "
                        "the venue's facts contradict the claimed "
                        "protective role")
                parsed.append({
                    "order_identity": _remember(
                        require_text("leg.id", leg["id"])),
                    "symbol": symbol,
                    "side": leg_side,
                    "quantity": bounded(
                        "leg.qty",
                        require_decimal_string("leg.qty", leg["qty"]),
                        positive=True),
                    "order_type": leg_type,
                    "position_intent": None,
                    "role": role,
                    "status": require_text("leg.status",
                                           leg["status"]),
                })
            continue

        # a CLOSING intent: this object IS the protective child
        if has_legs:
            raise VenueEvidenceError(
                f"order[{index}]: position_intent {intent!r} closes a "
                "position but the order carries legs — a protective "
                "child has no children of its own")
        role = _ALPACA_PROTECTIVE_TYPE_ROLE.get(order_type)
        if role is None:
            raise VenueEvidenceError(
                f"order[{index}]: a closing bracket order of type "
                f"{order_type!r} states no protective role; only "
                f"{sorted(_ALPACA_PROTECTIVE_TYPE_ROLE)} do")
        parsed.append({
            "order_identity": _remember(
                require_text(f"order[{index}].id", row["id"])),
            "symbol": symbol, "side": side, "quantity": quantity,
            "order_type": order_type, "position_intent": intent,
            "role": role, "status": status,
        })

    entries = tuple(o for o in parsed if o["role"] == "entry")
    return {"orders": tuple(parsed),
            "orders_total": len(parsed),
            "entry_orders": len(entries),
            "protective_orders": len(parsed) - len(entries),
            "internal_symbols": tuple(sorted(
                {row["symbol"] for row in parsed})),
            "observed_at": require_utc(
                "orders.observed_at",
                payload["observed_at"]).isoformat()}


# E2: TERMINAL verdicts are their own evidence type, derived from
# original venue bytes like everything else. A bare string can no
# longer release the cancellation gate, and ABSENCE from an
# open-order list is never a terminal verdict: only a status the
# venue itself declares terminal produces one.
_ALPACA_TERMINAL_STATUS_VERDICT = {
    "canceled": "cancelled",
    "expired": "cancelled",
    "filled": "filled_before_cancel",
    "rejected": "rejected",
    "replaced": "replaced",
    "failed": "failed",
}
_MT5_TERMINAL_STATE_VERDICT = {
    "ORDER_STATE_CANCELED": "cancelled",
    "ORDER_STATE_EXPIRED": "cancelled",
    "ORDER_STATE_FILLED": "filled_before_cancel",
    "ORDER_STATE_REJECTED": "rejected",
}


# E10: the venue timestamp that corresponds to each declared status.
# A status whose dedicated stamp field is set for a DIFFERENT outcome
# is a contradiction and refuses.
_ALPACA_STATUS_STAMP_FIELD = {"canceled": "canceled_at",
                              "expired": "expired_at",
                              "filled": "filled_at"}
_ALPACA_OPTIONAL_STAMPS = ("canceled_at", "expired_at", "filled_at")


def _parse_terminal_rows(payload: Any, *, what: str,
                         identity_field: str, status_field: str,
                         event_time_field: str,
                         verdict_map: Mapping[str, str],
                         status_stamp_fields: Mapping[str, str] = (),
                         optional_stamps: tuple = ()) -> dict:
    """E8/E10: the payload is the venue's ARRAY of order objects — no
    wrapper, no caller-inserted timestamp. Each verdict carries ITS
    OWN venue event time, taken from the stamp field the venue
    dedicates to the declared status where one exists, else the
    generic event field. No aggregate maximum may authorise another
    row: consumers must judge freshness PER identity."""
    if not isinstance(payload, list):
        raise VenueEvidenceError(
            f"{what}: the venue body is an ARRAY of order objects; a "
            "wrapper object is a local construction, not venue bytes")
    if not payload:
        raise VenueEvidenceError(
            f"{what}: an empty terminal body carries no verdict — "
            "absence is never a terminal verdict")
    status_stamp_fields = dict(status_stamp_fields)
    verdicts: dict = {}
    symbols = set()
    latest = None
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise VenueEvidenceError(f"{what}[{index}] not an object")
        base = {identity_field, "symbol", status_field,
                event_time_field}
        allowed = base | set(optional_stamps)
        missing = sorted(base - set(row))
        unknown = sorted(set(row) - allowed)
        if missing:
            raise VenueEvidenceError(
                f"{what}[{index}]: missing fields {missing}")
        if unknown:
            raise VenueEvidenceError(
                f"{what}[{index}]: unknown fields {unknown}")
        identity = require_text(f"{what}[{index}].{identity_field}",
                                row[identity_field])
        if identity in verdicts:
            raise VenueEvidenceError(
                f"{what}: identity {identity!r} appears twice — "
                "duplicate identities refuse")
        status = require_text(f"{what}[{index}].{status_field}",
                              row[status_field])
        verdict = verdict_map.get(status)
        if verdict is None:
            raise VenueEvidenceError(
                f"{what}[{index}]: status {status!r} is not a "
                "TERMINAL status this parser models — a non-terminal "
                "or unknown status can never be a verdict")
        # a stamp dedicated to a DIFFERENT outcome contradicts the
        # declared status
        own_stamp = status_stamp_fields.get(status)
        for stamp_field in optional_stamps:
            value = row.get(stamp_field)
            if value is not None and stamp_field != own_stamp:
                raise VenueEvidenceError(
                    f"{what}[{index}]: status {status!r} contradicts "
                    f"a set {stamp_field!r} — the venue's own facts "
                    "disagree")
        source_field = own_stamp if own_stamp and \
            row.get(own_stamp) is not None else event_time_field
        stamp = require_utc(f"{what}[{index}].{source_field}",
                            row[source_field])
        latest = stamp if latest is None or stamp > latest else latest
        verdicts[identity] = {"verdict": verdict,
                              "event_at": stamp.isoformat()}
        symbols.add(require_text(f"{what}[{index}].symbol",
                                 row["symbol"]))
    return {"verdicts": tuple(sorted(verdicts.items())),
            "orders_total": len(verdicts),
            "internal_symbols": tuple(sorted(symbols)),
            "observed_at": latest.isoformat()}


def _parse_alpaca_terminal_orders_v1(payload: Any) -> dict:
    """Alpaca terminal order verdicts, e.g. ``GET /v2/orders/{id}``
    per identity: the venue's own terminal ``status`` decides."""
    return _parse_terminal_rows(
        payload, what="alpaca.terminal_orders", identity_field="id",
        status_field="status", event_time_field="updated_at",
        verdict_map=_ALPACA_TERMINAL_STATUS_VERDICT,
        status_stamp_fields=_ALPACA_STATUS_STAMP_FIELD,
        optional_stamps=_ALPACA_OPTIONAL_STAMPS)


def _parse_mt5_terminal_orders_v1(payload: Any) -> dict:
    """MT5 terminal order verdicts, from the EA's history rows: the
    venue's own terminal ``state`` decides."""
    return _parse_terminal_rows(
        payload, what="mt5.terminal_orders", identity_field="ticket",
        status_field="state", event_time_field="done_time",
        verdict_map=_MT5_TERMINAL_STATE_VERDICT)


def _parse_mt5_account_session_v1(payload: Any) -> dict:
    """MT5 heartbeat, as the EA emits it. Numbers are JSON numbers."""
    if not isinstance(payload, dict):
        raise VenueEvidenceError("heartbeat must be an object")
    require_fields("mt5.account_session", payload,
                   ("schema", "adapter_version", "account_fingerprint",
                    "server_fingerprint", "environment", "connected",
                    "trade_allowed", "terminal_build",
                    "terminal_ping_ms", "observed_at"))
    environment = require_enum("environment", payload["environment"],
                               ("demo",))
    return {
        "account_fingerprint": require_text(
            "account_fingerprint", payload["account_fingerprint"]),
        "server_fingerprint": require_text(
            "server_fingerprint", payload["server_fingerprint"]),
        "environment": environment,
        "session_connected": require_bool("connected",
                                          payload["connected"]),
        "trading_enabled": require_bool("trade_allowed",
                                        payload["trade_allowed"]),
        "terminal_build": bounded(
            "terminal_build",
            require_json_number("terminal_build",
                                payload["terminal_build"]),
            positive=True),
        "observed_at": require_utc("observed_at",
                                   payload["observed_at"]).isoformat(),
        "internal_symbols": (),
    }


def _parse_mt5_positions_v1(payload: Any) -> dict:
    """MT5 positions. Protection is NOT a separate order here: it
    lives on the position as stop_loss/take_profit, and that
    difference from Alpaca is preserved rather than flattened."""
    if not isinstance(payload, dict):
        raise VenueEvidenceError("positions payload must be an object")
    require_fields("mt5.positions", payload,
                   ("positions", "observed_at"))
    rows = payload["positions"]
    if not isinstance(rows, list):
        raise VenueEvidenceError("positions must be a list")
    parsed, identities = [], set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise VenueEvidenceError(f"position[{index}] not an object")
        require_fields(f"mt5.position[{index}]", row,
                       ("ticket", "symbol", "side", "volume",
                        "price_open", "time_open_unix", "stop_loss",
                        "take_profit", "profit"))
        side = require_enum(f"position[{index}].side", row["side"],
                            ("long", "short"))
        volume = bounded(
            f"position[{index}].volume",
            require_json_number(f"position[{index}].volume",
                                row["volume"]), positive=True)
        stop_loss = bounded(
            f"position[{index}].stop_loss",
            require_json_number(f"position[{index}].stop_loss",
                                row["stop_loss"]), nonnegative=True)
        take_profit = bounded(
            f"position[{index}].take_profit",
            require_json_number(f"position[{index}].take_profit",
                                row["take_profit"]), nonnegative=True)
        identity = require_text(f"position[{index}].ticket",
                                row["ticket"])
        if identity in identities:
            raise VenueEvidenceError(
                f"position identity {identity!r} appears twice — "
                "duplicate identities refuse")
        identities.add(identity)
        parsed.append({
            "position_identity": identity,
            # the MT5 ticket IS a position-instance identity: a
            # reopened position receives a new ticket
            "identity_kind": "venue_position_instance",
            "symbol": require_text(f"position[{index}].symbol",
                                   row["symbol"]),
            "side": side,
            "signed_quantity": volume if side == "long" else -volume,
            "entry_price": bounded(
                f"position[{index}].price_open",
                require_json_number(f"position[{index}].price_open",
                                    row["price_open"]),
                positive=True),
            "opened_at_unix": bounded(
                f"position[{index}].time_open_unix",
                require_json_number(
                    f"position[{index}].time_open_unix",
                    row["time_open_unix"]), positive=True),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "native_protection_present": stop_loss > 0.0
            and take_profit > 0.0,
        })
    return {"positions": tuple(parsed),
            "positions_total": len(parsed),
            "internal_symbols": tuple(sorted(
                {row["symbol"] for row in parsed})),
            "observed_at": require_utc(
                "positions.observed_at",
                payload["observed_at"]).isoformat()}


_MT5_PENDING_ENTRY_TYPES = (
    "ORDER_TYPE_BUY_LIMIT", "ORDER_TYPE_SELL_LIMIT",
    "ORDER_TYPE_BUY_STOP", "ORDER_TYPE_SELL_STOP",
    "ORDER_TYPE_BUY_STOP_LIMIT", "ORDER_TYPE_SELL_STOP_LIMIT",
)


def _parse_mt5_open_orders_v1(payload: Any) -> dict:
    """MT5 pending orders. Every resting MT5 order is a pending ENTRY:
    protection is carried on the position, not as an order. A type
    outside the pending-entry vocabulary has no establishable role and
    refuses."""
    if not isinstance(payload, dict):
        raise VenueEvidenceError("orders payload must be an object")
    require_fields("mt5.open_orders", payload,
                   ("orders", "observed_at"))
    rows = payload["orders"]
    if not isinstance(rows, list):
        raise VenueEvidenceError("orders must be a list")
    parsed, identities = [], set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise VenueEvidenceError(f"order[{index}] not an object")
        require_fields(f"mt5.order[{index}]", row,
                       ("ticket", "symbol", "order_type", "volume",
                        "price_open", "stop_loss", "take_profit",
                        "state"))
        order_type = require_enum(f"order[{index}].order_type",
                                  row["order_type"],
                                  _MT5_PENDING_ENTRY_TYPES)
        identity = require_text(f"order[{index}].ticket",
                                row["ticket"])
        if identity in identities:
            raise VenueEvidenceError(
                f"order identity {identity!r} appears twice — "
                "duplicate identities refuse")
        identities.add(identity)
        parsed.append({
            "order_identity": identity,
            "symbol": require_text(f"order[{index}].symbol",
                                   row["symbol"]),
            "order_type": order_type,
            "side": "buy" if "BUY" in order_type else "sell",
            "quantity": bounded(
                f"order[{index}].volume",
                require_json_number(f"order[{index}].volume",
                                    row["volume"]), positive=True),
            "role": "entry",
            "status": require_text(f"order[{index}].state",
                                   row["state"]),
        })
    return {"orders": tuple(parsed),
            "orders_total": len(parsed),
            "entry_orders": len(parsed),
            "protective_orders": 0,
            "internal_symbols": tuple(sorted(
                {row["symbol"] for row in parsed})),
            "observed_at": require_utc(
                "orders.observed_at",
                payload["observed_at"]).isoformat()}


def _parse_mt5_market_clock_v1(payload: Any) -> dict:
    """The last CLOSED bar and the live tick, bound to one symbol."""
    if not isinstance(payload, dict):
        raise VenueEvidenceError("market clock must be an object")
    require_fields("mt5.market_clock", payload,
                   ("symbol", "timeframe", "last_closed_bar", "tick",
                    "observed_at"))
    bar = payload["last_closed_bar"]
    tick = payload["tick"]
    if not isinstance(bar, dict) or not isinstance(tick, dict):
        raise VenueEvidenceError("bar and tick must both be objects")
    require_fields("mt5.bar", bar,
                   ("time", "open", "high", "low", "close", "volume"))
    require_fields("mt5.tick", tick, ("bid", "ask", "observed_at"))
    high = bounded("bar.high",
                   require_json_number("bar.high", bar["high"]),
                   positive=True)
    low = bounded("bar.low",
                  require_json_number("bar.low", bar["low"]),
                  positive=True)
    if low > high:
        raise VenueEvidenceError(
            f"bar geometry is impossible: low {low} > high {high}")
    bid = bounded("tick.bid",
                  require_json_number("tick.bid", tick["bid"]),
                  positive=True)
    ask = bounded("tick.ask",
                  require_json_number("tick.ask", tick["ask"]),
                  positive=True)
    if ask < bid:
        raise VenueEvidenceError(
            f"quote is crossed: ask {ask} < bid {bid}")
    symbol = require_text("symbol", payload["symbol"])
    return {
        "symbol": symbol,
        "timeframe": require_text("timeframe", payload["timeframe"]),
        "bar_time": require_utc("bar.time", bar["time"]).isoformat(),
        "bar_close": bounded(
            "bar.close",
            require_json_number("bar.close", bar["close"]),
            positive=True),
        "bid": bid,
        "ask": ask,
        "spread": ask - bid,
        "quote_observed_at": require_utc(
            "tick.observed_at", tick["observed_at"]).isoformat(),
        "internal_symbols": (symbol,),
        "observed_at": require_utc(
            "market_clock.observed_at",
            payload["observed_at"]).isoformat(),
    }


PARSERS: Mapping[tuple, Callable] = MappingProxyType({
    ("alpaca_paper", "account_session", "v1"):
        _parse_alpaca_account_session_v1,
    ("alpaca_paper", "positions", "v1"): _parse_alpaca_positions_v1,
    ("alpaca_paper", "open_orders", "v1"):
        _parse_alpaca_open_orders_v1,
    ("mt5_demo", "account_session", "v1"):
        _parse_mt5_account_session_v1,
    ("mt5_demo", "positions", "v1"): _parse_mt5_positions_v1,
    ("mt5_demo", "open_orders", "v1"): _parse_mt5_open_orders_v1,
    ("mt5_demo", "market_clock", "v1"): _parse_mt5_market_clock_v1,
    ("alpaca_paper", "terminal_orders", "v1"):
        _parse_alpaca_terminal_orders_v1,
    ("mt5_demo", "terminal_orders", "v1"):
        _parse_mt5_terminal_orders_v1,
})


def parser_identity(key: tuple, parser: Callable) -> str:
    source = inspect.getsource(parser).encode("utf-8")
    material = b"|".join([
        "::".join(key).encode("utf-8"),
        parser.__name__.encode("utf-8"),
        sha256_hex(source).encode("utf-8"),
    ])
    return sha256_hex(material)[:32]


# Sealed identities, COMMITTED. Recomputing at use catches evidence
# derived under a different parser, but a substituted parser is
# self-consistent for NEW evidence and would sign its own forgery, so
# the executing source is checked against these constants. Any drift
# -- a substitution or an unreviewed edit -- refuses until the
# constant is deliberately updated in review.
SEALED_PARSER_IDENTITIES: Mapping[tuple, str] = MappingProxyType({
    ("alpaca_paper", "account_session", "v1"):
        "91fa22c79095caddbf82ada5f525ab1f",
    ("alpaca_paper", "positions", "v1"):
        "4bb954bb7064dcd64133247c636bc991",
    ("alpaca_paper", "open_orders", "v1"):
        "3788ff42f8e06d4fb64ca9b1b7f7ebec",
    ("mt5_demo", "account_session", "v1"):
        "2fea3126c04002638e45d77cac493398",
    ("mt5_demo", "positions", "v1"):
        "1a40f36b6980157c700df0b5fd87aa58",
    ("mt5_demo", "open_orders", "v1"):
        "34e0be893f2e43eac00a06b6a50a3301",
    ("mt5_demo", "market_clock", "v1"):
        "a2da56287687f89dda0c144710511b82",
    ("alpaca_paper", "terminal_orders", "v1"):
        "a0f6089cb27d3718443a6bf58e59b1c5",
    ("mt5_demo", "terminal_orders", "v1"):
        "96cca1f60e270c7e8bb718124a0d1676",
})


def resolve_parser(key: tuple):
    parser = PARSERS.get(key)
    if parser is None:
        raise VenueEvidenceError(
            f"unknown venue/evidence/schema {key} — no allowlisted "
            "parser")
    identity = parser_identity(key, parser)
    sealed = SEALED_PARSER_IDENTITIES.get(key)
    if sealed is None:
        raise VenueEvidenceError(
            f"no sealed parser identity committed for {key}")
    if identity != sealed:
        raise VenueEvidenceError(
            f"executing parser for {key} has identity "
            f"{identity[:12]}… but the SEALED committed identity is "
            f"{sealed[:12]}… — parser substitution or unreviewed code "
            "change refused")
    return parser, identity


# ---------------------------------------------------------------- #
# the acquisition receipt (E8)                                      #
# ---------------------------------------------------------------- #

@dataclass(frozen=True)
class AcquisitionReceipt:
    """Trusted LOCAL acquisition metadata, kept OUTSIDE the venue
    payload. The receipt binds the exact body bytes it was issued
    for, names the collector and its code identity, and carries a
    monotonic acquisition ordinal. Placing a local timestamp inside
    the purported venue payload is exactly the confusion that let a
    replayed body verify as fresh."""

    collector_source: str
    collector_code_identity: str
    received_at: datetime
    monotonic_seq: int
    body_sha256: str

    def __post_init__(self):
        require_text("collector_source", self.collector_source)
        require_text("collector_code_identity",
                     self.collector_code_identity)
        if not isinstance(self.received_at, datetime) or \
                self.received_at.tzinfo is None:
            raise VenueEvidenceError(
                "received_at must be a timezone-aware datetime")
        if isinstance(self.monotonic_seq, bool) or not isinstance(
                self.monotonic_seq, int) or self.monotonic_seq < 0:
            raise VenueEvidenceError(
                f"monotonic_seq must be a nonnegative int, got "
                f"{self.monotonic_seq!r}")
        if not isinstance(self.body_sha256, str) or \
                len(self.body_sha256) != 64 or any(
                    c not in "0123456789abcdef"
                    for c in self.body_sha256):
            raise VenueEvidenceError(
                "body_sha256 must be the canonical 64-character "
                "lowercase hex digest of the exact venue bytes this "
                "receipt was issued for — no other shape is a digest")

    @staticmethod
    def build(*, collector_source: str,
              collector_code_identity: str, received_at: Any,
              monotonic_seq: int, body: bytes
              ) -> "AcquisitionReceipt":
        return AcquisitionReceipt(
            collector_source=collector_source,
            collector_code_identity=collector_code_identity,
            received_at=require_utc("received_at", received_at),
            monotonic_seq=monotonic_seq,
            body_sha256=sha256_hex(bytes(body)))

    def as_dict(self) -> dict:
        return {"collector_source": self.collector_source,
                "collector_code_identity":
                    self.collector_code_identity,
                "received_at": self.received_at.isoformat(),
                "monotonic_seq": self.monotonic_seq,
                "body_sha256": self.body_sha256}


# evidence types whose payload is a venue body that MUST arrive with
# an acquisition receipt
RECEIPT_REQUIRED = ("terminal_orders",)


class ReceiptLedgerError(VenueEvidenceError):
    """The receipt ledger refuses — typed, never a default."""


class ReceiptLedger:
    """E11: a DURABLE per-route ledger that makes receipt
    monotonicity and body uniqueness facts instead of declarations.

    One directory per route (venue|account|symbol). One file per
    sequence number, created with the audited uncertain-write
    protocol: an O_EXCL temporary, write, fchmod 0600, fsync, an
    exclusive final create, rename, parent fsync — a failure at any
    point registers nothing. A route-level registration lock makes
    the read-check-create transaction exclusive, so:

    * a sequence rollback or reuse refuses (strictly increasing,
      atomically enforced);
    * a replayed BODY under a fresh, higher, fabricated sequence
      refuses (body uniqueness per route);
    * two concurrent collectors elect exactly one registration;
    * re-registering the identical receipt is idempotent, because a
      resume must not fail on its own history.

    Registration happens BEFORE a receipt's evidence may authorize
    any effect; an unregistered receipt authorizes nothing."""

    def __init__(self, root: Any):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ReceiptLedgerError(
                f"{self.root}: symlinked ledger root refused")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _route_dir(self, route: str) -> Path:
        require_text("route", route)
        safe = route.replace("|", "_").replace("/", "_")
        directory = self.root / safe
        if directory.is_symlink():
            raise ReceiptLedgerError(
                f"{safe}: symlinked route refused")
        directory.mkdir(exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        return directory

    def register(self, receipt: AcquisitionReceipt, *,
                 route: str) -> dict:
        if not isinstance(receipt, AcquisitionReceipt):
            raise ReceiptLedgerError(
                "a typed AcquisitionReceipt is required")
        directory = self._route_dir(route)
        payload = receipt.as_dict()
        lock = directory / "register.lock"
        if lock.is_symlink():
            raise ReceiptLedgerError("symlinked register lock refused")
        try:
            lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT |
                              os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ReceiptLedgerError(
                f"route {route!r}: a concurrent registration holds "
                "the lock — exactly one collector registers at a "
                "time") from exc
        try:
            os.write(lock_fd, str(os.getpid()).encode())
            os.fsync(lock_fd)
        finally:
            os.close(lock_fd)
        try:
            existing = {}
            for path in directory.glob("[0-9]*.json"):
                if path.is_symlink():
                    raise ReceiptLedgerError(
                        f"{path.name}: symlinked ledger record "
                        "refused")
                record = json.loads(path.read_text())
                existing[int(record["monotonic_seq"])] = record
            seq = receipt.monotonic_seq
            if seq in existing:
                if existing[seq] == payload:
                    return existing[seq]        # idempotent resume
                raise ReceiptLedgerError(
                    f"route {route!r}: sequence {seq} is already "
                    "registered with DIFFERENT content — reuse "
                    "refused")
            if existing and seq <= max(existing):
                raise ReceiptLedgerError(
                    f"route {route!r}: sequence {seq} does not "
                    f"exceed the registered maximum {max(existing)} "
                    "— rollback or reuse refused")
            for record in existing.values():
                if record["body_sha256"] == payload["body_sha256"]:
                    raise ReceiptLedgerError(
                        f"route {route!r}: this exact body was "
                        f"already registered at sequence "
                        f"{record['monotonic_seq']} — a replayed "
                        "body under a fresh receipt is refused")
            path = directory / f"{seq:08d}.json"
            tmp = path.with_suffix(f".json.tmp.{os.getpid()}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600)
            try:
                os.write(fd, json.dumps(payload, indent=1,
                                        default=str).encode())
                os.fchmod(fd, 0o600)
                os.fsync(fd)
            except Exception:
                os.close(fd)
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise
            os.close(fd)
            try:
                final = os.open(path, os.O_WRONLY | os.O_CREAT |
                                os.O_EXCL, 0o600)
            except FileExistsError as exc:
                os.unlink(tmp)
                raise ReceiptLedgerError(
                    f"route {route!r}: sequence {seq} was registered "
                    "concurrently") from exc
            os.close(final)
            os.replace(tmp, path)
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return payload
        finally:
            try:
                os.unlink(lock)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- #
# the evidence envelope                                             #
# ---------------------------------------------------------------- #

@dataclass(frozen=True)
class VenueDirectEvidence:
    venue: str
    account_fingerprint: str
    symbol: str
    evidence_type: str
    schema_version: str
    source: str
    evidence_id: str
    observed_at: datetime
    raw_sha256: str
    payload_sha256: str
    parser_digest: str
    receipt: Optional[AcquisitionReceipt] = None
    _facts: tuple = field(default=(), repr=False)

    @property
    def facts(self) -> dict:
        return dict(self._facts)

    @property
    def venue_direct(self) -> bool:
        """Always True for this envelope. It is a property of the
        SOURCE and of the parser that produced the facts, never a
        field a payload may assert about itself."""
        return True

    @staticmethod
    def parse(*, venue: str, account_fingerprint: str, symbol: str,
              evidence_type: str, schema_version: str, source: str,
              evidence_id: str, raw_bytes: bytes,
              transport_observed_at: Any = None,
              receipt: Optional[AcquisitionReceipt] = None
              ) -> "VenueDirectEvidence":
        """C1: freshness comes from the BYTES.

        The envelope has no free ``observed_at`` any more. The
        timestamp is the one the parser extracts from the payload
        itself, so a 2020 payload cannot be rewrapped in a 2026
        envelope and pass a 120-second policy. A transport timestamp
        may be supplied, but only as a CHECK: it must equal the
        internal one to the microsecond, and a mismatch refuses."""
        require_enum("venue", venue, VENUES)
        require_enum("evidence_type", evidence_type, EVIDENCE_TYPES)
        for name, value in (("account_fingerprint",
                             account_fingerprint),
                            ("symbol", symbol),
                            ("schema_version", schema_version),
                            ("source", source),
                            ("evidence_id", evidence_id)):
            require_text(name, value)
        if source in REFUSED_PROVENANCE:
            raise VenueEvidenceError(
                f"source {source!r} is not venue-direct evidence and "
                "is refused by name")
        if evidence_type in RECEIPT_REQUIRED:
            if not isinstance(receipt, AcquisitionReceipt):
                raise VenueEvidenceError(
                    f"{evidence_type} evidence requires a typed "
                    "AcquisitionReceipt — a venue body without an "
                    "acquisition envelope has no trusted receipt "
                    "time")
            if receipt.body_sha256 != sha256_hex(bytes(raw_bytes)):
                raise VenueEvidenceError(
                    "the acquisition receipt does not bind these "
                    "bytes — body/envelope substitution refused")
        decoded = decode_payload_bytes(
            raw_bytes, what=f"{venue}.{evidence_type}")
        if isinstance(decoded, dict) and "venue_direct" in decoded:
            raise VenueEvidenceError(
                "a payload may not assert venue_direct about itself; "
                "provenance is a property of the source")
        key = (venue, evidence_type, schema_version)
        parser, identity = resolve_parser(key)
        facts = parser(decoded)
        if "observed_at" not in facts:
            raise VenueEvidenceError(
                f"{key}: the parser produced no observed_at; "
                "freshness must come from the payload")
        stamp = require_utc("payload.observed_at",
                            facts["observed_at"])
        if transport_observed_at is not None:
            transport = require_utc("transport_observed_at",
                                    transport_observed_at)
            if transport != stamp:
                raise VenueEvidenceError(
                    f"transport timestamp {transport.isoformat()} "
                    f"does not match the payload's "
                    f"{stamp.isoformat()} — a transport stamp is a "
                    "check on the body, never a substitute for it")
        canonical = canonical_bytes(decoded)
        return VenueDirectEvidence(
            venue=venue, account_fingerprint=account_fingerprint,
            symbol=symbol, evidence_type=evidence_type,
            schema_version=schema_version, source=source,
            evidence_id=evidence_id, observed_at=stamp,
            raw_sha256=sha256_hex(bytes(raw_bytes)),
            payload_sha256=sha256_hex(canonical),
            parser_digest=identity, receipt=receipt,
            _facts=tuple(sorted(facts.items())))

    def verify(self, policy: VenueEvidencePolicy, *,
               now: Any) -> "VenueDirectEvidence":
        """POLICY-owned admission. The evidence never declares its own
        freshness or its own acceptable source."""
        if not isinstance(policy, VenueEvidencePolicy):
            raise VenueEvidenceError(
                "a validated VenueEvidencePolicy is required")
        moment = require_utc("now", now)
        if self.venue != policy.venue:
            raise VenueEvidenceError(
                f"venue {self.venue!r} is not the policy's "
                f"{policy.venue!r}")
        if self.account_fingerprint != policy.account_fingerprint:
            raise VenueEvidenceError(
                "account fingerprint does not match the policy — "
                "evidence from a foreign account is refused")
        if self.symbol != policy.symbol:
            raise VenueEvidenceError(
                f"symbol {self.symbol!r} is not the policy's "
                f"{policy.symbol!r}")
        if self.schema_version != policy.schema_version and \
                policy.schema_version != SCHEMA_VERSION:
            raise VenueEvidenceError(
                f"schema {self.schema_version!r} is not the policy's "
                f"{policy.schema_version!r}")
        if self.source not in policy.allowed_sources:
            raise VenueEvidenceError(
                f"source {self.source!r} is not in the policy's "
                f"allowlist {list(policy.allowed_sources)}")
        if self.evidence_type in RECEIPT_REQUIRED:
            receipt = self.receipt
            if receipt is None:
                raise VenueEvidenceError(
                    "the acquisition receipt is missing")
            drift = (receipt.received_at - moment).total_seconds()
            if drift > 0.0:
                raise VenueEvidenceError(
                    f"the receipt is stamped {drift:.3f}s in the "
                    "future")
            if receipt.collector_source != \
                    policy.collector_source or \
                    receipt.collector_code_identity != \
                    policy.collector_code_identity:
                raise VenueEvidenceError(
                    f"the receipt names collector "
                    f"{receipt.collector_source!r}/"
                    f"{receipt.collector_code_identity!r} but the "
                    f"policy binds {policy.collector_source!r}/"
                    f"{policy.collector_code_identity!r} — a foreign "
                    "collector is refused")
            if receipt.received_at < self.observed_at:
                raise VenueEvidenceError(
                    "the receipt predates the venue event it carries "
                    "— an event cannot be received before it "
                    "happened")
            receipt_age = (moment -
                           receipt.received_at).total_seconds()
            if receipt_age > policy.max_age_seconds:
                raise VenueEvidenceError(
                    f"the acquisition receipt is {receipt_age:.3f}s "
                    f"old and the policy allows "
                    f"{policy.max_age_seconds:.3f}s — a stale "
                    "receipt is refused")
        age = (moment - self.observed_at).total_seconds()
        if age < 0.0:
            raise VenueEvidenceError(
                f"evidence is stamped {abs(age):.3f}s in the future")
        if age > policy.max_age_seconds:
            raise VenueEvidenceError(
                f"evidence is {age:.3f}s old and the policy allows "
                f"{policy.max_age_seconds:.3f}s — stale evidence is "
                "refused")
        _, identity = resolve_parser(
            (self.venue, self.evidence_type, self.schema_version))
        if identity != self.parser_digest:
            raise VenueEvidenceError(
                "parser identity mismatch — the executing parser is "
                "not the one this evidence was derived under")

        # C2: the ENVELOPE agreeing with the policy proves nothing
        # about the FACTS. Every symbol inside the payload, and the
        # account fingerprint the payload itself states, are bound
        # here. A mixed-symbol payload refuses; it is never filtered
        # and never silently summed.
        facts = self.facts
        internal = tuple(facts.get("internal_symbols", ()))
        foreign = sorted({s for s in internal if s != policy.symbol})
        if foreign:
            raise VenueEvidenceError(
                f"payload carries symbols {foreign} but the policy "
                f"binds {policy.symbol!r} — a foreign or mixed-symbol "
                "payload is refused, never filtered")
        stated = facts.get("account_fingerprint")
        if stated is not None and stated != policy.account_fingerprint:
            raise VenueEvidenceError(
                f"the payload states account fingerprint {stated!r} "
                f"but the policy binds "
                f"{policy.account_fingerprint!r} — the envelope may "
                "not vouch for an account the facts contradict")
        return self

    def provenance(self) -> dict:
        return {
            "venue": self.venue,
            "evidence_type": self.evidence_type,
            "schema_version": self.schema_version,
            "source": self.source,
            "evidence_id": self.evidence_id,
            "observed_at": self.observed_at.isoformat(),
            "raw_sha256": self.raw_sha256,
            "payload_sha256": self.payload_sha256,
            "parser_digest": self.parser_digest,
            "venue_direct": True,
            "receipt": (None if self.receipt is None
                        else self.receipt.as_dict()),
        }


def require_venue_direct(provenance: Mapping[str, Any]) -> None:
    """Guard for any consumer that demands venue authority. The
    simulator's own cycle evidence is explicitly refused by name."""
    if not isinstance(provenance, Mapping):
        raise VenueEvidenceError("provenance must be a mapping")
    declared = provenance.get("evidence_provenance") or \
        provenance.get("source")
    if declared in REFUSED_PROVENANCE:
        raise VenueEvidenceError(
            f"{declared!r} is simulator evidence and may never stand "
            "in for a venue fact")
    if provenance.get("venue_direct") is not True:
        raise VenueEvidenceError(
            f"venue_direct is {provenance.get('venue_direct')!r}; "
            "only direct venue evidence carries this authority")
