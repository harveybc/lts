"""Real venue adapters for champion succession (finding 257 correction).

Finding 257: every succession primitive was reachable only from tests.
Nothing obtained real venue facts and nothing constructed a real venue
executor, so the machinery could not act on a seat.

This module is that missing bridge. One adapter per Paper/Demo venue,
each built from the SAME runner config and the SAME runner assembly the
live seat uses, so the executor a promotion drains through is literally
the executor that seat trades through:

- :class:`AlpacaSuccessionVenue`  — ``AlpacaModelRunner``: direct REST
  account/orders/positions/asset facts; drain through
  ``AlpacaL1Executor.drain_for_succession`` (journaled, idempotent).
- :class:`IbkrSuccessionVenue`    — ``IbkrModelRunner``: direct TWS
  ``connected_account``/``account_balance``/``open_order_facts``/
  ``position_facts``; drain through the journaled
  ``request_verified_model_switch_flatten`` + outbox consumer.
- :class:`Mt5SuccessionVenue`     — ``Mt5ModelRunner``: the account
  snapshot the terminal ITSELF posted to the execution bridge; drain
  through the bridge's durable close command.

Two rules hold everywhere in this file:

1. Broker truth is only ever read from the venue's own fact interface.
   There is no code path that turns operator-supplied account, order or
   position JSON into a :class:`VenueFacts`. The CLI cannot supply one
   either — see ``tools/promote_paper_champion.py``.
2. The transport is injectable, the interface is not. Tests inject a
   stub HTTP client / stub TWS client / a temporary bridge database and
   exercise the REAL adapter, executor and journal code.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from app.champion_succession import (
    ActionContract,
    CandidateContract,
    ExecutionContract,
    FeatureProvisioningContract,
    SeatContract,
    SuccessionError,
    VenueFacts,
)

#: runner config schema -> venue id
RUNNER_SCHEMA_VENUES = {
    "lts.alpaca.model_runner.v1": "alpaca_paper",
    "lts.ibkr.model_runner.v1": "ibkr_paper",
    "lts.mt5.model_runner.v1": "mt5_demo",
}

LINEAR_FEATURE_CONTRACT = "prediction_provider.closed_bars.linear.v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _path(value: Any) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      default=str).encode()


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SuccessionError(
            f"direct venue fact {name!r} is not a number — unknown is a"
            " refusal, never a default") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise SuccessionError(
            f"direct venue fact {name!r} is not finite")
    return number


# ── seat and candidate construction from the REAL runner config ────────


def load_runner_config(path: Path) -> dict[str, Any]:
    config = json.loads(_path(path).read_text(encoding="utf-8"))
    schema = config.get("schema")
    if schema not in RUNNER_SCHEMA_VENUES:
        raise SuccessionError(
            f"unsupported runner config schema {schema!r}; succession runs"
            f" only against {sorted(RUNNER_SCHEMA_VENUES)}")
    return config


def venue_of(config: Mapping[str, Any]) -> str:
    return RUNNER_SCHEMA_VENUES[str(config.get("schema"))]


def linear_provisioning_contract() -> FeatureProvisioningContract:
    """What live provisioning can ACTUALLY produce on every seat today:
    the 11-feature closed-bar linear contract every runner feeds."""
    from prediction_provider_mechanics.live_linear_policy import FEATURE_NAMES

    descriptor = {"feature_contract": LINEAR_FEATURE_CONTRACT,
                  "feature_names": list(FEATURE_NAMES)}
    return FeatureProvisioningContract(
        contract_id=LINEAR_FEATURE_CONTRACT,
        feature_names=tuple(FEATURE_NAMES),
        preprocessing_sha256=hashlib.sha256(
            _canonical(descriptor)).hexdigest(),
        observation_dim=len(FEATURE_NAMES),
    )


def seat_contract_from_runner_config(
    config: Mapping[str, Any],
    *,
    provisioning: Optional[FeatureProvisioningContract] = None,
) -> SeatContract:
    """The seat exactly as the live runner sees it: its own manifest, its
    own instrument, its own SL/TP geometry."""
    venue = venue_of(config)
    manifest_file = _path(config["model"]["manifest_file"])
    manifest = json.loads(manifest_file.read_bytes())
    if venue == "alpaca_paper":
        profile = json.loads(_path(config["profile_file"]).read_bytes())
        instrument = str(profile["symbol"])
    elif venue == "ibkr_paper":
        profile = json.loads(_path(config["profile_file"]).read_bytes())
        instrument = str(profile["instrument"])
    else:
        instrument = str(config["route"]["symbol"])
    champion = manifest.get("champion") or {}
    return SeatContract(
        venue=venue,
        asset_id=str(manifest["asset_id"]),
        instrument=instrument,
        timeframe=str(manifest["timeframe"]),
        manifest_file=str(manifest_file),
        provisioning=provisioning or linear_provisioning_contract(),
        action=ActionContract(
            kind="probability_threshold",
            threshold=float(champion.get("probability_threshold", 0.5))),
        execution=ExecutionContract(
            native_stop_loss=True, native_take_profit=True,
            native_bracket=True,
            sl_tp_geometry="fraction_of_reference",
            transfer_policy="close_all"),
    )


def load_candidate_descriptor(path: Path) -> CandidateContract:
    """A candidate descriptor is MODEL metadata (hashes, contracts), never
    broker truth: every field is re-verified against artifact bytes and
    against the live provisioning contract by ``preflight_candidate``."""
    data = json.loads(_path(path).read_bytes())
    return CandidateContract(
        model_id=str(data["model_id"]),
        model_kind=str(data["model_kind"]),
        artifact_file=str(data["artifact_file"]),
        artifact_sha256=str(data["artifact_sha256"]),
        config_file=str(data.get("config_file", "")),
        config_sha256=str(data["config_sha256"]),
        asset_id=str(data["asset_id"]),
        timeframe=str(data["timeframe"]),
        observation_dim=int(data["observation_dim"]),
        feature_names=tuple(data["feature_names"]),
        preprocessing_sha256=str(data["preprocessing_sha256"]),
        action=ActionContract(**data["action"]),
        execution=ExecutionContract(**data["execution"]),
    )


def build_successor_manifest(
    seat: SeatContract, candidate: CandidateContract,
) -> dict[str, Any]:
    """The exact bytes the seat manifest must carry after the switch.

    Only the model identity moves. Eligibility flags, schema, asset and
    timeframe stay exactly as the owner left them — a promotion changes
    WHICH model the seat runs, never WHAT the seat is allowed to do.
    """
    manifest = json.loads(_path(seat.manifest_file).read_bytes())
    if not candidate.config_file:
        raise SuccessionError(
            "candidate descriptor has no config_file; the seat manifest"
            " requires a config whose bytes hash to config_sha256")
    artifact = _path(candidate.artifact_file)
    config = _path(candidate.config_file)
    for label, path, expected in (
        ("artifact", artifact, candidate.artifact_sha256),
        ("config", config, candidate.config_sha256),
    ):
        if not path.is_file():
            raise SuccessionError(
                f"candidate {label} {path} does not exist")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise SuccessionError(
                f"candidate {label} bytes do not hash to the declared"
                f" sha256 ({actual[:16]}… != {expected[:16]}…)")
    successor = dict(manifest)
    successor.update({
        "model_id": candidate.model_id,
        "artifact_file": str(artifact),
        "artifact_sha256": candidate.artifact_sha256,
        "config_file": str(config),
        "config_sha256": candidate.config_sha256,
    })
    return successor


# ── shadow inference driver (real policy, persisted venue bars) ────────


def linear_shadow_inference(
    candidate: CandidateContract, bars: Sequence[Mapping[str, Any]],
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """An ``infer`` callable for ``candidate_shadow_replay``.

    The candidate re-decides each incumbent due bar from the venue's OWN
    closed-bar history, through the real policy loader and the real
    feature builder. A due bar the history cannot reproduce becomes a
    typed refusal inside the shadow report — never a guessed row.
    """
    from prediction_provider_mechanics import (
        LiveLinearPolicy,
        build_closed_bar_features,
    )

    if candidate.model_kind != "linear":
        raise SuccessionError(
            f"no shadow inference driver for model_kind"
            f" {candidate.model_kind!r}; a candidate that cannot re-decide"
            " the incumbent's persisted due bars cannot be promoted")
    policy = LiveLinearPolicy.load(
        _path(candidate.artifact_file), candidate.artifact_sha256)
    ordered = sorted(bars, key=lambda bar: str(bar["time"]))

    def infer(row: Mapping[str, Any]) -> Mapping[str, Any]:
        bar_close = str(row["bar_close"])
        window = [bar for bar in ordered if str(bar["time"]) <= bar_close]
        if len(window) < 51 or str(window[-1]["time"]) != bar_close:
            raise SuccessionError(
                f"venue bar history cannot reproduce the observation for"
                f" due bar {bar_close}")
        observation = build_closed_bar_features(window[-60:])
        return policy.predict(observation)

    return infer


# ── venue adapters ─────────────────────────────────────────────────────


class _BaseSuccessionVenue:
    """Shared shape. Subclasses NEVER accept facts as an argument."""

    venue = ""

    def __init__(self, runner: Any, *, source: str) -> None:
        self.runner = runner
        self.source = source
        self._executor_bound = False

    # -- facts ---------------------------------------------------------
    def fetch_facts(self) -> VenueFacts:      # pragma: no cover - abstract
        raise NotImplementedError

    # -- executor ------------------------------------------------------
    def bind_executor(self) -> dict[str, Any]:
        """Declare the REAL executor this adapter will drain through."""
        self._executor_bound = True
        return self.executor_identity()

    def executor_identity(self) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def _require_executor(self) -> None:
        if not self._executor_bound:
            raise SuccessionError(
                "the venue executor was never bound; a drain may only run"
                " through an explicitly constructed real executor")

    def drain_for_succession(
        self, *, reason: str, incumbent_session_id: str,
        successor_artifact_sha256: str, now: datetime,
    ) -> list[dict[str, Any]]:               # pragma: no cover - abstract
        raise NotImplementedError

    def close(self) -> None:
        close = getattr(self.runner, "close", None)
        if callable(close):
            close()


class AlpacaSuccessionVenue(_BaseSuccessionVenue):
    """Alpaca Paper: REST facts, journaled bracket drain."""

    venue = "alpaca_paper"

    def __init__(self, runner: Any) -> None:
        super().__init__(runner, source="alpaca_paper:rest:v2")

    @classmethod
    def from_config(cls, config: Mapping[str, Any],
                    *, client_factory: Optional[Callable[..., Any]] = None
                    ) -> "AlpacaSuccessionVenue":
        from app.alpaca_model_runner import AlpacaModelRunner

        return cls(AlpacaModelRunner(dict(config),
                                     client_factory=client_factory))

    def fetch_facts(self) -> VenueFacts:
        runner = self.runner
        observed_at = _utc_now()
        account = runner.client.account()
        fingerprint = runner.client.account_fingerprint(account)
        if fingerprint != runner.profile.account_fingerprint:
            raise SuccessionError(
                "Alpaca account fingerprint changed; refusing to treat"
                " this session as the seat's account")
        symbol = runner.profile.symbol
        open_orders = [order for order in runner.client.open_orders()
                       if order.get("symbol") in (None, symbol)]
        positions = [item for item in runner.client.positions()
                     if item.get("symbol") == symbol]
        asset = runner.client.asset(symbol)
        return VenueFacts(
            venue=self.venue,
            account_fingerprint=fingerprint,
            instrument=symbol,
            observed_at=observed_at,
            cash=_finite(account.get("cash"), "cash"),
            equity=_finite(account.get("equity"), "equity"),
            open_orders=tuple(open_orders),
            positions=tuple(positions),
            instrument_capability={
                "native_stop_loss": True,
                "native_take_profit": True,
                "native_bracket": True,
                "tradeable": bool(asset.get("tradable")),
                "shortable": bool(asset.get("shortable")),
            },
            source=self.source,
        )

    def historical_closed_bars(self) -> list[dict[str, Any]]:
        from app.alpaca_model_runner import _bars

        return _bars(self.runner.client, self.runner.profile.symbol,
                     self.runner.config["data"]["start"])

    def executor_identity(self) -> dict[str, Any]:
        return {
            "executor": type(self.runner.executor).__module__ + "."
                        + type(self.runner.executor).__name__,
            "drain": "AlpacaL1Executor.drain_for_succession",
            "ledger": str(self.runner.config["service"]["database_path"]),
        }

    def drain_for_succession(
        self, *, reason: str, incumbent_session_id: str,
        successor_artifact_sha256: str, now: datetime,
    ) -> list[dict[str, Any]]:
        self._require_executor()
        return list(self.runner.executor.drain_for_succession(reason))


class IbkrSuccessionVenue(_BaseSuccessionVenue):
    """IBKR Paper: direct TWS facts, journaled model-switch flatten."""

    venue = "ibkr_paper"
    _OPEN_STATUSES = {"PendingSubmit", "PendingCancel", "PreSubmitted",
                      "Submitted"}

    def __init__(self, runner: Any) -> None:
        super().__init__(runner, source="ibkr_paper:tws:v1")

    @classmethod
    def from_config(cls, config: Mapping[str, Any],
                    *, client_factory: Optional[Callable[..., Any]] = None
                    ) -> "IbkrSuccessionVenue":
        from app.ibkr_model_runner import IbkrModelRunner

        return cls(IbkrModelRunner(dict(config),
                                   client_factory=client_factory))

    def fetch_facts(self) -> VenueFacts:
        runner = self.runner
        observed_at = _utc_now()
        account = runner.client.connected_account()
        if not account:
            raise SuccessionError("TWS Paper account is disconnected")
        fingerprint = hashlib.sha256(str(account).encode()).hexdigest()[:16]
        if fingerprint != runner.profile.account_fingerprint:
            raise SuccessionError(
                "TWS Paper account fingerprint does not match the seat"
                " profile")
        balance = runner.client.account_balance()
        orders = runner._route_orders()
        units = runner._route_position()
        positions = ([{"instrument": runner.profile.instrument,
                       "units": units}] if units else [])
        return VenueFacts(
            venue=self.venue,
            account_fingerprint=fingerprint,
            instrument=runner.profile.instrument,
            observed_at=observed_at,
            cash=_finite(balance.get("cash"), "cash"),
            equity=_finite(balance.get("equity"), "equity"),
            open_orders=tuple(orders),
            positions=tuple(positions),
            instrument_capability={
                "native_stop_loss": True,
                "native_take_profit": True,
                "native_bracket": True,
                "instrument": runner.profile.instrument,
                "con_id": runner.profile.contract_con_id,
            },
            source=self.source,
        )

    def historical_closed_bars(self) -> list[dict[str, Any]]:
        return list(self.runner.client.historical_closed_bars(
            self.runner.profile.instrument,
            timeframe=self.runner.config["model"]["expected_timeframe"],
            count=60))

    def executor_identity(self) -> dict[str, Any]:
        return {
            "executor": type(self.runner.consumer).__module__ + "."
                        + type(self.runner.consumer).__name__,
            "drain": "DemoExecutionService."
                     "request_verified_model_switch_flatten"
                     " + L1OutboxConsumer.consume_flattens",
            "ledger": str(self.runner.config["service"]["database_path"]),
        }

    def drain_for_succession(
        self, *, reason: str, incumbent_session_id: str,
        successor_artifact_sha256: str, now: datetime,
    ) -> list[dict[str, Any]]:
        self._require_executor()
        runner = self.runner
        emitted: list[dict[str, Any]] = []
        if runner.olap.open_exposures():
            emitted = runner.service.request_verified_model_switch_flatten(
                trace_id=f"ibkr-succession-{now.isoformat()}",
                current_session_id=incumbent_session_id,
                next_model_artifact_sha256=successor_artifact_sha256,
                now=now)
        flattens = runner.consumer.consume_flattens(now=now)
        return [{"reason": reason, "emitted": emitted,
                 "flattens": flattens}]


class Mt5SuccessionVenue(_BaseSuccessionVenue):
    """MT5 Demo: the terminal's own posted snapshot, bridge close drain."""

    venue = "mt5_demo"

    def __init__(self, runner: Any) -> None:
        super().__init__(runner, source="mt5_demo:execution_bridge:v2")

    @classmethod
    def from_config(cls, config: Mapping[str, Any]
                    ) -> "Mt5SuccessionVenue":
        from app.mt5_model_runner import Mt5ModelRunner

        return cls(Mt5ModelRunner(dict(config)))

    def fetch_facts(self) -> VenueFacts:
        runner = self.runner
        observed_at = _utc_now()
        snapshot = runner._latest_snapshot()
        if snapshot is None:
            raise SuccessionError(
                "the MT5 execution bridge holds no account snapshot; the"
                " terminal must post direct facts before a succession")
        received = datetime.fromisoformat(snapshot["_received_at"])
        max_age = float(runner.config["snapshot_max_age_seconds"])
        age = (observed_at - received).total_seconds()
        if age > max_age:
            raise SuccessionError(
                f"the newest MT5 snapshot is {age:.0f}s old (budget"
                f" {max_age:.0f}s); stale facts cannot authorize anything")
        symbol = runner.config["route"]["symbol"]
        if str(snapshot.get("account_fingerprint",
                            runner.bridge_config.account_fingerprint)) != \
                runner.bridge_config.account_fingerprint:
            raise SuccessionError(
                "the MT5 snapshot names a different account fingerprint")
        positions = [item for item in snapshot.get("positions", [])
                     if item.get("symbol") == symbol]
        orders = [item for item in snapshot.get("orders", [])
                  if item.get("symbol") == symbol]
        symbol_fact = next(
            (item for item in snapshot.get("symbols", [])
             if item.get("symbol") == symbol), None)
        if symbol_fact is None:
            raise SuccessionError(
                f"the MT5 snapshot carries no {symbol} symbol fact")
        return VenueFacts(
            venue=self.venue,
            account_fingerprint=runner.bridge_config.account_fingerprint,
            instrument=symbol,
            observed_at=received,
            cash=_finite(snapshot.get("balance"), "balance"),
            equity=_finite(snapshot.get("equity"), "equity"),
            open_orders=tuple(orders),
            positions=tuple(positions),
            instrument_capability={
                "native_stop_loss": True,
                "native_take_profit": True,
                "native_bracket": True,
                "tradeable": int(symbol_fact.get("trade_mode", 0)) != 0,
                "volume_min": symbol_fact.get("volume_min"),
            },
            source=self.source,
        )

    def historical_closed_bars(self) -> list[dict[str, Any]]:
        snapshot = self.runner._latest_snapshot()
        if snapshot is None:
            raise SuccessionError(
                "the MT5 execution bridge holds no account snapshot")
        return list(self.runner._bars(snapshot))

    def executor_identity(self) -> dict[str, Any]:
        return {
            "executor": type(self.runner.bridge_store).__module__ + "."
                        + type(self.runner.bridge_store).__name__,
            "drain": "Mt5ExecutionStore.enqueue(action=close)",
            "ledger": str(self.runner.bridge_config.database_path),
        }

    def drain_for_succession(
        self, *, reason: str, incumbent_session_id: str,
        successor_artifact_sha256: str, now: datetime,
    ) -> list[dict[str, Any]]:
        self._require_executor()
        runner = self.runner
        snapshot = runner._latest_snapshot()
        if snapshot is None:
            raise SuccessionError(
                "the MT5 execution bridge holds no account snapshot")
        symbol = runner.config["route"]["symbol"]
        positions = [item for item in snapshot.get("positions", [])
                     if item.get("symbol") == symbol]
        if not positions:
            return [{"reason": reason, "commands": [],
                     "detail": "route already flat; no close command"}]
        bars = runner._bars(snapshot)
        command = runner._queue_close(
            snapshot=snapshot,
            current_session={"session_id": incumbent_session_id},
            last_bar=bars[-1]["time"] if bars else snapshot["_received_at"],
            reason="succession")
        return [{"reason": reason, "commands": [command]}]


VENUE_ADAPTERS = {
    "alpaca_paper": AlpacaSuccessionVenue,
    "ibkr_paper": IbkrSuccessionVenue,
    "mt5_demo": Mt5SuccessionVenue,
}


def build_venue(config: Mapping[str, Any], **kwargs: Any):
    """Construct the REAL adapter for this runner config.

    There is deliberately no branch here that builds a venue from
    operator-supplied facts: an adapter can only be a real one.
    """
    venue = venue_of(config)
    adapter = VENUE_ADAPTERS[venue]
    if venue == "mt5_demo":
        return adapter.from_config(config)
    return adapter.from_config(config, **kwargs)
