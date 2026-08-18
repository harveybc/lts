"""Pure train/live position-control semantics for selected model signals."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Signal = Literal["long", "short", "hold", "close"]
Disposition = Literal["enter_long", "enter_short", "monitor", "close", "hold"]


@dataclass(frozen=True)
class PositionControlDecision:
    disposition: Disposition
    reason: str


def decide_position_control(
    signal: str,
    *,
    current_exposure: float,
    pending_entry: bool = False,
) -> PositionControlDecision:
    """Translate a model signal without allowing one-step reversal.

    An opposite directional signal closes first. A later bar may open the
    opposite side after the venue proves flat. This keeps every transition
    risk-reducing and makes simulation and Paper/Demo behavior comparable.
    """
    if signal not in {"long", "short", "hold", "close"}:
        raise ValueError(f"unsupported model signal {signal!r}")
    side = 1 if current_exposure > 0 else (-1 if current_exposure < 0 else 0)
    if side == 0 and not pending_entry:
        if signal == "long":
            return PositionControlDecision("enter_long", "flat_long_target")
        if signal == "short":
            return PositionControlDecision("enter_short", "flat_short_target")
        return PositionControlDecision("hold", "flat_without_directional_target")
    if signal == "close":
        return PositionControlDecision("close", "explicit_model_close")
    if pending_entry and side == 0:
        if signal == "hold":
            return PositionControlDecision("monitor", "pending_entry_hold")
        return PositionControlDecision("close", "pending_entry_target_changed")
    if signal == "long" and side < 0:
        return PositionControlDecision("close", "opposite_long_target")
    if signal == "short" and side > 0:
        return PositionControlDecision("close", "opposite_short_target")
    return PositionControlDecision("monitor", "target_preserves_open_exposure")
