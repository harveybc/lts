"""Continuous ETH model -> L0 risk -> MT5 Demo command-outbox runner."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from prediction_provider_mechanics import build_closed_bar_features
from trading_contracts import (
    AssetIntent,
    BrokerCapabilitySnapshot,
    ExecutionReportV2,
    InstrumentCapability,
)

from app.alpaca_model_runner import ModelSessionStore
from app.demo_execution_service import DemoExecutionConfig, DemoExecutionService, ZeroNetworkSink
from app.ibkr_l1_journal import L1ExecutionOlap
from app.live_sac_selection import LiveSacSelectionError, SelectedSacPolicy
from app.live_model_selection import LiveModelSelectionError, SelectedLinearPolicy
from app.model_position_control import decide_position_control
from app.model_runner_heartbeat import (
    selected_model_identity,
    write_runner_heartbeat,
)
from app.mt5_execution_bridge import Mt5ExecutionConfig, Mt5ExecutionStore
from app.project3_sac_observation import (
    SacObservationError,
    build_sac_observation,
    load_live_observation_spec,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


class Mt5ModelRunnerError(RuntimeError):
    pass


def _mt5_utc(value: Any) -> datetime:
    """Parse the Unix timestamp emitted directly by the MT5 bridge EA."""
    try:
        parsed = datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        raise Mt5ModelRunnerError("MT5 position time_open is invalid") from exc
    return parsed


def _ledger_time(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise Mt5ModelRunnerError(
            f"durable MT5 command has invalid {field}") from error
    if parsed.tzinfo is None:
        raise Mt5ModelRunnerError(
            f"durable MT5 command has timezone-naive {field}")
    return parsed.astimezone(timezone.utc)


def durable_command_heartbeat(
    store: Mt5ExecutionStore,
    *,
    account_fingerprint: str,
    idempotency_key: str,
    snapshot_received_at: str,
    positions_total: int,
    orders_total: int,
) -> Optional[dict[str, Any]]:
    """Derive runner state from the direct execution-command ledger.

    A replayed model decision never gets to relabel an existing command as
    newly queued.  Terminal success is called flat only when a direct account
    snapshot received after command completion proves zero positions and zero
    orders.  Missing/unknown durable states fail closed.
    """
    command = store.command_for_idempotency(
        account_fingerprint, idempotency_key)
    if command is None:
        return None
    state = str(command.get("state") or "")
    command_id = str(command.get("command_id") or "")
    if not command_id:
        raise Mt5ModelRunnerError(
            "durable MT5 command is missing command_id")
    common = {
        "command_id": command_id,
        "command_state": state,
        "state_source": "execution_commands",
    }
    if state == "pending":
        return {"state": "command_pending", **common}
    if state == "delivered":
        return {"state": "command_delivered", **common}
    if state == "failed":
        return {"state": "command_failed", **common}
    if state != "succeeded":
        raise Mt5ModelRunnerError(
            f"unsupported durable MT5 command state {state!r}")

    completed = _ledger_time(command.get("completed_at"), "completed_at")
    snapshot_at = _ledger_time(snapshot_received_at, "snapshot received_at")
    if snapshot_at < completed:
        return {
            "state": "command_succeeded_awaiting_fresh_snapshot",
            **common,
            "completed_at": completed.isoformat(),
            "snapshot_received_at": snapshot_at.isoformat(),
        }
    if positions_total or orders_total:
        return {
            "state": "command_succeeded_exposure_present",
            **common,
            "positions": positions_total,
            "orders": orders_total,
            "snapshot_received_at": snapshot_at.isoformat(),
        }
    return {
        "state": "command_succeeded_flat",
        **common,
        "positions": 0,
        "orders": 0,
        "snapshot_received_at": snapshot_at.isoformat(),
    }


def close_idempotency_key(
    *, session_id: str, reason: str, snapshot_received_at: str, last_bar: str,
) -> tuple[str, str]:
    identity = json.dumps(
        {
            "session_id": session_id,
            "reason": reason,
            "snapshot_received_at": snapshot_received_at,
            "last_bar": last_bar,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"close:{digest}", digest


def reconcile_completed_lifecycles(
    l0: "L1ExecutionOlap",
    *,
    account_fingerprint: str,
    symbol: str,
    producer: dict[str, Any],
    snapshot_max_age_seconds: float = 600.0,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """AUD-F2-20260804-104: reconcile collected MT5 deal/history events
    into accepted/filled/closed L0 lifecycle facts and release the
    reservation — idempotently, through the accepted APIs only.

    Preconditions per active reservation (anything unproven skips):

    - a fresh account snapshot proves the account flat (zero positions,
      zero orders) — broker-flat evidence gathered by the bridge;
    - the reservation holds no open exposure;
    - exactly one succeeded execution command exists for its idempotency
      identity (retry variants included) carrying the entry order ticket;
    - the entry DEAL_ADD event exists for that ticket, and later
      DEAL_ADD events on the same symbol/account (foreign tickets and
      symbols excluded, duplicates collapsed by deal ticket, order
      restored by observation time) sum to at least the entry volume —
      a partial close leaves the reservation held.

    The repair appends only the MISSING lifecycle stages
    (accepted/filled/closed) with broker tickets in ``broker_ids`` and
    releases the reservation as consumed, all in one atomic unit, so
    restart and replay can never duplicate a close or alter balances.
    """
    now = now or _utc_now()
    connection = l0._con
    snapshot = connection.execute(
        "SELECT positions_total, orders_total, received_at FROM"
        " account_snapshots WHERE account_fingerprint=?"
        " ORDER BY id DESC LIMIT 1",
        (account_fingerprint,),
    ).fetchone()
    if snapshot is None:
        return []
    positions_total, orders_total, received_at = snapshot
    age = (now - datetime.fromisoformat(received_at)).total_seconds()
    if age > snapshot_max_age_seconds:
        return []
    if int(positions_total) != 0 or int(orders_total) != 0:
        return []

    repaired: list[dict[str, Any]] = []
    for reservation in l0.active_reservation_intents():
        reservation_id = reservation["reservation_id"]
        key = str(reservation["idempotency_key"])
        if l0.reservation_has_open_exposure(reservation_id):
            continue
        command = connection.execute(
            "SELECT command_id, action, volume, result_json FROM"
            " execution_commands WHERE state='succeeded' AND"
            " account_fingerprint=? AND (idempotency_key=? OR"
            " idempotency_key LIKE ?)",
            (account_fingerprint, key, key + ":retry:%"),
        ).fetchall()
        if len(command) != 1:
            continue                       # unknown linkage: never guessed
        command_id, action, volume, result_json = command[0]
        result = json.loads(result_json or "{}")
        entry_ticket = str(result.get("order_ticket") or "")
        if not entry_ticket:
            continue
        entry = connection.execute(
            "SELECT price, volume, terminal_observed_at FROM trade_events"
            " WHERE event_type='TRADE_TRANSACTION_DEAL_ADD' AND"
            " order_ticket=? AND symbol=? AND account_fingerprint=?"
            " AND volume > 0",
            (entry_ticket, symbol, account_fingerprint),
        ).fetchone()
        if entry is None:
            continue
        entry_price, entry_volume, entry_at = entry
        closes = connection.execute(
            "SELECT DISTINCT deal_ticket, price, volume,"
            " terminal_observed_at FROM trade_events WHERE"
            " event_type='TRADE_TRANSACTION_DEAL_ADD' AND symbol=? AND"
            " account_fingerprint=? AND order_ticket != ? AND volume > 0"
            " AND terminal_observed_at > ?"
            " ORDER BY terminal_observed_at",
            (symbol, account_fingerprint, entry_ticket, entry_at),
        ).fetchall()
        seen: set[str] = set()
        closed_volume = 0.0
        close_notional = 0.0
        close_tickets: list[str] = []
        for deal_ticket, price, close_volume, _at in closes:
            if deal_ticket in seen:
                continue                   # duplicate event collapsed
            seen.add(deal_ticket)
            closed_volume += float(close_volume)
            close_notional += float(close_volume) * float(price)
            close_tickets.append(str(deal_ticket))
        if closed_volume + 1e-9 < float(entry_volume):
            continue                       # partial close: stay held
        close_vwap = close_notional / closed_volume if closed_volume else 0.0

        order_intent_id = f"oi2-{reservation_id}"
        existing = {
            row[0] for row in connection.execute(
                "SELECT state FROM lifecycle_events WHERE order_intent_id=?",
                (order_intent_id,),
            )
        }
        sign = -1.0 if action == "open_short" else 1.0
        units = sign * float(entry_volume)
        with l0.atomic_unit():
            for state, price, tickets in (
                ("accepted", None, {"order_ticket": entry_ticket}),
                ("filled", float(entry_price),
                 {"order_ticket": entry_ticket}),
                ("closed", close_vwap,
                 {"order_ticket": entry_ticket,
                  "close_deals": ",".join(close_tickets)}),
            ):
                if state in existing:
                    continue
                extra: dict[str, Any] = {}
                if state != "accepted":
                    # filled_units is a magnitude by contract; direction
                    # lives in requested_units.
                    extra = {"filled_units": float(entry_volume),
                             "filled_price": price}
                l0.append_lifecycle(ExecutionReportV2(
                    object_id=f"er-{reservation_id}-{state}", as_of=now,
                    producer=producer,
                    trace_id=f"mt5-reconcile-{reservation_id}",
                    order_intent_id=order_intent_id,
                    attempt_id=f"attempt-{reservation_id}",
                    bracket_role="parent", state=state,
                    requested_units=units,
                    broker_ids=tickets,
                    reconciliation_required=False,
                    **extra,
                ))
            l0.release(reservation_id, "consumed")
        repaired.append({
            "reservation_id": reservation_id,
            "command_id": command_id,
            "entry_ticket": entry_ticket,
            "close_deals": close_tickets,
            "closed_volume": closed_volume,
        })
    return repaired


class Mt5ModelRunner:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.bridge_config = Mt5ExecutionConfig.load(_path(config["bridge_config_file"]))
        self.bridge_store = Mt5ExecutionStore(self.bridge_config.database_path)
        config["service"]["database_path"] = str(self.bridge_config.database_path)
        self.l0 = L1ExecutionOlap(self.bridge_config.database_path)
        self.service = DemoExecutionService(
            DemoExecutionConfig.from_dict(config["service"]), self.l0,
            ZeroNetworkSink(),
        )
        self.sessions = ModelSessionStore(self.l0._con)
        self.policy_type = str(config["model"].get("policy_type", "linear"))
        selector_type = {
            "linear": SelectedLinearPolicy,
            "sac": SelectedSacPolicy,
        }.get(self.policy_type)
        if selector_type is None:
            raise Mt5ModelRunnerError("unsupported model policy_type")
        self.selector = selector_type(
            manifest_file=config["model"]["manifest_file"],
            expected_asset_id=config["model"]["expected_asset_id"],
            expected_timeframe=config["model"]["expected_timeframe"],
            execution_tier=config["model"]["execution_tier"],
        )
        self.manifest = self.selector.manifest
        self.policy = self.selector.policy
        self.sac_observation_spec = (
            load_live_observation_spec(self.selector)
            if self.policy_type == "sac" else None
        )

    def _latest_snapshot(self) -> Optional[dict[str, Any]]:
        row = self.bridge_store.connection.execute(
            "SELECT payload_json,received_at FROM account_snapshots "
            "WHERE account_fingerprint=? ORDER BY id DESC LIMIT 1",
            (self.bridge_config.account_fingerprint,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        payload["_received_at"] = row[1]
        return payload

    def _symbol_fact(self, snapshot: dict[str, Any]) -> Optional[dict[str, Any]]:
        symbol = self.config["route"]["symbol"]
        return next((item for item in snapshot.get("symbols", [])
                     if item.get("symbol") == symbol), None)

    def _bars(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        symbol = self.config["route"]["symbol"]
        timeframe = self.policy.timeframe
        bars = [
            {
                "time": item["time"], "open": item["open"],
                "high": item["high"], "low": item["low"],
                "close": item["close"], "volume": item["volume"],
                "complete": True,
            }
            for item in snapshot.get("bars", [])
            if item.get("symbol") == symbol and item.get("timeframe") == timeframe
        ]
        return sorted(bars, key=lambda item: item["time"])

    def _capability(
        self, snapshot: dict[str, Any], symbol_fact: dict[str, Any], now: datetime
    ) -> BrokerCapabilitySnapshot:
        symbol = self.config["route"]["symbol"]
        source_hash = hashlib.sha256(
            json.dumps(symbol_fact, sort_keys=True).encode()
        ).hexdigest()
        return BrokerCapabilitySnapshot(
            object_id=f"mt5-cap-{snapshot['_received_at']}", as_of=now,
            producer={"name": "lts.mt5_model_runner", "version": "0.1.0"},
            trace_id=f"mt5-cap-{source_hash[:16]}", venue="mt5_demo",
            account_fingerprint=self.bridge_config.account_fingerprint,
            environment="demo", capability_evidence="live_observed",
            source_artifact_hash="sha256:" + source_hash,
            source_observed_at=datetime.fromisoformat(snapshot["_received_at"]),
            instruments=[InstrumentCapability(
                instrument=symbol, tradeable=int(symbol_fact["trade_mode"]) != 0,
                shortable=True, min_units=float(symbol_fact["volume_min"]),
                unit_step=float(symbol_fact["volume_step"]), price_decimals=max(
                    0, len(str(symbol_fact["point"]).split(".")[-1].rstrip("0"))
                ), margin_rate=1.0, native_stop_loss=True,
                native_take_profit=True, native_bracket=True,
            )],
        )

    def _queue_close(
        self,
        *,
        snapshot: dict[str, Any],
        current_session: dict[str, Any],
        last_bar: str,
        reason: str,
    ) -> dict[str, Any]:
        idempotency_key, input_sha256 = close_idempotency_key(
            session_id=current_session["session_id"],
            reason=reason,
            snapshot_received_at=snapshot["_received_at"],
            last_bar=last_bar,
        )
        return self.bridge_store.enqueue(
            config=self.bridge_config,
            idempotency_key=idempotency_key,
            action="close", symbol=self.config["route"]["symbol"],
            volume=0, stop_loss=0, take_profit=0,
            model_id=self.policy.model_id,
            artifact_sha256=self.policy.artifact_sha256,
            config_sha256=self.manifest["config_sha256"],
            input_sha256=input_sha256,
        )

    def tick(self) -> dict[str, Any]:
        now = _utc_now()
        selection_error = None
        try:
            if self.selector.refresh():
                self.manifest = self.selector.manifest
                self.policy = self.selector.policy
                self.sac_observation_spec = (
                    load_live_observation_spec(self.selector)
                    if self.policy_type == "sac" else None
                )
        except (
            LiveModelSelectionError, LiveSacSelectionError,
            SacObservationError,
        ) as exc:
            selection_error = str(exc)
        snapshot = self._latest_snapshot()
        if snapshot is None:
            return {"state": "waiting_for_snapshot"}
        received = datetime.fromisoformat(snapshot["_received_at"])
        if (now - received).total_seconds() > self.config["snapshot_max_age_seconds"]:
            return {"state": "snapshot_stale", "received_at": snapshot["_received_at"]}
        symbol = self.config["route"]["symbol"]
        symbol_fact = self._symbol_fact(snapshot)
        if symbol_fact is None:
            return {"state": "symbol_unavailable", "symbol": symbol}
        lifecycles_reconciled = reconcile_completed_lifecycles(
            self.l0,
            account_fingerprint=self.bridge_config.account_fingerprint,
            symbol=symbol,
            producer={"name": "lts.mt5_model_runner", "version": "0.1.0"},
            now=now,
        )
        if lifecycles_reconciled:
            self.write_heartbeat({
                "state": "lifecycle_reconciled",
                "lifecycles_reconciled": lifecycles_reconciled,
            })
        bars = self._bars(snapshot)
        minimum_bars = 800 if self.policy_type == "sac" else 51
        if len(bars) < minimum_bars:
            return {"state": "waiting_for_closed_bars", "bars": len(bars)}
        positions = [item for item in snapshot.get("positions", [])
                     if item.get("symbol") == symbol]
        orders = [item for item in snapshot.get("orders", [])
                  if item.get("symbol") == symbol]
        current = self.sessions.active(
            "mt5_demo", self.bridge_config.account_fingerprint, symbol
        )
        changed = current is not None and (
            current["model_id"] != self.policy.model_id
            or current["artifact_sha256"] != self.policy.artifact_sha256
            or current["config_sha256"] != self.manifest["config_sha256"]
        )
        if changed:
            if positions or orders:
                command = self._queue_close(
                    snapshot=snapshot,
                    current_session=current,
                    last_bar=bars[-1]["time"],
                    reason="model_switch",
                )
                return {"state": "draining_for_model_switch",
                        "command_id": command["command_id"]}
            self.sessions.end(
                current["session_id"], balance=float(snapshot["balance"]),
                equity=float(snapshot["equity"]),
            )
            current = None
        if current is None:
            if positions or orders:
                return {"state": "blocked_foreign_exposure"}
            if selection_error:
                return {"state": "selection_refused", "reason": selection_error,
                        "commands_queued": 0}
            current = self.sessions.activate(
                venue="mt5_demo", account=self.bridge_config.account_fingerprint,
                symbol=symbol, model_id=self.policy.model_id,
                artifact_sha256=self.policy.artifact_sha256,
                config_sha256=self.manifest["config_sha256"],
                balance=float(snapshot["balance"]), equity=float(snapshot["equity"]),
            )
        if positions:
            unprotected = [item for item in positions
                           if float(item.get("stop_loss", 0)) <= 0
                           or float(item.get("take_profit", 0)) <= 0]
            if unprotected:
                command = self._queue_close(
                    snapshot=snapshot,
                    current_session=current,
                    last_bar=bars[-1]["time"],
                    reason="unprotected_position",
                )
                return {"state": "closing_unprotected_position",
                        "command_id": command["command_id"]}
        if selection_error:
            if positions or orders:
                return {"state": "monitoring", "positions": len(positions),
                        "orders": len(orders), "model_id": self.policy.model_id,
                        "selection_error": selection_error}
            return {"state": "selection_refused", "reason": selection_error,
                    "commands_queued": 0}

        exposure = sum(
            float(item.get("volume", 0.0))
            * (-1.0 if str(item.get("side", "long")).lower() == "short" else 1.0)
            for item in positions
        )
        pending_entry = bool(orders and not positions)
        if self.policy_type == "sac":
            if len(positions) > 1:
                return {"state": "blocked_multiple_route_positions"}
            entry_price = 0.0
            holding_bars = 0
            if positions:
                entry_price = float(positions[0].get("price_open", 0.0))
                if positions[0].get("time_open_unix") is None:
                    return {"state": "waiting_for_position_open_time"}
                opened = _mt5_utc(positions[0]["time_open_unix"])
                bar_time = datetime.fromisoformat(
                    bars[-1]["time"].replace("Z", "+00:00")
                )
                holding_bars = max(
                    0, int((bar_time - opened).total_seconds() // (4 * 3600))
                )
            spec = self.sac_observation_spec or {}
            built = build_sac_observation(
                bars,
                feature_columns=spec["feature_columns"],
                binary_columns=spec["binary_columns"],
                window_size=spec["window_size"],
                scaling_window=spec["scaling_window"],
                clip=spec["clip"],
                initial_equity=float(current["starting_equity"]),
                current_equity=float(snapshot["equity"]),
                position_units=exposure,
                entry_price=entry_price,
                holding_bars=holding_bars,
                holding_duration_scale_bars=spec[
                    "holding_duration_scale_bars"
                ],
            )
            inference = self.policy.predict(
                built["observation"], current_exposure=exposure,
                pending_entry=pending_entry,
            )
            inference.update({
                "last_closed_bar": built["last_closed_bar"],
                "feature_rows": built["feature_rows"],
                "observation_dimension": built["observation_dimension"],
            })
        else:
            observation = build_closed_bar_features(bars[-60:])
            inference = self.policy.predict(observation)
        self.sessions.record_inference(current["session_id"], inference)
        control = decide_position_control(
            inference["action"], current_exposure=exposure,
            pending_entry=pending_entry,
        )
        if control.disposition == "close" and positions:
            command = self._queue_close(
                snapshot=snapshot,
                current_session=current,
                last_bar=bars[-1]["time"],
                reason=control.reason,
            )
            self._record_due_bar(
                inference, bars[-1]["time"],
                outcome="model_close_requested", reason=control.reason,
                command_id=command["command_id"],
            )
            return {
                "state": "closing_on_model_signal",
                "command_id": command["command_id"],
                "inference": inference, "control": control.reason,
            }
        if positions or orders:
            self._record_due_bar(
                inference, bars[-1]["time"],
                outcome="monitoring_open_exposure", reason=control.reason,
            )
            return {"state": (
                        "monitoring" if positions else "monitoring_pending_order"
                    ), "positions": len(positions), "orders": len(orders),
                    "model_id": self.policy.model_id,
                    "inference": inference, "control": control.reason,
                    "selection_error": selection_error}
        if control.disposition == "hold":
            self._record_due_bar(inference, bars[-1]["time"],
                                 outcome="hold", reason=control.reason)
            return {"state": "hold", "inference": inference}
        bid, ask = float(symbol_fact["bid"]), float(symbol_fact["ask"])
        reference = (bid + ask) / 2.0
        side = 1.0 if inference["action"] == "long" else -1.0
        stop_fraction = float(self.config["strategy"]["stop_fraction"])
        take_fraction = float(self.config["strategy"]["take_profit_fraction"])
        digits = int(round(-__import__("math").log10(float(symbol_fact["point"]))))
        stop = round(reference * (1.0 - side * stop_fraction), digits)
        take = round(reference * (1.0 + side * take_fraction), digits)
        last_bar = datetime.fromisoformat(bars[-1]["time"].replace("Z", "+00:00"))
        intent = AssetIntent(
            object_id=f"{self.policy.model_id}:{bars[-1]['time']}",
            as_of=last_bar + timedelta(hours=4),
            valid_until=last_bar + timedelta(hours=8),
            producer={"name": f"prediction_provider.live_{self.policy_type}_policy",
                      "version": "0.1.0"},
            trace_id=f"mt5-{inference['input_sha256'][:16]}",
            config_hash="sha256:" + self.manifest["config_sha256"],
            cell_id=f"crypto:ETHUSD@4h:{self.policy.model_id}",
            asset_id=self.policy.asset_id, action="target",
            target_exposure=side, confidence=(
                max(inference["probability_up"],
                    1.0 - inference["probability_up"])
                if "probability_up" in inference
                else min(1.0, abs(float(inference["raw_action"])))
            ), strategy_rel_volume=1.0,
            risk_geometry={"mode": "fixed_price", "stop_price": stop,
                           "take_profit_price": take},
            reason_codes=[f"model:{self.policy.model_id}",
                          f"input:{inference['input_sha256']}",
                          "mt5_demo_infrastructure_canary"],
            artifact_hash="sha256:" + self.policy.artifact_sha256,
        )
        capability = self._capability(snapshot, symbol_fact, now)
        decision = self.service.process_intent(
            intent, capability, equity=float(snapshot["equity"]),
            reference_price=reference,
            quote_time=datetime.fromisoformat(symbol_fact["observed_at"]),
            instrument=symbol, now=now,
        )
        if decision["outcome"] != "would_be_order":
            self._record_due_bar(
                inference, bars[-1]["time"],
                outcome=str(decision.get("outcome")),
                reason=decision.get("reason"),
                quote={"bid": bid, "ask": ask},
                risk={"stop_price": stop, "take_profit_price": take,
                      "target_exposure": side})
            return {"state": "l0_refused", "decision": decision,
                    "inference": inference}
        decision_key = str(
            (decision.get("payload") or {}).get("idempotency_key") or ""
        )
        if not decision_key:
            raise Mt5ModelRunnerError(
                "accepted MT5 decision is missing idempotency_key")
        durable = durable_command_heartbeat(
            self.bridge_store,
            account_fingerprint=self.bridge_config.account_fingerprint,
            idempotency_key=decision_key,
            snapshot_received_at=snapshot["_received_at"],
            positions_total=len(positions),
            orders_total=len(orders),
        )
        if durable is not None:
            durable["inference"] = inference
            return durable
        pending = self.l0.l1_pending_decisions("would_be_order")
        accepted = next((row for row in pending
                         if row["idempotency_key"] == decision_key), None)
        if accepted is None:
            return {"state": "replayed_signal", "decision": decision}
        from trading_contracts import OrderIntentV2
        order_intent = OrderIntentV2.model_validate_json(accepted["intent_json"])
        command = self.bridge_store.enqueue(
            config=self.bridge_config,
            idempotency_key=order_intent.idempotency_key,
            action="open_long" if order_intent.delta_units > 0 else "open_short",
            symbol=symbol, volume=abs(float(order_intent.delta_units)),
            stop_loss=float(order_intent.protection.stop_loss_price),
            take_profit=float(order_intent.protection.take_profit_price),
            model_id=self.policy.model_id,
            artifact_sha256=self.policy.artifact_sha256,
            config_sha256=self.manifest["config_sha256"],
            input_sha256=inference["input_sha256"],
        )
        self._record_due_bar(
            inference, bars[-1]["time"],
            outcome="would_be_order", reason=None,
            quote={"bid": bid, "ask": ask},
            risk={"stop_price": stop, "take_profit_price": take,
                  "target_exposure": side},
            command_id=command["command_id"])
        return {"state": "command_queued", "command_id": command["command_id"],
                "inference": inference, "decision": decision}

    def _record_due_bar(self, inference, bar_close, *, outcome,
                        reason=None, quote=None, risk=None,
                        command_id=None):
        """Order C1: one normalized decision fact per due closed bar —
        HOLDs and refusals included. Never raises into the tick."""
        try:
            self.l0.record_due_bar_decision({
                "venue": "mt5_demo",
                "account_fingerprint":
                    self.bridge_config.account_fingerprint,
                "asset_id": self.policy.asset_id,
                "instrument": self.config["route"]["symbol"],
                "timeframe": self.config["model"]["expected_timeframe"],
                "bar_close": bar_close,
                "decided_at": _utc_now().isoformat(),
                "feature_cutoff": bar_close,
                "input_sha256": inference["input_sha256"],
                "config_sha256": self.manifest["config_sha256"],
                "model_id": self.policy.model_id,
                "artifact_sha256": self.policy.artifact_sha256,
                "manifest_sha256": self.manifest.get("manifest_sha256"),
                "action": inference["action"],
                "score": inference.get(
                    "probability_up", inference.get("raw_action")
                ),
                "outcome": outcome, "reason": reason,
                "risk_envelope": risk, "quote": quote,
                "decision_id": f"{self.policy.model_id}:{bar_close}",
                "effect_or_command_id": command_id,
            })
        except Exception as exc:
            print(json.dumps({"due_bar_fact_error": str(exc)[:160]}),
                  flush=True)

    def close(self) -> None:
        self.l0.close()
        self.bridge_store.close()

    def write_heartbeat(self, payload: dict[str, Any]) -> None:
        runtime = {
            **payload,
            **selected_model_identity(self.selector),
            "policy_type": self.policy_type,
            "venue": "mt5_demo",
            "environment": "demo",
            "read_only": False,
            "account_binding_verified": True,
            "account_fingerprint": self.bridge_config.account_fingerprint,
            "instrument": self.config["route"]["symbol"],
        }
        write_runner_heartbeat(
            self.config.get(
                "heartbeat_path", "~/.local/state/lts/mt5-model-runner-heartbeat.json"
            ),
            schema="lts.mt5.model_runner.heartbeat.v1",
            payload=runtime,
        )


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "lts.mt5.model_runner.v1":
        raise Mt5ModelRunnerError("unsupported MT5 model runner config")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    runner = Mt5ModelRunner(load_config(args.config))
    stopped = threading.Event()

    def stop(*_args):
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopped.is_set():
            try:
                result = runner.tick()
                runner.write_heartbeat(result)
                print(json.dumps(result, sort_keys=True, default=str), flush=True)
            except Exception as exc:
                runner.write_heartbeat({
                    "state": "degraded_error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "commands_queued": None,
                })
                raise
            if args.once:
                break
            stopped.wait(float(runner.config["loop_seconds"]))
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
