"""WP3.5 — effect-free acceptance for the venue direct-evidence layer.

Every payload here is SANITIZED and synthetic: no account number, no
server name, no host, no credential. Nothing in this file opens a
socket, and the autouse fixture below makes that structural rather
than aspirational.
"""
from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from app.venue_direct_evidence import (
    PARSERS, SEALED_PARSER_IDENTITIES, VenueDirectEvidence,
    VenueEvidenceError, VenueEvidencePolicy, VenuePolicyError,
    parser_identity, require_venue_direct, resolve_parser)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*_args, **_kwargs):
        raise AssertionError(
            "network operation attempted in a WP3 evidence test")
    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


NOW = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)
OBSERVED = (NOW - timedelta(seconds=5)).isoformat()

ALPACA_ACCOUNT = {
    "account": {"id": "sanitized-account-uuid",
                "account_number": "SANITIZED0001",
                "status": "ACTIVE", "trading_blocked": False,
                "cash": "10000.00", "equity": "10250.00"},
    "clock": {"timestamp": OBSERVED, "is_open": True,
              "next_open": OBSERVED, "next_close": OBSERVED},
}
ALPACA_POSITIONS = {
    "observed_at": OBSERVED,
    "positions": [{"asset_id": "sanitized-asset-uuid",
                   "symbol": "SPY", "qty": "10", "side": "long",
                   "avg_entry_price": "500.25"}],
}
ALPACA_ORDERS = {
    "observed_at": OBSERVED,
    "orders": [{
        "id": "parent-order-id", "symbol": "SPY", "side": "buy",
        "qty": "10", "status": "new", "order_class": "bracket",
        "type": "market", "position_intent": "buy_to_open",
        "legs": [
            {"id": "stop-leg-id", "side": "sell", "type": "stop",
             "qty": "10", "status": "held"},
            {"id": "limit-leg-id", "side": "sell", "type": "limit",
             "qty": "10", "status": "held"}],
    }],
}
ALPACA_PROTECTIVE_CHILD = {
    "observed_at": OBSERVED,
    "orders": [{
        "id": "synthetic-order-0001", "symbol": "SPY",
        "side": "buy", "qty": "1", "status": "new",
        "order_class": "bracket", "type": "limit",
        "position_intent": "buy_to_close", "legs": None,
    }],
}
ALPACA_SHORT_POSITION = {
    "observed_at": OBSERVED,
    "positions": [{"asset_id": "synthetic-asset-0001",
                   "symbol": "SPY", "qty": "1", "side": "short",
                   "avg_entry_price": "500.00"}],
}
MT5_ACCOUNT = {
    "schema": "lts.mt5.heartbeat.v1", "adapter_version": "2.0.0",
    "account_fingerprint": "sanitizedfp01",
    "server_fingerprint": "sanitizedsrv1", "environment": "demo",
    "connected": True, "trade_allowed": True,
    "terminal_build": 4260, "terminal_ping_ms": 12,
    "observed_at": OBSERVED,
}
MT5_POSITIONS = {
    "observed_at": OBSERVED,
    "positions": [{"ticket": "100001", "symbol": "USDCAD",
                   "side": "long", "volume": 0.1,
                   "price_open": 1.35, "time_open_unix": 1756400000,
                   "stop_loss": 1.34, "take_profit": 1.36,
                   "profit": 4.2}],
}
MT5_ORDERS = {
    "observed_at": OBSERVED,
    "orders": [{"ticket": "200001", "symbol": "USDCAD",
                "order_type": "ORDER_TYPE_BUY_LIMIT", "volume": 0.1,
                "price_open": 1.30, "stop_loss": 1.29,
                "take_profit": 1.31, "state": "ORDER_STATE_PLACED"}],
}
MT5_CLOCK = {
    "symbol": "USDCAD", "timeframe": "H4", "observed_at": OBSERVED,
    "last_closed_bar": {"time": OBSERVED, "open": 1.35, "high": 1.36,
                        "low": 1.34, "close": 1.355, "volume": 900},
    "tick": {"bid": 1.3549, "ask": 1.3551, "observed_at": OBSERVED},
}


def policy(venue="alpaca_paper", symbol="SPY",
           account=None, **kw):
    account = ALPACA_FP if account is None else account
    base = dict(venue=venue, account_fingerprint=account,
                symbol=symbol,
                allowed_sources=("alpaca_paper_rest_v2",),
                max_age_seconds=120.0,
                calendar_identity="cal-venue-v1")
    base.update(kw)
    return VenueEvidencePolicy.build(**base)


ALPACA_FP = "7853afed1025c1ba"


def evidence(venue, kind, payload, *, symbol="SPY",
             account=ALPACA_FP, source="alpaca_paper_rest_v2",
             raw=None, transport=None):
    return VenueDirectEvidence.parse(
        venue=venue, account_fingerprint=account, symbol=symbol,
        evidence_type=kind, schema_version="v1", source=source,
        evidence_id=f"ev-{kind}",
        raw_bytes=raw if raw is not None
        else json.dumps(payload).encode(),
        transport_observed_at=transport)


MT5_SOURCE = "mt5_bridge_snapshot_v1"


def mt5_policy(**kw):
    base = dict(venue="mt5_demo", symbol="USDCAD",
                account="sanitizedfp01",
                allowed_sources=(MT5_SOURCE,))
    base.update(kw)
    return policy(**base)


def mt5_evidence(kind, payload, **kw):
    return evidence("mt5_demo", kind, payload, symbol="USDCAD",
                    account="sanitizedfp01", source=MT5_SOURCE, **kw)


# =================================================================== #
# both venues parse                                                   #
# =================================================================== #

class TestBothVenuesParse:

    def test_alpaca_account_session(self):
        facts = evidence("alpaca_paper", "account_session",
                         ALPACA_ACCOUNT).facts
        assert facts["session_connected"] is True
        assert facts["trading_enabled"] is True
        assert facts["market_open"] is True
        assert len(facts["account_fingerprint"]) == 16

    def test_alpaca_positions_signed_quantity(self):
        facts = evidence("alpaca_paper", "positions",
                         ALPACA_POSITIONS).facts
        assert facts["positions_total"] == 1
        assert facts["positions"][0]["signed_quantity"] == 10.0
        short = json.loads(json.dumps(ALPACA_POSITIONS))
        short["positions"][0]["side"] = "short"
        facts = evidence("alpaca_paper", "positions", short).facts
        assert facts["positions"][0]["signed_quantity"] == -10.0

    def test_alpaca_order_roles_are_structural(self):
        facts = evidence("alpaca_paper", "open_orders",
                         ALPACA_ORDERS).facts
        roles = [o["role"] for o in facts["orders"]]
        assert roles == ["entry", "protective_stop",
                         "protective_take_profit"]
        assert facts["entry_orders"] == 1
        assert facts["protective_orders"] == 2

    def test_mt5_account_session(self):
        facts = mt5_evidence("account_session", MT5_ACCOUNT).facts
        assert facts["environment"] == "demo"
        assert facts["session_connected"] is True

    def test_mt5_protection_lives_on_the_position(self):
        facts = mt5_evidence("positions", MT5_POSITIONS).facts
        row = facts["positions"][0]
        assert row["signed_quantity"] == pytest.approx(0.1)
        assert row["native_protection_present"] is True
        orders = mt5_evidence("open_orders", MT5_ORDERS).facts
        assert orders["protective_orders"] == 0, (
            "MT5 carries protection on the POSITION; a resting order "
            "is a pending entry")
        assert orders["entry_orders"] == 1

    def test_mt5_market_clock(self):
        facts = mt5_evidence("market_clock", MT5_CLOCK).facts
        assert facts["symbol"] == "USDCAD"
        assert facts["spread"] == pytest.approx(0.0002, abs=1e-9)


# =================================================================== #
# refusals                                                            #
# =================================================================== #

class TestRefusals:

    def test_a_pre_parsed_mapping_refuses(self):
        with pytest.raises(VenueEvidenceError,
                           match="original payload bytes"):
            VenueDirectEvidence.parse(
                venue="alpaca_paper",
                account_fingerprint=ALPACA_FP, symbol="SPY",
                evidence_type="positions", schema_version="v1",
                source="alpaca_paper_rest_v2", evidence_id="e",
                raw_bytes=ALPACA_POSITIONS)

    def test_duplicate_keys_refuse(self):
        raw = (b'{"observed_at":"' + OBSERVED.encode() +
               b'","positions":[],"positions":[{"asset_id":"x"}]}')
        with pytest.raises(VenueEvidenceError, match="duplicate key"):
            evidence("alpaca_paper", "positions", None, raw=raw)

    @pytest.mark.parametrize("raw,match", [
        (b'{"observed_at":"x","positions":[],"x":NaN}',
         "non-finite"),
        (b'{"observed_at":', "invalid JSON"),
        (b'\xff\xfe{"a":1}', "invalid encoding"),
    ])
    def test_malformed_bytes_refuse(self, raw, match):
        with pytest.raises(VenueEvidenceError, match=match):
            evidence("alpaca_paper", "positions", None, raw=raw)

    def test_unknown_and_missing_fields_refuse(self):
        extra = json.loads(json.dumps(ALPACA_POSITIONS))
        extra["positions"][0]["surprise"] = 1
        with pytest.raises(VenueEvidenceError, match="unknown fields"):
            evidence("alpaca_paper", "positions", extra)
        missing = json.loads(json.dumps(ALPACA_POSITIONS))
        del missing["positions"][0]["side"]
        with pytest.raises(VenueEvidenceError, match="missing fields"):
            evidence("alpaca_paper", "positions", missing)

    def test_contradictory_qty_and_side_refuse(self):
        bad = json.loads(json.dumps(ALPACA_POSITIONS))
        bad["positions"][0]["qty"] = "-10"
        bad["positions"][0]["side"] = "long"
        with pytest.raises(VenueEvidenceError,
                           match="redundant facts disagree"):
            evidence("alpaca_paper", "positions", bad)

    def test_a_naive_timestamp_refuses(self):
        bad = json.loads(json.dumps(ALPACA_POSITIONS))
        bad["observed_at"] = "2026-08-28T15:00:00"
        with pytest.raises(VenueEvidenceError,
                           match="timezone-aware UTC required"):
            evidence("alpaca_paper", "positions", bad)

    def test_a_non_bracket_alpaca_order_has_no_role(self):
        bad = json.loads(json.dumps(ALPACA_ORDERS))
        bad["orders"][0]["order_class"] = "simple"
        with pytest.raises(VenueEvidenceError,
                           match="does not state a role"):
            evidence("alpaca_paper", "open_orders", bad)

    def test_an_unknown_mt5_order_type_has_no_role(self):
        bad = json.loads(json.dumps(MT5_ORDERS))
        bad["orders"][0]["order_type"] = "ORDER_TYPE_BUY"
        with pytest.raises(VenueEvidenceError, match="not one of"):
            mt5_evidence("open_orders", bad)

    def test_impossible_bar_geometry_and_crossed_quotes_refuse(self):
        bad = json.loads(json.dumps(MT5_CLOCK))
        bad["last_closed_bar"]["low"] = 9.0
        with pytest.raises(VenueEvidenceError,
                           match="geometry is impossible"):
            mt5_evidence("market_clock", bad)
        crossed = json.loads(json.dumps(MT5_CLOCK))
        crossed["tick"]["ask"] = 1.0
        with pytest.raises(VenueEvidenceError, match="crossed"):
            mt5_evidence("market_clock", crossed)

    def test_a_payload_may_not_assert_its_own_provenance(self):
        bad = json.loads(json.dumps(ALPACA_POSITIONS))
        bad["venue_direct"] = True
        with pytest.raises(VenueEvidenceError,
                           match="may not assert venue_direct"):
            evidence("alpaca_paper", "positions", bad)


# =================================================================== #
# the POLICY owns freshness and sources                               #
# =================================================================== #

class TestPolicyOwnsAdmission:

    def test_stale_evidence_refuses(self):
        stale = json.loads(json.dumps(ALPACA_POSITIONS))
        stale["observed_at"] = (
            NOW - timedelta(seconds=600)).isoformat()
        item = evidence("alpaca_paper", "positions", stale)
        with pytest.raises(VenueEvidenceError, match="stale evidence"):
            item.verify(policy(), now=NOW)

    def test_future_evidence_refuses(self):
        ahead = json.loads(json.dumps(ALPACA_POSITIONS))
        ahead["observed_at"] = (
            NOW + timedelta(seconds=30)).isoformat()
        item = evidence("alpaca_paper", "positions", ahead)
        with pytest.raises(VenueEvidenceError, match="in the future"):
            item.verify(policy(), now=NOW)

    def test_a_source_outside_the_allowlist_refuses(self):
        item = evidence("alpaca_paper", "positions", ALPACA_POSITIONS,
                        source="some_other_feed")
        with pytest.raises(VenueEvidenceError, match="allowlist"):
            item.verify(policy(), now=NOW)

    def test_a_foreign_account_or_symbol_refuses(self):
        item = evidence("alpaca_paper", "positions", ALPACA_POSITIONS,
                        account="ffffffffffffffff")
        with pytest.raises(VenueEvidenceError,
                           match="foreign account"):
            item.verify(policy(), now=NOW)
        item = evidence("alpaca_paper", "positions", ALPACA_POSITIONS,
                        symbol="QQQ")
        with pytest.raises(VenueEvidenceError, match="is not the "
                           "policy's"):
            item.verify(policy(), now=NOW)

    def test_the_honest_path_verifies(self):
        item = evidence("alpaca_paper", "positions", ALPACA_POSITIONS)
        assert item.verify(policy(), now=NOW) is item
        assert item.venue_direct is True

    def test_the_policy_digest_covers_its_terms(self):
        base = policy().policy_digest
        assert policy(max_age_seconds=121.0).policy_digest != base
        assert policy(allowed_sources=("a", "b")).policy_digest != base
        assert policy(calendar_identity="other").policy_digest != base
        assert policy().policy_digest == base


# =================================================================== #
# simulator provenance is refused by name                             #
# =================================================================== #

class TestSimulatorProvenanceIsRefused:

    def test_the_policy_may_not_allowlist_simulator_evidence(self):
        with pytest.raises(VenuePolicyError,
                           match="not venue-direct"):
            policy(allowed_sources=("simulator_bar_local",))

    def test_evidence_from_simulator_provenance_refuses(self):
        with pytest.raises(VenueEvidenceError,
                           match="not venue-direct"):
            evidence("alpaca_paper", "positions", ALPACA_POSITIONS,
                     source="simulator_bar_local")

    @pytest.mark.parametrize("provenance", [
        {"evidence_provenance": "simulator_bar_local",
         "venue_direct": False},
        {"evidence_provenance": "simulator_bar_local",
         "venue_direct": True},
        {"source": "replay", "venue_direct": True},
        {"venue_direct": False},
        {},
    ])
    def test_require_venue_direct_refuses(self, provenance):
        with pytest.raises(VenueEvidenceError):
            require_venue_direct(provenance)

    def test_require_venue_direct_accepts_real_provenance(self):
        item = evidence("alpaca_paper", "positions", ALPACA_POSITIONS)
        require_venue_direct(item.provenance())


# =================================================================== #
# sealed parser identity                                              #
# =================================================================== #

class TestSealedParserIdentity:

    def test_every_parser_matches_its_sealed_identity(self):
        assert set(PARSERS) == set(SEALED_PARSER_IDENTITIES)
        for key in PARSERS:
            parser, identity = resolve_parser(key)
            assert identity == SEALED_PARSER_IDENTITIES[key]

    def test_the_registry_is_immutable(self):
        with pytest.raises(TypeError):
            PARSERS[("alpaca_paper", "positions", "v1")] = lambda p: {}

    def test_a_same_named_parser_hashes_differently(self):
        key = ("alpaca_paper", "positions", "v1")
        original = parser_identity(key, PARSERS[key])

        def _parse_alpaca_positions_v1(payload):
            return {"positions": (), "positions_total": 0}

        assert parser_identity(
            key, _parse_alpaca_positions_v1) != original

    def test_a_substituted_parser_is_refused(self, monkeypatch):
        import app.venue_direct_evidence as mod
        key = ("alpaca_paper", "positions", "v1")

        def forging(payload):
            return {"positions": (), "positions_total": 0,
                    "observed_at": OBSERVED}

        monkeypatch.setattr(mod, "PARSERS",
                            {**dict(mod.PARSERS), key: forging})
        with pytest.raises(VenueEvidenceError, match="substitution"):
            evidence("alpaca_paper", "positions", ALPACA_POSITIONS)

    def test_an_unknown_schema_refuses(self):
        with pytest.raises(VenueEvidenceError,
                           match="no allowlisted parser"):
            VenueDirectEvidence.parse(
                venue="alpaca_paper",
                account_fingerprint=ALPACA_FP, symbol="SPY",
                evidence_type="positions", schema_version="v99",
                source="alpaca_paper_rest_v2", evidence_id="e",
                raw_bytes=json.dumps(ALPACA_POSITIONS).encode())


# =================================================================== #
# C1: freshness comes from the BYTES                                  #
# =================================================================== #

class TestC1FreshnessIsBoundToThePayload:

    def test_a_stale_payload_cannot_be_rewrapped_as_fresh(self):
        """FROZEN COUNTEREXAMPLE. A 2020 payload inside a 2026
        envelope was accepted under a 120-second policy, because the
        age was computed from an envelope timestamp supplied outside
        the bytes and never bound to the signed internal one."""
        stale = json.loads(json.dumps(MT5_POSITIONS))
        stale["observed_at"] = "2020-01-01T00:00:00+00:00"
        item = mt5_evidence("positions", stale)
        assert item.observed_at.year == 2020, (
            "the envelope no longer supplies a timestamp at all")
        with pytest.raises(VenueEvidenceError, match="stale evidence"):
            item.verify(mt5_policy(), now=NOW)

    def test_a_future_payload_refuses(self):
        ahead = json.loads(json.dumps(MT5_POSITIONS))
        ahead["observed_at"] = "2030-01-01T00:00:00+00:00"
        with pytest.raises(VenueEvidenceError, match="in the future"):
            mt5_evidence("positions", ahead).verify(mt5_policy(),
                                                    now=NOW)

    def test_the_envelope_has_no_observed_at_parameter(self):
        import inspect
        signature = inspect.signature(VenueDirectEvidence.parse)
        assert "observed_at" not in signature.parameters
        assert "transport_observed_at" in signature.parameters

    def test_a_transport_stamp_is_a_check_not_a_substitute(self):
        item = mt5_evidence("positions", MT5_POSITIONS,
                            transport=OBSERVED)
        assert item.observed_at.isoformat() == \
            item.facts["observed_at"]
        with pytest.raises(VenueEvidenceError,
                           match="does not match the payload"):
            mt5_evidence("positions", MT5_POSITIONS,
                         transport="2026-08-28T14:00:00+00:00")


# =================================================================== #
# C2: identity is bound to the FACTS                                  #
# =================================================================== #

class TestC2IdentityIsBoundToTheFacts:

    def test_a_foreign_symbol_position_refuses(self):
        """FROZEN COUNTEREXAMPLE. A BTCUSD position was accepted as
        USDCAD exposure because verify only compared the ENVELOPE
        against the policy."""
        foreign = json.loads(json.dumps(MT5_POSITIONS))
        foreign["positions"][0]["symbol"] = "BTCUSD"
        item = mt5_evidence("positions", foreign)
        with pytest.raises(VenueEvidenceError,
                           match="foreign or mixed-symbol"):
            item.verify(mt5_policy(), now=NOW)

    def test_a_mixed_symbol_payload_is_refused_not_filtered(self):
        mixed = json.loads(json.dumps(MT5_POSITIONS))
        mixed["positions"].append(
            {**mixed["positions"][0], "ticket": "100002",
             "symbol": "EURUSD"})
        with pytest.raises(VenueEvidenceError,
                           match="never filtered"):
            mt5_evidence("positions", mixed).verify(mt5_policy(),
                                                    now=NOW)

    def test_a_foreign_symbol_order_refuses(self):
        foreign = json.loads(json.dumps(MT5_ORDERS))
        foreign["orders"][0]["symbol"] = "BTCUSD"
        with pytest.raises(VenueEvidenceError,
                           match="foreign or mixed-symbol"):
            mt5_evidence("open_orders", foreign).verify(mt5_policy(),
                                                        now=NOW)

    def test_a_foreign_symbol_clock_refuses(self):
        foreign = json.loads(json.dumps(MT5_CLOCK))
        foreign["symbol"] = "BTCUSD"
        with pytest.raises(VenueEvidenceError,
                           match="foreign or mixed-symbol"):
            mt5_evidence("market_clock", foreign).verify(mt5_policy(),
                                                         now=NOW)

    def test_the_internal_account_fingerprint_is_compared(self):
        """The envelope AGREES with the policy while the facts do
        not -- the case the envelope check alone cannot catch."""
        foreign = json.loads(json.dumps(MT5_ACCOUNT))
        foreign["account_fingerprint"] = "someotherfp9"
        item = mt5_evidence("account_session", foreign)
        assert item.account_fingerprint == "sanitizedfp01"
        assert item.facts["account_fingerprint"] == "someotherfp9"
        with pytest.raises(VenueEvidenceError,
                           match="the payload states account"):
            item.verify(mt5_policy(), now=NOW)

    def test_the_alpaca_derived_fingerprint_is_compared(self):
        foreign = json.loads(json.dumps(ALPACA_ACCOUNT))
        foreign["account"]["id"] = "a-completely-different-account"
        item = evidence("alpaca_paper", "account_session", foreign)
        assert item.account_fingerprint == ALPACA_FP
        assert item.facts["account_fingerprint"] != ALPACA_FP
        with pytest.raises(VenueEvidenceError,
                           match="the payload states account"):
            item.verify(policy(), now=NOW)

    def test_duplicate_identities_refuse(self):
        dup = json.loads(json.dumps(MT5_POSITIONS))
        dup["positions"].append(dict(dup["positions"][0]))
        with pytest.raises(VenueEvidenceError,
                           match="appears twice"):
            mt5_evidence("positions", dup)
        dup_orders = json.loads(json.dumps(MT5_ORDERS))
        dup_orders["orders"].append(dict(dup_orders["orders"][0]))
        with pytest.raises(VenueEvidenceError,
                           match="appears twice"):
            mt5_evidence("open_orders", dup_orders)

    def test_an_alpaca_leg_reusing_the_parent_identity_refuses(self):
        clash = json.loads(json.dumps(ALPACA_ORDERS))
        clash["orders"][0]["legs"][0]["id"] = "parent-order-id"
        with pytest.raises(VenueEvidenceError,
                           match="appears twice"):
            evidence("alpaca_paper", "open_orders", clash)

    def test_the_honest_path_still_binds(self):
        for kind, payload in (("positions", MT5_POSITIONS),
                              ("open_orders", MT5_ORDERS),
                              ("market_clock", MT5_CLOCK),
                              ("account_session", MT5_ACCOUNT)):
            mt5_evidence(kind, payload).verify(mt5_policy(), now=NOW)


# =================================================================== #
# C3: per-venue numeric grammars                                      #
# =================================================================== #

class TestC3PerVenueNumericTypes:

    @pytest.mark.parametrize("bad", [
        True, False, "0.1", "  0.1  ", "1e-1", "NaN", "Infinity",
        "0x10", None, [0.1]])
    def test_mt5_refuses_anything_that_is_not_a_json_number(self,
                                                            bad):
        """FROZEN COUNTEREXAMPLE: volume=true was coerced to a 1.0-lot
        position because float() ran before the strict check."""
        payload = json.loads(json.dumps(MT5_POSITIONS))
        payload["positions"][0]["volume"] = bad
        with pytest.raises(VenueEvidenceError,
                           match="JSON number is required"):
            mt5_evidence("positions", payload)

    def test_mt5_accepts_real_json_numbers(self):
        for good in (0.1, 1, 2.5):
            payload = json.loads(json.dumps(MT5_POSITIONS))
            payload["positions"][0]["volume"] = good
            facts = mt5_evidence("positions", payload).facts
            assert facts["positions"][0]["signed_quantity"] == \
                pytest.approx(float(good))

    @pytest.mark.parametrize("bad", [
        True, 10, 10.0, " 10", "10 ", "1e1", "NaN", "Infinity",
        "+10", "010", "", None])
    def test_alpaca_refuses_anything_outside_the_decimal_grammar(
            self, bad):
        payload = json.loads(json.dumps(ALPACA_POSITIONS))
        payload["positions"][0]["qty"] = bad
        with pytest.raises(VenueEvidenceError):
            evidence("alpaca_paper", "positions", payload)

    @pytest.mark.parametrize("good,expected", [
        ("10", 10.0), ("10.5", 10.5), ("-3", -3.0), ("0", 0.0)])
    def test_alpaca_accepts_its_documented_decimal_strings(self, good,
                                                           expected):
        payload = json.loads(json.dumps(ALPACA_POSITIONS))
        payload["positions"][0]["qty"] = good
        payload["positions"][0]["side"] = (
            "long" if expected > 0 else "short")
        if expected == 0.0:
            with pytest.raises(VenueEvidenceError):
                evidence("alpaca_paper", "positions", payload)
            return
        facts = evidence("alpaca_paper", "positions", payload).facts
        assert facts["positions"][0]["signed_quantity"] == \
            pytest.approx(expected if expected > 0 else expected)

    def test_no_parser_calls_float_on_untrusted_input(self):
        import inspect
        import app.venue_direct_evidence as mod
        for key, parser in mod.PARSERS.items():
            source = inspect.getsource(parser)
            assert "float(row[" not in source, key
            assert "float(payload[" not in source, key
            assert "float(account[" not in source, key


# =================================================================== #
# WP3-C9: the role comes from the venue's DECLARED intent             #
# =================================================================== #

class TestC9RoleFromDeclaredIntent:

    def test_the_recorded_protective_child_is_not_an_entry(self):
        """FROZEN COUNTEREXAMPLE. The owner-authorized read-only
        capture observed a top-level bracket order with legs null and
        position_intent buy_to_close, standing against a SHORT SPY
        position. The old parser called it an ENTRY, and geometry
        could not have rescued it either: a BUY while SHORT looks
        exactly like a reversal."""
        facts = evidence("alpaca_paper", "open_orders",
                         ALPACA_PROTECTIVE_CHILD).facts
        assert [o["role"] for o in facts["orders"]] == [
            "protective_take_profit"]
        assert facts["entry_orders"] == 0
        assert facts["protective_orders"] == 1
        assert facts["orders"][0]["position_intent"] == "buy_to_close"

    def test_the_intent_is_required_by_the_contract(self):
        payload = json.loads(json.dumps(ALPACA_PROTECTIVE_CHILD))
        del payload["orders"][0]["position_intent"]
        with pytest.raises(VenueEvidenceError,
                           match="missing fields"):
            evidence("alpaca_paper", "open_orders", payload)

    @pytest.mark.parametrize("intent", [
        "buy", "close", "buy_to_hold", "", None, True, 1,
        "BUY_TO_CLOSE"])
    def test_an_unknown_intent_refuses(self, intent):
        payload = json.loads(json.dumps(ALPACA_PROTECTIVE_CHILD))
        payload["orders"][0]["position_intent"] = intent
        with pytest.raises(VenueEvidenceError, match="not one of"):
            evidence("alpaca_paper", "open_orders", payload)

    @pytest.mark.parametrize("order_type,role", [
        ("limit", "protective_take_profit"),
        ("stop", "protective_stop"),
        ("stop_limit", "protective_stop")])
    def test_a_closing_child_is_typed_by_its_own_type(self,
                                                      order_type,
                                                      role):
        payload = json.loads(json.dumps(ALPACA_PROTECTIVE_CHILD))
        payload["orders"][0]["type"] = order_type
        facts = evidence("alpaca_paper", "open_orders", payload).facts
        assert facts["orders"][0]["role"] == role
        assert facts["entry_orders"] == 0

    @pytest.mark.parametrize("order_type", [
        "market", "trailing_stop", "oco"])
    def test_a_closing_child_of_an_untyped_kind_refuses(self,
                                                        order_type):
        payload = json.loads(json.dumps(ALPACA_PROTECTIVE_CHILD))
        payload["orders"][0]["type"] = order_type
        with pytest.raises(VenueEvidenceError,
                           match="states no protective role"):
            evidence("alpaca_paper", "open_orders", payload)

    @pytest.mark.parametrize("intent,side", [
        ("buy_to_close", "sell"), ("sell_to_close", "buy"),
        ("buy_to_open", "sell"), ("sell_to_open", "buy")])
    def test_a_side_contradicting_the_intent_refuses(self, intent,
                                                     side):
        payload = json.loads(json.dumps(ALPACA_PROTECTIVE_CHILD))
        payload["orders"][0]["position_intent"] = intent
        payload["orders"][0]["side"] = side
        with pytest.raises(VenueEvidenceError,
                           match="contradicts position_intent"):
            evidence("alpaca_paper", "open_orders", payload)

    def test_a_short_side_closing_child_is_a_protective_stop(self):
        """The mirror shape: a SELL closing a LONG position."""
        payload = json.loads(json.dumps(ALPACA_PROTECTIVE_CHILD))
        payload["orders"][0]["position_intent"] = "sell_to_close"
        payload["orders"][0]["side"] = "sell"
        payload["orders"][0]["type"] = "stop"
        facts = evidence("alpaca_paper", "open_orders", payload).facts
        assert facts["orders"][0]["role"] == "protective_stop"

    def test_an_unfilled_parent_with_legs_still_parses(self):
        facts = evidence("alpaca_paper", "open_orders",
                         ALPACA_ORDERS).facts
        assert [o["role"] for o in facts["orders"]] == [
            "entry", "protective_stop", "protective_take_profit"]
        assert facts["entry_orders"] == 1
        assert facts["protective_orders"] == 2
        assert facts["orders"][0]["position_intent"] == "buy_to_open"
        assert facts["orders"][1]["position_intent"] is None

    def test_null_is_not_normalised_to_an_empty_list(self):
        """The misclassification was produced by exactly this
        normalisation, so null and a list must reach the contract as
        the venue sent them."""
        import inspect
        import app.venue_direct_evidence as mod
        source = inspect.getsource(mod._parse_alpaca_open_orders_v1)
        assert "legs or []" not in source
        assert "legs = []" not in source
        payload = json.loads(json.dumps(ALPACA_PROTECTIVE_CHILD))
        payload["orders"][0]["legs"] = []
        empty = evidence("alpaca_paper", "open_orders", payload).facts
        payload["orders"][0]["legs"] = None
        null = evidence("alpaca_paper", "open_orders", payload).facts
        assert empty["orders"][0]["role"] == null["orders"][0]["role"]

    @pytest.mark.parametrize("legs", [{}, "legs", 0, 1.5])
    def test_legs_of_a_wrong_kind_refuse(self, legs):
        payload = json.loads(json.dumps(ALPACA_PROTECTIVE_CHILD))
        payload["orders"][0]["legs"] = legs
        with pytest.raises(VenueEvidenceError,
                           match="must be null or a list"):
            evidence("alpaca_paper", "open_orders", payload)

    def test_a_closing_order_carrying_legs_refuses(self):
        payload = json.loads(json.dumps(ALPACA_ORDERS))
        payload["orders"][0]["position_intent"] = "buy_to_close"
        with pytest.raises(VenueEvidenceError,
                           match="has no children of its own"):
            evidence("alpaca_paper", "open_orders", payload)

    def test_an_opening_order_without_legs_refuses(self):
        for legs in (None, []):
            payload = json.loads(json.dumps(ALPACA_PROTECTIVE_CHILD))
            payload["orders"][0]["position_intent"] = "buy_to_open"
            payload["orders"][0]["legs"] = legs
            with pytest.raises(VenueEvidenceError,
                               match="opening bracket with no legs"):
                evidence("alpaca_paper", "open_orders", payload)

    def test_duplicate_identities_still_refuse_across_shapes(self):
        payload = json.loads(json.dumps(ALPACA_PROTECTIVE_CHILD))
        payload["orders"].append(dict(payload["orders"][0]))
        with pytest.raises(VenueEvidenceError, match="appears twice"):
            evidence("alpaca_paper", "open_orders", payload)

    def test_a_mixed_book_of_both_shapes_parses(self):
        payload = json.loads(json.dumps(ALPACA_ORDERS))
        payload["orders"].append(
            json.loads(json.dumps(
                ALPACA_PROTECTIVE_CHILD))["orders"][0])
        facts = evidence("alpaca_paper", "open_orders", payload).facts
        assert facts["entry_orders"] == 1
        assert facts["protective_orders"] == 3
