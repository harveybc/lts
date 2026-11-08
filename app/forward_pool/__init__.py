"""Offline hourly forward model pool. No broker, no live ledger, no training."""

from app.forward_pool.adapter import authenticate_bundle, greedy_signal
from app.forward_pool.cohort import (
    SELECTED70_MODEL_HEX,
    CandidateSpec,
    PoolService,
    WindowMetrics,
    explain_divergence,
    performance_report,
    select_week,
)
from app.forward_pool.core import BusinessContract, PoolRefusal, command_checked, order_units

__all__ = [
    "SELECTED70_MODEL_HEX",
    "BusinessContract",
    "CandidateSpec",
    "PoolRefusal",
    "PoolService",
    "WindowMetrics",
    "authenticate_bundle",
    "command_checked",
    "explain_divergence",
    "greedy_signal",
    "order_units",
    "performance_report",
    "select_week",
]
