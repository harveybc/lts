"""Forecast-versus-naive eligibility gate for the heuristic strategy (owner order b327b771 s5).

The heuristic strategy may consume learned predictions only when, on held-out
validation or chronological out-of-fold evidence, finite model MAE is strictly
below the same-row persistence MAE for EVERY consumed horizon of BOTH families.
Failure records SKIPPED_NOT_BETTER_THAN_NAIVE and invokes the strategy zero times,
proved here through the real ``run_heartbeat_cycle``.
"""
import copy
import json
import math
from unittest.mock import AsyncMock, patch

import pytest

from app.forecast_naive_gate import (
    ELIGIBLE, SKIPPED, consumption_from, evaluate, seal,
)

ROWS = "a" * 64
SCALER_SHORT = "eurusd-1h-train-standard:" + "b" * 64
SCALER_LONG = "eurusd-1d-train-standard:" + "d" * 64


def _horizon(h, model_mae, naive_mae, *, rows=500):
    def pair(m, n):
        if not all(isinstance(x, float) and math.isfinite(x) for x in (m, n)):
            return {"skill": None, "delta": None, "status": "NOT_AVAILABLE", "reason": "missing or nonfinite metric"}
        if n == 0:
            return {"skill": None, "delta": m - n, "status": "NOT_AVAILABLE", "reason": "ZERO_NAIVE"}
        return {"skill": 1 - m / n, "delta": m - n, "status": "OK"}
    entry = {"horizon": h, "rows": rows, "model_MAE": model_mae, "naive_MAE": naive_mae,
             "model_MSE": model_mae ** 2 if math.isfinite(model_mae) else None,
             "naive_MSE": naive_mae ** 2 if math.isfinite(naive_mae) else None}
    entry["MAE"] = pair(float(model_mae), float(naive_mae))
    entry["MSE"] = pair(float(model_mae) ** 2, float(naive_mae) ** 2)
    return entry


def _record(per_horizon, *, sample_hours, scaler, model="m" * 64, rows=500):
    """One predictor.forecast_naive_evidence.v1 record (M04 de164a09 format)."""
    return seal({
        "schema": "predictor.forecast_naive_evidence.v1",
        "frozen_metric": {"primary": "MAE", "secondary": "MSE",
                          "frozen": "declared before selection; never switched after seeing results"},
        "artifact": {"candidate_cid": "c" * 64, "model_sha256": model, "weights_sha256": "w" * 64,
                     "predictor_revision": "r" * 40, "campaign_id": "test"},
        "population": {"dataset_id": "data-gov:fx:eurusd", "asset": "EURUSD", "targets": ["CLOSE"],
                       "rows": rows, "row_ids_sha256": ROWS, "first_origin": "2024-01-01T00:00:00+00:00",
                       "last_origin": "2024-06-30T23:00:00+00:00", "sample_hours": sample_hours,
                       "horizon_unit": f"steps of {sample_hours} h", "validation_sha256": "v" * 64},
        "scale": {"metric_space": "z_train", "scaler_identity": scaler,
                  "reduction": "mean over rows x targets of absolute / squared error, per horizon"},
        "split": {"provenance": "held_out_validation", "test_used": False, "reserved_trading_test": False},
        "naive": {"definition": "persistence"},
        "per_horizon": per_horizon})


def _evidence(short=None, long=None):
    short = short or [_horizon(h, 0.10 * h, 0.12 * h) for h in range(1, 7)]
    long = long or [_horizon(h, 0.40 * h, 0.50 * h) for h in range(1, 7)]
    return {"short_term": _record(short, sample_hours=1.0, scaler=SCALER_SHORT, model="s" * 64),
            "long_term": _record(long, sample_hours=24.0, scaler=SCALER_LONG, model="l" * 64)}


PREDICTIONS = {"status": "success", "predictions": {
    "short_term": [1.10, 1.101, 1.102, 1.103, 1.104, 1.105],
    "long_term": [1.10, 1.105, 1.11, 1.115, 1.12, 1.125]},
    "historical_context": {"data": [{"CLOSE": 1.10}]}}


def _declared(evidence=None, **family_overrides):
    """The strategy's declared consumption: asset, period, scale and horizon mapping."""
    evidence = evidence or _evidence()
    declared = {"asset": "EURUSD", "families": {}}
    for family, hours, scaler, model in (("short_term", 1.0, SCALER_SHORT, "s" * 64),
                                         ("long_term", 24.0, SCALER_LONG, "l" * 64)):
        declared["families"][family] = {
            "evidence_sha256": evidence[family]["evidence_sha256"], "model_sha256": model,
            "period_hours": hours, "metric_space": "z_train", "scaler_identity": scaler,
            "horizons": [1, 2, 3, 4, 5, 6], **family_overrides.get(family, {})}
    return declared


STRATEGY = {"exit_variant": "E"}


def _consumption(declared=None, predictions=PREDICTIONS):
    return consumption_from("EURUSD", dict(STRATEGY, forecast_evidence=declared or _declared()), predictions)


def _reasons(decision):
    return {r["reason"] for r in decision["failures"]}


# ------------------------------------------------------------ the eight required decisions


def test_genuinely_passing_configuration_is_eligible():
    decision = evaluate(_evidence(), _consumption())
    assert decision["status"] == ELIGIBLE and decision["failures"] == []
    assert len(decision["horizons"]) == 12
    row = decision["horizons"][0]
    assert row["mae"]["baseline"] == pytest.approx(0.12) and row["mae"]["delta"] < 0
    assert row["mae"]["skill"] == pytest.approx(1 - 0.10 / 0.12)
    assert row["mse"]["baseline"] == pytest.approx(0.0144) and math.isfinite(row["mse"]["skill"])
    assert decision["provenance"] == {"short_term": "held_out_validation", "long_term": "held_out_validation"}


def test_one_failing_short_horizon_skips():
    short = [_horizon(h, 0.10 * h, 0.12 * h) for h in range(1, 7)]
    short[2] = _horizon(3, 0.40, 0.36)
    evidence = _evidence(short=short)
    decision = evaluate(evidence, _consumption(_declared(evidence)))
    assert decision["status"] == SKIPPED
    assert [(f["family"], f["horizon"], f["reason"]) for f in decision["failures"]] == [
        ("short_term", 3, "not_better_than_naive")]


def test_one_failing_long_horizon_skips():
    long = [_horizon(h, 0.40 * h, 0.50 * h) for h in range(1, 7)]
    long[5] = _horizon(6, 3.1, 3.0)
    evidence = _evidence(long=long)
    decision = evaluate(evidence, _consumption(_declared(evidence)))
    assert decision["status"] == SKIPPED
    assert [(f["family"], f["horizon"]) for f in decision["failures"]] == [("long_term", 6)]


def test_favourable_macro_mean_cannot_mask_a_failing_member():
    long = [_horizon(h, 0.1, 1.0) for h in range(1, 7)]
    long[0] = _horizon(1, 1.01, 1.00)
    evidence = _evidence(long=long)
    decision = evaluate(evidence, _consumption(_declared(evidence)))
    macro = decision["macro"]["long_term"]["mae"]
    assert macro["model"] < macro["baseline"]
    assert decision["status"] == SKIPPED and _reasons(decision) == {"not_better_than_naive"}


def test_equality_with_naive_is_not_a_pass():
    short = [_horizon(h, 0.10 * h, 0.12 * h) for h in range(1, 7)]
    short[0] = _horizon(1, 0.12, 0.12)
    evidence = _evidence(short=short)
    decision = evaluate(evidence, _consumption(_declared(evidence)))
    assert decision["status"] == SKIPPED and _reasons(decision) == {"tie_with_naive"}


def test_zero_naive_rejects_without_infinite_skill():
    short = [_horizon(h, 0.10 * h, 0.12 * h) for h in range(1, 7)]
    short[1] = _horizon(2, 0.0, 0.0)
    evidence = _evidence(short=short)
    decision = evaluate(evidence, _consumption(_declared(evidence)))
    assert decision["status"] == SKIPPED and _reasons(decision) == {"naive_zero"}
    row = [r for r in decision["horizons"] if r["family"] == "short_term" and r["horizon"] == 2][0]
    assert row["mae"]["skill"] == "NOT_AVAILABLE" and "zero" in row["mae"]["skill_reason"].lower()
    json.dumps(decision, allow_nan=False)  # no inf/nan anywhere in the record


@pytest.mark.parametrize("damage", ["missing_horizon", "nan_model", "null_naive", "missing_key"])
def test_missing_or_nan_metric_skips(damage):
    short = [_horizon(h, 0.10 * h, 0.12 * h) for h in range(1, 7)]
    if damage == "missing_horizon":
        short = short[:5]
    elif damage == "nan_model":
        short[4] = _horizon(5, float("nan"), 0.6)
        short[4]["model_MAE"] = None  # the producer writes null, never NaN
    elif damage == "null_naive":
        short[4]["naive_MAE"] = None
        short[4]["MAE"] = {"skill": None, "delta": None, "status": "NOT_AVAILABLE",
                           "reason": "missing or nonfinite metric"}
    else:
        del short[4]["model_MAE"]
    evidence = _evidence(short=short)
    decision = evaluate(evidence, _consumption(_declared(evidence)))
    assert decision["status"] == SKIPPED and decision["failures"]
    assert _reasons(decision) <= {"missing_metric", "non_finite_metric"}


def test_nan_written_into_a_record_is_refused():
    evidence = _evidence()
    evidence["short_term"]["per_horizon"][0]["model_MAE"] = float("nan")
    decision = evaluate(evidence, _consumption())
    assert decision["status"] == SKIPPED
    assert _reasons(decision) & {"non_finite_metric", "evidence_digest_mismatch"}


@pytest.mark.parametrize("what", ["horizon_rows", "scaler", "metric_space", "period"])
def test_mismatched_rows_scaler_or_period_skips(what):
    evidence = _evidence()
    declared = _declared(evidence)
    expected = {"horizon_rows": "rows_mismatch", "scaler": "scaler_mismatch",
                "metric_space": "scaler_mismatch", "period": "period_mismatch"}[what]
    if what == "horizon_rows":
        short = [_horizon(h, 0.10 * h, 0.12 * h) for h in range(1, 7)]
        short[0]["rows"] = 499  # one horizon scored on a different row set
        evidence = _evidence(short=short)
        declared = _declared(evidence)
    elif what == "scaler":
        declared["families"]["short_term"]["scaler_identity"] = "other:" + "e" * 64
    elif what == "metric_space":
        declared["families"]["long_term"]["metric_space"] = "price"
    else:
        declared["families"]["long_term"]["period_hours"] = 1.0
    decision = evaluate(evidence, _consumption(declared))
    assert decision["status"] == SKIPPED and _reasons(decision) == {expected}


def test_receipt_carries_every_baseline_the_evidence_holds():
    evidence = _evidence()
    decision = evaluate(evidence, _consumption())
    for family in ("short_term", "long_term"):
        block = decision["baselines"][family]
        assert block["eligibility_baseline"] == "persistence"
        assert block["persistence"]["definition"] == "persistence"
        assert block["seasonal_naive"]["status"] == "NOT_AVAILABLE"
    assert decision["horizons"][0]["seasonal_naive"]["status"] == "NOT_AVAILABLE"
    short = [_horizon(h, 0.10 * h, 0.12 * h) for h in range(1, 7)]
    for entry in short[:5]:  # M04 b5ed0982 nested form; better than the model: reported only
        value = 0.05 * entry["horizon"]
        entry["seasonal_naive"] = {"naive_MAE": value, "naive_MSE": value ** 2,
                                   "MAE": {"skill": None, "delta": None, "status": "OK"}, "MSE": {}}
    short[5]["seasonal_naive"] = {"status": "NOT_AVAILABLE",
                                  "reason": "target time minus one period is not inside the input window"}
    record = _record(short, sample_hours=1.0, scaler=SCALER_SHORT, model="s" * 64)
    record["seasonal_naive"] = {"period_steps": 24, "declared": True, "definition": "value one period earlier"}
    evidence = dict(_evidence(), short_term=seal(record))
    decision = evaluate(evidence, _consumption(_declared(evidence)))
    assert decision["status"] == ELIGIBLE  # eligibility stays against persistence, as written
    assert decision["baselines"]["short_term"]["seasonal_naive"]["period_steps"] == 24
    seasonal = decision["horizons"][0]["seasonal_naive"]
    assert seasonal["mae"]["baseline"] == pytest.approx(0.05) and seasonal["mae"]["delta"] > 0
    assert seasonal["used_for_eligibility"] is False
    assert decision["horizons"][5]["seasonal_naive"]["reason"].startswith("target time minus one period")
    long_row = [r for r in decision["horizons"] if r["family"] == "long_term"][0]
    assert long_row["seasonal_naive"]["status"] == "NOT_AVAILABLE"


# ------------------------------------------------------------ provenance, identity, freezing


@pytest.mark.parametrize("split", ["reserved_trading_test", "test", "unknown", None])
def test_trading_test_or_unknown_provenance_is_refused(split):
    evidence = _evidence()
    evidence["long_term"]["split"]["provenance"] = split
    evidence["long_term"] = seal(evidence["long_term"])
    decision = evaluate(evidence, _consumption(_declared(evidence)))
    assert decision["status"] == SKIPPED and "provenance_not_admissible" in _reasons(decision)
    assert decision["provenance"]["long_term"] == split


def test_test_used_flag_is_refused():
    evidence = _evidence()
    evidence["short_term"]["split"]["test_used"] = True
    evidence["short_term"] = seal(evidence["short_term"])
    assert "provenance_not_admissible" in _reasons(evaluate(evidence, _consumption(_declared(evidence))))


def test_metric_switch_tampering_identity_and_asset_are_refused():
    evidence = _evidence()
    evidence["short_term"]["frozen_metric"]["primary"] = "MSE"
    evidence["short_term"] = seal(evidence["short_term"])
    assert "primary_metric_not_mae" in _reasons(evaluate(evidence, _consumption(_declared(evidence))))
    tampered = _evidence()
    declared = _declared(tampered)
    tampered["short_term"]["per_horizon"][0]["model_MAE"] = 0.0
    assert "evidence_digest_mismatch" in _reasons(evaluate(tampered, _consumption(declared)))
    swapped = _evidence()
    declared = _declared(swapped, short_term={"model_sha256": "x" * 64})
    assert "candidate_mismatch" in _reasons(evaluate(swapped, _consumption(declared)))
    wrong = consumption_from("USDJPY", dict(STRATEGY, forecast_evidence=_declared()), PREDICTIONS)
    assert "asset_mismatch" in _reasons(evaluate(_evidence(), wrong))
    stale = _declared(short_term={"evidence_sha256": "0" * 64})
    assert "evidence_not_declared_record" in _reasons(evaluate(_evidence(), _consumption(stale)))


def test_consumed_horizons_come_from_the_strategy_inputs_and_none_are_dropped():
    consumption = _consumption()
    assert consumption["families"]["short_term"]["consumed"] == 6
    assert consumption["families"]["long_term"]["consumed"] == 6
    more = copy.deepcopy(PREDICTIONS)
    more["predictions"]["long_term"].append(1.13)  # a 7th consumed long prediction
    with pytest.raises(ValueError, match="consumes 7"):
        _consumption(predictions=more)
    undeclared = _declared()
    del undeclared["families"]["short_term"]["horizons"]
    with pytest.raises(ValueError, match="mapping"):
        _consumption(undeclared)
    # Mapping the long family onto horizons the record lacks fails; nothing is dropped.
    off_grid = _declared(long_term={"horizons": [24, 48, 72, 96, 120, 144]})
    decision = evaluate(_evidence(), _consumption(off_grid))
    assert decision["status"] == SKIPPED
    assert {(f["family"], f["reason"]) for f in decision["failures"]} == {("long_term", "missing_metric")}
    assert len([f for f in decision["failures"] if f["family"] == "long_term"]) == 6


# ------------------------------------------------------------ the actual runner


async def _run_cycle(tmp_path, evidence, *, declared=None, configure=True):
    from app.database import Asset, Database, Order, Portfolio, User
    from app.heartbeat import run_heartbeat_cycle
    import plugins_strategy.heuristic_strategy as heuristic

    strategy = dict(STRATEGY)
    if configure:
        declared = declared or _declared(evidence)
        for family, record in evidence.items():
            path = tmp_path / f"{family}.json"
            path.write_text(json.dumps(record))
            declared["families"][family]["file"] = str(path)
        strategy["forecast_evidence"] = declared
    db = Database(":memory:")
    await db.initialize()
    async with db.get_session() as session:
        user = User(username="gate", email="g@example.com", password_hash="x", role="user", is_active=True)
        session.add(user)
        await session.flush()
        portfolio = Portfolio(user_id=user.id, name="gate", is_active=True, total_capital=10000)
        session.add(portfolio)
        await session.flush()
        session.add(Asset(portfolio_id=portfolio.id, symbol="EURUSD", is_active=True,
                          allocated_capital=5000, strategy_config=json.dumps(strategy)))
    calls = []
    real = heuristic.compute_signal

    def counting(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    with patch("app.heartbeat.PredictionProviderClient") as client, \
            patch.object(heuristic, "compute_signal", counting):
        client.return_value.get_predictions = AsyncMock(return_value=copy.deepcopy(PREDICTIONS))
        result = await run_heartbeat_cycle({"csv_test_mode": False}, db)
    async with db.get_session() as session:
        from sqlalchemy import func, select
        orders = (await session.execute(select(func.count()).select_from(Order))).scalar_one()
    return result, calls, orders


@pytest.mark.asyncio
async def test_failing_gate_invokes_the_strategy_zero_times_at_the_actual_runner(tmp_path):
    long = [_horizon(h, 0.40 * h, 0.50 * h) for h in range(1, 7)]
    long[3] = _horizon(4, 3.0, 2.0)
    result, calls, orders = await _run_cycle(tmp_path, _evidence(long=long))
    assert calls == [] and orders == 0
    assert result["signals_generated"] == 0 and result["orders_placed"] == 0
    [skip] = result["forecast_gate"]
    assert skip["status"] == SKIPPED and skip["asset"] == "EURUSD"
    assert [(f["family"], f["horizon"]) for f in skip["failures"]] == [("long_term", 4)]
    assert skip["provenance"] == {"short_term": "held_out_validation", "long_term": "held_out_validation"}


@pytest.mark.asyncio
async def test_missing_evidence_also_invokes_the_strategy_zero_times(tmp_path):
    result, calls, orders = await _run_cycle(tmp_path, {}, configure=False)
    assert calls == [] and orders == 0 and result["orders_placed"] == 0
    assert result["forecast_gate"][0]["failures"][0]["reason"] == "evidence_not_configured"


@pytest.mark.asyncio
async def test_unreadable_or_substituted_evidence_file_invokes_zero_times(tmp_path):
    evidence = _evidence()
    declared = _declared(evidence)
    good = _evidence()
    bad = copy.deepcopy(good)
    bad["short_term"] = _record([_horizon(h, 0.5, 0.1) for h in range(1, 7)],
                                sample_hours=1.0, scaler=SCALER_SHORT, model="s" * 64)
    result, calls, orders = await _run_cycle(tmp_path, bad, declared=declared)
    assert calls == [] and orders == 0
    assert "evidence_not_declared_record" in {f["reason"] for f in result["forecast_gate"][0]["failures"]}


@pytest.mark.asyncio
async def test_passing_gate_lets_the_actual_runner_invoke_the_strategy(tmp_path):
    result, calls, _ = await _run_cycle(tmp_path, _evidence())
    assert len(calls) == 1 and result["signals_generated"] == 1
    assert result["forecast_gate"][0]["status"] == ELIGIBLE
    assert math.isfinite(result["forecast_gate"][0]["horizons"][0]["mae"]["skill"])
