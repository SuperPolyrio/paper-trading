"""Effective-dated expected holding reward model and deterministic sampling."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .models import deterministic_reward_id

HOLDING_REWARD_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_holding_reward_position_samples (
        position_sample_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        sample_ts TIMESTAMPTZ NOT NULL,
        quantity NUMERIC NOT NULL,
        mark_price NUMERIC NOT NULL,
        position_value NUMERIC NOT NULL CHECK (position_value >= 0),
        annual_rate NUMERIC NOT NULL CHECK (annual_rate >= 0),
        eligible BOOLEAN NOT NULL,
        expected_hourly_reward NUMERIC NOT NULL CHECK (expected_hourly_reward >= 0),
        reward_regime_id TEXT NOT NULL,
        sampling_method TEXT NOT NULL,
        evidence_class TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_holding_samples_account_ts_idx
    ON quant.paper_holding_reward_position_samples (account_id,sample_ts)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_holding_reward_daily_estimates (
        estimate_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        reward_date DATE NOT NULL,
        sample_count INTEGER NOT NULL,
        eligible_sample_count INTEGER NOT NULL,
        sampled_position_value NUMERIC NOT NULL,
        estimated_reward NUMERIC NOT NULL,
        carry_in NUMERIC NOT NULL,
        payable_amount NUMERIC NOT NULL,
        carry_out NUMERIC NOT NULL,
        minimum_payout NUMERIC NOT NULL,
        reward_regime_id TEXT NOT NULL,
        sampling_method TEXT NOT NULL,
        evidence_class TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (strategy_id,account_id,reward_date,reward_regime_id)
    )
    """,
)


@dataclass(frozen=True)
class HoldingRewardProgramSchedule:
    schedule_id: str
    effective_from: datetime
    effective_until: datetime | None
    annual_rate: Decimal
    eligible_condition_ids: frozenset[str]
    minimum_payout: Decimal = Decimal(1)
    source: str = "OFFICIAL_HOLDING_REWARD_SCHEDULE"
    source_url_hash: str | None = None

    def __post_init__(self) -> None:
        if self.effective_from.tzinfo is None:
            raise ValueError("holding reward schedule must be timezone-aware")
        if (
            self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("holding reward schedule end must follow start")
        if self.annual_rate < 0 or self.minimum_payout < 0:
            raise ValueError(
                "holding reward rate and payout minimum must be non-negative"
            )

    def applies_at(self, at: datetime) -> bool:
        return self.effective_from <= at and (
            self.effective_until is None or at < self.effective_until
        )


@dataclass(frozen=True)
class PositionValueObservation:
    strategy_id: str
    account_id: str
    condition_id: str
    asset_id: str
    sample_ts: datetime
    quantity: Decimal
    mark_price: Decimal
    sampling_method: str = "FIXED_SEED_RESEARCH_SAMPLE"
    evidence_class: str = "EXPECTED_HOLDING_REWARD"
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class HoldingRewardPositionSample:
    position_sample_id: str
    observation: PositionValueObservation
    position_value: Decimal
    annual_rate: Decimal
    eligible: bool
    expected_hourly_reward: Decimal
    reward_regime_id: str
    idempotency_key: str


@dataclass(frozen=True)
class HoldingRewardDailyEstimate:
    estimate_id: str
    strategy_id: str
    account_id: str
    reward_date: date
    sample_count: int
    eligible_sample_count: int
    sampled_position_value: Decimal
    estimated_reward: Decimal
    carry_in: Decimal
    payable_amount: Decimal
    carry_out: Decimal
    minimum_payout: Decimal
    reward_regime_id: str
    sampling_method: str
    evidence_class: str
    idempotency_key: str
    position_sample_ids: tuple[str, ...]


def deterministic_hourly_sample_ts(
    reward_date: date,
    hour: int,
    *,
    seed: str,
) -> datetime:
    if not 0 <= hour <= 23:
        raise ValueError("holding reward sample hour must be within [0,23]")
    digest = hashlib.sha256(
        f"{seed}:{reward_date.isoformat()}:{hour}".encode()
    ).digest()
    second = int.from_bytes(digest[:4], "big") % 3600
    return datetime.combine(
        reward_date, datetime.min.time(), tzinfo=timezone.utc
    ) + timedelta(hours=hour, seconds=second)


def sample_holding_position(
    observation: PositionValueObservation,
    *,
    program: HoldingRewardProgramSchedule,
) -> HoldingRewardPositionSample:
    if observation.sample_ts.tzinfo is None:
        raise ValueError("holding reward sample timestamp must be timezone-aware")
    if observation.quantity < 0 or not Decimal(0) <= observation.mark_price <= Decimal(
        1
    ):
        raise ValueError("invalid holding reward quantity or mark")
    if not program.applies_at(observation.sample_ts):
        raise LookupError("holding reward schedule does not apply to sample")
    eligible = observation.condition_id in program.eligible_condition_ids
    value = observation.quantity * observation.mark_price
    hourly = value * program.annual_rate / Decimal(365 * 24) if eligible else Decimal(0)
    key = (
        f"holding-sample:{observation.strategy_id}:{observation.account_id.lower()}:"
        f"{observation.asset_id}:{observation.sample_ts.isoformat()}:{program.schedule_id}"
    )
    return HoldingRewardPositionSample(
        position_sample_id=deterministic_reward_id("holding-sample", {"key": key}),
        observation=observation,
        position_value=value,
        annual_rate=program.annual_rate,
        eligible=eligible,
        expected_hourly_reward=hourly,
        reward_regime_id=program.schedule_id,
        idempotency_key=key,
    )


def estimate_daily_holding_reward(
    samples: Iterable[HoldingRewardPositionSample],
    *,
    strategy_id: str,
    account_id: str,
    reward_date: date,
    carry_in: Decimal,
    program: HoldingRewardProgramSchedule,
) -> HoldingRewardDailyEstimate:
    rows = tuple(
        row
        for row in samples
        if row.observation.strategy_id == strategy_id
        and row.observation.account_id.lower() == account_id.lower()
        and row.observation.sample_ts.date() == reward_date
        and row.reward_regime_id == program.schedule_id
    )
    estimated = sum((row.expected_hourly_reward for row in rows), Decimal(0))
    accumulated = Decimal(carry_in) + estimated
    if accumulated >= program.minimum_payout:
        payable, carry_out = accumulated, Decimal(0)
    else:
        payable, carry_out = Decimal(0), accumulated
    methods = sorted({row.observation.sampling_method for row in rows})
    method = methods[0] if len(methods) == 1 else "MIXED"
    key = (
        f"holding-daily:{strategy_id}:{account_id.lower()}:"
        f"{reward_date.isoformat()}:{program.schedule_id}"
    )
    return HoldingRewardDailyEstimate(
        estimate_id=deterministic_reward_id("holding-daily", {"key": key}),
        strategy_id=strategy_id,
        account_id=account_id,
        reward_date=reward_date,
        sample_count=len(rows),
        eligible_sample_count=sum(1 for row in rows if row.eligible),
        sampled_position_value=sum((row.position_value for row in rows), Decimal(0)),
        estimated_reward=estimated,
        carry_in=Decimal(carry_in),
        payable_amount=payable,
        carry_out=carry_out,
        minimum_payout=program.minimum_payout,
        reward_regime_id=program.schedule_id,
        sampling_method=method,
        evidence_class="EXPECTED_HOLDING_REWARD",
        idempotency_key=key,
        position_sample_ids=tuple(row.position_sample_id for row in rows),
    )


class PostgresHoldingRewardStore:
    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS quant")
            for statement in HOLDING_REWARD_SCHEMA_STATEMENTS:
                cur.execute(statement)
            conn.commit()

    def record_position_sample(self, row: HoldingRewardPositionSample) -> bool:
        item = row.observation
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_holding_reward_position_samples (
                    position_sample_id,strategy_id,account_id,condition_id,asset_id,
                    sample_ts,quantity,mark_price,position_value,annual_rate,eligible,
                    expected_hourly_reward,reward_regime_id,sampling_method,
                    evidence_class,idempotency_key,metadata
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                ) ON CONFLICT (idempotency_key) DO NOTHING RETURNING position_sample_id
                """,
                (
                    row.position_sample_id,
                    item.strategy_id,
                    item.account_id,
                    item.condition_id,
                    item.asset_id,
                    item.sample_ts,
                    item.quantity,
                    item.mark_price,
                    row.position_value,
                    row.annual_rate,
                    row.eligible,
                    row.expected_hourly_reward,
                    row.reward_regime_id,
                    item.sampling_method,
                    item.evidence_class,
                    row.idempotency_key,
                    json.dumps(item.metadata or {}, sort_keys=True, default=str),
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
        return inserted

    def record_daily_estimate(self, row: HoldingRewardDailyEstimate) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_holding_reward_daily_estimates (
                    estimate_id,strategy_id,account_id,reward_date,sample_count,
                    eligible_sample_count,sampled_position_value,estimated_reward,
                    carry_in,payable_amount,carry_out,minimum_payout,reward_regime_id,
                    sampling_method,evidence_class,idempotency_key,metadata
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                ) ON CONFLICT (idempotency_key) DO NOTHING RETURNING estimate_id
                """,
                (
                    row.estimate_id,
                    row.strategy_id,
                    row.account_id,
                    row.reward_date,
                    row.sample_count,
                    row.eligible_sample_count,
                    row.sampled_position_value,
                    row.estimated_reward,
                    row.carry_in,
                    row.payable_amount,
                    row.carry_out,
                    row.minimum_payout,
                    row.reward_regime_id,
                    row.sampling_method,
                    row.evidence_class,
                    row.idempotency_key,
                    json.dumps({"position_sample_ids": row.position_sample_ids}),
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
        return inserted
