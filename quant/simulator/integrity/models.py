"""Typed surveillance observations, findings and review states."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class IntegrityFindingType(str, Enum):
    SELF_DEALING = "SELF_DEALING"
    WASH_TRADING = "WASH_TRADING"
    SPOOFING_LAYERING = "SPOOFING_LAYERING"
    FRONT_RUNNING = "FRONT_RUNNING"
    OUTCOME_INFLUENCE = "OUTCOME_INFLUENCE"
    CONFIDENTIAL_INFORMATION = "CONFIDENTIAL_INFORMATION"
    FICTITIOUS_TRANSACTION = "FICTITIOUS_TRANSACTION"
    DISRUPTIVE_PRACTICE = "DISRUPTIVE_PRACTICE"


class CaseStatus(str, Enum):
    OPEN = "OPEN"
    TRIAGED = "TRIAGED"
    INVESTIGATING = "INVESTIGATING"
    DISMISSED = "DISMISSED"
    CONFIRMED = "CONFIRMED"
    REMEDIATED = "REMEDIATED"

    @property
    def terminal(self) -> bool:
        return self in {CaseStatus.DISMISSED, CaseStatus.REMEDIATED}


@dataclass(frozen=True)
class SurveillanceObservation:
    observation_id: str
    event_type: str
    account_id: str
    beneficial_owner_id: str | None
    condition_id: str
    asset_id: str | None
    side: str | None
    price: Decimal | None
    size: Decimal | None
    event_ts: datetime
    counterparty_account_id: str | None = None
    counterparty_owner_id: str | None = None
    order_id: str | None = None
    trade_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.event_ts.tzinfo is None:
            raise ValueError("surveillance event timestamp must be timezone-aware")
        if not self.observation_id or not self.account_id or not self.condition_id:
            raise ValueError("surveillance observation identity is required")


@dataclass(frozen=True)
class IntegrityEvidence:
    evidence_id: str
    finding_type: IntegrityFindingType
    severity: str
    account_id: str
    condition_id: str
    observed_at: datetime
    observation_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class IntegrityCase:
    case_id: str
    finding_type: IntegrityFindingType
    account_id: str
    condition_id: str
    status: CaseStatus
    severity: str
    policy_version: str
    opened_at: datetime
    assigned_to: str | None = None
    resolution_reason: str | None = None
    updated_at: datetime | None = None
