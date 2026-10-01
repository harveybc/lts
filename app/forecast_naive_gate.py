"""Forecast-versus-naive eligibility gate for the heuristic strategy.

Owner order (predictor b327b771, section 5): the heuristic strategy must not run
with learned predictions that have not beaten persistence.  This is an
eligibility rule, not a claim that worse MAE implies a trading loss.

Evidence is M04's frozen record ``predictor.forecast_naive_evidence.v1``
(predictor ``tools/modular_forecast_evidence.py``), one record per prediction
family.  The consumer declares, per family, which record (``evidence_sha256``),
which model (``model_sha256``), the period, the scale (``metric_space`` and
``scaler_identity``) and the mapping of the strategy's consumed predictions onto
record horizon numbers.  The gate does not choose that mapping.

The decision is ELIGIBLE only if, for EVERY consumed horizon of BOTH families:

* the record verifies, is the declared record, freezes MAE as primary, and comes
  from held-out validation or chronological out-of-fold evidence (``test_used``
  false; the reserved trading test or an unknown provenance is refused);
* asset, period, scale and scaler identity equal the declaration, and the
  horizon was scored on the population's own row set;
* model and naive MAE are present and finite, naive MAE is not zero, and
  ``model_MAE < naive_MAE`` strictly (a tie fails).

Otherwise the decision is SKIPPED_NOT_BETTER_THAN_NAIVE with every failure,
the forecast scores, the paired baselines and the horizon/population identities.
A favourable mean never masks a failing member and no horizon is dropped.  The
gate does not apply to RL policy admission.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

EVIDENCE_SCHEMA = "predictor.forecast_naive_evidence.v1"
DECISION_SCHEMA = "lts.forecast_naive_gate_decision.v1"
ELIGIBLE = "ELIGIBLE"
SKIPPED = "SKIPPED_NOT_BETTER_THAN_NAIVE"
ADMISSIBLE_PROVENANCE = ("held_out_validation", "chronological_oof")
FAMILIES = ("short_term", "long_term")
NOT_AVAILABLE = "NOT_AVAILABLE"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def evidence_digest(record: Mapping[str, Any]) -> str | None:
    body = {k: v for k, v in record.items() if k != "evidence_sha256"}
    try:
        return hashlib.sha256(_canonical(body).encode()).hexdigest()
    except ValueError:  # NaN/inf inside the record: it cannot be canonical evidence
        return None


def seal(record: dict) -> dict:
    """Recompute ``evidence_sha256`` exactly as the M04 producer does (tests, fixtures)."""
    record = {k: v for k, v in record.items() if k != "evidence_sha256"}
    record["evidence_sha256"] = evidence_digest(record)
    return record


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _paired(model: Any, naive: Any) -> dict[str, Any]:
    """Model metric with its same-row baseline and skill/delta, or NOT_AVAILABLE and why."""
    if not (_finite(model) and _finite(naive)):
        return {"model": model if _finite(model) else None, "baseline": naive if _finite(naive) else None,
                "delta": NOT_AVAILABLE, "skill": NOT_AVAILABLE,
                "skill_reason": "missing or non-finite model or naive metric"}
    paired = {"model": model, "baseline": naive, "delta": model - naive}
    if naive == 0:
        paired.update(skill=NOT_AVAILABLE, skill_reason="ZERO_NAIVE: zero naive error, skill undefined")
    else:
        paired["skill"] = 1.0 - model / naive
    return paired


def consumption_from(symbol: str, strategy_cfg: Mapping[str, Any],
                     predictions: Mapping[str, Any]) -> dict[str, Any]:
    """What the strategy actually consumes, bound to the declared evidence.

    The heartbeat hands the heuristic strategy every short-term and every
    long-term prediction it receives, so each one is a consumed horizon of its
    family.  The declaration must map each of them onto a record horizon; a
    length mismatch or a missing mapping is a configuration error, not a pass.
    """
    declared = (strategy_cfg or {}).get("forecast_evidence")
    lists = (predictions or {}).get("predictions") or {}
    consumption: dict[str, Any] = {"asset": symbol, "declared": declared, "families": {}}
    if not isinstance(declared, Mapping):
        return consumption
    for family in FAMILIES:
        count = len(lists.get(family) or [])
        spec = (declared.get("families") or {}).get(family)
        if not isinstance(spec, Mapping) or not isinstance(spec.get("horizons"), list):
            raise ValueError(f"{family}: the consumed-horizon mapping is not declared")
        mapping = list(spec["horizons"])
        if len(mapping) != count:
            raise ValueError(f"{family}: the strategy consumes {count} predictions but "
                             f"{len(mapping)} horizons are declared")
        consumption["families"][family] = {"consumed": count, "horizons": mapping, "spec": dict(spec)}
    return consumption


def _load(spec: Mapping[str, Any]) -> tuple[dict | None, str | None]:
    path = spec.get("file")
    if not path:
        return None, "evidence_file_not_declared"
    try:
        record = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError):
        return None, "evidence_unreadable"
    return (record, None) if isinstance(record, dict) else (None, "evidence_unreadable")


def evaluate(evidence: Mapping[str, Mapping[str, Any]] | None,
             consumption: Mapping[str, Any]) -> dict[str, Any]:
    """Decide eligibility from per-family records; never raises on bad evidence."""
    failures: list[dict[str, Any]] = []
    horizons: list[dict[str, Any]] = []
    provenance: dict[str, Any] = {}
    population: dict[str, Any] = {}
    macro: dict[str, Any] = {}
    baselines: dict[str, Any] = {}

    def fail(reason, family=None, horizon=None, detail=None):
        failures.append({"reason": reason, "family": family, "horizon": horizon, "detail": detail})

    declared = consumption.get("declared")
    if not isinstance(declared, Mapping):
        fail("evidence_not_configured", detail="strategy_config.forecast_evidence is absent")
    elif declared.get("asset") != consumption.get("asset"):
        fail("asset_mismatch", detail={"declared": declared.get("asset"), "strategy": consumption.get("asset")})
    evidence = evidence or {}
    for family in FAMILIES if isinstance(declared, Mapping) else ():
        plan = consumption["families"].get(family)
        if plan is None:
            fail("family_not_consumed_or_declared", family)
            continue
        spec, record = plan["spec"], evidence.get(family)
        if not isinstance(record, Mapping):
            fail("evidence_missing", family)
            continue
        if record.get("schema") != EVIDENCE_SCHEMA:
            fail("evidence_schema_unsupported", family, detail=record.get("schema"))
            continue
        digest = evidence_digest(record)
        if digest is None or digest != record.get("evidence_sha256"):
            fail("evidence_digest_mismatch", family)
        if record.get("evidence_sha256") != spec.get("evidence_sha256"):
            fail("evidence_not_declared_record", family,
                 detail={"declared": spec.get("evidence_sha256"), "found": record.get("evidence_sha256")})
        if (record.get("frozen_metric") or {}).get("primary") != "MAE":
            fail("primary_metric_not_mae", family, detail=(record.get("frozen_metric") or {}).get("primary"))
        split = record.get("split") or {}
        provenance[family] = split.get("provenance")
        if (split.get("provenance") not in ADMISSIBLE_PROVENANCE or split.get("test_used") is not False
                or split.get("reserved_trading_test") is not False):
            fail("provenance_not_admissible", family, detail=split)
        artifact, pop, scale = record.get("artifact") or {}, record.get("population") or {}, record.get("scale") or {}
        if artifact.get("model_sha256") != spec.get("model_sha256"):
            fail("candidate_mismatch", family,
                 detail={"declared": spec.get("model_sha256"), "evidence": artifact.get("model_sha256")})
        if pop.get("asset") != declared.get("asset"):
            fail("asset_mismatch", family, detail={"declared": declared.get("asset"), "evidence": pop.get("asset")})
        if pop.get("sample_hours") != spec.get("period_hours"):
            fail("period_mismatch", family,
                 detail={"declared": spec.get("period_hours"), "evidence": pop.get("sample_hours")})
        if (scale.get("metric_space") != spec.get("metric_space")
                or scale.get("scaler_identity") != spec.get("scaler_identity")):
            fail("scaler_mismatch", family, detail={
                "declared": [spec.get("metric_space"), spec.get("scaler_identity")],
                "evidence": [scale.get("metric_space"), scale.get("scaler_identity")]})
        population[family] = {k: pop.get(k) for k in (
            "dataset_id", "asset", "rows", "row_ids_sha256", "first_origin", "last_origin",
            "sample_hours", "horizon_unit")}
        population[family].update(scaler_identity=scale.get("scaler_identity"),
                                  metric_space=scale.get("metric_space"),
                                  evidence_sha256=record.get("evidence_sha256"),
                                  model_sha256=artifact.get("model_sha256"))
        seasonal = record.get("seasonal_naive")
        baselines[family] = {
            "eligibility_baseline": "persistence",
            "persistence": {"definition": (record.get("naive") or {}).get("definition")},
            "seasonal_naive": (dict(seasonal) if isinstance(seasonal, Mapping) else {
                "status": NOT_AVAILABLE,
                "reason": "the evidence record declares no seasonal_naive; reported, never used for eligibility"}),
        }
        entries = {e.get("horizon"): e for e in record.get("per_horizon") or [] if isinstance(e, Mapping)}
        sums = {"model": 0.0, "baseline": 0.0, "n": 0}
        for position, horizon in enumerate(plan["horizons"], start=1):
            entry = entries.get(horizon)
            row = {"family": family, "consumed_position": position, "horizon": horizon,
                   "horizon_unit": pop.get("horizon_unit"), "rows": None,
                   "row_ids_sha256": pop.get("row_ids_sha256")}
            if entry is None:
                row.update(mae=_paired(None, None), mse=_paired(None, None))
                horizons.append(row)
                fail("missing_metric", family, horizon, "consumed horizon absent from the record")
                continue
            row["rows"] = entry.get("rows")
            model_mae, naive_mae = entry.get("model_MAE"), entry.get("naive_MAE")
            row.update(mae=_paired(model_mae, naive_mae), mse=_paired(entry.get("model_MSE"), entry.get("naive_MSE")))
            if isinstance(seasonal, Mapping) and ("seasonal_naive_MAE" in entry or "seasonal_naive_MSE" in entry):
                row["seasonal_naive"] = {
                    "mae": _paired(model_mae, entry.get("seasonal_naive_MAE")),
                    "mse": _paired(entry.get("model_MSE"), entry.get("seasonal_naive_MSE")),
                    "used_for_eligibility": False}
            else:
                row["seasonal_naive"] = {"status": NOT_AVAILABLE,
                                         "reason": "no seasonal_naive values for this horizon in the evidence"}
            horizons.append(row)
            if entry.get("rows") != pop.get("rows"):
                fail("rows_mismatch", family, horizon,
                     {"horizon_rows": entry.get("rows"), "population_rows": pop.get("rows")})
            if "model_MAE" not in entry or "naive_MAE" not in entry or model_mae is None or naive_mae is None:
                fail("missing_metric", family, horizon)
                continue
            if not (_finite(model_mae) and _finite(naive_mae)):
                fail("non_finite_metric", family, horizon)
                continue
            sums["model"] += model_mae
            sums["baseline"] += naive_mae
            sums["n"] += 1
            if naive_mae == 0:
                fail("naive_zero", family, horizon, "strict improvement over a zero naive error is impossible")
            elif model_mae == naive_mae:
                fail("tie_with_naive", family, horizon)
            elif not model_mae < naive_mae:
                fail("not_better_than_naive", family, horizon,
                     {"model_MAE": model_mae, "naive_MAE": naive_mae})
        if sums["n"]:
            macro[family] = {"mae": {"model": sums["model"] / sums["n"], "baseline": sums["baseline"] / sums["n"],
                                     "horizons": sums["n"],
                                     "note": "reported only; a favourable mean never overrides a failing member"}}
    decision = {
        "schema": DECISION_SCHEMA, "status": ELIGIBLE if not failures else SKIPPED,
        "asset": consumption.get("asset"), "primary_metric": "MAE", "reported_metrics": ["MAE", "MSE"],
        "rule": "finite model_MAE < naive_MAE on every consumed horizon of both families",
        "provenance": provenance, "population": population, "baselines": baselines, "horizons": horizons,
        "macro": macro, "failures": failures,
    }
    return json.loads(_canonical(decision))  # proves the record holds no NaN/inf


def gate_asset(symbol: str, strategy_cfg: Mapping[str, Any] | None,
               predictions: Mapping[str, Any]) -> dict[str, Any]:
    """Load the declared records and decide; any configuration fault is a SKIP."""
    try:
        consumption = consumption_from(symbol, strategy_cfg or {}, predictions)
    except ValueError as exc:
        return {"schema": DECISION_SCHEMA, "status": SKIPPED, "asset": symbol, "provenance": {},
                "population": {}, "horizons": [], "macro": {},
                "failures": [{"reason": "consumption_not_declared", "family": None,
                              "horizon": None, "detail": str(exc)}]}
    evidence, load_failures = {}, []
    declared = consumption.get("declared")
    if isinstance(declared, Mapping):
        for family in FAMILIES:
            spec = (declared.get("families") or {}).get(family) or {}
            record, error = _load(spec)
            if error:
                load_failures.append({"reason": error, "family": family, "horizon": None, "detail": spec.get("file")})
            else:
                evidence[family] = record
    decision = evaluate(evidence, consumption)
    if load_failures:
        decision["failures"] = load_failures + decision["failures"]
        decision["status"] = SKIPPED
    return decision
