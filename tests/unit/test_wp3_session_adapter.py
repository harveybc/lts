"""WP3.5 — effect-free acceptance for the venue decision adapter,
the calendar-aware watchdog and the dry run.

The authority is the ACCEPTED simulator policy, loaded from a sibling
checkout resolved relatively; the tests skip rather than vendor a copy
if it is not present. Nothing here opens a socket and nothing here can
write to a venue, and both are asserted structurally.
"""
from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.session_authority_adapter import (
    AdapterRefusal, AuthorityUnavailable, EFFECTS, STATES,
    VenueDirective, build_exposure_facts, derive_directive,
    load_authority)
from app.session_watchdog import (
    CALENDAR_SUPPRESSES_ONLY, CLASSIFICATIONS, classify)
from app.venue_direct_evidence import (
    VenueDirectEvidence, VenueEvidenceError, VenueEvidencePolicy)

from tests.unit.test_wp3_venue_direct_evidence import (  # noqa: E402
    ALPACA_ACCOUNT, ALPACA_FP, ALPACA_ORDERS, ALPACA_POSITIONS,
    ALPACA_PROTECTIVE_CHILD, ALPACA_SHORT_POSITION, MT5_ACCOUNT,
    MT5_CLOCK, MT5_ORDERS, MT5_POSITIONS, NOW, OBSERVED, evidence,
    mt5_evidence, mt5_policy, policy)


def reviewed_identity():
    """The digest of the authority checkout as it stands right now.
    C4 makes pinning MANDATORY, so every load in these tests states
    which authority it accepts."""
    import hashlib
    import importlib.util
    import sys as _sys
    material = []
    for dotted in ("app.session_exposure", "app.flatten_custody"):
        path = AUTHORITY_ROOT / Path(*dotted.split(".")).with_suffix(
            ".py")
        material.append(
            f"_lts_authority_{dotted.replace('.', '_')}="
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return hashlib.sha256(
        "|".join(sorted(material)).encode()).hexdigest()

AUTHORITY_ROOT = Path(__file__).resolve().parents[3] / "gym-fx"
pytestmark = pytest.mark.skipif(
    not (AUTHORITY_ROOT / "app" / "session_exposure.py").is_file(),
    reason="the accepted session authority checkout is not present; "
           "this adapter has no local reimplementation to test "
           "against, by design")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*_args, **_kwargs):
        raise AssertionError(
            "network operation attempted in a WP3 adapter test")
    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


@pytest.fixture(scope="module")
def authority():
    return load_authority(AUTHORITY_ROOT,
                          expected_code_identity=reviewed_identity())


def session_policy():
    return {
        "enabled": True,
        "session_source": "venue_symbol_sessions_v1",
        "wind_down_hours": 8.0,
        "forced_flatten_hours": 4.0,
        "cancel_pending_on_wind_down": True,
        "allow_risk_increase_during_wind_down": False,
        "reopen_min_hours": 8.0,
        "reopen_min_closed_bars": 2,
        "stability_consecutive_checks": 2,
        "max_spread_relative_to_baseline": 3.0,
        "max_gap_sigma": 6.0,
        "max_realized_vol_relative_to_baseline": 4.0,
        "carried_position_recovery":
            "protected_opportunistic_then_forced",
        "holiday_policy": "same_as_weekly",
        "calendar_identity": "cal-venue-v1",
        "reopen_baseline_bars": 4,
        "reopen_gap_sigma_bars": 4,
        "reopen_realized_vol_bars": 4,
    }


def block(state, **kw):
    base = {"state": state, "policy_enabled": True,
            "evidence_ok": True, "wind_down": state in
            ("WIND_DOWN", "FORCED_FLATTEN"),
            "forced_flatten": state == "FORCED_FLATTEN",
            "time_to_next_close_hours": 3.0,
            "time_since_reopen_hours": 20.0}
    base.update(kw)
    return base


def facts_for(venue="alpaca_paper", *, flat=False, orders=True):
    if venue == "alpaca_paper":
        pos = json.loads(json.dumps(ALPACA_POSITIONS))
        ords = json.loads(json.dumps(ALPACA_ORDERS))
        if flat:
            pos["positions"] = []
        if not orders:
            ords["orders"] = []
        return (evidence(venue, "positions", pos).facts,
                evidence(venue, "open_orders", ords).facts,
                evidence(venue, "positions", pos).provenance())
    pos = json.loads(json.dumps(MT5_POSITIONS))
    ords = json.loads(json.dumps(MT5_ORDERS))
    if flat:
        pos["positions"] = []
    if not orders:
        ords["orders"] = []
    return (mt5_evidence("positions", pos).facts,
            mt5_evidence("open_orders", ords).facts,
            mt5_evidence("positions", pos).provenance())


def directive(authority, state, *, venue="alpaca_paper",
              command=1, raw=1.0, flat=False, orders=True,
              recovery=None):
    positions, open_orders, provenance = facts_for(
        venue, flat=flat, orders=orders)
    return derive_directive(
        authority, policy=authority.session_exposure.validate_policy(
            session_policy()),
        state_block=block(state), venue=venue,
        account_fingerprint=("0c7d3b4e5f6a7b8c"
                             if venue == "alpaca_paper"
                             else "sanitizedfp01"),
        symbol="SPY" if venue == "alpaca_paper" else "USDCAD",
        raw_model_output=raw, mapped_command=command,
        positions=positions, orders=open_orders,
        provenance=provenance, recovery=recovery)


VENUES = ["alpaca_paper", "mt5_demo"]


# =================================================================== #
# the authority is consumed, never reimplemented                      #
# =================================================================== #

class TestAuthorityBinding:

    def test_the_accepted_authority_is_loaded_and_identified(
            self, authority):
        assert len(authority.code_identity) == 64
        assert authority.states == (
            "NORMAL_TRADING", "WIND_DOWN", "FORCED_FLATTEN",
            "EXPECTED_MARKET_CLOSED", "REOPEN_BLACKOUT")

    def test_a_mismatched_code_identity_refuses(self):
        with pytest.raises(AuthorityUnavailable,
                           match="does not match the reviewed"):
            load_authority(AUTHORITY_ROOT,
                           expected_code_identity="0" * 64)

    @pytest.mark.parametrize("digest", [
        None, "", "not-hex", "abc", "A" * 64, 123, "0" * 63])
    def test_an_unpinned_or_malformed_identity_refuses(self, digest):
        """C4: the digest was optional and defaulted to None, so ANY
        checkout loaded and the claim that a different authority is
        refused was simply untrue."""
        with pytest.raises(AuthorityUnavailable,
                           match="unpinned authority is refused"):
            load_authority(AUTHORITY_ROOT,
                           expected_code_identity=digest)

    def test_the_digest_cannot_be_omitted_at_all(self):
        with pytest.raises(TypeError):
            load_authority(AUTHORITY_ROOT)

    def test_a_missing_authority_refuses_with_no_fallback(self,
                                                          tmp_path):
        with pytest.raises(AuthorityUnavailable,
                           match="no local reimplementation"):
            load_authority(tmp_path / "absent",
                           expected_code_identity="0" * 64)

    def test_the_digest_is_recomputed_from_disk_each_load(self,
                                                          tmp_path):
        """A checkout that drifted since review refuses, because the
        files are re-hashed at load rather than trusted."""
        import shutil
        copy = tmp_path / "authority"
        (copy / "app").mkdir(parents=True)
        for name in ("session_exposure.py", "flatten_custody.py",
                     "migration_custody.py", "direct_evidence.py"):
            shutil.copy(AUTHORITY_ROOT / "app" / name,
                        copy / "app" / name)
        pinned = load_authority(
            copy, expected_code_identity=reviewed_identity()
        ).code_identity
        target = copy / "app" / "session_exposure.py"
        target.write_text(target.read_text() + "\n# drift\n")
        with pytest.raises(AuthorityUnavailable,
                           match="does not match the reviewed"):
            load_authority(copy, expected_code_identity=pinned)

    def test_this_repository_does_not_reimplement_the_states(self):
        """The adapter must translate, never re-derive. A local copy
        of the state machine would be a second authority free to
        drift from the accepted one."""
        import app.session_authority_adapter as adapter
        source = Path(adapter.__file__).read_text()
        for forbidden in ("def session_state(", "def overlay_action(",
                          "def classify_action(",
                          "def classify_discrete_command("):
            assert forbidden not in source, forbidden


# =================================================================== #
# the five states plus recovery, on both venues                       #
# =================================================================== #

class TestFiveStatesOnBothVenues:

    @pytest.mark.parametrize("venue", VENUES)
    def test_normal_trading_passes_the_decision_through(self,
                                                        authority,
                                                        venue):
        result = directive(authority, "NORMAL_TRADING", venue=venue,
                           flat=True, orders=False)
        assert result.session_state == "NORMAL_TRADING"
        assert result.overlay == "pass_through"
        assert result.final_command == 1
        assert result.effects == ("submit_decision",)
        assert result.blocks_risk_increase is False

    @pytest.mark.parametrize("venue", VENUES)
    def test_wind_down_blocks_entries_and_cancels_only_entries(
            self, authority, venue):
        result = directive(authority, "WIND_DOWN", venue=venue,
                           flat=True)
        assert result.overlay == "masked_risk_increase"
        assert result.final_command == 0, "a blocked entry sends HOLD"
        assert "submit_decision" not in result.effects
        assert result.preserve_protection is True
        for identity in result.cancel_order_identities:
            assert identity not in ("stop-leg-id", "limit-leg-id"), (
                "protection must never be cancelled")

    @pytest.mark.parametrize("venue", VENUES)
    def test_forced_flatten_keeps_protection_and_awaits_confirmation(
            self, authority, venue):
        result = directive(authority, "FORCED_FLATTEN", venue=venue)
        assert result.overlay == "forced_close"
        assert result.final_command == 3
        assert "request_close" in result.effects
        assert result.requires_direct_confirmation is True
        assert result.preserve_protection is True

    @pytest.mark.parametrize("venue", VENUES)
    def test_expected_market_closed_has_no_actionable_step(self,
                                                           authority,
                                                           venue):
        result = directive(authority, "EXPECTED_MARKET_CLOSED",
                           venue=venue)
        assert result.overlay == "no_actionable_step"
        assert result.final_command is None
        assert result.mapped_command is None
        assert result.effects == ("none",)
        assert result.blocks_risk_increase is True

    @pytest.mark.parametrize("venue", VENUES)
    def test_reopen_blackout_blocks_entries(self, authority, venue):
        result = directive(authority, "REOPEN_BLACKOUT", venue=venue,
                           flat=True)
        assert result.overlay == "masked_entry_during_blackout"
        assert result.final_command == 0
        assert result.blocks_risk_increase is True

    @pytest.mark.parametrize("venue", VENUES)
    def test_recovery_takes_precedence_over_every_state(self,
                                                        authority,
                                                        venue):
        for state in ("NORMAL_TRADING", "WIND_DOWN",
                      "REOPEN_BLACKOUT"):
            result = directive(
                authority, state, venue=venue,
                recovery={"blocks_risk_increase": True,
                          "reason": "outstanding_flatten_obligation"})
            assert result.session_state == "RECOVERY"
            assert result.overlay == "blocked_by_flatten_recovery"
            assert result.final_command == 0
            assert result.effects == ("none",)

    def test_every_state_is_translated(self, authority):
        covered = set()
        for state in ("NORMAL_TRADING", "WIND_DOWN",
                      "FORCED_FLATTEN", "EXPECTED_MARKET_CLOSED",
                      "REOPEN_BLACKOUT"):
            covered.add(directive(authority, state).session_state)
        covered.add(directive(
            authority, "NORMAL_TRADING",
            recovery={"blocks_risk_increase": True,
                      "reason": "x"}).session_state)
        assert covered == set(STATES)


# =================================================================== #
# four separate records, and long/short/flat                          #
# =================================================================== #

class TestActionRecordsAndExposureShapes:

    def test_four_records_stay_distinct(self, authority):
        result = directive(authority, "WIND_DOWN", command=2,
                           raw=-0.87, flat=True)
        payload = result.as_dict()
        assert payload["raw_model_output"] == -0.87
        assert payload["mapped_command"] == 2
        assert payload["mapped_action"]["command_name"] == "short"
        assert payload["overlay"] == "masked_risk_increase"
        assert payload["final_command"] == 0

    @pytest.mark.parametrize("venue", VENUES)
    @pytest.mark.parametrize("flat", [True, False])
    def test_long_short_and_flat_are_all_classified(self, authority,
                                                    venue, flat):
        for command in (0, 1, 2, 3):
            result = directive(authority, "NORMAL_TRADING",
                               venue=venue, command=command, flat=flat)
            assert result.mapped_action["command"] == command
            assert isinstance(
                result.mapped_action["risk_increasing"], bool)

    def test_a_bad_command_refuses(self, authority):
        with pytest.raises(Exception):
            directive(authority, "NORMAL_TRADING", command=7)

    def test_simulator_provenance_is_refused_by_the_adapter(self,
                                                            authority):
        positions, orders, _prov = facts_for()
        with pytest.raises(VenueEvidenceError):
            derive_directive(
                authority,
                policy=authority.session_exposure.validate_policy(
                    session_policy()),
                state_block=block("NORMAL_TRADING"),
                venue="alpaca_paper",
                account_fingerprint="0c7d3b4e5f6a7b8c", symbol="SPY",
                raw_model_output=1.0, mapped_command=1,
                positions=positions, orders=orders,
                provenance={"evidence_provenance":
                            "simulator_bar_local",
                            "venue_direct": False})

    def test_missing_order_split_refuses(self, authority):
        positions, orders, prov = facts_for()
        broken = dict(orders)
        del broken["entry_orders"]
        with pytest.raises(AdapterRefusal,
                           match="entry/protective split"):
            derive_directive(
                authority,
                policy=authority.session_exposure.validate_policy(
                    session_policy()),
                state_block=block("NORMAL_TRADING"),
                venue="alpaca_paper",
                account_fingerprint="0c7d3b4e5f6a7b8c", symbol="SPY",
                raw_model_output=1.0, mapped_command=1,
                positions=positions, orders=broken, provenance=prov)


# =================================================================== #
# core-vs-adapter decision equality                                   #
# =================================================================== #

class TestCoreAdapterDecisionEquality:

    @pytest.mark.parametrize("state", [
        "NORMAL_TRADING", "WIND_DOWN", "FORCED_FLATTEN",
        "REOPEN_BLACKOUT"])
    @pytest.mark.parametrize("command", [0, 1, 2, 3])
    def test_equivalent_facts_give_the_same_verdict(self, authority,
                                                    state, command):
        """For equivalent facts the adapter's overlay must equal the
        one the accepted authority produces directly. Any divergence
        means the adapter re-decided instead of translating."""
        session = authority.session_exposure
        positions, orders, prov = facts_for()
        signed = sum(float(row["signed_quantity"])
                     for row in positions["positions"])
        exposure = session.ExposureFacts.build(
            signed_exposure=signed,
            pending_orders=orders["entry_orders"] +
            orders["protective_orders"],
            protective_orders=orders["protective_orders"],
            action_mapping="discrete_command_v1")
        classification = session.classify_discrete_command(command,
                                                           exposure)
        core = session.overlay_action(
            session.validate_policy(session_policy()), block(state),
            exposure, float(command), classification=classification)

        adapted = directive(authority, state, command=command)
        assert adapted.overlay == core["overlay"]
        assert adapted.mapped_action == classification

    def test_both_venues_agree_for_equivalent_exposure(self,
                                                       authority):
        """Alpaca and MT5 state their facts differently; once both are
        parsed into the same signed exposure the DECISION is the same
        on either venue."""
        for state in ("WIND_DOWN", "FORCED_FLATTEN",
                      "REOPEN_BLACKOUT"):
            left = directive(authority, state, venue="alpaca_paper")
            right = directive(authority, state, venue="mt5_demo")
            assert left.overlay == right.overlay, state
            assert left.final_command == right.final_command, state
            assert left.effects == right.effects or (
                # the venues rest different order books, so the
                # cancellation effect may legitimately differ
                set(left.effects) ^ set(right.effects) <= {
                    "cancel_pending_entries"}), (state, left.effects,
                                                 right.effects)


# =================================================================== #
# calendar-aware watchdog                                             #
# =================================================================== #

PROV = {"venue_direct": True, "source": "alpaca_paper_rest_v2"}


def watch(**kw):
    base = dict(session_state="NORMAL_TRADING", session_connected=True,
                trading_enabled=True, bar_age_seconds=10.0,
                max_bar_age_seconds=120.0, positions_total=0,
                orders_total=0, flatten_requested=False,
                flatten_confirmed=False, recovery_active=False,
                provenance=PROV)
    base.update(kw)
    return classify(**base)


class TestCalendarAwareWatchdog:

    def test_healthy(self):
        assert watch().classification == "healthy"

    def test_expected_closure_suppresses_only_the_stale_bar(self):
        verdict = watch(session_state="EXPECTED_MARKET_CLOSED",
                        bar_age_seconds=100_000.0)
        assert verdict.classification == "expected_market_closed"
        assert verdict.suppressed == CALENDAR_SUPPRESSES_ONLY

    def test_a_stale_feed_in_an_open_window_is_critical(self):
        verdict = watch(bar_age_seconds=100_000.0)
        assert verdict.classification == \
            "stale_feed_during_open_window"
        assert verdict.severity == "critical"

    @pytest.mark.parametrize("kw", [
        {"session_connected": False}, {"trading_enabled": False}])
    def test_a_closed_market_never_suppresses_a_disconnection(self,
                                                              kw):
        verdict = watch(session_state="EXPECTED_MARKET_CLOSED",
                        bar_age_seconds=100_000.0, **kw)
        assert verdict.classification == \
            "terminal_or_account_disconnected"
        assert verdict.severity == "critical"

    def test_a_closed_market_never_suppresses_exposure(self):
        verdict = watch(session_state="EXPECTED_MARKET_CLOSED",
                        positions_total=1, bar_age_seconds=100_000.0)
        assert verdict.classification == \
            "unexpected_exposure_during_closure"
        assert verdict.severity == "critical"

    def test_a_closed_market_never_suppresses_open_orders(self):
        verdict = watch(session_state="EXPECTED_MARKET_CLOSED",
                        orders_total=2, bar_age_seconds=100_000.0)
        assert verdict.classification == \
            "unexpected_exposure_during_closure"

    def test_an_unconfirmed_flatten_is_a_failure(self):
        verdict = watch(session_state="FORCED_FLATTEN",
                        flatten_requested=True,
                        flatten_confirmed=False, positions_total=1)
        assert verdict.classification == "flatten_failed"
        assert verdict.severity == "critical"

    def test_a_confirmed_flatten_is_not_a_failure(self):
        verdict = watch(session_state="FORCED_FLATTEN",
                        flatten_requested=True,
                        flatten_confirmed=True)
        assert verdict.classification != "flatten_failed"

    def test_recovery_is_reported(self):
        verdict = watch(recovery_active=True)
        assert verdict.classification == "recovery_active"

    def test_every_classification_is_reachable(self):
        seen = {
            watch().classification,
            watch(session_state="EXPECTED_MARKET_CLOSED"
                  ).classification,
            watch(bar_age_seconds=1e6).classification,
            watch(session_connected=False).classification,
            watch(flatten_requested=True).classification,
            watch(session_state="EXPECTED_MARKET_CLOSED",
                  positions_total=1).classification,
            watch(recovery_active=True).classification,
        }
        assert seen == set(CLASSIFICATIONS)

    @pytest.mark.parametrize("kw", [
        {"session_connected": None}, {"bar_age_seconds": None},
        {"positions_total": "1"}, {"recovery_active": 1},
        {"provenance": {"venue_direct": False}}])
    def test_an_unavailable_fact_refuses_rather_than_reading_healthy(
            self, kw):
        with pytest.raises(VenueEvidenceError):
            watch(**kw)


# =================================================================== #
# the dry run performs zero writes                                    #
# =================================================================== #

class TestDryRunIsStructurallyEffectFree:

    def test_the_interface_has_no_client_and_no_credential(self):
        from tools.session_directive_dry_run import (
            NoWriteVenueInterface, WriteAttempted)
        interface = NoWriteVenueInterface()
        assert interface.client is None
        assert interface.credentials is None
        assert interface.base_url is None
        for name in ("submit_order", "cancel_order",
                     "close_position", "replace_order", "enqueue",
                     "order_send"):
            with pytest.raises(WriteAttempted):
                getattr(interface, name)()

    def test_the_tool_imports_nothing_network_capable(self):
        import tools.session_directive_dry_run as tool
        source = Path(tool.__file__).read_text()
        for forbidden in ("import requests", "import socket",
                          "import http", "urllib", "AlpacaPaper",
                          "sqlite3"):
            assert forbidden not in source, forbidden

    def test_the_whole_wp3_package_contains_no_write_call(self):
        """WP3.5: zero write calls across the package."""
        import app.session_authority_adapter as adapter
        import app.session_watchdog as watchdog
        import app.venue_direct_evidence as ev
        import tools.session_directive_dry_run as tool
        writes = ("submit_bracket(", "cancel_order(",
                  "close_position(", "OrderSend(", "order_send(",
                  ".post(", ".delete(", ".put(", "_write_request(",
                  "INSERT INTO", "UPDATE ")
        for module in (ev, adapter, watchdog, tool):
            source = Path(module.__file__).read_text()
            for call in writes:
                if module is tool and call in ("cancel_order(",
                                               "close_position(",
                                               "order_send("):
                    continue        # the refusing stubs, by name
                assert call not in source, (module.__name__, call)

    def test_a_dry_run_produces_the_command_it_would_have_sent(
            self, tmp_path):
        from tools.session_directive_dry_run import run
        captures = tmp_path / "captures"
        captures.mkdir()

        def write(name, kind, payload, source, account, symbol,
                  venue):
            (captures / f"{name}.json").write_text(json.dumps({
                "venue": venue, "account_fingerprint": account,
                "symbol": symbol, "evidence_type": kind,
                "schema_version": "v1", "source": source,
                "evidence_id": f"ev-{kind}",
                "payload": json.dumps(payload)}))

        for name, kind, payload in (
                ("account_session", "account_session", MT5_ACCOUNT),
                ("positions", "positions", MT5_POSITIONS),
                ("open_orders", "open_orders", MT5_ORDERS),
                ("market_clock", "market_clock", MT5_CLOCK)):
            write(name, kind, payload, "mt5_bridge_snapshot_v1",
                  "sanitizedfp01", "USDCAD", "mt5_demo")

        report = run(captures, authority_root=AUTHORITY_ROOT,
                     expected_code_identity=reviewed_identity(),
                     now=NOW, raw_model_output=1.0, mapped_command=1,
                     state_block=block("FORCED_FLATTEN"),
                     policy=session_policy(),
                     evidence_policy=mt5_policy())
        assert report["dry_run"] is True
        assert report["writes_performed"] == 0
        assert report["interface"] == "NoWriteVenueInterface"
        would = report["would_send"]
        assert would["session_state"] == "FORCED_FLATTEN"
        assert would["final_command"] == 3
        assert "request_close" in would["effects"]
        assert would["requires_direct_confirmation"] is True
        assert would["reason"]
        assert report["watchdog"]["session_state"] == \
            "FORCED_FLATTEN"
        for name, prov in report["provenance"].items():
            assert prov["venue_direct"] is True, name

    def test_a_dry_run_refuses_a_re_serialized_payload(self,
                                                       tmp_path):
        from tools.session_directive_dry_run import evidence_from
        with pytest.raises(VenueEvidenceError,
                           match="ORIGINAL text"):
            evidence_from({"payload": MT5_POSITIONS})



# =================================================================== #
# WP3-C9: WIND_DOWN must never offer protection for cancellation      #
# =================================================================== #

class TestC9WindDownPreservesProtection:

    def _directive(self, authority, orders_payload,
                   positions_payload, state="WIND_DOWN"):
        orders_ev = evidence("alpaca_paper", "open_orders",
                             orders_payload)
        positions_ev = evidence("alpaca_paper", "positions",
                                positions_payload)
        return derive_directive(
            authority,
            policy=authority.session_exposure.validate_policy(
                session_policy()),
            state_block=block(state), venue="alpaca_paper",
            account_fingerprint=ALPACA_FP, symbol="SPY",
            raw_model_output=1.0, mapped_command=1,
            positions=positions_ev.facts, orders=orders_ev.facts,
            provenance=positions_ev.provenance())

    def test_the_recorded_protective_child_is_never_cancelled(
            self, authority):
        """FROZEN COUNTEREXAMPLE. Under the old parser this exact
        recorded shape put the protective take-profit's identity in
        the WIND_DOWN cancellation list."""
        result = self._directive(authority, ALPACA_PROTECTIVE_CHILD,
                                 ALPACA_SHORT_POSITION)
        assert result.cancel_order_identities == ()
        assert "cancel_pending_entries" not in result.effects
        assert result.preserve_protection is True
        assert "synthetic-order-0001" not in \
            result.cancel_order_identities

    def test_both_protective_children_survive_a_mixed_book(self,
                                                            authority):
        payload = json.loads(json.dumps(ALPACA_ORDERS))
        payload["orders"].append(json.loads(json.dumps(
            ALPACA_PROTECTIVE_CHILD))["orders"][0])
        result = self._directive(authority, payload,
                                 ALPACA_POSITIONS)
        assert set(result.cancel_order_identities) == {
            "parent-order-id"}, (
            "only the true pending ENTRY may be cancelled")
        for protective in ("stop-leg-id", "limit-leg-id",
                           "synthetic-order-0001"):
            assert protective not in result.cancel_order_identities

    def test_forced_flatten_also_preserves_protection(self,
                                                      authority):
        payload = json.loads(json.dumps(ALPACA_ORDERS))
        payload["orders"].append(json.loads(json.dumps(
            ALPACA_PROTECTIVE_CHILD))["orders"][0])
        result = self._directive(authority, payload,
                                 ALPACA_POSITIONS,
                                 state="FORCED_FLATTEN")
        assert result.overlay == "forced_close"
        assert result.preserve_protection is True
        assert set(result.cancel_order_identities) == {
            "parent-order-id"}

    def test_a_book_of_protection_only_offers_nothing_to_cancel(
            self, authority):
        result = self._directive(authority, ALPACA_PROTECTIVE_CHILD,
                                 ALPACA_SHORT_POSITION,
                                 state="FORCED_FLATTEN")
        assert result.cancel_order_identities == ()
        assert "cancel_pending_entries" not in result.effects
        assert "request_close" in result.effects

    def test_the_exposure_facts_count_the_split_correctly(self,
                                                          authority):
        payload = json.loads(json.dumps(ALPACA_ORDERS))
        payload["orders"].append(json.loads(json.dumps(
            ALPACA_PROTECTIVE_CHILD))["orders"][0])
        orders = evidence("alpaca_paper", "open_orders",
                          payload).facts
        positions = evidence("alpaca_paper", "positions",
                             ALPACA_POSITIONS).facts
        facts = build_exposure_facts(authority, positions=positions,
                                     orders=orders)
        assert facts.protective_orders == 3
        assert facts.entry_orders == 1
