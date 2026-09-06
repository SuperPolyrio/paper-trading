"""Domain contracts for delayed Polymarket rewards and rebates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class RewardType(str, Enum):
    MAKER_REBATE = "MAKER_REBATE"
    TAKER_REBATE = "TAKER_REBATE"
    LIQUIDITY_REWARD = "LIQUIDITY_REWARD"
    HOLDING_REWARD = "HOLDING_REWARD"
    REFERRAL_REWARD = "REFERRAL_REWARD"
    SPONSOR_REWARD = "SPONSOR_REWARD"
    DISPUTE_REWARD = "DISPUTE_REWARD"


class RewardStatus(str, Enum):
    ESTIMATED = "ESTIMATED"
    ACCRUED = "ACCRUED"
    PAYABLE = "PAYABLE"
    RECEIVED = "RECEIVED"
    FAILED = "FAILED"
    VOIDED = "VOIDED"
    CLAWED_BACK = "CLAWED_BACK"


NON_CASH_STATUSES = frozenset(
    {RewardStatus.ESTIMATED, RewardStatus.ACCRUED, RewardStatus.PAYABLE}
)


@dataclass(frozen=True)
class RewardSchedule:
    schedule_id: str
    reward_type: RewardType
    scope_key: str
    effective_from: datetime
    effective_until: datetime | None
    currency: str = "USDC"
    condition_id: str | None = None
    asset_id: str | None = None
    category: str | None = None
    source: str = "UNSPECIFIED"
    source_event_id: str | None = None
    economics_regime_id: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.schedule_id or not self.scope_key:
            raise ValueError("reward schedule id and scope key are required")
        if self.effective_from.tzinfo is None:
            raise ValueError("reward schedule effective_from must be timezone-aware")
        if (
            self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError(
                "reward schedule effective_until must follow effective_from"
            )
        if not self.currency:
            raise ValueError("reward schedule currency is required")

    def applies_at(self, at: datetime) -> bool:
        return self.effective_from <= at and (
            self.effective_until is None or at < self.effective_until
        )

    @property
    def regime_id(self) -> str:
        return self.economics_regime_id or self.schedule_id


@dataclass(frozen=True)
class RewardAccrual:
    accrual_id: str
    strategy_id: str
    account_id: str
    reward_type: RewardType
    status: RewardStatus
    amount: Decimal
    currency: str
    period_start: datetime
    period_end: datetime
    effective_ts: datetime
    idempotency_key: str
    schedule_id: str | None = None
    condition_id: str | None = None
    asset_id: str | None = None
    source: str = "MODEL"
    source_event_id: str | None = None
    model_version: str = "reward-ledger-v1"
    economics_regime_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((self.accrual_id, self.strategy_id, self.account_id)):
            raise ValueError("reward accrual identity is incomplete")
        if self.amount < 0:
            raise ValueError("reward accrual amount must be non-negative")
        if self.status not in NON_CASH_STATUSES:
            raise ValueError("accrual status must be ESTIMATED, ACCRUED or PAYABLE")
        _validate_window(self.period_start, self.period_end, self.effective_ts)


@dataclass(frozen=True)
class OfficialRewardRecord:
    source: str
    source_event_id: str
    reward_type: RewardType
    account_id: str
    amount: Decimal
    currency: str
    reward_date: date
    status: RewardStatus
    condition_id: str | None = None
    asset_id: str | None = None
    source_tx_hash: str | None = None
    raw_payload_hash: str | None = None
    rule_version: str = "official-reward-v1"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source or not self.source_event_id or not self.account_id:
            raise ValueError("official reward source identity is incomplete")
        if not self.rule_version:
            raise ValueError("official reward rule version is required")
        if self.amount < 0:
            raise ValueError("official reward amount must be non-negative")
        if self.status not in {
            RewardStatus.ACCRUED,
            RewardStatus.PAYABLE,
            RewardStatus.RECEIVED,
        }:
            raise ValueError("unsupported official reward status")


@dataclass(frozen=True)
class RewardPayout:
    payout_id: str
    strategy_id: str
    account_id: str
    reward_type: RewardType
    status: RewardStatus
    amount: Decimal
    currency: str
    reward_date: date
    effective_ts: datetime
    idempotency_key: str
    source: str
    source_event_id: str
    schedule_id: str | None = None
    condition_id: str | None = None
    asset_id: str | None = None
    source_tx_hash: str | None = None
    raw_payload_hash: str | None = None
    economics_regime_id: str | None = None
    rule_version: str = "reward-ledger-v1"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("reward payout amount must be non-negative")
        if not self.rule_version:
            raise ValueError("reward payout rule version is required")
        if self.status not in {
            RewardStatus.PAYABLE,
            RewardStatus.RECEIVED,
            RewardStatus.FAILED,
            RewardStatus.VOIDED,
        }:
            raise ValueError("unsupported reward payout status")
        if self.effective_ts.tzinfo is None:
            raise ValueError("reward payout effective_ts must be timezone-aware")


@dataclass(frozen=True)
class RewardReconciliation:
    reconciliation_id: str
    accrual_id: str
    payout_id: str
    status: str
    modeled_amount: Decimal
    official_amount: Decimal
    allocated_amount: Decimal
    amount_delta: Decimal
    tolerance: Decimal
    reconciled_at: datetime


@dataclass(frozen=True)
class RewardClawback:
    clawback_id: str
    strategy_id: str
    account_id: str
    original_payout_id: str
    reward_type: RewardType
    amount: Decimal
    currency: str
    status: RewardStatus
    effective_ts: datetime
    reason: str
    idempotency_key: str
    source: str
    source_event_id: str
    source_tx_hash: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("reward clawback amount must be positive")
        if self.status not in {RewardStatus.RECEIVED, RewardStatus.PAYABLE}:
            raise ValueError("reward clawback status must be PAYABLE or RECEIVED")
        if self.effective_ts.tzinfo is None:
            raise ValueError("reward clawback effective_ts must be timezone-aware")


def deterministic_reward_id(prefix: str, payload: Mapping[str, Any]) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def payload_hash(payload: Mapping[str, Any]) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_window(start: datetime, end: datetime, effective: datetime) -> None:
    if any(value.tzinfo is None for value in (start, end, effective)):
        raise ValueError("reward timestamps must be timezone-aware")
    if end <= start:
        raise ValueError("reward period end must follow start")
