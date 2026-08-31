"""F7 battery: the session-evidence collector schema is read-only by
construction, validates adversarially, fails closed on identity and
freshness, and its sanitized export separates collection gaps from
authority. Activation stays COORDINATED_WINDOW_REQUIRED."""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

import app.mt5_session_evidence as mod
from app.mt5_session_evidence import (ACTIVATION_STATUS,
                                      SESSION_EVIDENCE_SCHEMA,
                                      SessionEvidencePayload,
                                      sanitized_export,
                                      verify_freshness_and_identity)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
FP_A = "a" * 16
FP_S = "b" * 16


def payload(**kw):
    base = {
        "schema": SESSION_EVIDENCE_SCHEMA,
        "account_fingerprint": FP_A,
        "server_fingerprint": FP_S,
        "symbol": "ETHUSD",
        "ea_version": "wp3-ea-1.4",
        "terminal_build": 4620,
        "server_gmt_offset_minutes": 120,
        "observed_at": NOW,
        "quote_sessions": [
            {"day_of_week": 0, "from_minute": 0,
             "to_minute": 1440}],
        "trade_sessions": [
            {"day_of_week": 0, "from_minute": 5,
             "to_minute": 1435}],
        "acquisition_source":
            "SymbolInfoSessionTrade/SymbolInfoSessionQuote",
    }
    base.update(kw)
    return SessionEvidencePayload(**base)


class TestNoWriteByConstruction:

    def test_no_trading_or_write_surface_exists(self):
        source = inspect.getsource(mod)
        for forbidden in ("OrderSend", "PositionClose", "TradeReq",
                          "requests.", "urllib", "socket",
                          "subprocess", "os.system", "connect(",
                          "post(", "put(", "delete("):
            assert forbidden not in source, forbidden

    def test_activation_is_a_coordinated_window(self):
        assert ACTIVATION_STATUS == "COORDINATED_WINDOW_REQUIRED"
        doc = mod.__doc__
        assert "never worth risking the position" in doc.replace(
            "\n", " ").replace("  ", " ") or \
            "risking the position" in doc
        assert "runbook" in doc.lower()


class TestSchemaValidation:

    def test_a_valid_payload_parses(self):
        parsed = payload()
        assert parsed.symbol == "ETHUSD"

    def test_wrong_schema_refuses(self):
        with pytest.raises(Exception, match="schema"):
            payload(schema="lts.other.v1")

    def test_unknown_fields_refuse(self):
        with pytest.raises(Exception):
            payload(bonus_field=1)

    def test_foreign_acquisition_source_refuses(self):
        with pytest.raises(Exception, match="terminal API"):
            payload(acquisition_source="historical_gap_inference")

    def test_empty_sessions_refuse(self):
        with pytest.raises(Exception, match="no sessions"):
            payload(trade_sessions=[])

    def test_overlapping_intervals_refuse(self):
        with pytest.raises(Exception, match="overlapping"):
            payload(quote_sessions=[
                {"day_of_week": 1, "from_minute": 0,
                 "to_minute": 600},
                {"day_of_week": 1, "from_minute": 500,
                 "to_minute": 900}])

    def test_bad_day_and_zero_length_refuse(self):
        with pytest.raises(Exception):
            payload(quote_sessions=[
                {"day_of_week": 7, "from_minute": 0,
                 "to_minute": 100}])
        with pytest.raises(Exception, match="empty session"):
            payload(quote_sessions=[
                {"day_of_week": 1, "from_minute": 100,
                 "to_minute": 100}])

    def test_naive_timestamp_refuses(self):
        with pytest.raises(Exception):
            payload(observed_at=datetime(2026, 8, 31, 12, 0))


class TestIdentityAndFreshness:

    def _verify(self, p, **kw):
        base = {"expected_account_fingerprint": FP_A,
                "expected_server_fingerprint": FP_S,
                "expected_symbol": "ETHUSD", "now": NOW}
        base.update(kw)
        verify_freshness_and_identity(p, **base)

    def test_bound_identity_passes(self):
        self._verify(payload())

    def test_foreign_account_refuses(self):
        with pytest.raises(ValueError, match="foreign account"):
            self._verify(payload(),
                         expected_account_fingerprint="c" * 16)

    def test_foreign_server_refuses(self):
        with pytest.raises(ValueError, match="foreign server"):
            self._verify(payload(),
                         expected_server_fingerprint="c" * 16)

    def test_wrong_symbol_refuses(self):
        with pytest.raises(ValueError, match="not the bound"):
            self._verify(payload(), expected_symbol="EURUSD")

    def test_stale_evidence_refuses(self):
        with pytest.raises(ValueError, match="stale"):
            self._verify(payload(),
                         now=NOW + timedelta(hours=2))

    def test_future_evidence_refuses(self):
        with pytest.raises(ValueError, match="future"):
            self._verify(payload(),
                         now=NOW - timedelta(minutes=5))


class TestSanitizedExport:

    def test_empty_export_declares_unavailable(self):
        export = sanitized_export([])
        assert export["status"] == \
            "VENUE_SESSION_HISTORY_UNAVAILABLE"
        assert export["activation"] == ACTIVATION_STATUS
        assert "fabricated" in export["authority_note"]

    def test_gaps_are_separated_from_authority(self):
        early = payload()
        late = payload(observed_at=NOW + timedelta(hours=3))
        export = sanitized_export([late, early])
        assert export["status"] == "COLLECTED"
        assert len(export["gaps"]) == 1
        assert "never venue-closure authority" in \
            export["gaps"][0]["meaning"]
        assert export["export_sha256"]

    def test_export_carries_no_private_topology(self):
        import json as json_mod
        export = sanitized_export([payload()])
        blob = json_mod.dumps(export)
        assert "/home/" not in blob
        assert "harveybc" not in blob
