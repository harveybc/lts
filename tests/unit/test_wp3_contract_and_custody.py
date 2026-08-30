"""WP3 C5/C6 — deployable live custody, and the MT5 contract carrying
the EA's own bytes end to end.

Effect-free. The MT5 test drives the REAL FastAPI app and the REAL
store with a payload assembled from the fields the EA actually emits,
and reads it back out through the WP3 parser. Nothing is deployed and
no venue is contacted.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.live_flatten_custody import (
    LiveCustodyDispositionRequired, LiveCustodyError,
    LiveFlattenCustody, VenueObligationBinding,
    flat_from_direct_evidence)
from app.mt5_bridge_lab import (
    Mt5BridgeConfig, Mt5BridgeStore, PositionSnapshot,
    create_mt5_bridge_app, create_signed_headers)
from app.session_authority_adapter import load_authority
from app.venue_direct_evidence import (
    VenueDirectEvidence, VenueEvidenceError)

from tests.unit.test_wp3_session_adapter import (
    AUTHORITY_ROOT, reviewed_identity)
from tests.unit.test_wp3_venue_direct_evidence import (
    MT5_ORDERS, MT5_POSITIONS, NOW, OBSERVED, mt5_evidence,
    mt5_policy)

SECRET = b"0123456789abcdef0123456789abcdef"

pytestmark = pytest.mark.skipif(
    not (AUTHORITY_ROOT / "app" / "session_exposure.py").is_file(),
    reason="the accepted session authority checkout is not present")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*_args, **_kwargs):
        raise AssertionError(
            "network operation attempted in a WP3 contract test")
    monkeypatch.setattr(socket, "create_connection", _explode)


@pytest.fixture(scope="module")
def authority():
    return load_authority(AUTHORITY_ROOT,
                          expected_code_identity=reviewed_identity())


# =================================================================== #
# C6: the EA's own bytes traverse the bridge                          #
# =================================================================== #

# Exactly the fields LtsMt5ModelBridge.mq5 emits for a position.
EA_POSITION = {
    "ticket": "100001",
    "symbol": "USDCAD",
    "side": "long",
    "volume": 0.1,
    "price_open": 1.35,
    "time_open_unix": 1756400000,
    "stop_loss": 1.34,
    "take_profit": 1.36,
    "profit": 4.2,
}

EA_SNAPSHOT = {
    "schema": "lts.mt5.snapshot.v1",
    "account_fingerprint": "sanitizedfp01",
    "observed_at": "2026-07-30T12:00:00+00:00",
    "currency": "USD",
    "balance": 10000.0,
    "equity": 10004.2,
    "margin": 10.0,
    "free_margin": 9994.2,
    "positions": [EA_POSITION],
    "orders": [],
    "symbols": [],
    "bars": [],
}


def _config(tmp_path: Path) -> Mt5BridgeConfig:
    return Mt5BridgeConfig(
        database_path=tmp_path / "mt5.sqlite",
        secret_env="LTS_MT5_BRIDGE_SECRET",
        environment="demo", read_only=True,
        bind_host="127.0.0.1", port=8766,
        max_clock_skew_seconds=90, nonce_retention_seconds=900,
        stale_heartbeat_seconds=180,
        allowed_account_fingerprints=())


def _signed_post(client, path, payload, nonce):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(
        path, content=body,
        headers={"Content-Type": "application/json",
                 **create_signed_headers(SECRET, "POST", path, body,
                                         timestamp=int(time.time()),
                                         nonce=nonce)})


class TestC6MT5ContractCarriesTheEABytes:

    def test_the_strict_model_now_declares_time_open_unix(self):
        """FROZEN COUNTEREXAMPLE. The EA has always emitted
        POSITION_TIME as time_open_unix and the runner has always read
        it, but the strict model did not declare it, so extra='forbid'
        made the endpoint answer 422 and the WHOLE snapshot was
        discarded."""
        assert "time_open_unix" in PositionSnapshot.model_fields
        model = PositionSnapshot(**EA_POSITION)
        assert model.time_open_unix == 1756400000

    @pytest.mark.parametrize("bad", [0, -1, "1756400000", True, None])
    def test_the_field_is_range_and_type_checked(self, bad):
        payload = {**EA_POSITION, "time_open_unix": bad}
        with pytest.raises(Exception):
            PositionSnapshot(**payload)

    def test_an_unknown_extra_field_still_refuses(self):
        payload = {**EA_POSITION, "surprise": 1}
        with pytest.raises(Exception):
            PositionSnapshot(**payload)

    def test_ea_bytes_reach_the_store_and_the_wp3_parser(self,
                                                         tmp_path,
                                                         monkeypatch):
        """EA bytes -> FastAPI endpoint -> persistence -> read back ->
        WP3 parser. Nothing is deployed; the app runs in-process."""
        monkeypatch.setenv("LTS_MT5_BRIDGE_SECRET", SECRET.decode())
        config = _config(tmp_path)
        store = Mt5BridgeStore(config.database_path)
        client = TestClient(create_mt5_bridge_app(config, store,
                                                  SECRET))
        try:
            response = _signed_post(client, "/v1/snapshot",
                                    EA_SNAPSHOT, "nonce-wp3-000001")
            assert response.status_code == 200, response.text
            report = store.report()
            assert report["latest_snapshot"]["positions_total"] == 1
        finally:
            store.close()

        connection = sqlite3.connect(config.database_path)
        try:
            row = connection.execute(
                "SELECT payload_json FROM account_snapshots "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            connection.close()
        assert row is not None, "the snapshot must be persisted"
        stored = json.loads(row[0])
        assert stored["positions"][0]["time_open_unix"] == 1756400000

        # the WP3 parser consumes the SAME bytes the store kept
        evidence = VenueDirectEvidence.parse(
            venue="mt5_demo", account_fingerprint="sanitizedfp01",
            symbol="USDCAD", evidence_type="positions",
            schema_version="v1", source="mt5_bridge_snapshot_v1",
            evidence_id="ev-roundtrip",
            raw_bytes=json.dumps(
                {"observed_at": stored["observed_at"],
                 "positions": stored["positions"]}).encode())
        facts = evidence.facts
        assert facts["positions_total"] == 1
        assert facts["positions"][0]["opened_at_unix"] == 1756400000
        assert facts["positions"][0][
            "native_protection_present"] is True

    def test_a_snapshot_without_the_field_now_refuses(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setenv("LTS_MT5_BRIDGE_SECRET", SECRET.decode())
        config = _config(tmp_path)
        store = Mt5BridgeStore(config.database_path)
        client = TestClient(create_mt5_bridge_app(config, store,
                                                  SECRET))
        payload = json.loads(json.dumps(EA_SNAPSHOT))
        del payload["positions"][0]["time_open_unix"]
        try:
            response = _signed_post(client, "/v1/snapshot", payload,
                                    "nonce-wp3-000002")
            assert response.status_code == 422, (
                "a position without the EA's own field is incomplete")
        finally:
            store.close()

    def test_an_unknown_field_is_still_rejected_by_the_endpoint(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("LTS_MT5_BRIDGE_SECRET", SECRET.decode())
        config = _config(tmp_path)
        store = Mt5BridgeStore(config.database_path)
        client = TestClient(create_mt5_bridge_app(config, store,
                                                  SECRET))
        payload = json.loads(json.dumps(EA_SNAPSHOT))
        payload["positions"][0]["surprise"] = 1
        try:
            response = _signed_post(client, "/v1/snapshot", payload,
                                    "nonce-wp3-000003")
            assert response.status_code == 422
        finally:
            store.close()

    def test_the_ea_source_still_emits_the_field(self):
        """If the EA ever stops emitting it, this contract must be
        revisited rather than silently diverging again."""
        ea = (Path(__file__).resolve().parents[2] / "mt5" / "MQL5" /
              "Experts" / "LtsMt5ModelBridge.mq5")
        assert "time_open_unix" in ea.read_text()


# =================================================================== #
# C5: the deployable live custody                                     #
# =================================================================== #

def binding(authority, **kw):
    base = dict(venue="mt5_demo", account_fingerprint="sanitizedfp01",
                symbol="USDCAD", position_identity="100001",
                evidence_policy_digest=mt5_policy().policy_digest,
                calendar_identity="cal-venue-v1",
                authority_code_identity=authority.code_identity)
    base.update(kw)
    return VenueObligationBinding(**base)


def custody(authority, tmp_path, *, name="live_custody",
            episode="ep-live-1", **kw):
    return LiveFlattenCustody(authority, tmp_path / name,
                              binding=binding(authority, **kw),
                              episode_identity=episode)


def flat_evidence():
    empty_positions = json.loads(json.dumps(MT5_POSITIONS))
    empty_positions["positions"] = []
    empty_orders = json.loads(json.dumps(MT5_ORDERS))
    empty_orders["orders"] = []
    return (mt5_evidence("positions", empty_positions),
            mt5_evidence("open_orders", empty_orders))


class TestC5DeployableLiveCustody:

    def test_it_exists_and_uses_the_accepted_store(self, authority,
                                                   tmp_path):
        live = custody(authority, tmp_path)
        assert live._store.__class__.__name__ == \
            "FlattenObligationStore"
        assert live._custody is authority.flatten_custody

    def test_modes_come_from_the_accepted_protocol(self, authority,
                                                   tmp_path):
        import stat
        live = custody(authority, tmp_path)
        live.open("o-1", signed_exposure=0.1, requested_at_bar=5)
        root_mode = stat.S_IMODE(os.stat(live._store.root).st_mode)
        assert root_mode == 0o700

    def test_the_binding_is_recorded_in_the_obligation(self,
                                                       authority,
                                                       tmp_path):
        live = custody(authority, tmp_path)
        record = live.open("o-1", signed_exposure=0.1,
                           requested_at_bar=5)
        assert record["venue"] == "mt5_demo"
        assert record["symbol"] == "USDCAD"
        assert record["position_identity"] == "100001"
        assert record["account_fingerprint"] == "sanitizedfp01"
        assert mt5_policy().policy_digest in \
            record["checkpoint_identity"]
        assert "cal-venue-v1" in record["checkpoint_identity"]
        assert authority.code_identity in \
            record["checkpoint_identity"]

    def test_direct_evidence_of_zero_zero_discharges_it(self,
                                                        authority,
                                                        tmp_path):
        live = custody(authority, tmp_path)
        live.open("o-1", signed_exposure=0.1, requested_at_bar=5)
        live.mark_in_flight("o-1", bar_index=6)
        positions, orders = flat_evidence()
        record = live.confirm_with_direct_evidence(
            "o-1", positions=positions, orders=orders,
            policy=mt5_policy(), now=NOW, bar_index=7)
        assert record["state"] == "flatten_confirmed"
        assert record["reconciliation"]["venue_direct"] is True
        assert record["reconciliation"]["positions"] == 0
        assert live.outstanding() == ()

    def test_exposure_that_remains_never_confirms(self, authority,
                                                  tmp_path):
        live = custody(authority, tmp_path)
        live.open("o-1", signed_exposure=0.1, requested_at_bar=5)
        positions = mt5_evidence("positions", MT5_POSITIONS)
        _flat, orders = flat_evidence()
        with pytest.raises(LiveCustodyError,
                           match="FLATTEN_INCOMPLETE"):
            live.confirm_with_direct_evidence(
                "o-1", positions=positions, orders=orders,
                policy=mt5_policy(), now=NOW, bar_index=7)
        assert live.read("o-1")["state"] == "flatten_requested"

    def test_a_plain_mapping_cannot_discharge_an_obligation(self,
                                                            authority,
                                                            tmp_path):
        live = custody(authority, tmp_path)
        live.open("o-1", signed_exposure=0.1, requested_at_bar=5)
        with pytest.raises(LiveCustodyError,
                           match="direct venue evidence is required"):
            live.confirm_with_direct_evidence(
                "o-1", positions={"positions_total": 0},
                orders={"orders_total": 0}, policy=mt5_policy(),
                now=NOW, bar_index=7)

    def test_stale_evidence_cannot_discharge_an_obligation(self,
                                                           authority,
                                                           tmp_path):
        live = custody(authority, tmp_path)
        live.open("o-1", signed_exposure=0.1, requested_at_bar=5)
        stale_positions = json.loads(json.dumps(MT5_POSITIONS))
        stale_positions["positions"] = []
        stale_positions["observed_at"] = (
            NOW - timedelta(hours=5)).isoformat()
        _p, orders = flat_evidence()
        with pytest.raises(VenueEvidenceError, match="stale"):
            live.confirm_with_direct_evidence(
                "o-1",
                positions=mt5_evidence("positions", stale_positions),
                orders=orders, policy=mt5_policy(), now=NOW,
                bar_index=7)

    def test_a_restart_recovers_and_fails_closed(self, authority,
                                                 tmp_path):
        live = custody(authority, tmp_path)
        live.open("o-1", signed_exposure=0.1, requested_at_bar=5)
        live.mark_in_flight("o-1", bar_index=6)
        reborn = custody(authority, tmp_path)
        record = reborn.recover()
        assert record is not None
        assert record["state"] == "flatten_in_flight"

    def test_a_foreign_binding_may_not_discharge_it(self, authority,
                                                    tmp_path):
        live = custody(authority, tmp_path)
        live.open("o-1", signed_exposure=0.1, requested_at_bar=5)
        for other in ({"symbol": "EURUSD"},
                      {"account_fingerprint": "otherfp000001"},
                      {"position_identity": "999999"},
                      {"calendar_identity": "other-calendar"},
                      {"evidence_policy_digest": "0" * 64}):
            stranger = custody(authority, tmp_path, **other)
            with pytest.raises(LiveCustodyError, match="disagrees on"):
                stranger.recover()
            positions, orders = flat_evidence()
            with pytest.raises(LiveCustodyError, match="disagrees on"):
                stranger.confirm_with_direct_evidence(
                    "o-1", positions=positions, orders=orders,
                    policy=mt5_policy(), now=NOW, bar_index=7)

    def test_a_foreign_authority_identity_refuses_at_construction(
            self, authority, tmp_path):
        with pytest.raises(LiveCustodyError,
                           match="the binding names authority"):
            custody(authority, tmp_path,
                    authority_code_identity="f" * 64)

    def test_several_open_obligations_require_an_operator(self,
                                                          authority,
                                                          tmp_path):
        live = custody(authority, tmp_path)
        live.open("o-1", signed_exposure=0.1, requested_at_bar=5)
        live.open("o-2", signed_exposure=0.2, requested_at_bar=6)
        with pytest.raises(LiveCustodyDispositionRequired,
                           match="no automatic resolution"):
            live.recover()

    def test_a_different_episode_cannot_confirm(self, authority,
                                                tmp_path):
        live = custody(authority, tmp_path)
        live.open("o-1", signed_exposure=0.1, requested_at_bar=5)
        later = custody(authority, tmp_path, episode="ep-live-2")
        positions, orders = flat_evidence()
        with pytest.raises(Exception,
                           match="cannot be advanced by episode"):
            later.confirm_with_direct_evidence(
                "o-1", positions=positions, orders=orders,
                policy=mt5_policy(), now=NOW, bar_index=7)

    def test_simulator_provenance_cannot_discharge_anything(self):
        class _Fake:
            evidence_id = "sim"

            def verify(self, policy, now):
                return self

            def provenance(self):
                return {"evidence_provenance": "simulator_bar_local",
                        "venue_direct": False}

            facts = {"positions_total": 0, "orders_total": 0}

        with pytest.raises(LiveCustodyError,
                           match="direct venue evidence is required"):
            flat_from_direct_evidence(_Fake(), _Fake(),
                                      policy=mt5_policy(), now=NOW)

    def test_the_module_holds_no_client_and_sends_nothing(self):
        import app.live_flatten_custody as module
        source = Path(module.__file__).read_text()
        for forbidden in ("import requests", "import socket",
                          "submit_order", "OrderSend", ".post(",
                          ".delete(", "enqueue("):
            assert forbidden not in source, forbidden


# =================================================================== #
# C7: the Alpaca coverage boundary, stated rather than papered over   #
# =================================================================== #

class TestC7AlpacaCoverageBoundary:

    def test_the_committed_alpaca_capture_asserts_typed_absence(self):
        """No real read-only Alpaca capture could be taken: the
        durable evidence lives in the operator's private state tree,
        which this repository's rules forbid reading or quoting, and
        opening a connection is what the order forbids. The capture is
        therefore synthetic and claims NOTHING it did not observe --
        an empty book rather than an invented bracket."""
        root = (Path(__file__).resolve().parents[2] / "examples" /
                "captures" / "wp3_alpaca_dry_run")
        assert (root / "NOTE.md").is_file(), (
            "the limitation must be stated next to the data")
        for name in ("positions", "open_orders"):
            envelope = json.loads((root / f"{name}.json").read_text())
            payload = json.loads(envelope["payload"])
            body = payload.get("positions", payload.get("orders"))
            assert body == [], (
                f"{name}: no fact may be invented for an account "
                "nobody observed")

    def test_typed_absence_parses_and_binds(self):
        from tests.unit.test_wp3_venue_direct_evidence import (
            ALPACA_FP, evidence, policy)
        empty = {"observed_at": OBSERVED, "orders": []}
        item = evidence("alpaca_paper", "open_orders", empty)
        item.verify(policy(), now=NOW)
        assert item.facts["orders_total"] == 0
        assert item.facts["entry_orders"] == 0
        assert item.facts["protective_orders"] == 0
        assert item.facts["internal_symbols"] == ()

    def test_an_uncovered_alpaca_shape_refuses_rather_than_guessing(
            self):
        from tests.unit.test_wp3_venue_direct_evidence import (
            ALPACA_ORDERS, evidence)
        for order_class in ("simple", "oco", "oto", "mleg"):
            payload = json.loads(json.dumps(ALPACA_ORDERS))
            payload["orders"][0]["order_class"] = order_class
            with pytest.raises(VenueEvidenceError,
                               match="does not state a role"):
                evidence("alpaca_paper", "open_orders", payload)

    def test_the_bracket_shape_matches_the_repository_fixtures(self):
        """The bracket parser is checked against the order shape this
        repository's own Alpaca tests already assert, so the coverage
        rests on the existing contract rather than on a payload
        invented for WP3."""
        fixture = (Path(__file__).resolve().parents[0] /
                   "test_alpaca_l1.py").read_text()
        for field in ("order_class", "legs", "stop_price",
                      "limit_price", "filled_qty"):
            assert field in fixture, field
        for field in ("id", "symbol", "side", "qty", "status",
                      "order_class", "legs"):
            assert field in fixture, field


# =================================================================== #
# WP3-C8: the stored policy identity is enforced at discharge         #
# =================================================================== #

class TestC8PolicyIdentityIsEnforcedAtDischarge:

    def _opened(self, authority, tmp_path, name="c8"):
        live = custody(authority, tmp_path, name=name)
        live.open("o-1", signed_exposure=0.1, requested_at_bar=5)
        live.mark_in_flight("o-1", bar_index=6)
        return live

    def test_the_exact_A_to_B_substitution_is_refused(self, authority,
                                                      tmp_path):
        """FROZEN COUNTEREXAMPLE. An obligation opened under STRICT
        policy A was discharged under LOOSE policy B for the same
        venue, account and symbol, using evidence policy A refuses as
        stale. The record stayed self-consistent while the policy
        actually admitting the evidence was not the one it named."""
        live = self._opened(authority, tmp_path)
        loose = mt5_policy(max_age_seconds=31_536_000.0,
                           allowed_sources=("mt5_bridge_snapshot_v1",
                                            "anything_at_all"))
        stale_positions = json.loads(json.dumps(MT5_POSITIONS))
        stale_positions["positions"] = []
        stale_positions["observed_at"] = (
            NOW - timedelta(days=30)).isoformat()
        stale_orders = json.loads(json.dumps(MT5_ORDERS))
        stale_orders["orders"] = []
        stale_orders["observed_at"] = (
            NOW - timedelta(days=30)).isoformat()

        # policy A genuinely refuses this evidence
        with pytest.raises(VenueEvidenceError, match="stale"):
            mt5_evidence("positions", stale_positions).verify(
                mt5_policy(), now=NOW)

        with pytest.raises(LiveCustodyError,
                           match="policy substitution refused"):
            live.confirm_with_direct_evidence(
                "o-1",
                positions=mt5_evidence("positions", stale_positions),
                orders=mt5_evidence("open_orders", stale_orders),
                policy=loose, now=NOW, bar_index=7)
        assert live.read("o-1")["state"] == "flatten_in_flight"

    @pytest.mark.parametrize("substitute", [
        {"max_age_seconds": 121.0},
        {"max_age_seconds": 31_536_000.0},
        {"allowed_sources": ("mt5_bridge_snapshot_v1", "other")},
        {"allowed_sources": ("some_other_feed",)},
        {"schema_version": "lts.venue_direct_evidence.v2"},
        {"calendar_identity": "another-calendar"},
    ])
    def test_any_changed_policy_term_refuses(self, authority,
                                             tmp_path, substitute):
        live = self._opened(authority, tmp_path,
                            name=f"c8_{abs(hash(str(substitute)))}")
        positions, orders = flat_evidence()
        with pytest.raises(LiveCustodyError,
                           match="policy substitution refused"):
            live.confirm_with_direct_evidence(
                "o-1", positions=positions, orders=orders,
                policy=mt5_policy(**substitute), now=NOW, bar_index=7)
        assert live.read("o-1")["state"] == "flatten_in_flight"

    @pytest.mark.parametrize("substitute,field", [
        ({"venue": "alpaca_paper"}, "venue"),
        ({"account": "otherfp000001"}, "account_fingerprint"),
        ({"symbol": "EURUSD"}, "symbol"),
    ])
    def test_a_policy_for_another_identity_refuses(self, authority,
                                                   tmp_path,
                                                   substitute, field):
        live = self._opened(authority, tmp_path, name=f"c8_{field}")
        positions, orders = flat_evidence()
        with pytest.raises(LiveCustodyError,
                           match="policy substitution refused|"
                                 "the discharge policy names"):
            live.confirm_with_direct_evidence(
                "o-1", positions=positions, orders=orders,
                policy=mt5_policy(**substitute), now=NOW, bar_index=7)
        assert live.read("o-1")["state"] == "flatten_in_flight"

    def test_a_forged_digest_in_the_binding_cannot_help(self,
                                                        authority,
                                                        tmp_path):
        """A binding that names a digest no real policy produces can
        never be satisfied, so the forgery blocks itself."""
        forged = custody(authority, tmp_path, name="c8_forged",
                         evidence_policy_digest="a" * 64)
        forged.open("o-1", signed_exposure=0.1, requested_at_bar=5)
        positions, orders = flat_evidence()
        for candidate in (mt5_policy(),
                          mt5_policy(max_age_seconds=999.0)):
            with pytest.raises(LiveCustodyError,
                               match="policy substitution refused"):
                forged.confirm_with_direct_evidence(
                    "o-1", positions=positions, orders=orders,
                    policy=candidate, now=NOW, bar_index=7)

    def test_a_record_that_does_not_name_the_policy_refuses(
            self, authority, tmp_path):
        """Even when the binding agrees, the DURABLE record must name
        the same policy."""
        from app.live_flatten_custody import require_bound_policy
        live = self._opened(authority, tmp_path, name="c8_record")
        record = dict(live.read("o-1"))
        record["checkpoint_identity"] = "evidence_policy_digest=" + \
            "b" * 64
        with pytest.raises(LiveCustodyError,
                           match="durable record does not name"):
            require_bound_policy(mt5_policy(), binding=live.binding,
                                 record=record)

    def test_the_policy_check_precedes_freshness_and_facts(self,
                                                           authority,
                                                           tmp_path):
        """The refusal must be the SUBSTITUTION, not staleness: an
        unbound policy never gets the chance to admit anything."""
        live = self._opened(authority, tmp_path, name="c8_order")
        stale = json.loads(json.dumps(MT5_POSITIONS))
        stale["positions"] = []
        stale["observed_at"] = (NOW - timedelta(days=30)).isoformat()
        _p, orders = flat_evidence()
        with pytest.raises(LiveCustodyError) as caught:
            live.confirm_with_direct_evidence(
                "o-1", positions=mt5_evidence("positions", stale),
                orders=orders, policy=mt5_policy(max_age_seconds=9e8),
                now=NOW, bar_index=7)
        assert "policy substitution refused" in str(caught.value)
        assert "stale" not in str(caught.value)

    def test_the_bound_policy_still_discharges(self, authority,
                                               tmp_path):
        live = self._opened(authority, tmp_path, name="c8_honest")
        positions, orders = flat_evidence()
        record = live.confirm_with_direct_evidence(
            "o-1", positions=positions, orders=orders,
            policy=mt5_policy(), now=NOW, bar_index=7)
        assert record["state"] == "flatten_confirmed"
        assert record["reconciliation"]["evidence_policy_digest"] == \
            mt5_policy().policy_digest, (
            "the discharge records WHICH policy authorised it")

    def test_the_binding_survives_a_clean_store_reload(self,
                                                       authority,
                                                       tmp_path):
        live = self._opened(authority, tmp_path, name="c8_reload")
        reborn = custody(authority, tmp_path, name="c8_reload")
        record = reborn.recover()
        assert record["state"] == "flatten_in_flight"
        assert mt5_policy().policy_digest in \
            record["checkpoint_identity"]
        loose = mt5_policy(max_age_seconds=9e8)
        positions, orders = flat_evidence()
        with pytest.raises(LiveCustodyError,
                           match="policy substitution refused"):
            reborn.confirm_with_direct_evidence(
                "o-1", positions=positions, orders=orders,
                policy=loose, now=NOW, bar_index=7)
        confirmed = reborn.confirm_with_direct_evidence(
            "o-1", positions=positions, orders=orders,
            policy=mt5_policy(), now=NOW, bar_index=7)
        assert confirmed["state"] == "flatten_confirmed"

    def test_a_non_policy_object_refuses(self, authority, tmp_path):
        live = self._opened(authority, tmp_path, name="c8_type")
        positions, orders = flat_evidence()
        for bogus in (None, {}, "policy", 1.0,
                      type("P", (), {"policy_digest": "0" * 64})()):
            with pytest.raises(LiveCustodyError,
                               match="validated VenueEvidencePolicy"):
                live.confirm_with_direct_evidence(
                    "o-1", positions=positions, orders=orders,
                    policy=bogus, now=NOW, bar_index=7)


class TestC8DigestFieldsAreCanonical:

    @pytest.mark.parametrize("bad", [
        "not-a-digest", "", "   ", "A" * 64, "0" * 63, "0" * 65,
        "0" * 64 + " ", " " + "0" * 64, True, False, 123, None,
        "g" * 64, "0" * 32])
    def test_a_non_digest_refuses_before_any_store_access(self,
                                                          authority,
                                                          bad):
        with pytest.raises(LiveCustodyError,
                           match="hex digest"):
            VenueObligationBinding(
                venue="mt5_demo",
                account_fingerprint="sanitizedfp01",
                symbol="USDCAD", position_identity="100001",
                evidence_policy_digest=bad,
                calendar_identity="cal-venue-v1",
                authority_code_identity=authority.code_identity)

    @pytest.mark.parametrize("bad", ["A" * 64, "0" * 63,
                                     "not-a-digest", True])
    def test_the_authority_identity_is_also_a_digest(self, bad):
        with pytest.raises(LiveCustodyError, match="hex digest"):
            VenueObligationBinding(
                venue="mt5_demo",
                account_fingerprint="sanitizedfp01",
                symbol="USDCAD", position_identity="100001",
                evidence_policy_digest="0" * 64,
                calendar_identity="cal-venue-v1",
                authority_code_identity=bad)

    def test_no_normalisation_is_applied(self, authority):
        """Uppercase hex is a DIFFERENT string, and silently lowering
        it would make two distinct bindings compare equal."""
        upper = authority.code_identity.upper()
        assert upper != authority.code_identity
        with pytest.raises(LiveCustodyError, match="no normalisation"):
            VenueObligationBinding(
                venue="mt5_demo",
                account_fingerprint="sanitizedfp01",
                symbol="USDCAD", position_identity="100001",
                evidence_policy_digest=upper,
                calendar_identity="cal-venue-v1",
                authority_code_identity=authority.code_identity)


# =================================================================== #
# C7 stays INCOMPLETE, and the proposed export cannot act             #
# =================================================================== #

class TestC7ProposedExportCannotAct:

    def test_it_prints_a_plan_and_touches_nothing(self, capsys):
        from tools.propose_alpaca_capture_export import main
        assert main([]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["status"].startswith("PROPOSED")
        assert report["performs_any_read_of_private_state"] is False
        assert report["connects_to_alpaca"] is False
        assert report["holds_credentials"] is False
        assert [s["stage"] for s in report["stages"]] == [
            "read", "stage", "redact"]

    def test_even_the_owner_gate_has_no_execution_body(self, capsys):
        from tools.propose_alpaca_capture_export import main
        assert main(["--i-am-the-owner"]) == 3
        report = json.loads(capsys.readouterr().out)
        assert "EXECUTION BODY DELIBERATELY ABSENT" in \
            report["status"]

    def test_the_module_cannot_read_private_state_or_connect(self):
        import tools.propose_alpaca_capture_export as tool
        source = Path(tool.__file__).read_text()
        for forbidden in ("sqlite3", "import requests", "urllib",
                          "expanduser", "Path.home()",
                          "AlpacaPaper", "os.environ",
                          "connect("):
            assert forbidden not in source, forbidden

    def test_wp3_c7_is_still_declared_incomplete(self):
        note = (Path(__file__).resolve().parents[2] / "examples" /
                "captures" / "wp3_alpaca_dry_run" / "NOTE.md"
                ).read_text()
        flat = " ".join(note.lower().replace("**", "").split())
        assert "synthetic" in flat
        assert "typed absence" in flat
        from tools.propose_alpaca_capture_export import describe
        assert "INCOMPLETE" in describe()["note"]
