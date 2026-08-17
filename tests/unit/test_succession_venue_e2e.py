"""AUD-F2-20260816-257: one socket-free end-to-end succession per venue.

The finding: the succession primitives had no production entry point —
nothing obtained real venue facts and nothing constructed a real venue
executor.

These three tests are the counter-proof. Each builds the REAL runner
assembly for its venue (real profile loader, real L0 service, real L1
ledger, real executor/outbox/bridge, real model selector) and injects
only the TRANSPORT: a stub HTTP client, a stub TWS client, a temporary
bridge database that the terminal would otherwise post into. Each then
drives ``app.champion_succession.promote_paper_champion`` through the
real ``app.succession_venue`` adapter, end to end, from direct facts to
the switched manifest.

No socket is opened, no order is submitted and no live store, manifest or
capability is touched: every path is a temporary directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from prediction_provider_mechanics import FEATURE_NAMES

from app.champion_succession import (
    BOUNDARY_CAPABILITY_BURNED,
    ActionContract,
    CandidateContract,
    ExecutionContract,
    candidate_activity_report,
    candidate_shadow_replay,
    preflight_candidate,
    promote_paper_champion,
    succession_pending,
)
from app.ibkr_l1_broker import FakeIbkrClient
from app.succession_venue import (
    AlpacaSuccessionVenue,
    IbkrSuccessionVenue,
    Mt5SuccessionVenue,
    build_successor_manifest,
    linear_provisioning_contract,
    linear_shadow_inference,
    seat_contract_from_runner_config,
)
from succession_fixtures import (
    capability_payload,
    make_signer,
    sign,
    write_capability,
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("a succession test opened a socket")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)


def _json(path: Path, value) -> str:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _linear_model(tmp_path: Path, name: str, asset_id: str, timeframe: str,
                  *, intercept: float) -> dict:
    """A REAL linear policy artifact plus its config, hashed."""
    artifact = tmp_path / f"{name}.json"
    artifact_sha = _json(artifact, {
        "schema": "prediction_provider.live_linear_policy.v1",
        "model_id": name, "asset_id": asset_id, "timeframe": timeframe,
        "feature_names": list(FEATURE_NAMES),
        "means": [0.0] * len(FEATURE_NAMES),
        "scales": [1.0] * len(FEATURE_NAMES),
        "coefficients": [0.0] * len(FEATURE_NAMES),
        "intercept": intercept, "probability_threshold": 0.5,
    })
    config = tmp_path / f"{name}-config.json"
    config_sha = _json(config, {"model_id": name})
    return {"model_id": name, "artifact_file": str(artifact),
            "artifact_sha256": artifact_sha, "config_file": str(config),
            "config_sha256": config_sha}


def _manifest(tmp_path: Path, model: dict, asset_id: str,
              timeframe: str) -> Path:
    path = tmp_path / "manifest.json"
    _json(path, {
        "schema": "prediction_provider.live_linear_manifest.v1",
        "model_id": model["model_id"], "asset_id": asset_id,
        "timeframe": timeframe, "artifact_file": model["artifact_file"],
        "artifact_sha256": model["artifact_sha256"],
        "config_file": model["config_file"],
        "config_sha256": model["config_sha256"],
        "research_validated": True, "live_inference_eligible": False,
        "live_execution_eligible": False,
    })
    return path


def _candidate(model: dict, asset_id: str, timeframe: str
               ) -> CandidateContract:
    provisioning = linear_provisioning_contract()
    return CandidateContract(
        model_id=model["model_id"], model_kind="linear",
        artifact_file=model["artifact_file"],
        artifact_sha256=model["artifact_sha256"],
        config_file=model["config_file"],
        config_sha256=model["config_sha256"],
        asset_id=asset_id, timeframe=timeframe,
        observation_dim=provisioning.observation_dim,
        feature_names=provisioning.feature_names,
        preprocessing_sha256=provisioning.preprocessing_sha256,
        action=ActionContract(kind="probability_threshold", threshold=0.5),
        execution=ExecutionContract(
            native_stop_loss=True, native_take_profit=True,
            native_bracket=True, sl_tp_geometry="fraction_of_reference",
            transfer_policy="close_all"),
    )


def _seed_seat(store, seat, incumbent: dict, fingerprint: str,
               bar_times: list[str], now: datetime) -> None:
    """The incumbent session and the incumbent's own due-bar decisions."""
    store._con.execute(
        "INSERT INTO live_model_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("model-session-incumbent", seat.venue, fingerprint,
         seat.instrument, incumbent["model_id"],
         incumbent["artifact_sha256"], incumbent["config_sha256"],
         now.isoformat(), 100000.0, 100000.0, None, None, None, "active"))
    for bar in bar_times:
        store.record_due_bar_decision({
            "venue": seat.venue, "account_fingerprint": fingerprint,
            "asset_id": seat.asset_id, "instrument": seat.instrument,
            "timeframe": seat.timeframe, "bar_close": bar,
            "decided_at": now.isoformat(), "feature_cutoff": bar,
            "input_sha256": hashlib.sha256(bar.encode()).hexdigest(),
            "config_sha256": incumbent["config_sha256"],
            "model_id": incumbent["model_id"],
            "artifact_sha256": incumbent["artifact_sha256"],
            "action": "long", "score": 0.6, "outcome": "decided",
            "decision_id": f"{incumbent['model_id']}:{bar}",
        })


def _owner_capability(tmp_path: Path, seat, candidate, incumbent,
                      shas, now):
    key, signers = make_signer(tmp_path)
    store_dir = tmp_path / "promotion-store"
    store_dir.mkdir(mode=0o700)
    payload = capability_payload(
        seat, candidate, shas, now=now,
        incumbent_model_id=incumbent["model_id"],
        incumbent_artifact_sha256=incumbent["artifact_sha256"])
    path = write_capability(store_dir, "promotion.json", payload)
    sign(key, path)
    return store_dir, signers


def _promote(store, venue, seat, candidate, incumbent, tmp_path, now,
             *, boundary=None):
    """The shared end-to-end promotion, driven through a REAL adapter."""
    compatibility = preflight_candidate(seat, candidate, now=now)
    assert compatibility["verdict"] == "COMPATIBLE"
    infer = linear_shadow_inference(candidate,
                                    venue.historical_closed_bars())
    shadow = candidate_shadow_replay(
        store, seat=seat, candidate=candidate, infer=infer, now=now)
    assert shadow["counts"]["shadowed"] >= 1
    store_dir, signers = _owner_capability(
        tmp_path, seat, candidate, incumbent,
        (compatibility["report_sha256"], shadow["report_sha256"]), now)
    venue.bind_executor()
    record = Path(candidate.artifact_file).parent / (
        f"cell_record_{candidate.model_id}.json")
    if not record.exists():
        record.write_text(json.dumps({
            "schema":
                "agent_multi.p1_difficulty_lr_cell_record.v2",
            "activity_status": "active",
            "promotion_eligible": True,
            "best_model_path": candidate.artifact_file,
            "best_model_sha256": candidate.artifact_sha256}))
    activity = candidate_activity_report(candidate, record, now=now)
    return promote_paper_champion(
        store=store, venue=venue, seat=seat, candidate=candidate,
        compatibility_report=compatibility,
        activity_report=activity, shadow_report=shadow,
        strategy_config={"stop_fraction": 0.01,
                         "take_profit_fraction": 0.02},
        capability_store_dir=store_dir,
        new_manifest=build_successor_manifest(seat, candidate),
        allowed_signers=signers, require_root_pin=False,
        boundary=boundary, now=now)


# ── Alpaca Paper ───────────────────────────────────────────────────────

ALPACA_ACCOUNT = "PA-SUCCESSION-TEST"
ALPACA_FINGERPRINT = hashlib.sha256(
    ALPACA_ACCOUNT.encode()).hexdigest()[:16]


class StubAlpacaTransport:
    """The HTTP transport only. Every Alpaca object above it is real."""

    def __init__(self, *, orders=(), positions=()):
        self._orders = list(orders)
        self._positions = list(positions)
        self.cancelled: list[str] = []
        self.closed: list[str] = []
        self.submitted: list[dict] = []
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        self._bars = [
            {"t": (base + timedelta(days=index)).isoformat()
                  .replace("+00:00", "Z"),
             "o": 500.0 + index * 0.1, "h": 501.0 + index * 0.1,
             "l": 499.0 + index * 0.1, "c": 500.5 + index * 0.1,
             "v": 1_000_000 + index}
            for index in range(70)
        ]

    # -- direct fact interface ----------------------------------------
    def account(self):
        return {"account_number": ALPACA_ACCOUNT, "status": "ACTIVE",
                "cash": "98750.25", "equity": "98901.10"}

    def account_fingerprint(self, account):
        return hashlib.sha256(
            str(account["account_number"]).encode()).hexdigest()[:16]

    def open_orders(self):
        return list(self._orders)

    def positions(self):
        return list(self._positions)

    def asset(self, symbol):
        return {"symbol": symbol, "tradable": True, "shortable": True}

    def stock_bars(self, symbol, *, timeframe, start, feed,
                   page_token=None):
        return {"bars": self._bars, "next_page_token": None}

    # -- drain interface ----------------------------------------------
    def cancel_order(self, order_id):
        self.cancelled.append(str(order_id))
        self._orders = [o for o in self._orders
                        if str(o.get("id")) != str(order_id)]

    def close_position(self, symbol):
        self.closed.append(str(symbol))
        self._positions = [p for p in self._positions
                           if p.get("symbol") != symbol]
        return {"id": "close-order", "status": "accepted"}

    def submit_bracket(self, plan):        # pragma: no cover - must never run
        self.submitted.append(dict(plan))
        raise AssertionError("a succession test submitted an order")


def _alpaca_config(tmp_path: Path, incumbent: dict) -> dict:
    profile = tmp_path / "profile.json"
    _json(profile, {
        "schema": "lts.alpaca.paper_l1_profile.v1", "venue": "alpaca_paper",
        "environment": "paper", "asset_class": "us_equity",
        "account_fingerprint": ALPACA_FINGERPRINT, "symbol": "SPY",
        "asset_id": "equity:SPY", "quantity_ceiling": 1,
        "max_orders_per_day": 4, "max_risk_fraction_at_stop": 0.00005,
        "orders": {"enabled": True},
    })
    manifest = _manifest(tmp_path, incumbent, "equity:SPY", "1d")
    return {
        "schema": "lts.alpaca.model_runner.v1",
        "profile_file": str(profile),
        "secrets": {"api_key_env": "TEST_ALPACA_KEY",
                    "api_secret_env": "TEST_ALPACA_SECRET"},
        "model": {"manifest_file": str(manifest),
                  "expected_asset_id": "equity:SPY",
                  "expected_timeframe": "1d",
                  "execution_tier": "demo_research_canary"},
        "data": {"source": "alpaca_iex", "start": "2026-01-01T00:00:00Z"},
        "strategy": {"stop_fraction": 0.005, "take_profit_fraction": 0.01},
        "loop_seconds": 60,
        "heartbeat_path": str(tmp_path / "heartbeat.json"),
        "service": {
            "venue": "alpaca_paper",
            "account_fingerprint": ALPACA_FINGERPRINT,
            "environment": "paper",
            "database_path": str(tmp_path / "state" / "alpaca.sqlite"),
            "risk_fraction_at_stop": 0.00004, "max_overshoot_ratio": 0.5,
            "gross_notional_fraction_max": 0.01,
            "margin_fraction_max": 0.01,
            "daily_loss_budget_fraction": 0.001,
            "max_concurrent_positions": 1,
            "signal_max_age_seconds": 345600,
            "owner_issuer_allowlist": ["owner"], "command_phrases": {},
            "asset_instrument_bindings": {"equity:SPY": "SPY"},
        },
    }


def test_alpaca_paper_succession_end_to_end(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    incumbent = _linear_model(tmp_path, "spy-incumbent-v1", "equity:SPY",
                              "1d", intercept=1.0)
    challenger = _linear_model(tmp_path, "spy-challenger-v2", "equity:SPY",
                               "1d", intercept=-1.0)
    config = _alpaca_config(tmp_path, incumbent)
    transport = StubAlpacaTransport(
        orders=[{"id": "order-1", "symbol": "SPY", "status": "new"}],
        positions=[{"symbol": "SPY", "qty": "1"}])
    venue = AlpacaSuccessionVenue.from_config(
        config, client_factory=lambda key, secret: transport)
    try:
        runner = venue.runner
        seat = seat_contract_from_runner_config(config)

        # -- direct facts, through the REAL adapter --------------------
        facts = venue.fetch_facts()
        assert facts.venue == "alpaca_paper"
        assert facts.account_fingerprint == ALPACA_FINGERPRINT
        assert facts.cash == 98750.25 and facts.equity == 98901.10
        assert len(facts.open_orders) == 1 and len(facts.positions) == 1
        assert facts.flat is False
        assert facts.source == "alpaca_paper:rest:v2"
        assert facts.instrument_capability["native_bracket"] is True
        assert "cash" not in facts.summary()      # counts, never balances

        # -- the REAL executor ----------------------------------------
        identity = venue.bind_executor()
        assert identity["executor"] == "app.alpaca_l1.AlpacaL1Executor"

        # a journaled bracket the ledger owns is drained through the real
        # AlpacaL1Executor: one cancel, one flatten, zero submissions
        runner.store.create_effect(
            "alpaca-e2e-effect", "spy-incumbent-v1:2026-08-15",
            "alpaca_bracket_entry", ["order-1"])
        runner.store.store_effect_contract(
            "alpaca-e2e-effect", {"symbol": "SPY"})
        drained = venue.drain_for_succession(
            reason="test", incumbent_session_id="s", now=now,
            successor_artifact_sha256=challenger["artifact_sha256"])
        assert transport.cancelled == ["order-1"]
        assert transport.closed == ["SPY"]
        assert transport.submitted == []
        assert drained[0]["actions"] == ["cancel", "flatten"]
        assert venue.fetch_facts().flat is True   # drain really drained

        # -- the whole succession -------------------------------------
        bars = venue.historical_closed_bars()
        _seed_seat(runner.store, seat, incumbent, ALPACA_FINGERPRINT,
                   [bar["time"] for bar in bars[-3:]], now)
        candidate = _candidate(challenger, "equity:SPY", "1d")
        result = _promote(runner.store, venue, seat, candidate, incumbent,
                          tmp_path, now)
        assert result["state"] == "promoted"
        assert transport.submitted == []
        manifest = json.loads(Path(seat.manifest_file).read_bytes())
        assert manifest["model_id"] == "spy-challenger-v2"
        assert manifest["artifact_sha256"] == challenger["artifact_sha256"]
        assert manifest["live_execution_eligible"] is False   # unchanged
        row = runner.store._con.execute(
            "SELECT model_id, starting_balance FROM live_model_sessions"
            " WHERE state='active'").fetchone()
        assert row == ("spy-challenger-v2", 98750.25)
    finally:
        venue.close()


def test_alpaca_runner_refuses_new_risk_while_a_saga_is_open(tmp_path):
    """Order §3.4 at the runner: a pending saga blocks the tick and shows
    the split state in the heartbeat."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    incumbent = _linear_model(tmp_path, "spy-incumbent-v1", "equity:SPY",
                              "1d", intercept=1.0)
    challenger = _linear_model(tmp_path, "spy-challenger-v2", "equity:SPY",
                               "1d", intercept=-1.0)
    config = _alpaca_config(tmp_path, incumbent)
    transport = StubAlpacaTransport()
    venue = AlpacaSuccessionVenue.from_config(
        config, client_factory=lambda key, secret: transport)
    try:
        runner, seat = venue.runner, seat_contract_from_runner_config(config)
        bars = venue.historical_closed_bars()
        _seed_seat(runner.store, seat, incumbent, ALPACA_FINGERPRINT,
                   [bar["time"] for bar in bars[-3:]], now)
        candidate = _candidate(challenger, "equity:SPY", "1d")

        class Crash(RuntimeError):
            pass

        def boundary(name):
            if name == BOUNDARY_CAPABILITY_BURNED:
                raise Crash(name)

        with pytest.raises(Crash):
            _promote(runner.store, venue, seat, candidate, incumbent,
                     tmp_path, now, boundary=boundary)

        pending = succession_pending(
            runner.store, venue="alpaca_paper", instrument="SPY",
            account_fingerprint=ALPACA_FINGERPRINT)
        assert pending["split_authority"] is True
        assert runner.succession_gate() == pending
        result = runner.tick()
        assert result["state"] == "blocked_succession_pending"
        assert result["orders_submitted"] == 0
        assert transport.submitted == []
        runner.write_heartbeat(result)
        heartbeat = json.loads(
            Path(config["heartbeat_path"]).read_bytes())
        assert heartbeat["succession_state"]["state"] == "manifest_pending"
    finally:
        venue.close()


# ── IBKR Paper ─────────────────────────────────────────────────────────

IBKR_ACCOUNT = "DU-SUCCESSION-TEST"
IBKR_FINGERPRINT = hashlib.sha256(IBKR_ACCOUNT.encode()).hexdigest()[:16]


class StubTwsTransport(FakeIbkrClient):
    """The TWS transport only; the outbox, ledger and gate are real."""

    def account_balance(self):
        return {"equity": 250000.0, "cash": 249500.0,
                "available_funds": 249500.0}

    def current_quote(self, _instrument):
        return {"conId": 15016062, "symbol": "USD", "currency": "CAD",
                "secType": "CASH", "bid": 1.4045, "ask": 1.4046,
                "observed_at": datetime.now(timezone.utc)}

    def historical_closed_bars(self, _instrument, *, timeframe, count):
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        return [
            {"time": (base + timedelta(hours=4 * index)).isoformat(),
             "open": 1.39 + index * 0.0002,
             "high": 1.391 + index * 0.0002,
             "low": 1.389 + index * 0.0002,
             "close": 1.3905 + index * 0.0002,
             "volume": 0.0, "complete": True}
            for index in range(count)
        ]


def _ibkr_config(tmp_path: Path, incumbent: dict) -> dict:
    from app.ibkr_model_authority import MANDATE_SCHEMA, ContinuousPaperProfile

    profile_path = tmp_path / "profile.json"
    _json(profile_path, {
        "schema_version": "lts.ibkr.paper.model_profile.v1",
        "venue": "ibkr_paper", "environment": "paper",
        "host": "127.0.0.1", "port": 7497, "client_id": 78,
        "account_fingerprint_algorithm": "account_id_sha256_16",
        "account_fingerprint": IBKR_FINGERPRINT,
        "instrument": "USD.CAD", "asset_id": "fx:USD/CAD",
        "max_entries_per_day": 4, "quantity_ceiling": 25000,
        "stop_distance_price_max": 0.003,
        "take_profit_distance_price_max": 0.006,
        "max_spread_price": 0.0003, "contract_con_id": None,
    })
    profile = ContinuousPaperProfile.load(profile_path)
    mandate_path = tmp_path / "mandate.json"
    now = datetime.now(timezone.utc)
    _json(mandate_path, {
        "schema": MANDATE_SCHEMA, "environment": "paper",
        "venue": "ibkr_paper", "profile_hash": profile.profile_hash,
        "asset_id": "fx:USD/CAD", "instrument": "USD.CAD",
        "execution_tier": "demo_research_canary",
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "max_risk_fraction_at_stop": 0.0000625,
        "quantity_ceiling": 25000, "max_entries_per_day": 4,
        "mandate_id": "succession-e2e",
    })
    os.chmod(mandate_path, 0o600)
    manifest = _manifest(tmp_path, incumbent, "fx:USD/CAD", "4h")
    return {
        "schema": "lts.ibkr.model_runner.v1",
        "profile_file": str(profile_path),
        "mandate_file": str(mandate_path),
        "model": {"manifest_file": str(manifest),
                  "expected_timeframe": "4h",
                  "execution_tier": "demo_research_canary"},
        "route": {"minimum_units": 25000, "unit_step": 1000,
                  "margin_rate": 1},
        "strategy": {"stop_fraction": 0.0015,
                     "take_profit_fraction": 0.003},
        "price_decimals": 5, "quantity_decimals": 0,
        "max_decision_age_seconds": 300, "loop_seconds": 60,
        "heartbeat_path": str(tmp_path / "heartbeat.json"),
        "service": {
            "venue": "ibkr_paper", "account_fingerprint": IBKR_FINGERPRINT,
            "environment": "paper",
            "database_path": str(tmp_path / "state" / "ibkr.sqlite"),
            "risk_fraction_at_stop": 0.00005, "max_overshoot_ratio": 0.25,
            "gross_notional_fraction_max": 0.029,
            "margin_fraction_max": 0.029,
            "daily_loss_budget_fraction": 0.0002,
            "max_concurrent_positions": 1, "signal_max_age_seconds": 28800,
            "owner_issuer_allowlist": ["owner"], "command_phrases": {},
            "asset_instrument_bindings": {"fx:USD/CAD": "USD.CAD"},
        },
    }


def test_ibkr_paper_succession_end_to_end(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    incumbent = _linear_model(tmp_path, "usdcad-incumbent-v1",
                              "fx:USD/CAD", "4h", intercept=1.0)
    challenger = _linear_model(tmp_path, "usdcad-challenger-v2",
                               "fx:USD/CAD", "4h", intercept=-1.0)
    config = _ibkr_config(tmp_path, incumbent)
    transport = StubTwsTransport(account=IBKR_ACCOUNT)
    venue = IbkrSuccessionVenue.from_config(
        config, client_factory=lambda _profile: transport)
    try:
        runner = venue.runner
        seat = seat_contract_from_runner_config(config)

        facts = venue.fetch_facts()
        assert facts.venue == "ibkr_paper"
        assert facts.account_fingerprint == IBKR_FINGERPRINT
        assert facts.cash == 249500.0 and facts.equity == 250000.0
        assert facts.flat is True
        assert facts.source == "ibkr_paper:tws:v1"

        identity = venue.bind_executor()
        assert identity["executor"] == "app.ibkr_l1_outbox.L1OutboxConsumer"

        drained = venue.drain_for_succession(
            reason="test", incumbent_session_id="s", now=now,
            successor_artifact_sha256=challenger["artifact_sha256"])
        assert drained[0]["emitted"] == []       # nothing to flatten
        assert not [call for call in transport.calls
                    if call[0] == "place_order"]

        bars = venue.historical_closed_bars()
        _seed_seat(runner.olap, seat, incumbent, IBKR_FINGERPRINT,
                   [bar["time"] for bar in bars[-3:]], now)
        candidate = _candidate(challenger, "fx:USD/CAD", "4h")
        result = _promote(runner.olap, venue, seat, candidate, incumbent,
                          tmp_path, now)
        assert result["state"] == "promoted"
        assert not [call for call in transport.calls
                    if call[0] == "place_order"]
        manifest = json.loads(Path(seat.manifest_file).read_bytes())
        assert manifest["model_id"] == "usdcad-challenger-v2"
        row = runner.olap._con.execute(
            "SELECT model_id, starting_balance FROM live_model_sessions"
            " WHERE state='active'").fetchone()
        assert row == ("usdcad-challenger-v2", 249500.0)
    finally:
        venue.close()


# ── MT5 Demo ───────────────────────────────────────────────────────────

MT5_ACCOUNT = "0123456789abcdef01234567"


def _mt5_config(tmp_path: Path, incumbent: dict, *, positions=()) -> dict:
    from app.mt5_bridge_lab import SnapshotPayload
    from app.mt5_execution_bridge import Mt5ExecutionStore

    database = tmp_path / "mt5.sqlite"
    bridge_path = tmp_path / "bridge.json"
    _json(bridge_path, {
        "schema": "lts.mt5.execution_bridge_config.v2",
        "environment": "demo", "execution_enabled": True,
        "database_path": str(database), "secret_env": "TEST_MT5_SECRET",
        "bind_host": "127.0.0.1", "port": 8766,
        "account_fingerprint": MT5_ACCOUNT, "allowed_symbols": ["ETHUSD"],
        "max_volume": 0.01, "max_open_commands_per_day": 4,
    })
    now = datetime.now(timezone.utc).replace(microsecond=0)
    bars = [
        {"symbol": "ETHUSD", "timeframe": "4h",
         "time": (now - timedelta(hours=4 * (70 - index))).isoformat(),
         "open": 1900.0 + index * 0.2, "high": 1902.0 + index * 0.2,
         "low": 1898.0 + index * 0.2, "close": 1901.0 + index * 0.2,
         "volume": 1000 + index}
        for index in range(70)
    ]
    # the TERMINAL posts this; the bridge store is the real interface
    store = Mt5ExecutionStore(database)
    store.record_snapshot(SnapshotPayload.model_validate({
        "schema": "lts.mt5.snapshot.v1", "account_fingerprint": MT5_ACCOUNT,
        "observed_at": now, "currency": "USD", "balance": 9998.82,
        "equity": 9998.85, "margin": 42.46, "free_margin": 9956.39,
        "positions": list(positions), "orders": [], "bars": bars,
        "symbols": [{
            "symbol": "ETHUSD", "bid": 1958.0, "ask": 1960.0,
            "point": 0.01, "volume_min": 0.01, "volume_max": 65,
            "volume_step": 0.01, "trade_mode": 4, "observed_at": now,
        }],
    }))
    store.close()
    manifest = _manifest(tmp_path, incumbent, "crypto:ETHUSD", "4h")
    return {
        "schema": "lts.mt5.model_runner.v1",
        "bridge_config_file": str(bridge_path),
        "model": {"manifest_file": str(manifest),
                  "expected_asset_id": "crypto:ETHUSD",
                  "expected_timeframe": "4h",
                  "execution_tier": "demo_research_canary"},
        "route": {"symbol": "ETHUSD", "timeframe": "4h"},
        "strategy": {"stop_fraction": 0.01, "take_profit_fraction": 0.02},
        "snapshot_max_age_seconds": 600, "loop_seconds": 15,
        "heartbeat_path": str(tmp_path / "heartbeat.json"),
        "service": {
            "venue": "mt5_demo", "account_fingerprint": MT5_ACCOUNT,
            "environment": "demo", "database_path": str(database),
            "risk_fraction_at_stop": 0.00002, "max_overshoot_ratio": 0.5,
            "gross_notional_fraction_max": 0.003,
            "margin_fraction_max": 0.003,
            "daily_loss_budget_fraction": 0.00008,
            "max_concurrent_positions": 1, "signal_max_age_seconds": 28800,
            "owner_issuer_allowlist": ["owner"], "command_phrases": {},
            "asset_instrument_bindings": {"crypto:ETHUSD": "ETHUSD"},
        },
    }


def test_mt5_demo_succession_end_to_end(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    incumbent = _linear_model(tmp_path, "eth-incumbent-v1",
                              "crypto:ETHUSD", "4h", intercept=1.0)
    challenger = _linear_model(tmp_path, "eth-challenger-v2",
                               "crypto:ETHUSD", "4h", intercept=-1.0)
    config = _mt5_config(tmp_path, incumbent)
    venue = Mt5SuccessionVenue.from_config(config)
    try:
        runner = venue.runner
        seat = seat_contract_from_runner_config(config)

        facts = venue.fetch_facts()
        assert facts.venue == "mt5_demo"
        assert facts.account_fingerprint == MT5_ACCOUNT
        assert facts.cash == 9998.82 and facts.equity == 9998.85
        assert facts.flat is True
        assert facts.source == "mt5_demo:execution_bridge:v2"
        assert facts.instrument_capability["tradeable"] is True

        identity = venue.bind_executor()
        assert identity["executor"] == (
            "app.mt5_execution_bridge.Mt5ExecutionStore")

        drained = venue.drain_for_succession(
            reason="test", incumbent_session_id="s", now=now,
            successor_artifact_sha256=challenger["artifact_sha256"])
        assert drained[0]["commands"] == []      # already flat
        assert runner.bridge_store.command_counts() == {}

        bars = venue.historical_closed_bars()
        _seed_seat(runner.l0, seat, incumbent, MT5_ACCOUNT,
                   [bar["time"] for bar in bars[-3:]], now)
        candidate = _candidate(challenger, "crypto:ETHUSD", "4h")
        result = _promote(runner.l0, venue, seat, candidate, incumbent,
                          tmp_path, now)
        assert result["state"] == "promoted"
        assert runner.bridge_store.command_counts() == {}
        manifest = json.loads(Path(seat.manifest_file).read_bytes())
        assert manifest["model_id"] == "eth-challenger-v2"
        row = runner.l0._con.execute(
            "SELECT model_id, starting_balance FROM live_model_sessions"
            " WHERE state='active'").fetchone()
        assert row == ("eth-challenger-v2", 9998.82)
    finally:
        venue.close()


def test_mt5_demo_drain_queues_one_durable_close_when_not_flat(tmp_path):
    """The MT5 drain is the bridge's own close command — never a raw
    terminal call, never an opening order."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    incumbent = _linear_model(tmp_path, "eth-incumbent-v1",
                              "crypto:ETHUSD", "4h", intercept=1.0)
    config = _mt5_config(tmp_path, incumbent, positions=[{
        "ticket": "1", "symbol": "ETHUSD", "side": "buy", "volume": 0.01,
        "price_open": 1900.0, "stop_loss": 1880.0, "take_profit": 1940.0,
        "profit": 1.0,
    }])
    venue = Mt5SuccessionVenue.from_config(config)
    try:
        facts = venue.fetch_facts()
        assert facts.flat is False and len(facts.positions) == 1
        venue.bind_executor()
        drained = venue.drain_for_succession(
            reason="owner_promotion:x", incumbent_session_id="session-1",
            successor_artifact_sha256="3" * 64, now=now)
        commands = drained[0]["commands"]
        assert len(commands) == 1
        row = venue.runner.bridge_store.connection.execute(
            "SELECT action, symbol FROM execution_commands"
            " WHERE command_id=?",
            (commands[0]["command_id"],)).fetchone()
        assert (row["action"], row["symbol"]) == ("close", "ETHUSD")
    finally:
        venue.close()


def test_a_venue_refuses_to_drain_without_a_bound_executor(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    incumbent = _linear_model(tmp_path, "eth-incumbent-v1",
                              "crypto:ETHUSD", "4h", intercept=1.0)
    venue = Mt5SuccessionVenue.from_config(_mt5_config(tmp_path, incumbent))
    try:
        with pytest.raises(Exception, match="executor was never bound"):
            venue.drain_for_succession(
                reason="x", incumbent_session_id="s",
                successor_artifact_sha256="3" * 64, now=now)
    finally:
        venue.close()
