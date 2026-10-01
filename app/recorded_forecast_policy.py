"""Recorded-forecast heuristic policy for the dormant shadow routes (MT5 demo, Alpaca paper).

This is the integration path for an eligible financial forecaster, before any model
weights are served. A verified cell's recorded predictions (predictor
forecast_naive_evidence.v1 plus its PREDICTIONS CSV) drive the heuristic strategy's
decision rule inside the REAL runners' shadow branch:

* the naive gate selects the readable horizons. Failing horizons are excluded as a
  declared reduced experiment; none passing keeps the route dormant (refused at selection);
* ``heuristic_step`` is the heuristic-strategy paired harness rule (heuristic-strategy
  ``app/paired_backtest.py::decide_targets``): entry when the predicted favourable move
  is at least ``profit_threshold_frac`` of the price, with the RR tie-break, TP/SL on the
  close, and long_only / short_only ablations;
* the state (position, TP, SL, last bar, processed count) is persisted after every bar
  with an atomic replace, so a crashed replay resumes identically. A late tick catches up
  bar by bar from ``episode.start``;
* each processed bar appends one JSONL observability line: decision, reason, forecasts,
  sizing and cost lines (MODELLED vs BROKER_FILL).

Shadow tier only: ``execution_authorized`` is false and nothing is sized or sent.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.forecast_naive_gate import evaluate
from app.live_model_selection import LiveModelSelectionError

CONTRACT_SCHEMA = "lts.recorded_forecast_contract.v1"
TIER = "shadow_inference_only"
SIZING = {"position_units": None, "rule": "shadow tier: nothing is sized; the demo route's own plan_units "
                                          "would apply only after promotion under the existing mandate"}
COST_LINES = [
    {"line": "commission", "value": None, "source": "MODELLED", "status": "NOT_APPLICABLE",
     "reason": "shadow tier places no order"},
    {"line": "broker_fill_costs", "value": None, "source": "BROKER_FILL", "status": "NOT_AVAILABLE",
     "reason": "no fills in shadow"},
]


def _iso(value: str) -> str:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def heuristic_step(state: Mapping[str, Any], close: float, preds: Sequence[float], params: Mapping[str, Any]):
    """One bar of the paired harness rule; returns (new_state, target, reason)."""
    position, tp, sl = state.get("position", 0), state.get("tp"), state.get("sl")
    price = float(close)
    allow_long = params.get("direction", "both") in ("both", "long_only")
    allow_short = params.get("direction", "both") in ("both", "short_only")
    reason = "hold_position" if position != 0 else ("hold_flat" if preds else "no_forecast")
    if position != 0:
        hit_tp = price >= tp if position > 0 else price <= tp
        hit_sl = price <= sl if position > 0 else price >= sl
        if hit_tp or hit_sl:
            position, tp, sl = 0, None, None
            reason = "take_profit" if hit_tp else "stop_loss"
    elif preds:
        high, low = max(preds), min(preds)
        profit_long, profit_short = high - price, price - low
        dd_long = max(price - low, params["min_drawdown_frac"] * price)
        dd_short = max(high - price, params["min_drawdown_frac"] * price)
        rr_long = profit_long / dd_long if dd_long > 0 else 0.0
        rr_short = profit_short / dd_short if dd_short > 0 else 0.0
        threshold = params["profit_threshold_frac"]
        if allow_long and profit_long / price >= threshold and (rr_long >= rr_short or not allow_short):
            position, tp, sl = 1, price + params["tp_multiplier"] * profit_long, price - params["sl_multiplier"] * dd_long
            reason = "entry_long"
        elif allow_short and profit_short / price >= threshold and (rr_short > rr_long or not allow_long):
            position, tp, sl = -1, price - params["tp_multiplier"] * profit_short, price + params["sl_multiplier"] * dd_short
            reason = "entry_short"
    return {**state, "position": position, "tp": tp, "sl": sl}, position, reason


class RecordedForecastPolicy:
    shadow_only = True

    def __init__(self, contract_path: Path, contract: dict, consumed: list[int], predictions: dict,
                 predictions_sha256: str) -> None:
        self.contract_path, self.contract = contract_path, contract
        self.consumed_horizons = consumed
        self.predictions = predictions
        self.model_id = contract["model_id"]
        self.asset_id = contract["asset_id"]
        self.timeframe = contract["timeframe"]
        self.artifact_sha256 = predictions_sha256
        self.params = dict(contract["heuristic"])
        self.episode_start = _iso(contract["episode"]["start"])
        base = contract_path.parent
        self.state_path = base / contract.get("state_file", "state.json")
        self.observability_path = base / contract.get("observability_file", "decisions.jsonl")
        self.state = self._load_state()

    def _load_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text())
            if state.get("contract_sha256") == self._contract_sha():
                return state
        except (OSError, ValueError):
            pass
        return {"contract_sha256": self._contract_sha(), "position": 0, "tp": None, "sl": None,
                "last_time": None, "processed": 0, "last_target": 0, "last_reason": None}

    def _contract_sha(self) -> str:
        return hashlib.sha256(_canonical(self.contract)).hexdigest()

    def _persist(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, sort_keys=True))
        os.replace(tmp, self.state_path)

    def build_observation(self, bars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        tail = [{"time": _iso(b["time"]), "close": float(b["close"])} for b in bars if b.get("complete") is True]
        if not tail:
            raise LiveModelSelectionError("no completed bar")
        fact = {"schema": "lts.recorded_forecast_observation.v1", "last_closed_bar": tail[-1]["time"], "bars": tail}
        return {**fact, "input_sha256": hashlib.sha256(_canonical(fact)).hexdigest()}

    def predict(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        pending = [b for b in observation["bars"] if b["time"] >= self.episode_start
                   and (self.state["last_time"] is None or b["time"] > self.state["last_time"])]
        with open(self.observability_path, "a") as handle:
            for bar in pending:  # catch up bar by bar, persisting after each one
                preds = self.predictions.get(bar["time"], [])
                state, target, reason = heuristic_step(self.state, bar["close"], preds, self.params)
                self.state = {**state, "last_time": bar["time"], "processed": self.state["processed"] + 1,
                              "last_target": target, "last_reason": reason}
                self._persist()
                handle.write(json.dumps({"kind": "bar", "time": bar["time"], "close": bar["close"],
                                         "forecasts": preds, "target": target, "reason": reason,
                                         "sizing": SIZING, "cost_lines": COST_LINES}, sort_keys=True) + "\n")
        target = self.state["last_target"]
        result = {"schema": "lts.recorded_forecast_inference.v1", "tier": TIER, "execution_authorized": False,
                  "model_id": self.model_id, "asset_id": self.asset_id, "timeframe": self.timeframe,
                  "last_closed_bar": observation["last_closed_bar"], "input_sha256": observation["input_sha256"],
                  "target": target, "reason": self.state["last_reason"],
                  "action": {1: "long", -1: "short", 0: "flat"}[target],
                  "consumed_horizons": self.consumed_horizons, "processed": self.state["processed"]}
        result["output_sha256"] = hashlib.sha256(_canonical(result)).hexdigest()
        return result


class SelectedRecordedForecastPolicy:
    """Selector surface the runners use; refuses any tier but shadow and any failing record."""

    def __init__(self, *, contract_file: str | Path, expected_asset_id: str, expected_timeframe: str,
                 execution_tier: str, **_ignored) -> None:
        if execution_tier != TIER:
            raise LiveModelSelectionError(f"recorded-forecast policies run only in the {TIER} tier")
        self.contract_file = Path(os.path.expandvars(str(contract_file))).expanduser()
        self.expected_asset_id, self.expected_timeframe = expected_asset_id, expected_timeframe
        self.execution_tier = execution_tier
        self.contract_sha256 = ""
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> bool:
        try:
            raw = self.contract_file.read_bytes()
            contract = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise LiveModelSelectionError("recorded-forecast contract is unreadable") from exc
        digest = hashlib.sha256(raw).hexdigest()
        if not force and digest == self.contract_sha256:
            return False
        if contract.get("schema") != CONTRACT_SCHEMA:
            raise LiveModelSelectionError("recorded-forecast contract schema is unsupported")
        if contract.get("execution_authorized") is not False or contract.get("execution_tier") != TIER:
            raise LiveModelSelectionError("recorded-forecast contract must declare the shadow tier, no execution")
        if contract.get("asset_id") != self.expected_asset_id or contract.get("timeframe") != self.expected_timeframe:
            raise LiveModelSelectionError("recorded-forecast contract does not match the route")
        base = self.contract_file.parent
        pred_spec = contract["predictions"]
        pred_path = base / pred_spec["file"]
        pred_sha = hashlib.sha256(pred_path.read_bytes()).hexdigest()
        if pred_sha != pred_spec["sha256"]:
            raise LiveModelSelectionError("recorded predictions hash mismatch")
        declared = contract["evidence"]["declared"]
        try:
            record = json.loads((base / contract["evidence"]["record_file"]).read_text())
        except (OSError, ValueError) as exc:
            raise LiveModelSelectionError("SKIPPED_NOT_BETTER_THAN_NAIVE: dormant (evidence unreadable)") from exc
        horizons = list(declared["families"]["forecast"]["horizons"])
        consumption = {"asset": declared.get("asset"), "declared": declared, "families": {"forecast": {
            "consumed": len(horizons), "horizons": horizons, "spec": dict(declared["families"]["forecast"])}}}
        decision = evaluate({"forecast": record}, consumption, families=("forecast",))
        failing = {f["horizon"] for f in decision["failures"]}
        if None in failing or not [h for h in horizons if h not in failing]:
            reasons = sorted({f["reason"] for f in decision["failures"]})
            raise LiveModelSelectionError(f"SKIPPED_NOT_BETTER_THAN_NAIVE: dormant, not eligible ({', '.join(reasons)})")
        consumed = [h for h in horizons if h not in failing]
        column = pred_spec.get("price_column", "close_hat_h{h}")
        predictions = {}
        with open(pred_path, newline="") as handle:
            for row in csv.DictReader(handle):
                predictions[_iso(row[pred_spec.get("time_column", "DATE_TIME")])] = [
                    float(row[column.format(h=h)]) for h in consumed]  # failing columns never read
        self.policy = RecordedForecastPolicy(self.contract_file, contract, consumed, predictions, pred_sha)
        self.excluded = [h for h in horizons if h in failing]
        self.gate = decision
        self.manifest = {"schema": CONTRACT_SCHEMA, "model_id": contract["model_id"], "config_sha256": digest,
                         "manifest_sha256": digest, "execution_tier": TIER}
        self.contract_sha256 = digest
        return True

    def identity(self) -> dict[str, Any]:
        return {"model_family": "recorded_forecast", "model_id": self.policy.model_id,
                "artifact_sha256": self.policy.artifact_sha256, "contract_sha256": self.contract_sha256,
                "consumed_horizons": self.policy.consumed_horizons,
                "reduced_input_experiment": {"declared": bool(self.excluded), "excluded": self.excluded},
                "execution_tier": TIER, "execution_authorized": False,
                "forecast_eligibility": "ELIGIBLE_ON_CONSUMED_HORIZONS"}
