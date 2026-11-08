"""Registry, weekly ranking, reports and the restartable offline service.

The service writes only under an explicit directory. It refuses the live ledger
roots. It does not enable a broker route, mint a capability or start a unit.
``paper_demo_executing`` stays false.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from app.forward_pool.accounts import Position, RouteBook, VirtualAccount
from app.forward_pool.core import BusinessContract, PoolRefusal, digest
from app.forward_pool.snapshot import HourlyBar, materialize

RULE_PATH = Path(__file__).with_name("RANKING_RULE.json")
INITIAL_BALANCE = 10_000.0
SELECTED70_MODEL_HEX = "079bef0ea6f3b9327772f4204e5d5dbbd4be69a5ab58c32b424aa3cb7c686404"
SELECTED70_HISTORICAL = {
    "TRAIN": {"net_return_pct": 11.51453, "orders": 748, "orders_per_week": 3.57895},
    "VALIDATION": {"net_return_pct": 13.80849, "orders": 206, "orders_per_week": 3.81481},
    "evidence_class": "offline_reference_not_forward",
}
FORBIDDEN_ROOTS = (
    Path.home() / ".local" / "state" / "lts",
    Path.home() / ".config" / "lts",
    Path.home() / ".lts",
)


def assert_isolated(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    if resolved.name == "lts_trading.db":
        raise PoolRefusal("LIVE_LEDGER_PATH")
    for forbidden in FORBIDDEN_ROOTS:
        try:
            resolved.relative_to(forbidden)
        except ValueError:
            continue
        raise PoolRefusal("LIVE_LEDGER_PATH")
    return resolved


def load_ranking_rule() -> dict[str, Any]:
    rule = json.loads(RULE_PATH.read_text(encoding="utf-8"))
    if rule.get("schema_version") != "forward_pool.ranking_rule.v1":
        raise PoolRefusal("RANKING_RULE")
    if rule.get("objective") != "net_return_after_costs":
        raise PoolRefusal("RANKING_RULE")
    if float(rule["activity_orders_per_week_gt"]) < 2:
        raise PoolRefusal("RANKING_RULE")
    if rule.get("tie_break", [None])[-1] != "retain_incumbent":
        raise PoolRefusal("RANKING_RULE")
    forbidden = set(rule.get("forbid_objectives") or [])
    if "train_profit" not in forbidden or "critic_q" not in forbidden:
        raise PoolRefusal("RANKING_RULE")
    rule["content_sha256"] = digest(rule)
    return rule


@dataclass
class CandidateSpec:
    model_sha256: str
    recipe_sha256: str
    scaler_sha256: str
    strategy_sha256: str
    code_sha256: str
    train_cutoff: str
    columns: tuple[str, ...]
    train_return: float | None
    validation_return: float | None
    train_orders: int | None
    validation_orders: int | None
    train_orders_per_week: float | None
    validation_orders_per_week: float | None
    business_valid: bool = True
    inference: str = "greedy"
    loader: str = "categorical_greedy_bundle"
    fixed_signal: int = 0
    group_count: int = 28
    observations: int = 13
    stride: int = 2
    historical: dict[str, Any] | None = None
    role: str = "candidate"

    def candidate_id(self) -> str:
        return digest(
            {
                "code": self.code_sha256,
                "codec": "NOP_BUY_SELL_CLOSE_OPPOSITE_V1",
                "cutoff": self.train_cutoff,
                "inference": "greedy",
                "model": self.model_sha256,
                "recipe": self.recipe_sha256,
                "scaler": self.scaler_sha256,
                "strategy": self.strategy_sha256,
            }
        )


def eligibility_reasons(spec: CandidateSpec) -> list[str]:
    reasons: list[str] = []
    if spec.loader != "categorical_greedy_bundle" or spec.inference != "greedy":
        reasons.append("LEGACY_ADAPTER" if spec.loader != "categorical_greedy_bundle" else "STOCHASTIC_MODE")
    for label, value in (("TRAIN", spec.train_return), ("VALIDATION", spec.validation_return)):
        if value is None or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            reasons.append(f"METRIC_MISSING_{label}")
        elif float(value) < 0:
            reasons.append(f"NEGATIVE_RETURN_{label}")
    for label, orders, rate in (
        ("TRAIN", spec.train_orders, spec.train_orders_per_week),
        ("VALIDATION", spec.validation_orders, spec.validation_orders_per_week),
    ):
        if orders is None or rate is None or not isinstance(rate, (int, float)) or not math.isfinite(float(rate)):
            reasons.append(f"METRIC_MISSING_{label}")
        elif orders == 0:
            reasons.append("ZERO_ORDERS")
        elif float(rate) <= 2:
            reasons.append("ACTIVITY")
    if not spec.business_valid:
        reasons.append("BUSINESS_CONTRACT")
    return list(dict.fromkeys(reasons))


@dataclass
class WindowMetrics:
    window_id: str
    complete_common_week: bool
    coverage_valid: bool
    net_return: float | None
    max_drawdown: float | None
    orders: int | None
    orders_per_week: float | None
    sharpe: float | None = None
    uncertainty: float | None = None
    risk_contract_ok: bool = True
    train_profit: float | None = None
    critic_q: float | None = None


@dataclass
class CandidateRecord:
    spec: CandidateSpec
    state: str
    reasons: list[str]
    account: VirtualAccount
    artifact_sha256: str | None = None
    forward_started_utc_ns: int | None = None
    window: WindowMetrics | None = None
    admitted_utc_ns: int | None = None

    @property
    def candidate_id(self) -> str:
        return self.spec.candidate_id()


def select_week(
    records: Mapping[str, CandidateRecord],
    rule: Mapping[str, Any],
    *,
    incumbent_id: str,
    reference_id: str,
) -> dict[str, Any]:
    """Rank one common forward window. Insufficient support retains the incumbent."""

    if reference_id not in records:
        raise PoolRefusal("REFERENCE_MISSING")
    if incumbent_id not in records:
        raise PoolRefusal("INCUMBENT_MISSING")
    active = [record for record in records.values() if record.state != "INELIGIBLE"]
    if not active or any(
        record.window is None
        or not record.window.complete_common_week
        or not record.window.coverage_valid
        for record in active
    ):
        return {"winner": incumbent_id, "reason": "INSUFFICIENT_SUPPORT", "notes": [], "reference_id": reference_id}
    considered = active
    support = [record.window for record in considered if record.window is not None]
    if len({item.window_id for item in support}) != 1:
        return {"winner": incumbent_id, "reason": "WINDOW_MISMATCH", "notes": [], "reference_id": reference_id}
    notes: list[dict[str, str]] = []
    ranked: list[CandidateRecord] = []
    gate = float(rule["activity_orders_per_week_gt"])
    for record in considered:
        window = record.window
        assert window is not None
        if (
            window.orders is None
            or window.orders_per_week is None
            or window.net_return is None
            or not isinstance(window.net_return, (int, float))
            or not math.isfinite(float(window.net_return))
        ):
            notes.append({"candidate_id": record.candidate_id, "reason": "METRIC_MISSING"})
            continue
        if window.orders == 0:
            notes.append({"candidate_id": record.candidate_id, "reason": "ZERO_ORDERS"})
            continue
        if float(window.orders_per_week) <= gate:
            notes.append({"candidate_id": record.candidate_id, "reason": "ACTIVITY"})
            continue
        if not window.risk_contract_ok:
            notes.append({"candidate_id": record.candidate_id, "reason": "RISK_CONTRACT"})
            continue
        ranked.append(record)
    if not ranked:
        return {"winner": incumbent_id, "reason": "NO_ELIGIBLE", "notes": notes, "reference_id": reference_id}

    def sort_key(record: CandidateRecord) -> tuple[float, float, int]:
        window = record.window
        assert window is not None and window.net_return is not None
        drawdown = math.inf if window.max_drawdown is None else float(window.max_drawdown)
        keep_incumbent = 0 if record.candidate_id == incumbent_id else 1
        return (-float(window.net_return), drawdown, keep_incumbent)

    ranked.sort(key=sort_key)
    return {
        "winner": ranked[0].candidate_id,
        "reason": "SELECTED",
        "notes": notes,
        "reference_id": reference_id,
        "objective": "net_return_after_costs",
    }


def performance_report(account: VirtualAccount, daily_equity: list[float], *, initial: float = INITIAL_BALANCE) -> dict[str, Any]:
    """Daily-equity Sharpe uses sqrt(365). Undefined values stay null with a reason."""

    sharpe = None
    sharpe_reason = None
    drawdown = None
    drawdown_reason = None
    if len(daily_equity) < 2:
        sharpe_reason = "INSUFFICIENT_DAYS"
        drawdown_reason = "INSUFFICIENT_DAYS"
    else:
        if any(not math.isfinite(point) or point <= 0 for point in daily_equity):
            sharpe_reason = "NONFINITE"
        else:
            returns = [daily_equity[index] / daily_equity[index - 1] - 1.0 for index in range(1, len(daily_equity))]
            mean = sum(returns) / len(returns)
            variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
            deviation = math.sqrt(variance)
            if deviation == 0:
                sharpe_reason = "ZERO_VARIANCE"
            else:
                sharpe = mean / deviation * math.sqrt(365)
        peak = daily_equity[0]
        worst = 0.0
        finite = True
        for point in daily_equity:
            if not math.isfinite(point):
                finite = False
                break
            peak = max(peak, point)
            if peak > 0:
                worst = max(worst, (peak - point) / peak)
        if finite:
            drawdown = worst
        else:
            drawdown_reason = "NONFINITE"
    orders = account.entries
    wins = sum(1 for close in account.closes if close.pnl > 0)
    return {
        "net_cash": account.balance - initial,
        "balance": account.balance,
        "return_fraction": (account.balance - initial) / initial,
        "sharpe_daily_sqrt365": sharpe,
        "sharpe_reason": sharpe_reason,
        "max_drawdown": drawdown,
        "max_drawdown_reason": drawdown_reason,
        "orders": orders,
        "wins": wins,
        "win_rate": (wins / len(account.closes)) if account.closes else None,
        "win_rate_reason": None if account.closes else "NO_CLOSES",
        "annualization": "sqrt(365) on daily equity returns",
    }


def explain_divergence(
    *,
    decision_id: str,
    direction: int,
    units: int,
    modeled_price: float | None,
    observed_price: float | None,
    modeled_fee: float | None,
    observed_fee: float | None,
    modeled_financing: float | None,
    observed_financing: float | None,
    spread: float | None,
    latency_ms: float | None,
    rejection: str | None,
    partial_fill: bool,
    modeled_cash: float | None,
    observed_cash: float | None,
) -> dict[str, Any]:
    """Name the cash gap. Do not coerce a missing observation into zero."""

    def gap(observed: float | None, modeled: float | None) -> float | None:
        if observed is None or modeled is None:
            return None
        if not math.isfinite(observed) or not math.isfinite(modeled):
            raise PoolRefusal("NONFINITE")
        return observed - modeled

    slippage = gap(observed_price, modeled_price)
    fee = gap(observed_fee, modeled_fee)
    financing = gap(observed_financing, modeled_financing)
    cash_gap = gap(observed_cash, modeled_cash)
    explained = 0.0
    complete = cash_gap is not None
    if slippage is None or fee is None or financing is None:
        complete = False
    else:
        explained = direction * slippage * units - fee - financing
    unexplained = None if not complete or cash_gap is None else cash_gap - explained
    return {
        "schema_version": "forward_pool.divergence.v1",
        "decision_id": decision_id,
        "spread": spread,
        "slippage": slippage,
        "financing": financing,
        "fee": fee,
        "latency_ms": latency_ms,
        "rejection": rejection,
        "partial_fill": partial_fill,
        "modeled_cash": modeled_cash,
        "observed_cash": observed_cash,
        "unexplained_cash": unexplained,
    }


def _position_dict(position: Position | None) -> dict[str, Any] | None:
    if position is None:
        return None
    return {
        "direction": position.direction,
        "units": position.units,
        "entry": position.entry,
        "sl": position.sl,
        "tp": position.tp,
        "owner": position.owner,
    }


def _position_load(payload: dict[str, Any] | None) -> Position | None:
    if payload is None:
        return None
    return Position(
        int(payload["direction"]),
        int(payload["units"]),
        float(payload["entry"]),
        float(payload["sl"]),
        float(payload["tp"]),
        str(payload["owner"]),
    )


@dataclass
class PoolService:
    root: Path
    contract: BusinessContract = field(default_factory=BusinessContract)
    records: dict[str, CandidateRecord] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    route: RouteBook = field(default_factory=lambda: RouteBook("eurusd-shadow", tier="shadow", authorized=False))
    route_account: VirtualAccount = field(default_factory=lambda: VirtualAccount("route", "unassigned", INITIAL_BALANCE))
    last_bar_utc_ns: int | None = None
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    warmup: list[str] = field(default_factory=list)
    revision_ignored: int = 0
    reference_id: str | None = None
    incumbent_id: str | None = None
    materialize_calls: int = 0
    paper_demo_executing: bool = False
    code_verified: bool = False
    shadow_observing: bool = False
    _lock_fd: int | None = field(default=None, repr=False)
    _rule: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root = assert_isolated(Path(self.root))
        self.root.mkdir(parents=True, exist_ok=True)
        self.contract.validate()
        self._rule = load_ranking_rule()
        self.paper_demo_executing = False
        self.shadow_observing = False
        self._acquire_lock()
        state_path = self.root / "state.json"
        if state_path.exists():
            self._load(state_path)

    def close(self) -> None:
        if self._lock_fd is None:
            return
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        self._lock_fd = None

    def _acquire_lock(self) -> None:
        fd = os.open(self.root / "pool.lock", os.O_CREAT | os.O_RDWR, 0o600)
        last: PoolRefusal | None = None
        for _ in range(3):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_fd = fd
                return
            except BlockingIOError as exc:
                last = PoolRefusal("LOCK_HELD", str(exc))
        os.close(fd)
        raise PoolRefusal("RETRIES_EXHAUSTED", last.code if last else "LOCK_HELD")

    def register(self, spec: CandidateSpec, *, now_ns: int) -> CandidateRecord:
        reasons = eligibility_reasons(spec)
        candidate_id = spec.candidate_id()
        existing = self.records.get(candidate_id)
        if existing is not None:
            return existing
        state = "INELIGIBLE" if reasons else "REGISTERED"
        record = CandidateRecord(
            spec=spec,
            state=state,
            reasons=reasons,
            account=VirtualAccount(candidate_id, candidate_id, INITIAL_BALANCE),
            admitted_utc_ns=now_ns,
            artifact_sha256=spec.model_sha256,
        )
        self.records[candidate_id] = record
        self.order.append(candidate_id)
        if spec.role == "initial_reference":
            self.reference_id = candidate_id
            if self.incumbent_id is None:
                self.incumbent_id = candidate_id
        self._save()
        return record

    def seed_selected70(self, *, now_ns: int, recipe_sha256: str, scaler_sha256: str, strategy_sha256: str, code_sha256: str) -> CandidateRecord:
        spec = CandidateSpec(
            model_sha256="sha256:" + SELECTED70_MODEL_HEX,
            recipe_sha256=recipe_sha256,
            scaler_sha256=scaler_sha256,
            strategy_sha256=strategy_sha256,
            code_sha256=code_sha256,
            train_cutoff="2024-01-01",
            columns=("RAW_CLOSE_LOG_RETURN",),
            train_return=11.51453,
            validation_return=13.80849,
            train_orders=748,
            validation_orders=206,
            train_orders_per_week=3.57895,
            validation_orders_per_week=3.81481,
            historical=dict(SELECTED70_HISTORICAL),
            role="initial_reference",
            fixed_signal=0,
        )
        return self.register(spec, now_ns=now_ns)

    def seal(self, candidate_id: str, artifact_sha256: str) -> None:
        record = self._record(candidate_id)
        if record.artifact_sha256 is not None and record.artifact_sha256 != artifact_sha256:
            raise PoolRefusal("IMMUTABLE_ARTIFACT")
        record.artifact_sha256 = artifact_sha256

    def mark_parity(self, candidate_id: str) -> None:
        record = self._record(candidate_id)
        if record.state != "REGISTERED":
            raise PoolRefusal("ADMISSION_STATE")
        record.state = "PARITY_VERIFIED"

    def mark_shadow(self, candidate_id: str, *, now_ns: int) -> None:
        record = self._record(candidate_id)
        if record.state != "PARITY_VERIFIED":
            raise PoolRefusal("ADMISSION_STATE")
        record.state = "SHADOW_ACTIVE"
        record.forward_started_utc_ns = now_ns

    def mark_execution_eligible(self, candidate_id: str) -> None:
        record = self._record(candidate_id)
        if record.state != "SHADOW_ACTIVE":
            raise PoolRefusal("ADMISSION_STATE")
        record.state = "EXECUTION_ELIGIBLE"
        self.paper_demo_executing = False

    def post_window(self, candidate_id: str, window: WindowMetrics) -> None:
        self._record(candidate_id).window = window

    def rank(self) -> dict[str, Any]:
        if self.reference_id is None or self.incumbent_id is None:
            raise PoolRefusal("REFERENCE_MISSING")
        result = select_week(
            self.records,
            self._rule,
            incumbent_id=self.incumbent_id,
            reference_id=self.reference_id,
        )
        if self.reference_id not in self.records:
            raise PoolRefusal("REFERENCE_MISSING")
        return result

    def step(
        self,
        bar: HourlyBar,
        signals: Mapping[str, int] | None = None,
        *,
        sl: float | None = None,
        tp: float | None = None,
        transport: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return self._step(bar, signals, sl=sl, tp=tp, transport=transport)
        except PoolRefusal as exc:
            (self.root / "FAILURE.json").write_text(
                json.dumps({"code": exc.code, "detail": exc.detail}, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            raise

    def _step(
        self,
        bar: HourlyBar,
        signals: Mapping[str, int] | None,
        *,
        sl: float | None,
        tp: float | None,
        transport: Callable[..., Any] | None,
    ) -> dict[str, Any]:
        if self.paper_demo_executing:
            raise PoolRefusal("EXECUTION_FLAG")
        if bar.bar_close_utc_ns == self.last_bar_utc_ns or (
            self.last_bar_utc_ns is not None and bar.bar_close_utc_ns < self.last_bar_utc_ns
        ):
            self.revision_ignored += 1
            self._save()
            return {
                "replayed": True,
                "revision": "REVISION_IGNORED",
                "submitted": 0,
                "decisions": self._stored(bar.bar_close_utc_ns),
            }
        if bar.catch_up:
            self.warmup.append(bar.snapshot_id)
            self.last_bar_utc_ns = bar.bar_close_utc_ns
            self._save()
            return {"catch_up": True, "submitted": 0, "decisions": []}
        cache: dict[tuple[str, ...], Any] = {}
        calls = 0
        produced: list[dict[str, Any]] = []
        submitted = 0
        for candidate_id in self.order:
            record = self.records[candidate_id]
            if record.spec.columns not in cache:
                cache[record.spec.columns] = materialize(bar, record.spec.columns)
                calls += 1
            features = cache[record.spec.columns]
            if record.state == "INELIGIBLE":
                result = {
                    "signal": None,
                    "command": 0,
                    "intent": "refuse",
                    "outcome": "retained",
                    "refusal": record.reasons[0] if record.reasons else "INELIGIBLE",
                    "close_reason": None,
                    "balance": record.account.balance,
                    "position": 0,
                    "owner": record.candidate_id,
                    "entries": record.account.entries,
                    "units": 0,
                }
                result.update(
                    {
                        "candidate_id": candidate_id,
                        "snapshot_id": bar.snapshot_id,
                        "input_hash": features.feature_hash,
                        "config_hash": self.contract.digest(),
                        "model_hash": record.spec.model_sha256,
                        "account_balance": record.account.balance,
                    }
                )
                produced.append(result)
                self.decisions[f"{candidate_id}:{bar.bar_close_utc_ns}"] = result
                continue
            if features.refusal:
                result = record.account.apply(0, bar, self.contract, manage=True, entry_sl=sl, entry_tp=tp)
                result["signal"] = None
                result["refusal"] = features.refusal
                result["outcome"] = "refused"
                result["intent"] = "refuse"
            else:
                signal = int(signals[candidate_id]) if signals and candidate_id in signals else record.spec.fixed_signal
                manage = self._manages_route(candidate_id)
                result = record.account.apply(
                    signal,
                    bar,
                    self.contract,
                    manage=True,
                    entry_sl=sl,
                    entry_tp=tp,
                )
                if manage and record.state == "EXECUTION_ELIGIBLE" and transport is not None:
                    effect_id = digest(
                        {
                            "candidate": candidate_id,
                            "command": result["command"],
                            "snapshot": bar.snapshot_id,
                        }
                    )
                    self.route.submit(
                        effect_id=effect_id,
                        owner=candidate_id,
                        command=int(result["command"]),
                        snapshot_id=bar.snapshot_id,
                        now_ns=bar.decision_time_utc_ns,
                        transport=transport,
                    )
                    submitted += 1
                if manage:
                    self._mirror_route(candidate_id, bar)
            result.update(
                {
                    "candidate_id": candidate_id,
                    "snapshot_id": bar.snapshot_id,
                    "input_hash": features.feature_hash,
                    "config_hash": self.contract.digest(),
                    "model_hash": record.spec.model_sha256,
                    "account_balance": record.account.balance,
                }
            )
            produced.append(result)
            self.decisions[f"{candidate_id}:{bar.bar_close_utc_ns}"] = result
        self.materialize_calls += calls
        self.last_bar_utc_ns = bar.bar_close_utc_ns
        self._save()
        self._write_snapshot(bar, produced)
        return {"replayed": False, "submitted": submitted, "materialize_calls": calls, "decisions": produced}

    def _manages_route(self, candidate_id: str) -> bool:
        return self.route.writer == candidate_id

    def _mirror_route(self, candidate_id: str, bar: HourlyBar) -> None:
        shadow = self.records[candidate_id].account
        if self.route_account.owner != candidate_id and self.route.position is None:
            self.route_account.owner = candidate_id
        if self.route.position is None and shadow.position is not None and shadow.position.owner == candidate_id:
            self.route.position = Position(
                shadow.position.direction,
                shadow.position.units,
                shadow.position.entry,
                shadow.position.sl,
                shadow.position.tp,
                candidate_id,
            )
            self.route_account.position = self.route.position
            self.route.cash = self.route_account.balance
        elif shadow.position is None and self.route.position is not None and self.route.position.owner == candidate_id:
            self.route.position = None
            self.route_account.position = None
            self.route.cash = shadow.balance
            self.route_account.balance = shadow.balance
            self.route.note_flat(bar.decision_time_utc_ns, self.route.cash)

    def _stored(self, bar_ns: int) -> list[dict[str, Any]]:
        return [value for key, value in self.decisions.items() if key.endswith(":" + str(bar_ns))]

    def _record(self, candidate_id: str) -> CandidateRecord:
        try:
            return self.records[candidate_id]
        except KeyError as exc:
            raise PoolRefusal("UNKNOWN_CANDIDATE") from exc

    def _write_snapshot(self, bar: HourlyBar, decisions: list[dict[str, Any]]) -> None:
        payload = {
            "schema_version": "forward_pool.hourly_snapshot.v1",
            "evidence_class": "OFFLINE_FIXTURE_SNAPSHOT",
            "shadow_observing": False,
            "paper_demo_executing": False,
            "snapshot_id": bar.snapshot_id,
            "bar_close_utc_ns": bar.bar_close_utc_ns,
            "instrument": bar.instrument,
            "civil_offset_minutes": bar.civil_offset_minutes,
            "decisions": [
                {
                    "candidate_id": item["candidate_id"],
                    "signal": item["signal"],
                    "command": item["command"],
                    "intent": item["intent"],
                    "outcome": item["outcome"],
                    "refusal": item["refusal"],
                    "input_hash": item["input_hash"],
                    "model_hash": item["model_hash"],
                }
                for item in decisions
            ],
        }
        target = self.root / "snapshots"
        target.mkdir(exist_ok=True)
        (target / f"{bar.snapshot_id}.json").write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _save(self) -> None:
        payload = {
            "schema": "forward_pool.state.v1",
            "last_bar_utc_ns": self.last_bar_utc_ns,
            "reference_id": self.reference_id,
            "incumbent_id": self.incumbent_id,
            "revision_ignored": self.revision_ignored,
            "warmup": self.warmup,
            "ranking_rule_sha256": self._rule["content_sha256"],
            "paper_demo_executing": False,
            "shadow_observing": False,
            "records": [self._dump_record(self.records[key]) for key in self.order],
            "decisions": self.decisions,
            "route": {
                "writer": self.route.writer,
                "queued": self.route.queued,
                "cash": self.route.cash,
                "authorized": self.route.authorized,
                "tier": self.route.tier,
                "position": _position_dict(self.route.position),
                "tenure": self.route.tenure,
                "effects": {
                    key: {"state": value.state, "owner": value.owner, "command": value.command, "calls": value.transport_calls}
                    for key, value in self.route.effects.items()
                },
            },
        }
        temporary = self.root / "state.json.building"
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.root / "state.json")

    def _dump_record(self, record: CandidateRecord) -> dict[str, Any]:
        spec = record.spec
        window = None
        if record.window is not None:
            window = record.window.__dict__.copy()
        return {
            "spec": spec.__dict__.copy(),
            "state": record.state,
            "reasons": record.reasons,
            "artifact_sha256": record.artifact_sha256,
            "forward_started_utc_ns": record.forward_started_utc_ns,
            "admitted_utc_ns": record.admitted_utc_ns,
            "balance": record.account.balance,
            "entries": record.account.entries,
            "position": _position_dict(record.account.position),
            "window": window,
        }

    def _load(self, path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != "forward_pool.state.v1":
            raise PoolRefusal("STATE_SCHEMA")
        if payload.get("ranking_rule_sha256") != self._rule["content_sha256"]:
            raise PoolRefusal("RANKING_RULE_UNFROZEN")
        self.last_bar_utc_ns = payload["last_bar_utc_ns"]
        self.reference_id = payload["reference_id"]
        self.incumbent_id = payload["incumbent_id"]
        self.revision_ignored = int(payload["revision_ignored"])
        self.warmup = list(payload["warmup"])
        self.decisions = payload["decisions"]
        self.records.clear()
        self.order.clear()
        for raw in payload["records"]:
            spec_raw = raw["spec"]
            spec_raw["columns"] = tuple(spec_raw["columns"])
            spec = CandidateSpec(**spec_raw)
            account = VirtualAccount(spec.candidate_id(), spec.candidate_id(), float(raw["balance"]))
            account.entries = int(raw["entries"])
            account.position = _position_load(raw["position"])
            window = WindowMetrics(**raw["window"]) if raw["window"] else None
            record = CandidateRecord(
                spec=spec,
                state=raw["state"],
                reasons=list(raw["reasons"]),
                account=account,
                artifact_sha256=raw["artifact_sha256"],
                forward_started_utc_ns=raw["forward_started_utc_ns"],
                window=window,
                admitted_utc_ns=raw["admitted_utc_ns"],
            )
            self.records[record.candidate_id] = record
            self.order.append(record.candidate_id)
        route = payload["route"]
        self.route.writer = route["writer"]
        self.route.queued = route["queued"]
        self.route.cash = float(route["cash"])
        self.route.authorized = bool(route["authorized"])
        self.route.tier = route["tier"]
        self.route.position = _position_load(route["position"])
        self.route.tenure = list(route["tenure"])
        self.paper_demo_executing = False
        self.shadow_observing = False
