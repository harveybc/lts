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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence

SCHEMA_VERSION = "lts.venue_direct_evidence.v1"

VENUES = ("alpaca_paper", "mt5_demo")
EVIDENCE_TYPES = ("account_session", "positions", "open_orders",
                  "native_protection", "market_clock")
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

    def __post_init__(self):
        require_enum("venue", self.venue, VENUES)
        for name in ("account_fingerprint", "symbol",
                     "schema_version", "calendar_identity"):
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
            "parsers": sorted(
                f"{'|'.join(key)}={identity}"
                for key, identity in SEALED_PARSER_IDENTITIES.items()),
        })
        return sha256_hex(material)


# ---------------------------------------------------------------- #
# parsers — one per (venue, evidence type, schema version)           #
# ---------------------------------------------------------------- #

def _parse_alpaca_account_session_v1(payload: Any) -> dict:
    """Alpaca ``GET /v2/account`` joined with ``GET /v2/clock``."""
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
        "cash": require_real("account.cash", float(account["cash"])),
        "equity": require_real("account.equity",
                               float(account["equity"])),
        "observed_at": require_utc("clock.timestamp",
                                   clock["timestamp"]).isoformat(),
    }


def _parse_alpaca_positions_v1(payload: Any) -> dict:
    """Alpaca ``GET /v2/positions``. Signed quantity is derived from
    the venue's own redundant qty/side pair, and a contradiction
    between them refuses rather than picking a winner."""
    if not isinstance(payload, dict):
        raise VenueEvidenceError("positions payload must be an object")
    require_fields("alpaca.positions", payload,
                   ("positions", "observed_at"))
    rows = payload["positions"]
    if not isinstance(rows, list):
        raise VenueEvidenceError("positions must be a list")
    parsed = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise VenueEvidenceError(f"position[{index}] not an object")
        require_fields(f"alpaca.position[{index}]", row,
                       ("asset_id", "symbol", "qty", "side",
                        "avg_entry_price"))
        quantity = require_real(f"position[{index}].qty",
                                float(row["qty"]))
        side = require_enum(f"position[{index}].side", row["side"],
                            ("long", "short"))
        if side == "short" and quantity > 0.0:
            signed = -quantity
        elif side == "long" and quantity > 0.0:
            signed = quantity
        elif quantity < 0.0 and side == "short":
            signed = quantity
        else:
            raise VenueEvidenceError(
                f"position[{index}]: qty {quantity} contradicts side "
                f"{side!r} — the venue's redundant facts disagree")
        parsed.append({
            "position_identity": require_text(
                f"position[{index}].asset_id", row["asset_id"]),
            "symbol": require_text(f"position[{index}].symbol",
                                   row["symbol"]),
            "side": side,
            "signed_quantity": signed,
            "entry_price": require_real(
                f"position[{index}].avg_entry_price",
                float(row["avg_entry_price"]), positive=True),
        })
    return {"positions": tuple(parsed),
            "positions_total": len(parsed),
            "observed_at": require_utc(
                "positions.observed_at",
                payload["observed_at"]).isoformat()}


def _parse_alpaca_open_orders_v1(payload: Any) -> dict:
    """Alpaca ``GET /v2/orders?status=open&nested=true``.

    The role is STRUCTURAL: a bracket parent is the entry and each leg
    is protection typed by its own ``type``. An order whose class the
    venue does not state as a bracket has no establishable role and
    refuses -- guessing from side and size is exactly how an
    independent reversal gets mistaken for protection."""
    if not isinstance(payload, dict):
        raise VenueEvidenceError("orders payload must be an object")
    require_fields("alpaca.open_orders", payload,
                   ("orders", "observed_at"))
    rows = payload["orders"]
    if not isinstance(rows, list):
        raise VenueEvidenceError("orders must be a list")
    parsed = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise VenueEvidenceError(f"order[{index}] not an object")
        require_fields(f"alpaca.order[{index}]", row,
                       ("id", "symbol", "side", "qty", "status",
                        "order_class", "type", "legs"))
        order_class = require_text(f"order[{index}].order_class",
                                   row["order_class"])
        if order_class != "bracket":
            raise VenueEvidenceError(
                f"order[{index}]: order_class {order_class!r} does "
                "not state a role; only a bracket states one "
                "structurally, and a role may never be inferred from "
                "side or size")
        legs = row["legs"]
        if not isinstance(legs, list):
            raise VenueEvidenceError(f"order[{index}].legs not a list")
        parsed.append({
            "order_identity": require_text(f"order[{index}].id",
                                           row["id"]),
            "symbol": require_text(f"order[{index}].symbol",
                                   row["symbol"]),
            "side": require_enum(f"order[{index}].side", row["side"],
                                 ("buy", "sell")),
            "quantity": require_real(f"order[{index}].qty",
                                     float(row["qty"]), positive=True),
            "role": "entry",
            "status": require_text(f"order[{index}].status",
                                   row["status"]),
        })
        for leg_index, leg in enumerate(legs):
            if not isinstance(leg, dict):
                raise VenueEvidenceError(
                    f"order[{index}].legs[{leg_index}] not an object")
            require_fields(
                f"alpaca.order[{index}].leg[{leg_index}]", leg,
                ("id", "side", "type", "qty", "status"))
            leg_type = require_text("leg.type", leg["type"])
            if leg_type in ("stop", "stop_limit"):
                role = "protective_stop"
            elif leg_type == "limit":
                role = "protective_take_profit"
            else:
                raise VenueEvidenceError(
                    f"order[{index}].leg[{leg_index}]: type "
                    f"{leg_type!r} does not state a protective role")
            parsed.append({
                "order_identity": require_text("leg.id", leg["id"]),
                "symbol": require_text(f"order[{index}].symbol",
                                       row["symbol"]),
                "side": require_enum("leg.side", leg["side"],
                                     ("buy", "sell")),
                "quantity": require_real("leg.qty", float(leg["qty"]),
                                         positive=True),
                "role": role,
                "status": require_text("leg.status", leg["status"]),
            })
    entries = tuple(o for o in parsed if o["role"] == "entry")
    return {"orders": tuple(parsed),
            "orders_total": len(parsed),
            "entry_orders": len(entries),
            "protective_orders": len(parsed) - len(entries),
            "observed_at": require_utc(
                "orders.observed_at",
                payload["observed_at"]).isoformat()}


def _parse_mt5_account_session_v1(payload: Any) -> dict:
    """MT5 heartbeat, as the EA emits it."""
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
        "terminal_build": require_real("terminal_build",
                                       float(payload["terminal_build"]),
                                       positive=True),
        "observed_at": require_utc("observed_at",
                                   payload["observed_at"]).isoformat(),
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
    parsed = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise VenueEvidenceError(f"position[{index}] not an object")
        require_fields(f"mt5.position[{index}]", row,
                       ("ticket", "symbol", "side", "volume",
                        "price_open", "time_open_unix", "stop_loss",
                        "take_profit", "profit"))
        side = require_enum(f"position[{index}].side", row["side"],
                            ("long", "short"))
        volume = require_real(f"position[{index}].volume",
                              float(row["volume"]), positive=True)
        stop_loss = require_real(f"position[{index}].stop_loss",
                                 float(row["stop_loss"]),
                                 nonnegative=True)
        take_profit = require_real(f"position[{index}].take_profit",
                                   float(row["take_profit"]),
                                   nonnegative=True)
        parsed.append({
            "position_identity": require_text(
                f"position[{index}].ticket", row["ticket"]),
            "symbol": require_text(f"position[{index}].symbol",
                                   row["symbol"]),
            "side": side,
            "signed_quantity": volume if side == "long" else -volume,
            "entry_price": require_real(
                f"position[{index}].price_open",
                float(row["price_open"]), positive=True),
            "opened_at_unix": require_real(
                f"position[{index}].time_open_unix",
                float(row["time_open_unix"]), positive=True),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "native_protection_present": stop_loss > 0.0
            and take_profit > 0.0,
        })
    return {"positions": tuple(parsed),
            "positions_total": len(parsed),
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
    parsed = []
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
        parsed.append({
            "order_identity": require_text(f"order[{index}].ticket",
                                           row["ticket"]),
            "symbol": require_text(f"order[{index}].symbol",
                                   row["symbol"]),
            "order_type": order_type,
            "side": "buy" if "BUY" in order_type else "sell",
            "quantity": require_real(f"order[{index}].volume",
                                     float(row["volume"]),
                                     positive=True),
            "role": "entry",
            "status": require_text(f"order[{index}].state",
                                   row["state"]),
        })
    return {"orders": tuple(parsed),
            "orders_total": len(parsed),
            "entry_orders": len(parsed),
            "protective_orders": 0,
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
    high = require_real("bar.high", float(bar["high"]), positive=True)
    low = require_real("bar.low", float(bar["low"]), positive=True)
    if low > high:
        raise VenueEvidenceError(
            f"bar geometry is impossible: low {low} > high {high}")
    bid = require_real("tick.bid", float(tick["bid"]), positive=True)
    ask = require_real("tick.ask", float(tick["ask"]), positive=True)
    if ask < bid:
        raise VenueEvidenceError(
            f"quote is crossed: ask {ask} < bid {bid}")
    return {
        "symbol": require_text("symbol", payload["symbol"]),
        "timeframe": require_text("timeframe", payload["timeframe"]),
        "bar_time": require_utc("bar.time", bar["time"]).isoformat(),
        "bar_close": require_real("bar.close", float(bar["close"]),
                                  positive=True),
        "bid": bid,
        "ask": ask,
        "spread": ask - bid,
        "quote_observed_at": require_utc(
            "tick.observed_at", tick["observed_at"]).isoformat(),
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
        "a3aca5b763b767f1b086fb5a332a978c",
    ("alpaca_paper", "positions", "v1"):
        "fa83c4eb9b4bd97384b7775fc46158fa",
    ("alpaca_paper", "open_orders", "v1"):
        "100383bed07ae90910b066042f9721ac",
    ("mt5_demo", "account_session", "v1"):
        "d956657db548638f65847295c407c17b",
    ("mt5_demo", "positions", "v1"):
        "0bcafe790a37af252ff5b379025c73e6",
    ("mt5_demo", "open_orders", "v1"):
        "90142e533176e9bcf8be9e131663d841",
    ("mt5_demo", "market_clock", "v1"):
        "e573f2914d75464f6a184620be7fbf79",
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
              evidence_id: str, observed_at: Any,
              raw_bytes: bytes) -> "VenueDirectEvidence":
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
        stamp = require_utc("observed_at", observed_at)
        decoded = decode_payload_bytes(
            raw_bytes, what=f"{venue}.{evidence_type}")
        if isinstance(decoded, dict) and "venue_direct" in decoded:
            raise VenueEvidenceError(
                "a payload may not assert venue_direct about itself; "
                "provenance is a property of the source")
        key = (venue, evidence_type, schema_version)
        parser, identity = resolve_parser(key)
        facts = parser(decoded)
        canonical = canonical_bytes(decoded)
        return VenueDirectEvidence(
            venue=venue, account_fingerprint=account_fingerprint,
            symbol=symbol, evidence_type=evidence_type,
            schema_version=schema_version, source=source,
            evidence_id=evidence_id, observed_at=stamp,
            raw_sha256=sha256_hex(bytes(raw_bytes)),
            payload_sha256=sha256_hex(canonical),
            parser_digest=identity,
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
        if self.source not in policy.allowed_sources:
            raise VenueEvidenceError(
                f"source {self.source!r} is not in the policy's "
                f"allowlist {list(policy.allowed_sources)}")
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
