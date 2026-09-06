"""Point-in-time Polymarket taker tier and rebate economics."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from .models import deterministic_reward_id

TAKER_REBATE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_taker_weighted_volume_events (
        weighted_volume_id TEXT PRIMARY KEY,
        fill_id TEXT NOT NULL UNIQUE,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        fill_ts TIMESTAMPTZ NOT NULL,
        category TEXT NOT NULL,
        price NUMERIC NOT NULL,
        shares NUMERIC NOT NULL,
        trade_notional NUMERIC NOT NULL,
        upside_factor NUMERIC NOT NULL,
        category_weight NUMERIC NOT NULL,
        bonus_multiplier NUMERIC NOT NULL,
        weighted_volume NUMERIC NOT NULL CHECK (weighted_volume >= 0),
        platform_fee_paid NUMERIC NOT NULL CHECK (platform_fee_paid >= 0),
        taker_program_schedule_id TEXT NOT NULL,
        source TEXT NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_taker_weighted_volume_account_ts_idx
    ON quant.paper_taker_weighted_volume_events (account_id,fill_ts)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_taker_tier_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        calculated_at TIMESTAMPTZ NOT NULL,
        window_start TIMESTAMPTZ NOT NULL,
        window_end TIMESTAMPTZ NOT NULL,
        rolling_weighted_volume NUMERIC NOT NULL,
        tier_level INTEGER NOT NULL,
        tier_name TEXT NOT NULL,
        rebate_rate NUMERIC NOT NULL,
        activation_ts TIMESTAMPTZ NOT NULL,
        level_up_bonus NUMERIC NOT NULL,
        taker_program_schedule_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_taker_rebate_daily_estimates (
        estimate_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        reward_date DATE NOT NULL,
        tier_level INTEGER NOT NULL,
        tier_name TEXT NOT NULL,
        rebate_rate NUMERIC NOT NULL,
        eligible_platform_fees NUMERIC NOT NULL,
        trading_rebate NUMERIC NOT NULL,
        level_up_bonus NUMERIC NOT NULL,
        estimated_rebate NUMERIC NOT NULL,
        carry_in NUMERIC NOT NULL,
        payable_amount NUMERIC NOT NULL,
        carry_out NUMERIC NOT NULL,
        minimum_payout NUMERIC NOT NULL,
        taker_program_schedule_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (strategy_id,account_id,reward_date,taker_program_schedule_id)
    )
    """,
)


@dataclass(frozen=True, order=True)
class TakerTier:
    level: int
    name: str
    threshold: Decimal
    rebate_rate: Decimal
    level_up_bonus: Decimal

    def __post_init__(self) -> None:
        if self.level < 0 or self.threshold < 0 or self.level_up_bonus < 0:
            raise ValueError("taker tier values must be non-negative")
        if self.rebate_rate < 0 or self.rebate_rate > 1:
            raise ValueError("taker tier rebate rate must be within [0, 1]")


@dataclass(frozen=True)
class TakerRebateProgramSchedule:
    schedule_id: str
    effective_from: datetime
    effective_until: datetime | None
    category_weights: Mapping[str, Decimal]
    tiers: tuple[TakerTier, ...]
    minimum_payout: Decimal = Decimal(1)
    rolling_days: int = 30
    source: str = "OFFICIAL_TAKER_REBATE_PROGRAM"

    def __post_init__(self) -> None:
        if self.effective_from.tzinfo is None:
            raise ValueError("taker rebate schedule must be timezone-aware")
        if (
            self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("taker rebate schedule end must follow start")
        if self.rolling_days <= 0 or self.minimum_payout < 0:
            raise ValueError("invalid taker rebate window or payout minimum")
        if not self.tiers or self.tiers[0].level != 0:
            raise ValueError("taker rebate tiers must start at level zero")
        if tuple(sorted(self.tiers, key=lambda item: item.threshold)) != self.tiers:
            raise ValueError("taker rebate tiers must be sorted by threshold")
        if any(Decimal(value) < 0 for value in self.category_weights.values()):
            raise ValueError("taker category weights must be non-negative")

    def category_weight(self, category: str) -> Decimal:
        try:
            return Decimal(self.category_weights[category.lower()])
        except KeyError as exc:
            raise LookupError(f"unknown taker rebate category: {category}") from exc


@dataclass(frozen=True)
class TakerTradeEvidence:
    fill_id: str
    strategy_id: str
    account_id: str
    fill_ts: datetime
    category: str
    price: Decimal
    shares: Decimal
    platform_fee_paid: Decimal
    bonus_multiplier: Decimal = Decimal(1)
    source: str = "PAPER_TAKER_FILL"
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class WeightedVolumeEvent:
    weighted_volume_id: str
    fill_id: str
    strategy_id: str
    account_id: str
    fill_ts: datetime
    category: str
    price: Decimal
    shares: Decimal
    trade_notional: Decimal
    upside_factor: Decimal
    category_weight: Decimal
    bonus_multiplier: Decimal
    weighted_volume: Decimal
    platform_fee_paid: Decimal
    taker_program_schedule_id: str
    source: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class TakerTierSnapshot:
    snapshot_id: str
    strategy_id: str
    account_id: str
    calculated_at: datetime
    window_start: datetime
    window_end: datetime
    rolling_weighted_volume: Decimal
    tier: TakerTier
    activation_ts: datetime
    level_up_bonus: Decimal
    taker_program_schedule_id: str
    idempotency_key: str


@dataclass(frozen=True)
class TakerRebateDailyEstimate:
    estimate_id: str
    strategy_id: str
    account_id: str
    reward_date: date
    tier: TakerTier
    eligible_platform_fees: Decimal
    trading_rebate: Decimal
    level_up_bonus: Decimal
    estimated_rebate: Decimal
    carry_in: Decimal
    payable_amount: Decimal
    carry_out: Decimal
    minimum_payout: Decimal
    taker_program_schedule_id: str
    idempotency_key: str
    fill_ids: tuple[str, ...]


def calculate_weighted_volume(
    trade: TakerTradeEvidence,
    *,
    program: TakerRebateProgramSchedule,
) -> WeightedVolumeEvent:
    if trade.fill_ts.tzinfo is None:
        raise ValueError("taker trade fill_ts must be timezone-aware")
    if trade.price < 0 or trade.price > 1 or trade.shares <= 0:
        raise ValueError("invalid taker trade price or shares")
    if trade.platform_fee_paid < 0 or trade.bonus_multiplier < 0:
        raise ValueError("invalid taker fee or bonus multiplier")
    weight = program.category_weight(trade.category)
    notional = trade.shares * trade.price
    upside = Decimal(1) - trade.price
    weighted = notional * upside * weight * trade.bonus_multiplier
    event_id = deterministic_reward_id(
        "taker-weighted-volume",
        {"fill_id": trade.fill_id, "schedule_id": program.schedule_id},
    )
    return WeightedVolumeEvent(
        weighted_volume_id=event_id,
        fill_id=trade.fill_id,
        strategy_id=trade.strategy_id,
        account_id=trade.account_id,
        fill_ts=trade.fill_ts,
        category=trade.category,
        price=trade.price,
        shares=trade.shares,
        trade_notional=notional,
        upside_factor=upside,
        category_weight=weight,
        bonus_multiplier=trade.bonus_multiplier,
        weighted_volume=weighted,
        platform_fee_paid=trade.platform_fee_paid,
        taker_program_schedule_id=program.schedule_id,
        source=trade.source,
        metadata=dict(trade.metadata or {}),
    )


def rolling_weighted_volume(
    events: Iterable[WeightedVolumeEvent],
    *,
    account_id: str,
    as_of: datetime,
    rolling_days: int = 30,
) -> Decimal:
    if as_of.tzinfo is None:
        raise ValueError("taker tier as_of must be timezone-aware")
    start = as_of - timedelta(days=rolling_days)
    return sum(
        (
            event.weighted_volume
            for event in events
            if event.account_id.lower() == account_id.lower()
            and start <= event.fill_ts < as_of
        ),
        Decimal(0),
    )


def resolve_taker_tier(
    weighted_volume: Decimal,
    *,
    program: TakerRebateProgramSchedule,
) -> TakerTier:
    volume = Decimal(weighted_volume)
    eligible = [tier for tier in program.tiers if volume >= tier.threshold]
    return eligible[-1]


def build_tier_snapshot(
    events: Iterable[WeightedVolumeEvent],
    *,
    strategy_id: str,
    account_id: str,
    calculated_at: datetime,
    activation_ts: datetime,
    highest_tier_level_seen: int,
    program: TakerRebateProgramSchedule,
) -> TakerTierSnapshot:
    if activation_ts < calculated_at:
        raise ValueError("taker tier activation cannot precede calculation")
    volume = rolling_weighted_volume(
        events,
        account_id=account_id,
        as_of=calculated_at,
        rolling_days=program.rolling_days,
    )
    tier = resolve_taker_tier(volume, program=program)
    bonus = tier.level_up_bonus if tier.level > highest_tier_level_seen else Decimal(0)
    key = (
        f"taker-tier:{account_id.lower()}:{calculated_at.isoformat()}:"
        f"{program.schedule_id}"
    )
    return TakerTierSnapshot(
        snapshot_id=deterministic_reward_id("taker-tier-snapshot", {"key": key}),
        strategy_id=strategy_id,
        account_id=account_id,
        calculated_at=calculated_at,
        window_start=calculated_at - timedelta(days=program.rolling_days),
        window_end=calculated_at,
        rolling_weighted_volume=volume,
        tier=tier,
        activation_ts=activation_ts,
        level_up_bonus=bonus,
        taker_program_schedule_id=program.schedule_id,
        idempotency_key=key,
    )


def estimate_daily_taker_rebate(
    events: Iterable[WeightedVolumeEvent],
    *,
    snapshot: TakerTierSnapshot,
    reward_date: date,
    carry_in: Decimal,
    program: TakerRebateProgramSchedule,
) -> TakerRebateDailyEstimate:
    rows = tuple(
        event
        for event in events
        if event.account_id.lower() == snapshot.account_id.lower()
        and event.fill_ts >= snapshot.activation_ts
        and event.fill_ts.date() == reward_date
    )
    fees = sum((event.platform_fee_paid for event in rows), Decimal(0))
    trading_rebate = fees * snapshot.tier.rebate_rate
    estimated = trading_rebate + snapshot.level_up_bonus
    accumulated = Decimal(carry_in) + estimated
    if accumulated >= program.minimum_payout:
        payable, carry_out = accumulated, Decimal(0)
    else:
        payable, carry_out = Decimal(0), accumulated
    key = (
        f"taker-rebate:{snapshot.strategy_id}:{snapshot.account_id.lower()}:"
        f"{reward_date.isoformat()}:{program.schedule_id}"
    )
    return TakerRebateDailyEstimate(
        estimate_id=deterministic_reward_id("taker-rebate-estimate", {"key": key}),
        strategy_id=snapshot.strategy_id,
        account_id=snapshot.account_id,
        reward_date=reward_date,
        tier=snapshot.tier,
        eligible_platform_fees=fees,
        trading_rebate=trading_rebate,
        level_up_bonus=snapshot.level_up_bonus,
        estimated_rebate=estimated,
        carry_in=Decimal(carry_in),
        payable_amount=payable,
        carry_out=carry_out,
        minimum_payout=program.minimum_payout,
        taker_program_schedule_id=program.schedule_id,
        idempotency_key=key,
        fill_ids=tuple(event.fill_id for event in rows),
    )


class PostgresTakerRebateStore:
    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS quant")
            for statement in TAKER_REBATE_SCHEMA_STATEMENTS:
                cur.execute(statement)
            conn.commit()

    def record_weighted_volume(self, event: WeightedVolumeEvent) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_taker_weighted_volume_events (
                    weighted_volume_id,fill_id,strategy_id,account_id,fill_ts,
                    category,price,shares,trade_notional,upside_factor,
                    category_weight,bonus_multiplier,weighted_volume,
                    platform_fee_paid,taker_program_schedule_id,source,metadata
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                ) ON CONFLICT (fill_id) DO NOTHING RETURNING weighted_volume_id
                """,
                (
                    event.weighted_volume_id,
                    event.fill_id,
                    event.strategy_id,
                    event.account_id,
                    event.fill_ts,
                    event.category,
                    event.price,
                    event.shares,
                    event.trade_notional,
                    event.upside_factor,
                    event.category_weight,
                    event.bonus_multiplier,
                    event.weighted_volume,
                    event.platform_fee_paid,
                    event.taker_program_schedule_id,
                    event.source,
                    json.dumps(event.metadata, sort_keys=True, default=str),
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
        return inserted

    def record_tier_snapshot(self, snapshot: TakerTierSnapshot) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_taker_tier_snapshots (
                    snapshot_id,strategy_id,account_id,calculated_at,window_start,
                    window_end,rolling_weighted_volume,tier_level,tier_name,
                    rebate_rate,activation_ts,level_up_bonus,
                    taker_program_schedule_id,idempotency_key
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (idempotency_key) DO NOTHING RETURNING snapshot_id
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.strategy_id,
                    snapshot.account_id,
                    snapshot.calculated_at,
                    snapshot.window_start,
                    snapshot.window_end,
                    snapshot.rolling_weighted_volume,
                    snapshot.tier.level,
                    snapshot.tier.name,
                    snapshot.tier.rebate_rate,
                    snapshot.activation_ts,
                    snapshot.level_up_bonus,
                    snapshot.taker_program_schedule_id,
                    snapshot.idempotency_key,
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
        return inserted

    def record_daily_estimate(self, item: TakerRebateDailyEstimate) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_taker_rebate_daily_estimates (
                    estimate_id,strategy_id,account_id,reward_date,tier_level,
                    tier_name,rebate_rate,eligible_platform_fees,trading_rebate,
                    level_up_bonus,estimated_rebate,carry_in,payable_amount,
                    carry_out,minimum_payout,taker_program_schedule_id,
                    idempotency_key,metadata
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                ) ON CONFLICT (idempotency_key) DO NOTHING RETURNING estimate_id
                """,
                (
                    item.estimate_id,
                    item.strategy_id,
                    item.account_id,
                    item.reward_date,
                    item.tier.level,
                    item.tier.name,
                    item.tier.rebate_rate,
                    item.eligible_platform_fees,
                    item.trading_rebate,
                    item.level_up_bonus,
                    item.estimated_rebate,
                    item.carry_in,
                    item.payable_amount,
                    item.carry_out,
                    item.minimum_payout,
                    item.taker_program_schedule_id,
                    item.idempotency_key,
                    json.dumps({"fill_ids": item.fill_ids}),
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
        return inserted
