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
        "type": "market",
        "legs": [
            {"id": "stop-leg-id", "side": "sell", "type": "stop",
             "qty": "10", "status": "held"},
            {"id": "limit-leg-id", "side": "sell", "type": "limit",
             "qty": "10", "status": "held"}],
    }],
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
           account="0c7d3b4e5f6a7b8c", **kw):
    base = dict(venue=venue, account_fingerprint=account,
                symbol=symbol,
                allowed_sources=("alpaca_paper_rest_v2",),
                max_age_seconds=120.0,
                calendar_identity="cal-venue-v1")
    base.update(kw)
    return VenueEvidencePolicy.build(**base)


def evidence(venue, kind, payload, *, symbol="SPY",
             account="0c7d3b4e5f6a7b8c",
             source="alpaca_paper_rest_v2", observed=OBSERVED,
             raw=None):
    return VenueDirectEvidence.parse(
        venue=venue, account_fingerprint=account, symbol=symbol,
        evidence_type=kind, schema_version="v1", source=source,
        evidence_id=f"ev-{kind}", observed_at=observed,
        raw_bytes=raw if raw is not None
        else json.dumps(payload).encode())


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
                account_fingerprint="0c7d3b4e5f6a7b8c", symbol="SPY",
                evidence_type="positions", schema_version="v1",
                source="alpaca_paper_rest_v2", evidence_id="e",
                observed_at=OBSERVED, raw_bytes=ALPACA_POSITIONS)

    def test_duplicate_keys_refuse(self):
        raw = (b'{"observed_at":"' + OBSERVED.encode() +
               b'","positions":[],"positions":[{"asset_id":"x"}]}')
        with pytest.raises(VenueEvidenceError, match="duplicate key"):
            evidence("alpaca_paper", "positions", None, raw=raw)

    @pytest.mark.parametrize("raw,match", [
        (b'{"observed_at":"x","positions":NaN}', "non-finite"),
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
        old = (NOW - timedelta(seconds=600)).isoformat()
        stale = json.loads(json.dumps(ALPACA_POSITIONS))
        stale["observed_at"] = old
        item = evidence("alpaca_paper", "positions", stale,
                        observed=old)
        with pytest.raises(VenueEvidenceError, match="stale evidence"):
            item.verify(policy(), now=NOW)

    def test_future_evidence_refuses(self):
        ahead = (NOW + timedelta(seconds=30)).isoformat()
        item = evidence("alpaca_paper", "positions", ALPACA_POSITIONS,
                        observed=ahead)
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
                account_fingerprint="0c7d3b4e5f6a7b8c", symbol="SPY",
                evidence_type="positions", schema_version="v99",
                source="alpaca_paper_rest_v2", evidence_id="e",
                observed_at=OBSERVED,
                raw_bytes=json.dumps(ALPACA_POSITIONS).encode())
