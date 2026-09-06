"""Typed evidence contracts for CTF exchange settlement paths."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class CtfSettlementMatchType(str, Enum):
    COMPLEMENTARY = "COMPLEMENTARY"
    MINT = "MINT"
    MERGE = "MERGE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CtfMatchEvidence:
    evidence_id: str
    evidence_source: str
    maker_side: str
    taker_side: str
    maker_asset_id: str
    taker_asset_id: str
    complementary_assets: bool | None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence_id.strip() or not self.evidence_source.strip():
            raise ValueError("CTF match evidence requires id and source")
        for side in (self.maker_side, self.taker_side):
            if side.upper() not in {"BUY", "SELL"}:
                raise ValueError(f"unsupported CTF order side: {side}")
        if not self.maker_asset_id or not self.taker_asset_id:
            raise ValueError("CTF match evidence requires both asset ids")


@dataclass(frozen=True)
class CtfConservationDeltas:
    """Aggregate participant deltas for one matched complete-set quantity."""

    quantity: Decimal
    collateral_delta: Decimal
    outcome_a_delta: Decimal
    outcome_b_delta: Decimal

    def __post_init__(self) -> None:
        if Decimal(self.quantity) <= 0:
            raise ValueError("CTF conservation quantity must be positive")


@dataclass(frozen=True)
class CtfConservationResult:
    status: str
    reason: str
    conservation_hash: str
    expected: Mapping[str, str]
    observed: Mapping[str, str]


@dataclass(frozen=True)
class CtfSettlementAudit:
    evidence_id: str
    evidence_source: str
    settlement_match_type: CtfSettlementMatchType
    conservation: CtfConservationResult
    evidence: Mapping[str, Any] = field(default_factory=dict)
