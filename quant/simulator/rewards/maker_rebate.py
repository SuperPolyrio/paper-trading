"""Fee-curve weighted maker rebate estimation."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from quant.simulator.economics import FeeEngine, FeeSchedule, LiquidityRole

from .models import deterministic_reward_id

MAKER_REBATE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_maker_rebate_fill_equivalents (
        equivalent_id TEXT PRIMARY KEY,
        fill_id TEXT NOT NULL UNIQUE,
        strategy_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        reward_date DATE NOT NULL,
        category TEXT NOT NULL,
        price NUMERIC NOT NULL,
        shares NUMERIC NOT NULL,
        fee_equivalent NUMERIC NOT NULL CHECK (fee_equivalent >= 0),
        fee_schedule_id TEXT NOT NULL,
        maker_rebate_schedule_id TEXT NOT NULL,
        source TEXT NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_maker_rebate_fill_market_day_idx
    ON quant.paper_maker_rebate_fill_equivalents
       (condition_id,reward_date,strategy_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_maker_rebate_daily_estimates (
        estimate_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        reward_date DATE NOT NULL,
        category TEXT NOT NULL,
        own_fee_equivalent NUMERIC NOT NULL CHECK (own_fee_equivalent >= 0),
        market_fee_equivalent NUMERIC NOT NULL CHECK (market_fee_equivalent >= 0),
        collected_taker_fees NUMERIC NOT NULL CHECK (collected_taker_fees >= 0),
        rebate_pool_fraction NUMERIC NOT NULL CHECK (
            rebate_pool_fraction >= 0 AND rebate_pool_fraction <= 1
        ),
        rebate_pool NUMERIC NOT NULL CHECK (rebate_pool >= 0),
        estimated_rebate NUMERIC NOT NULL CHECK (estimated_rebate >= 0),
        carry_in NUMERIC NOT NULL CHECK (carry_in >= 0),
        payable_amount NUMERIC NOT NULL CHECK (payable_amount >= 0),
        carry_out NUMERIC NOT NULL CHECK (carry_out >= 0),
        minimum_payout NUMERIC NOT NULL CHECK (minimum_payout >= 0),
        maker_rebate_schedule_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (strategy_id,condition_id,reward_date,maker_rebate_schedule_id)
    )
    """,
)


@dataclass(frozen=True)
class MakerRebateProgramSchedule:
    schedule_id: str
    category: str
    effective_from: datetime
    effective_until: datetime | None
    rebate_pool_fraction: Decimal
    minimum_payout: Decimal = Decimal(1)
    source: str = "OFFICIAL_MAKER_REBATE_PROGRAM"

    def __post_init__(self) -> None:
        if self.effective_from.tzinfo is None:
            raise ValueError("maker rebate schedule must be timezone-aware")
        if (
            self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("maker rebate schedule end must follow start")
        if self.rebate_pool_fraction < 0 or self.rebate_pool_fraction > 1:
            raise ValueError("maker rebate pool fraction must be within [0, 1]")
        if self.minimum_payout < 0:
            raise ValueError("maker rebate minimum payout must be non-negative")

    def applies_at(self, at: datetime) -> bool:
        return self.effective_from <= at and (
            self.effective_until is None or at < self.effective_until
        )


@dataclass(frozen=True)
class MakerFillEvidence:
    fill_id: str
    strategy_id: str
    condition_id: str
    asset_id: str
    reward_date: date
    category: str
    price: Decimal
    shares: Decimal
    fee_schedule: FeeSchedule
    source: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class MakerFeeEquivalent:
    equivalent_id: str
    fill_id: str
    strategy_id: str
    condition_id: str
    asset_id: str
    reward_date: date
    category: str
    price: Decimal
    shares: Decimal
    fee_equivalent: Decimal
    fee_schedule_id: str
    maker_rebate_schedule_id: str
    source: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MakerRebateDailyEstimate:
    estimate_id: str
    strategy_id: str
    condition_id: str
    reward_date: date
    category: str
    own_fee_equivalent: Decimal
    market_fee_equivalent: Decimal
    collected_taker_fees: Decimal
    rebate_pool_fraction: Decimal
    rebate_pool: Decimal
    estimated_rebate: Decimal
    carry_in: Decimal
    payable_amount: Decimal
    carry_out: Decimal
    minimum_payout: Decimal
    maker_rebate_schedule_id: str
    idempotency_key: str
    metadata: dict[str, Any]


def calculate_maker_fee_equivalent(
    fill: MakerFillEvidence,
    *,
    program: MakerRebateProgramSchedule,
) -> MakerFeeEquivalent:
    if fill.shares <= 0:
        raise ValueError("maker rebate fill shares must be positive")
    if fill.category.lower() != program.category.lower():
        raise ValueError("maker fill category does not match rebate schedule")
    charge = FeeEngine.calculate(
        fill_id=f"maker-fee-equivalent:{fill.fill_id}",
        schedule=fill.fee_schedule,
        liquidity_role=LiquidityRole.TAKER,
        price=fill.price,
        shares=fill.shares,
    )
    equivalent_id = deterministic_reward_id(
        "maker-fee-equivalent",
        {
            "fill_id": fill.fill_id,
            "maker_rebate_schedule_id": program.schedule_id,
        },
    )
    return MakerFeeEquivalent(
        equivalent_id=equivalent_id,
        fill_id=fill.fill_id,
        strategy_id=fill.strategy_id,
        condition_id=fill.condition_id,
        asset_id=fill.asset_id,
        reward_date=fill.reward_date,
        category=fill.category,
        price=fill.price,
        shares=fill.shares,
        fee_equivalent=charge.platform_fee,
        fee_schedule_id=fill.fee_schedule.schedule_id,
        maker_rebate_schedule_id=program.schedule_id,
        source=fill.source,
        metadata=dict(fill.metadata or {}),
    )


def estimate_daily_maker_rebate(
    equivalents: Iterable[MakerFeeEquivalent],
    *,
    strategy_id: str,
    condition_id: str,
    reward_date: date,
    category: str,
    market_fee_equivalent: Decimal,
    collected_taker_fees: Decimal,
    carry_in: Decimal,
    program: MakerRebateProgramSchedule,
) -> MakerRebateDailyEstimate:
    rows = tuple(equivalents)
    if any(
        row.strategy_id != strategy_id
        or row.condition_id != condition_id
        or row.reward_date != reward_date
        for row in rows
    ):
        raise ValueError("maker rebate equivalent scope mismatch")
    own = sum((row.fee_equivalent for row in rows), Decimal(0))
    market_total = Decimal(market_fee_equivalent)
    collected = Decimal(collected_taker_fees)
    carry = Decimal(carry_in)
    if min(market_total, collected, carry) < 0:
        raise ValueError("maker rebate totals must be non-negative")
    if market_total < own:
        raise ValueError("market fee equivalent cannot be below own contribution")
    pool = collected * program.rebate_pool_fraction
    estimate = Decimal(0) if market_total == 0 else own / market_total * pool
    accumulated = carry + estimate
    if accumulated >= program.minimum_payout:
        payable, carry_out = accumulated, Decimal(0)
    else:
        payable, carry_out = Decimal(0), accumulated
    idempotency_key = (
        f"maker-rebate:{strategy_id}:{condition_id}:{reward_date.isoformat()}:"
        f"{program.schedule_id}"
    )
    estimate_id = deterministic_reward_id(
        "maker-rebate-estimate", {"idempotency_key": idempotency_key}
    )
    return MakerRebateDailyEstimate(
        estimate_id=estimate_id,
        strategy_id=strategy_id,
        condition_id=condition_id,
        reward_date=reward_date,
        category=category,
        own_fee_equivalent=own,
        market_fee_equivalent=market_total,
        collected_taker_fees=collected,
        rebate_pool_fraction=program.rebate_pool_fraction,
        rebate_pool=pool,
        estimated_rebate=estimate,
        carry_in=carry,
        payable_amount=payable,
        carry_out=carry_out,
        minimum_payout=program.minimum_payout,
        maker_rebate_schedule_id=program.schedule_id,
        idempotency_key=idempotency_key,
        metadata={"fill_ids": [row.fill_id for row in rows]},
    )


class PostgresMakerRebateStore:
    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS quant")
            for statement in MAKER_REBATE_SCHEMA_STATEMENTS:
                cur.execute(statement)
            conn.commit()

    def record_equivalent(self, item: MakerFeeEquivalent) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_maker_rebate_fill_equivalents (
                    equivalent_id,fill_id,strategy_id,condition_id,asset_id,
                    reward_date,category,price,shares,fee_equivalent,
                    fee_schedule_id,maker_rebate_schedule_id,source,metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (fill_id) DO NOTHING RETURNING equivalent_id
                """,
                (
                    item.equivalent_id,
                    item.fill_id,
                    item.strategy_id,
                    item.condition_id,
                    item.asset_id,
                    item.reward_date,
                    item.category,
                    item.price,
                    item.shares,
                    item.fee_equivalent,
                    item.fee_schedule_id,
                    item.maker_rebate_schedule_id,
                    item.source,
                    json.dumps(item.metadata, sort_keys=True, default=str),
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
        return inserted

    def record_daily_estimate(self, item: MakerRebateDailyEstimate) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_maker_rebate_daily_estimates (
                    estimate_id,strategy_id,condition_id,reward_date,category,
                    own_fee_equivalent,market_fee_equivalent,collected_taker_fees,
                    rebate_pool_fraction,rebate_pool,estimated_rebate,carry_in,
                    payable_amount,carry_out,minimum_payout,
                    maker_rebate_schedule_id,idempotency_key,metadata
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                ) ON CONFLICT (idempotency_key) DO NOTHING RETURNING estimate_id
                """,
                (
                    item.estimate_id,
                    item.strategy_id,
                    item.condition_id,
                    item.reward_date,
                    item.category,
                    item.own_fee_equivalent,
                    item.market_fee_equivalent,
                    item.collected_taker_fees,
                    item.rebate_pool_fraction,
                    item.rebate_pool,
                    item.estimated_rebate,
                    item.carry_in,
                    item.payable_amount,
                    item.carry_out,
                    item.minimum_payout,
                    item.maker_rebate_schedule_id,
                    item.idempotency_key,
                    json.dumps(item.metadata, sort_keys=True, default=str),
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
        return inserted
