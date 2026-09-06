"""Effective-dated Polymarket referral reward economics."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from .models import deterministic_reward_id


REFERRAL_REWARD_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_referral_fee_evidence (
        evidence_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        referred_account_id TEXT NOT NULL,
        relationship TEXT NOT NULL,
        signup_ts TIMESTAMPTZ NOT NULL,
        trade_ts TIMESTAMPTZ NOT NULL,
        gross_platform_fee NUMERIC NOT NULL,
        referred_taker_rebate NUMERIC NOT NULL,
        net_platform_fee NUMERIC NOT NULL,
        referred_tier_level INTEGER NOT NULL,
        eligible BOOLEAN NOT NULL,
        ineligibility_reason TEXT,
        referral_program_schedule_id TEXT NOT NULL,
        source TEXT NOT NULL,
        source_event_id TEXT,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_referral_reward_daily_estimates (
        estimate_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        reward_date DATE NOT NULL,
        owner_lifetime_volume NUMERIC NOT NULL,
        eligible_evidence_count INTEGER NOT NULL,
        direct_net_fees NUMERIC NOT NULL,
        indirect_net_fees NUMERIC NOT NULL,
        estimated_reward NUMERIC NOT NULL,
        referral_program_schedule_id TEXT NOT NULL,
        evidence_class TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (strategy_id,account_id,reward_date,referral_program_schedule_id)
    )
    """,
)


class ReferralRelationship(str, Enum):
    DIRECT = "DIRECT"
    INDIRECT = "INDIRECT"


@dataclass(frozen=True)
class ReferralProgramSchedule:
    schedule_id: str
    effective_from: datetime
    effective_until: datetime | None
    minimum_owner_lifetime_volume: Decimal
    direct_rate: Decimal
    indirect_rate: Decimal
    earning_window_days: int
    platinum_tier_level: int
    source: str
    source_document_hash: str | None = None

    def __post_init__(self) -> None:
        if self.effective_from.tzinfo is None:
            raise ValueError("referral schedule must be timezone-aware")
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("referral schedule end must follow start")
        if self.minimum_owner_lifetime_volume < 0 or self.earning_window_days <= 0:
            raise ValueError("invalid referral eligibility window")
        if not Decimal(0) <= self.direct_rate <= Decimal(1):
            raise ValueError("direct referral rate must be within [0,1]")
        if not Decimal(0) <= self.indirect_rate <= Decimal(1):
            raise ValueError("indirect referral rate must be within [0,1]")
        if self.platinum_tier_level <= 0:
            raise ValueError("platinum tier level must be positive")

    def applies_at(self, at: datetime) -> bool:
        return self.effective_from <= at and (
            self.effective_until is None or at < self.effective_until
        )

    def rate(self, relationship: ReferralRelationship) -> Decimal:
        return self.direct_rate if relationship is ReferralRelationship.DIRECT else self.indirect_rate


@dataclass(frozen=True)
class ReferralTradeEvidence:
    evidence_id: str
    strategy_id: str
    account_id: str
    referred_account_id: str
    relationship: ReferralRelationship
    signup_ts: datetime
    trade_ts: datetime
    gross_platform_fee: Decimal
    referred_taker_rebate: Decimal
    referred_tier_level: int
    source: str
    source_event_id: str | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.signup_ts.tzinfo is None or self.trade_ts.tzinfo is None:
            raise ValueError("referral evidence timestamps must be timezone-aware")
        if min(self.gross_platform_fee, self.referred_taker_rebate) < 0:
            raise ValueError("referral fees and rebate must be non-negative")
        if self.referred_taker_rebate > self.gross_platform_fee:
            raise ValueError("referred taker rebate cannot exceed gross fee")
        if self.referred_tier_level < 0:
            raise ValueError("referred tier level cannot be negative")

    @property
    def net_platform_fee(self) -> Decimal:
        return self.gross_platform_fee - self.referred_taker_rebate


@dataclass(frozen=True)
class ReferralEvidenceAssessment:
    evidence: ReferralTradeEvidence
    eligible: bool
    ineligibility_reason: str | None
    reward_rate: Decimal
    estimated_reward: Decimal
    referral_program_schedule_id: str
    idempotency_key: str


@dataclass(frozen=True)
class ReferralRewardDailyEstimate:
    estimate_id: str
    strategy_id: str
    account_id: str
    reward_date: date
    owner_lifetime_volume: Decimal
    eligible_evidence_count: int
    direct_net_fees: Decimal
    indirect_net_fees: Decimal
    estimated_reward: Decimal
    referral_program_schedule_id: str
    evidence_class: str
    idempotency_key: str
    evidence_ids: tuple[str, ...]


def assess_referral_trade(
    evidence: ReferralTradeEvidence,
    *,
    owner_lifetime_volume: Decimal,
    program: ReferralProgramSchedule,
) -> ReferralEvidenceAssessment:
    reason: str | None = None
    if not program.applies_at(evidence.trade_ts):
        reason = "SCHEDULE_INACTIVE"
    elif Decimal(owner_lifetime_volume) < program.minimum_owner_lifetime_volume:
        reason = "OWNER_VOLUME_BELOW_THRESHOLD"
    elif evidence.trade_ts < evidence.signup_ts:
        reason = "TRADE_PRECEDES_SIGNUP"
    elif evidence.trade_ts >= evidence.signup_ts + timedelta(days=program.earning_window_days):
        reason = "REFERRAL_WINDOW_EXPIRED"
    elif evidence.referred_tier_level >= program.platinum_tier_level:
        reason = "REFERRED_USER_REACHED_PLATINUM"
    rate = program.rate(evidence.relationship)
    reward = evidence.net_platform_fee * rate if reason is None else Decimal(0)
    key = f"referral-evidence:{evidence.evidence_id}:{program.schedule_id}"
    return ReferralEvidenceAssessment(
        evidence=evidence,
        eligible=reason is None,
        ineligibility_reason=reason,
        reward_rate=rate,
        estimated_reward=reward,
        referral_program_schedule_id=program.schedule_id,
        idempotency_key=key,
    )


def estimate_daily_referral_reward(
    assessments: Iterable[ReferralEvidenceAssessment],
    *,
    strategy_id: str,
    account_id: str,
    reward_date: date,
    owner_lifetime_volume: Decimal,
    program: ReferralProgramSchedule,
) -> ReferralRewardDailyEstimate:
    rows = tuple(
        row
        for row in assessments
        if row.evidence.strategy_id == strategy_id
        and row.evidence.account_id.lower() == account_id.lower()
        and row.evidence.trade_ts.date() == reward_date
        and row.referral_program_schedule_id == program.schedule_id
    )
    eligible = tuple(row for row in rows if row.eligible)
    direct = sum(
        (
            row.evidence.net_platform_fee
            for row in eligible
            if row.evidence.relationship is ReferralRelationship.DIRECT
        ),
        Decimal(0),
    )
    indirect = sum(
        (
            row.evidence.net_platform_fee
            for row in eligible
            if row.evidence.relationship is ReferralRelationship.INDIRECT
        ),
        Decimal(0),
    )
    estimated = sum((row.estimated_reward for row in eligible), Decimal(0))
    key = (
        f"referral-daily:{strategy_id}:{account_id.lower()}:"
        f"{reward_date.isoformat()}:{program.schedule_id}"
    )
    return ReferralRewardDailyEstimate(
        estimate_id=deterministic_reward_id("referral-daily", {"key": key}),
        strategy_id=strategy_id,
        account_id=account_id,
        reward_date=reward_date,
        owner_lifetime_volume=Decimal(owner_lifetime_volume),
        eligible_evidence_count=len(eligible),
        direct_net_fees=direct,
        indirect_net_fees=indirect,
        estimated_reward=estimated,
        referral_program_schedule_id=program.schedule_id,
        evidence_class="MODELED_FROM_REFERRED_FEE_EVIDENCE",
        idempotency_key=key,
        evidence_ids=tuple(row.evidence.evidence_id for row in eligible),
    )


class PostgresReferralRewardStore:
    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS quant")
            for statement in REFERRAL_REWARD_SCHEMA_STATEMENTS:
                cur.execute(statement)
            conn.commit()

    def record_assessment(self, row: ReferralEvidenceAssessment) -> bool:
        item = row.evidence
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_referral_fee_evidence (
                    evidence_id,strategy_id,account_id,referred_account_id,
                    relationship,signup_ts,trade_ts,gross_platform_fee,
                    referred_taker_rebate,net_platform_fee,referred_tier_level,
                    eligible,ineligibility_reason,referral_program_schedule_id,
                    source,source_event_id,idempotency_key,metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (idempotency_key) DO NOTHING RETURNING evidence_id
                """,
                (
                    item.evidence_id,
                    item.strategy_id,
                    item.account_id,
                    item.referred_account_id,
                    item.relationship.value,
                    item.signup_ts,
                    item.trade_ts,
                    item.gross_platform_fee,
                    item.referred_taker_rebate,
                    item.net_platform_fee,
                    item.referred_tier_level,
                    row.eligible,
                    row.ineligibility_reason,
                    row.referral_program_schedule_id,
                    item.source,
                    item.source_event_id,
                    row.idempotency_key,
                    json.dumps(item.metadata or {}, sort_keys=True),
                ),
            )
            inserted = cur.fetchone() is not None
            if not inserted:
                cur.execute(
                    """
                    SELECT * FROM quant.paper_referral_fee_evidence
                    WHERE idempotency_key=%s
                    """,
                    (row.idempotency_key,),
                )
                existing = cur.fetchone()
                if existing is None or (
                    str(existing["evidence_id"]) != item.evidence_id
                    or str(existing["strategy_id"]) != item.strategy_id
                    or str(existing["account_id"]) != item.account_id
                    or str(existing["referred_account_id"]) != item.referred_account_id
                    or str(existing["relationship"]) != item.relationship.value
                    or Decimal(existing["gross_platform_fee"])
                    != item.gross_platform_fee
                    or Decimal(existing["referred_taker_rebate"])
                    != item.referred_taker_rebate
                    or Decimal(existing["net_platform_fee"]) != item.net_platform_fee
                    or int(existing["referred_tier_level"])
                    != item.referred_tier_level
                    or bool(existing["eligible"]) != row.eligible
                    or str(existing["ineligibility_reason"] or "")
                    != str(row.ineligibility_reason or "")
                    or str(existing["referral_program_schedule_id"])
                    != row.referral_program_schedule_id
                    or str(existing["source"]) != item.source
                    or str(existing["source_event_id"] or "")
                    != str(item.source_event_id or "")
                ):
                    raise ValueError("referral evidence idempotency collision")
            conn.commit()
        return inserted

    def record_daily_estimate(self, row: ReferralRewardDailyEstimate) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_referral_reward_daily_estimates (
                    estimate_id,strategy_id,account_id,reward_date,
                    owner_lifetime_volume,eligible_evidence_count,direct_net_fees,
                    indirect_net_fees,estimated_reward,referral_program_schedule_id,
                    evidence_class,idempotency_key,metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (idempotency_key) DO NOTHING RETURNING estimate_id
                """,
                (
                    row.estimate_id,
                    row.strategy_id,
                    row.account_id,
                    row.reward_date,
                    row.owner_lifetime_volume,
                    row.eligible_evidence_count,
                    row.direct_net_fees,
                    row.indirect_net_fees,
                    row.estimated_reward,
                    row.referral_program_schedule_id,
                    row.evidence_class,
                    row.idempotency_key,
                    json.dumps({"evidence_ids": row.evidence_ids}, sort_keys=True),
                ),
            )
            inserted = cur.fetchone() is not None
            if not inserted:
                cur.execute(
                    """
                    SELECT * FROM quant.paper_referral_reward_daily_estimates
                    WHERE idempotency_key=%s
                    """,
                    (row.idempotency_key,),
                )
                existing = cur.fetchone()
                if existing is None or (
                    str(existing["estimate_id"]) != row.estimate_id
                    or str(existing["strategy_id"]) != row.strategy_id
                    or str(existing["account_id"]) != row.account_id
                    or existing["reward_date"] != row.reward_date
                    or Decimal(existing["owner_lifetime_volume"])
                    != row.owner_lifetime_volume
                    or int(existing["eligible_evidence_count"])
                    != row.eligible_evidence_count
                    or Decimal(existing["direct_net_fees"]) != row.direct_net_fees
                    or Decimal(existing["indirect_net_fees"])
                    != row.indirect_net_fees
                    or Decimal(existing["estimated_reward"]) != row.estimated_reward
                    or str(existing["referral_program_schedule_id"])
                    != row.referral_program_schedule_id
                    or str(existing["evidence_class"]) != row.evidence_class
                    or tuple(existing["metadata"].get("evidence_ids", ()))
                    != row.evidence_ids
                ):
                    raise ValueError("referral estimate idempotency collision")
            conn.commit()
        return inserted
