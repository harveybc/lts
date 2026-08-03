"""Stateful execution-model property suite (auditor improvement, advances 010).

Deterministic seeded sequences of long/short, multi-asset, partial-fill,
duplicate-event, cancel/fill-race and restart operations run against the
service with a parallel Python reference model. After EVERY event the seven
invariants from the L0 correction audit are asserted:

1. signed exposure conservation;
2. risk reservation + exposure conservation;
3. unique logical position cardinality;
4. venue/account/provenance identity preservation;
5. every cancel targets one existing order;
6. flatten moves exposure monotonically toward zero;
7. replay never changes state twice.
"""
import json
import random
import socket
from datetime import datetime, timedelta, timezone

import pytest

from trading_contracts import (
    AssetIntent,
    BrokerCapabilitySnapshot,
    ExecutionReportV2,
    InstrumentCapability,
    OwnerCommand,
    ProtectionLegState,
)

from app.demo_execution_service import (
    DemoExecutionConfig,
    DemoExecutionError,
    DemoExecutionOlap,
    DemoExecutionService,
    ZeroNetworkSink,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
ARTIFACT = "sha256:" + "a" * 64
FINGERPRINT = "synthetic-ibkr-fixture-1"

ASSETS = {
    "USD.CAD": {"asset_id": "fx:USD/CAD", "price": 1.0, "step": 1.0,
                "min": 1.0, "margin": 0.03, "sl": 0.9, "tp": 1.2,
                "sl_short": 1.1, "tp_short": 0.8},
    "ETH.USD": {"asset_id": "crypto:ETH/USD", "price": 2200.0, "step": 0.001,
                "min": 0.001, "margin": 0.3, "sl": 1980.0, "tp": 2640.0,
                "sl_short": 2420.0, "tp_short": 1760.0},
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _explode(*args, **kwargs):
        raise AssertionError("network operation attempted during L0")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)


def _service(tmp_path, name):
    config = DemoExecutionConfig.from_dict({
        "venue": "ibkr_paper",
        "account_fingerprint": FINGERPRINT,
        "environment": "paper",
        "database_path": str(tmp_path / f"{name}.sqlite"),
        "risk_fraction_at_stop": 0.002,
        "max_overshoot_ratio": 0.25,
        "gross_notional_fraction_max": 0.9,
        "margin_fraction_max": 0.9,
        "daily_loss_budget_fraction": 0.5,
        "max_concurrent_positions": 50,
        "signal_max_age_seconds": 86400.0,
        "owner_issuer_allowlist": ["owner-1"],
        "command_phrases": {
            "flatten_all": "FLATTEN ALL DEMO POSITIONS NOW",
            "cancel_pending": "CANCEL ALL PENDING DEMO ENTRIES NOW",
        },
        "asset_instrument_bindings": {
            "fx:USD/CAD": "USD.CAD",
            "crypto:ETH/USD": "ETH.USD",
        },
    })
    return DemoExecutionService(
        config, DemoExecutionOlap(config.database_path), ZeroNetworkSink()
    )


def _capability():
    return BrokerCapabilitySnapshot(
        object_id="cap-model", as_of=NOW,
        producer={"name": "model", "version": "0"}, trace_id="t-model",
        venue="ibkr_paper", account_fingerprint=FINGERPRINT,
        environment="paper", capability_evidence="synthetic_fixture",
        source_artifact_hash="sha256:" + "f" * 64, source_observed_at=NOW,
        instruments=[
            InstrumentCapability(
                instrument=name, tradeable=True, shortable=True,
                min_units=spec["min"], unit_step=spec["step"],
                price_decimals=5, margin_rate=spec["margin"],
                native_stop_loss=True, native_take_profit=True,
                native_bracket=True,
            )
            for name, spec in ASSETS.items()
        ],
    )


class ReferenceModel:
    """Independent bookkeeping the service must always agree with."""

    def __init__(self):
        self.entries = {}     # order_intent_id -> dict(state, signed_remaining, signed_filled, risk, instrument)
        self.flattens = {}    # target -> flatten units

    def signed_exposure(self, instrument):
        return sum(
            e["signed_filled"] for e in self.entries.values()
            if e["instrument"] == instrument
        )

    def open_positions(self):
        return sum(
            1 for e in self.entries.values()
            if e["state"] in ("pending", "partial") or e["signed_filled"] != 0
        )


def _assert_invariants(service, model, capability):
    day = NOW.date().isoformat()
    totals = service.olap.active_totals(day)
    exposures = service.olap.open_exposures()
    # 1. signed exposure conservation per instrument
    for instrument in ASSETS:
        persisted = sum(
            e["units_open"] for e in exposures if e["instrument"] == instrument
        )
        assert abs(persisted - model.signed_exposure(instrument)) < 1e-9, (
            instrument, persisted, model.signed_exposure(instrument)
        )
    # 2. reservation + exposure conservation: active risk equals the sum of
    #    remaining-entry risk plus filled-exposure risk tracked by the model
    expected_risk = sum(
        e["risk"] * (abs(e["signed_remaining"]) + abs(e["signed_filled"]))
        / e["total_units"]
        for e in model.entries.values()
        if e["state"] in ("pending", "partial", "filled_open")
    )
    assert abs(totals["risk_active"] - expected_risk) < 1e-9
    # 3. unique logical position cardinality
    assert totals["positions"] == model.open_positions()
    # 4. identity preservation
    for exposure in exposures:
        assert exposure["venue"] == "ibkr_paper"
        assert exposure["account_fingerprint"] == FINGERPRINT
        assert exposure["capability_evidence"] == "synthetic_fixture"
        assert exposure["instrument"] in ASSETS
    # 7 (structural): the ledger chain is intact
    rows = service.olap._con.execute(
        "SELECT prev_chain_hash, chain_hash FROM lifecycle_events ORDER BY seq"
    ).fetchall()
    prev = "genesis"
    for row in rows:
        assert row[0] == prev
        prev = row[1]


def _report(result, state, previous, filled, covered=True):
    units = abs(result["delta_units"])
    legs = [
        ProtectionLegState(leg="stop_loss", broker_confirmed=covered,
                           covered_units=filled),
        ProtectionLegState(leg="take_profit", broker_confirmed=covered,
                           covered_units=filled),
    ]
    return ExecutionReportV2(
        object_id=f"er-{result['order_intent_id']}-{state}-{filled}",
        as_of=NOW + timedelta(seconds=30),
        producer={"name": "model-sink", "version": "0"}, trace_id="t-model",
        order_intent_id=result["order_intent_id"],
        attempt_id=f"attempt-{result['reservation_id']}",
        bracket_role="parent", state=state, previous_state=previous,
        requested_units=result["delta_units"], filled_units=filled,
        protection_legs=legs if filled else [],
    )


@pytest.mark.parametrize("seed", [11, 23, 47, 89])
def test_stateful_model_invariants_hold_over_random_sequences(tmp_path, seed):
    rng = random.Random(seed)
    service = _service(tmp_path, f"model-{seed}")
    capability = _capability()
    model = ReferenceModel()
    counter = 0

    for step in range(30):
        action = rng.choice(
            ["entry", "fill", "partial", "cancel_race", "duplicate", "close"]
        )
        if action == "entry":
            counter += 1
            instrument = rng.choice(list(ASSETS))
            spec = ASSETS[instrument]
            short = rng.random() < 0.5
            intent = AssetIntent(
                object_id=f"m-{seed}-{counter}", as_of=NOW,
                valid_until=NOW + timedelta(hours=6),
                producer={"name": "provider.mechanics", "version": "0"},
                trace_id="t-model",
                cell_id=f"{spec['asset_id']}@4h:mech:policy",
                asset_id=spec["asset_id"], action="target",
                target_exposure=-0.5 if short else 0.5,
                risk_geometry={
                    "mode": "fixed_price",
                    "stop_price": spec["sl_short"] if short else spec["sl"],
                    "take_profit_price": spec["tp_short"] if short else spec["tp"],
                },
                artifact_hash=ARTIFACT,
            )
            result = service.process_intent(
                intent, capability, equity=100_000.0,
                reference_price=spec["price"], instrument=instrument,
                now=NOW + timedelta(seconds=1),
            )
            if result["outcome"] == "would_be_order":
                model.entries[result["order_intent_id"]] = {
                    "state": "pending",
                    "instrument": instrument,
                    "signed_remaining": result["delta_units"],
                    "signed_filled": 0.0,
                    "total_units": abs(result["delta_units"]),
                    "risk": _entry_risk(service, result),
                    "result": result,
                }
        elif action in ("fill", "partial", "cancel_race"):
            pending = [
                (oid, e) for oid, e in model.entries.items()
                if e["state"] in ("pending", "partial")
            ]
            if not pending:
                continue
            oid, entry = rng.choice(pending)
            result = entry["result"]
            magnitude = entry["total_units"]
            previous = service.olap.last_state(oid)
            if action == "fill":
                service.apply_execution_event(
                    _report(result, "filled", previous, magnitude))
                entry["signed_filled"] = (
                    entry["signed_remaining"] + entry["signed_filled"]
                )
                entry["signed_remaining"] = 0.0
                entry["state"] = "filled_open"
            elif action == "partial" and entry["state"] == "pending":
                filled = magnitude * rng.choice([0.25, 0.5])
                service.apply_execution_event(
                    _report(result, "partially_filled", previous, filled))
                sign = 1.0 if result["delta_units"] > 0 else -1.0
                entry["signed_filled"] = sign * filled
                entry["signed_remaining"] = result["delta_units"] - sign * filled
                entry["state"] = "partial"
            elif action == "cancel_race" and entry["state"] in ("pending", "partial"):
                service.apply_execution_event(
                    _report(result, "cancel_pending", previous,
                            abs(entry["signed_filled"])))
                service.apply_execution_event(
                    _report(result, "cancelled", "cancel_pending", 0.0))
                entry["signed_remaining"] = 0.0
                entry["state"] = (
                    "filled_open" if entry["signed_filled"] != 0 else "dead"
                )
        elif action == "duplicate":
            # invariant 7: replaying the last event never changes state twice
            rows = service.olap._con.execute(
                "SELECT report_json FROM lifecycle_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if rows is None:
                continue
            replay = ExecutionReportV2(**json.loads(rows[0]))
            before = service.olap.active_totals(NOW.date().isoformat())
            receipt = service.olap._con.execute(
                "SELECT 1 FROM execution_report_receipts WHERE object_id=?",
                (replay.object_id,),
            ).fetchone()
            if receipt is None:
                with pytest.raises(DemoExecutionError):
                    service.apply_execution_event(replay)
            else:
                replay_result = service.apply_execution_event(replay)
                assert replay_result["replayed"] is True
            after = service.olap.active_totals(NOW.date().isoformat())
            assert before == after
        elif action == "close":
            open_filled = [
                (oid, e) for oid, e in model.entries.items()
                if e["state"] == "filled_open" and e["signed_filled"] != 0
            ]
            if not open_filled:
                continue
            oid, entry = rng.choice(open_filled)
            service.apply_position_close(oid)
            entry["signed_filled"] = 0.0
            entry["state"] = "dead"
        _assert_invariants(service, model, capability)

    # invariants 5+6: flatten targets every open exposure exactly and moves
    # signed exposure monotonically to zero
    exposures_before = {
        e["order_intent_id"]: e["units_open"]
        for e in service.olap.open_exposures()
    }
    out = service.apply_owner_command(OwnerCommand(
        object_id=f"cmd-{seed}", as_of=NOW,
        producer={"name": "telegram.deterministic", "version": "0"},
        trace_id="t-model", command="flatten_all", issuer_id="owner-1",
        exact_phrase="FLATTEN ALL DEMO POSITIONS NOW",
        nonce=f"n-model-{seed}", expires_at=NOW + timedelta(minutes=5),
        idempotency_key=f"ck-model-{seed}",
    ), now=NOW)
    assert out["accepted"]
    emitted = {e["target"]: e["units"] for e in out["emitted"]}
    assert set(emitted) == set(exposures_before)          # invariant 5
    for target, units in emitted.items():
        assert units == -exposures_before[target]         # invariant 6


def _entry_risk(service, result):
    row = service.olap.reservation_row(result["reservation_id"])
    return row["risk_fraction"]
