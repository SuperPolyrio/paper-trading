"""Shared execution-domain contracts for paper, replay, and live adapters."""

from .domain import (
    CapacityStatus,
    FinalityState,
    ImpactMode,
    InternalOrderState,
    PnlTier,
)

__all__ = [
    "CapacityStatus",
    "FinalityState",
    "ImpactMode",
    "InternalOrderState",
    "PnlTier",
]
