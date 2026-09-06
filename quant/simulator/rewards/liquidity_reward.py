"""Counterfactual Polymarket liquidity reward scoring and persistence."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .models import deterministic_reward_id

LIQUIDITY_REWARD_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_liquidity_reward_order_samples (
        order_sample_id TEXT PRIMARY KEY,
        sampling_round_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        maker_id TEXT NOT NULL,
        order_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        sample_ts TIMESTAMPTZ NOT NULL,
        outcome_index INTEGER NOT NULL CHECK (outcome_index IN (0,1)),
        side TEXT NOT NULL CHECK (side IN ('BID','ASK')),
        adjusted_midpoint NUMERIC NOT NULL,
        price NUMERIC NOT NULL,
        size NUMERIC NOT NULL,
        qualifying_size NUMERIC NOT NULL,
        max_spread NUMERIC NOT NULL,
        spread NUMERIC NOT NULL,
        in_game_multiplier NUMERIC NOT NULL,
        score_side TEXT NOT NULL CHECK (score_side IN ('ONE','TWO')),
        raw_score NUMERIC NOT NULL CHECK (raw_score >= 0),
        qualifies BOOLEAN NOT NULL,
        reward_regime_id TEXT NOT NULL,
        evidence_class TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_liquidity_order_samples_round_idx
    ON quant.paper_liquidity_reward_order_samples
       (sampling_round_id,maker_id,sample_ts)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_liquidity_reward_sample_scores (
        maker_sample_score_id TEXT PRIMARY KEY,
        sampling_round_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        maker_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        sample_ts TIMESTAMPTZ NOT NULL,
        adjusted_midpoint NUMERIC NOT NULL,
        q_one NUMERIC NOT NULL,
        q_two NUMERIC NOT NULL,
        q_min NUMERIC NOT NULL CHECK (q_min >= 0),
        market_q_min NUMERIC NOT NULL CHECK (market_q_min >= 0),
        normalized_score NUMERIC NOT NULL CHECK (normalized_score >= 0),
        reward_regime_id TEXT NOT NULL,
        evidence_class TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (sampling_round_id,maker_id,reward_regime_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_liquidity_reward_epoch_estimates (
        estimate_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        maker_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        reward_date DATE NOT NULL,
        own_epoch_score NUMERIC NOT NULL,
        market_epoch_score NUMERIC NOT NULL,
        final_share NUMERIC NOT NULL,
        reward_pool NUMERIC NOT NULL,
        estimated_reward NUMERIC NOT NULL,
        carry_in NUMERIC NOT NULL,
        payable_amount NUMERIC NOT NULL,
        carry_out NUMERIC NOT NULL,
        minimum_payout NUMERIC NOT NULL,
        reward_regime_id TEXT NOT NULL,
        evidence_class TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (strategy_id,maker_id,condition_id,reward_date,reward_regime_id)
    )
    """,
)


@dataclass(frozen=True)
class LiquidityRewardProgramSchedule:
    schedule_id: str
    condition_id: str
    effective_from: datetime
    effective_until: datetime | None
    minimum_order_size: Decimal
    maximum_spread: Decimal
    daily_reward_pool: Decimal
    scaling_factor: Decimal = Decimal(3)
    minimum_payout: Decimal = Decimal(1)
    source: str = "OFFICIAL_LIQUIDITY_REWARD_CONFIG"

    def __post_init__(self) -> None:
        if self.effective_from.tzinfo is None:
            raise ValueError("liquidity reward schedule must be timezone-aware")
        if (
            self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("liquidity reward schedule end must follow start")
        if self.minimum_order_size <= 0 or self.maximum_spread <= 0:
            raise ValueError("liquidity reward size and spread must be positive")
        if self.daily_reward_pool < 0 or self.minimum_payout < 0:
            raise ValueError("liquidity reward amounts must be non-negative")
        if self.scaling_factor <= 0:
            raise ValueError("liquidity reward scaling factor must be positive")

    def applies_at(self, at: datetime) -> bool:
        return self.effective_from <= at and (
            self.effective_until is None or at < self.effective_until
        )


@dataclass(frozen=True)
class LiquidityOrderObservation:
    sampling_round_id: str
    strategy_id: str
    account_id: str
    maker_id: str
    order_id: str
    condition_id: str
    asset_id: str
    sample_ts: datetime
    outcome_index: int
    side: str
    adjusted_midpoint: Decimal
    price: Decimal
    size: Decimal
    in_game_multiplier: Decimal = Decimal(1)
    evidence_class: str = "COUNTERFACTUAL_ESTIMATE"
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class LiquidityOrderScore:
    order_sample_id: str
    observation: LiquidityOrderObservation
    qualifying_size: Decimal
    max_spread: Decimal
    spread: Decimal
    score_side: str
    raw_score: Decimal
    qualifies: bool
    reward_regime_id: str
    idempotency_key: str


@dataclass(frozen=True)
class LiquidityMakerSampleScore:
    maker_sample_score_id: str
    sampling_round_id: str
    strategy_id: str
    account_id: str
    maker_id: str
    condition_id: str
    sample_ts: datetime
    adjusted_midpoint: Decimal
    q_one: Decimal
    q_two: Decimal
    q_min: Decimal
    market_q_min: Decimal
    normalized_score: Decimal
    reward_regime_id: str
    evidence_class: str
    idempotency_key: str


@dataclass(frozen=True)
class LiquidityRewardEpochEstimate:
    estimate_id: str
    strategy_id: str
    account_id: str
    maker_id: str
    condition_id: str
    reward_date: date
    own_epoch_score: Decimal
    market_epoch_score: Decimal
    final_share: Decimal
    reward_pool: Decimal
    estimated_reward: Decimal
    carry_in: Decimal
    payable_amount: Decimal
    carry_out: Decimal
    minimum_payout: Decimal
    reward_regime_id: str
    evidence_class: str
    idempotency_key: str
    sampling_round_ids: tuple[str, ...]


def score_liquidity_order(
    observation: LiquidityOrderObservation,
    *,
    program: LiquidityRewardProgramSchedule,
) -> LiquidityOrderScore:
    if observation.sample_ts.tzinfo is None:
        raise ValueError("liquidity sample timestamp must be timezone-aware")
    if observation.condition_id != program.condition_id or not program.applies_at(
        observation.sample_ts
    ):
        raise LookupError("liquidity reward schedule does not apply to observation")
    if observation.outcome_index not in (0, 1):
        raise ValueError("liquidity outcome index must be zero or one")
    side = observation.side.upper()
    if side not in {"BID", "ASK"}:
        raise ValueError("liquidity order side must be BID or ASK")
    if not Decimal(0) <= observation.adjusted_midpoint <= Decimal(1):
        raise ValueError("liquidity adjusted midpoint must be within [0,1]")
    if not Decimal(0) <= observation.price <= Decimal(1) or observation.size <= 0:
        raise ValueError("invalid liquidity order price or size")
    if observation.in_game_multiplier < 0:
        raise ValueError("liquidity in-game multiplier cannot be negative")
    spread = abs(observation.price - observation.adjusted_midpoint)
    qualifies = (
        observation.size >= program.minimum_order_size
        and spread <= program.maximum_spread
    )
    score = Decimal(0)
    if qualifies:
        score = (
            ((program.maximum_spread - spread) / program.maximum_spread) ** 2
            * observation.in_game_multiplier
            * observation.size
        )
    score_side = (
        "ONE"
        if (observation.outcome_index == 0 and side == "BID")
        or (observation.outcome_index == 1 and side == "ASK")
        else "TWO"
    )
    key = (
        f"liquidity-order-sample:{observation.sampling_round_id}:"
        f"{observation.order_id}:{program.schedule_id}"
    )
    return LiquidityOrderScore(
        order_sample_id=deterministic_reward_id("liquidity-order-sample", {"key": key}),
        observation=LiquidityOrderObservation(**{**observation.__dict__, "side": side}),
        qualifying_size=program.minimum_order_size,
        max_spread=program.maximum_spread,
        spread=spread,
        score_side=score_side,
        raw_score=score,
        qualifies=qualifies,
        reward_regime_id=program.schedule_id,
        idempotency_key=key,
    )


def aggregate_liquidity_sample(
    scores: Iterable[LiquidityOrderScore],
    *,
    program: LiquidityRewardProgramSchedule,
) -> tuple[LiquidityMakerSampleScore, ...]:
    rows = tuple(scores)
    if not rows:
        return ()
    rounds = {row.observation.sampling_round_id for row in rows}
    conditions = {row.observation.condition_id for row in rows}
    timestamps = {row.observation.sample_ts for row in rows}
    midpoints = {row.observation.adjusted_midpoint for row in rows}
    regimes = {row.reward_regime_id for row in rows}
    if any(
        len(values) != 1
        for values in (rounds, conditions, timestamps, midpoints, regimes)
    ):
        raise ValueError("liquidity sample aggregation requires one market sample")
    if regimes != {program.schedule_id}:
        raise ValueError("liquidity sample regime mismatch")
    grouped: dict[tuple[str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: {"ONE": Decimal(0), "TWO": Decimal(0)}
    )
    evidence: dict[tuple[str, str, str], str] = {}
    for row in rows:
        observation = row.observation
        key = (observation.strategy_id, observation.account_id, observation.maker_id)
        grouped[key][row.score_side] += row.raw_score
        evidence[key] = observation.evidence_class
    midpoint = next(iter(midpoints))
    raw: list[tuple[tuple[str, str, str], Decimal, Decimal, Decimal]] = []
    for identity, sides in grouped.items():
        q_one, q_two = sides["ONE"], sides["TWO"]
        if Decimal("0.10") <= midpoint <= Decimal("0.90"):
            q_min = max(
                min(q_one, q_two),
                max(q_one / program.scaling_factor, q_two / program.scaling_factor),
            )
        else:
            q_min = min(q_one, q_two)
        raw.append((identity, q_one, q_two, q_min))
    market_total = sum((item[3] for item in raw), Decimal(0))
    round_id = next(iter(rounds))
    condition_id = next(iter(conditions))
    sample_ts = next(iter(timestamps))
    result = []
    for identity, q_one, q_two, q_min in raw:
        strategy_id, account_id, maker_id = identity
        normalized = q_min / market_total if market_total > 0 else Decimal(0)
        key = f"liquidity-maker-sample:{round_id}:{maker_id}:{program.schedule_id}"
        result.append(
            LiquidityMakerSampleScore(
                maker_sample_score_id=deterministic_reward_id(
                    "liquidity-maker-sample", {"key": key}
                ),
                sampling_round_id=round_id,
                strategy_id=strategy_id,
                account_id=account_id,
                maker_id=maker_id,
                condition_id=condition_id,
                sample_ts=sample_ts,
                adjusted_midpoint=midpoint,
                q_one=q_one,
                q_two=q_two,
                q_min=q_min,
                market_q_min=market_total,
                normalized_score=normalized,
                reward_regime_id=program.schedule_id,
                evidence_class=evidence[identity],
                idempotency_key=key,
            )
        )
    return tuple(sorted(result, key=lambda item: item.maker_id))


def estimate_liquidity_epoch(
    samples: Iterable[LiquidityMakerSampleScore],
    *,
    strategy_id: str,
    account_id: str,
    maker_id: str,
    reward_date: date,
    carry_in: Decimal,
    program: LiquidityRewardProgramSchedule,
) -> LiquidityRewardEpochEstimate:
    rows = tuple(
        row
        for row in samples
        if row.condition_id == program.condition_id
        and row.sample_ts.date() == reward_date
    )
    own_rows = tuple(
        row
        for row in rows
        if row.strategy_id == strategy_id
        and row.account_id.lower() == account_id.lower()
        and row.maker_id.lower() == maker_id.lower()
    )
    own_epoch = sum((row.normalized_score for row in own_rows), Decimal(0))
    market_epoch = sum((row.normalized_score for row in rows), Decimal(0))
    final_share = own_epoch / market_epoch if market_epoch > 0 else Decimal(0)
    estimated = final_share * program.daily_reward_pool
    accumulated = Decimal(carry_in) + estimated
    if accumulated >= program.minimum_payout:
        payable, carry_out = accumulated, Decimal(0)
    else:
        payable, carry_out = Decimal(0), accumulated
    key = (
        f"liquidity-epoch:{strategy_id}:{maker_id.lower()}:{program.condition_id}:"
        f"{reward_date.isoformat()}:{program.schedule_id}"
    )
    evidence_class = (
        "COUNTERFACTUAL_ESTIMATE"
        if any(row.evidence_class == "COUNTERFACTUAL_ESTIMATE" for row in own_rows)
        else "OFFICIAL_SAMPLE_EVIDENCE"
    )
    return LiquidityRewardEpochEstimate(
        estimate_id=deterministic_reward_id("liquidity-epoch", {"key": key}),
        strategy_id=strategy_id,
        account_id=account_id,
        maker_id=maker_id,
        condition_id=program.condition_id,
        reward_date=reward_date,
        own_epoch_score=own_epoch,
        market_epoch_score=market_epoch,
        final_share=final_share,
        reward_pool=program.daily_reward_pool,
        estimated_reward=estimated,
        carry_in=Decimal(carry_in),
        payable_amount=payable,
        carry_out=carry_out,
        minimum_payout=program.minimum_payout,
        reward_regime_id=program.schedule_id,
        evidence_class=evidence_class,
        idempotency_key=key,
        sampling_round_ids=tuple(sorted({row.sampling_round_id for row in own_rows})),
    )


class PostgresLiquidityRewardStore:
    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS quant")
            for statement in LIQUIDITY_REWARD_SCHEMA_STATEMENTS:
                cur.execute(statement)
            conn.commit()

    def record_order_score(self, row: LiquidityOrderScore) -> bool:
        item = row.observation
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_liquidity_reward_order_samples (
                    order_sample_id,sampling_round_id,strategy_id,account_id,maker_id,
                    order_id,condition_id,asset_id,sample_ts,outcome_index,side,
                    adjusted_midpoint,price,size,qualifying_size,max_spread,spread,
                    in_game_multiplier,score_side,raw_score,qualifies,reward_regime_id,
                    evidence_class,idempotency_key,metadata
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s::jsonb
                ) ON CONFLICT (idempotency_key) DO NOTHING RETURNING order_sample_id
                """,
                (
                    row.order_sample_id,
                    item.sampling_round_id,
                    item.strategy_id,
                    item.account_id,
                    item.maker_id,
                    item.order_id,
                    item.condition_id,
                    item.asset_id,
                    item.sample_ts,
                    item.outcome_index,
                    item.side,
                    item.adjusted_midpoint,
                    item.price,
                    item.size,
                    row.qualifying_size,
                    row.max_spread,
                    row.spread,
                    item.in_game_multiplier,
                    row.score_side,
                    row.raw_score,
                    row.qualifies,
                    row.reward_regime_id,
                    item.evidence_class,
                    row.idempotency_key,
                    json.dumps(item.metadata or {}, sort_keys=True, default=str),
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
        return inserted

    def record_maker_sample(self, row: LiquidityMakerSampleScore) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_liquidity_reward_sample_scores (
                    maker_sample_score_id,sampling_round_id,strategy_id,account_id,
                    maker_id,condition_id,sample_ts,adjusted_midpoint,q_one,q_two,
                    q_min,market_q_min,normalized_score,reward_regime_id,
                    evidence_class,idempotency_key
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (idempotency_key) DO NOTHING RETURNING maker_sample_score_id
                """,
                (
                    row.maker_sample_score_id,
                    row.sampling_round_id,
                    row.strategy_id,
                    row.account_id,
                    row.maker_id,
                    row.condition_id,
                    row.sample_ts,
                    row.adjusted_midpoint,
                    row.q_one,
                    row.q_two,
                    row.q_min,
                    row.market_q_min,
                    row.normalized_score,
                    row.reward_regime_id,
                    row.evidence_class,
                    row.idempotency_key,
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
        return inserted

    def record_epoch_estimate(self, row: LiquidityRewardEpochEstimate) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_liquidity_reward_epoch_estimates (
                    estimate_id,strategy_id,account_id,maker_id,condition_id,
                    reward_date,own_epoch_score,market_epoch_score,final_share,
                    reward_pool,estimated_reward,carry_in,payable_amount,carry_out,
                    minimum_payout,reward_regime_id,evidence_class,idempotency_key,metadata
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                ) ON CONFLICT (idempotency_key) DO NOTHING RETURNING estimate_id
                """,
                (
                    row.estimate_id,
                    row.strategy_id,
                    row.account_id,
                    row.maker_id,
                    row.condition_id,
                    row.reward_date,
                    row.own_epoch_score,
                    row.market_epoch_score,
                    row.final_share,
                    row.reward_pool,
                    row.estimated_reward,
                    row.carry_in,
                    row.payable_amount,
                    row.carry_out,
                    row.minimum_payout,
                    row.reward_regime_id,
                    row.evidence_class,
                    row.idempotency_key,
                    json.dumps({"sampling_round_ids": row.sampling_round_ids}),
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
        return inserted
