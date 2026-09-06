from datetime import date, datetime, timezone
from decimal import Decimal

from quant.simulator.rewards import (
    LiquidityOrderObservation,
    LiquidityRewardProgramSchedule,
    aggregate_liquidity_sample,
    estimate_liquidity_epoch,
    score_liquidity_order,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)
DAY = date(2026, 8, 18)


def _program(*, midpoint_extreme: bool = False) -> LiquidityRewardProgramSchedule:
    del midpoint_extreme
    return LiquidityRewardProgramSchedule(
        schedule_id="liquidity-program-1",
        condition_id="condition-1",
        effective_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
        effective_until=None,
        minimum_order_size=Decimal(10),
        maximum_spread=Decimal("0.03"),
        daily_reward_pool=Decimal(6),
    )


def _order(
    maker: str,
    order: str,
    *,
    side: str,
    outcome: int,
    price: str,
    midpoint: str = "0.50",
    size: str = "100",
) -> LiquidityOrderObservation:
    return LiquidityOrderObservation(
        sampling_round_id="round-1",
        strategy_id=f"strategy-{maker}",
        account_id=f"account-{maker}",
        maker_id=maker,
        order_id=order,
        condition_id="condition-1",
        asset_id=f"asset-{outcome}",
        sample_ts=NOW,
        outcome_index=outcome,
        side=side,
        adjusted_midpoint=Decimal(midpoint),
        price=Decimal(price),
        size=Decimal(size),
    )


def test_quadratic_order_score_and_side_mapping() -> None:
    program = _program()
    one = score_liquidity_order(
        _order("maker-a", "one", side="BID", outcome=0, price="0.49"),
        program=program,
    )
    two = score_liquidity_order(
        _order("maker-a", "two", side="ASK", outcome=0, price="0.51"),
        program=program,
    )

    assert one.raw_score == Decimal(100) * (Decimal(2) / Decimal(3)) ** 2
    assert one.score_side == "ONE"
    assert two.score_side == "TWO"


def test_middle_market_scores_single_sided_at_one_third() -> None:
    program = _program()
    order = score_liquidity_order(
        _order("maker-a", "one", side="BID", outcome=0, price="0.49"),
        program=program,
    )
    (sample,) = aggregate_liquidity_sample((order,), program=program)

    assert sample.q_two == 0
    assert sample.q_min == order.raw_score / Decimal(3)
    assert sample.normalized_score == 1


def test_extreme_market_requires_two_sided_quotes() -> None:
    program = _program()
    order = score_liquidity_order(
        _order(
            "maker-a",
            "one",
            side="BID",
            outcome=0,
            price="0.04",
            midpoint="0.05",
        ),
        program=program,
    )
    (sample,) = aggregate_liquidity_sample((order,), program=program)

    assert sample.q_one > 0
    assert sample.q_two == 0
    assert sample.q_min == 0
    assert sample.normalized_score == 0


def test_market_normalization_epoch_and_counterfactual_label() -> None:
    program = _program()
    orders = (
        _order("maker-a", "a-one", side="BID", outcome=0, price="0.49"),
        _order("maker-a", "a-two", side="ASK", outcome=0, price="0.51"),
        _order("maker-b", "b-one", side="BID", outcome=0, price="0.49"),
        _order("maker-b", "b-two", side="ASK", outcome=0, price="0.51"),
    )
    scores = tuple(score_liquidity_order(row, program=program) for row in orders)
    samples = aggregate_liquidity_sample(scores, program=program)
    estimate = estimate_liquidity_epoch(
        samples,
        strategy_id="strategy-maker-a",
        account_id="account-maker-a",
        maker_id="maker-a",
        reward_date=DAY,
        carry_in=Decimal(0),
        program=program,
    )

    assert {row.normalized_score for row in samples} == {Decimal("0.5")}
    assert estimate.final_share == Decimal("0.5")
    assert estimate.estimated_reward == Decimal(3)
    assert estimate.payable_amount == Decimal(3)
    assert estimate.evidence_class == "COUNTERFACTUAL_ESTIMATE"


def test_order_below_minimum_size_does_not_score() -> None:
    score = score_liquidity_order(
        _order("maker-a", "small", side="BID", outcome=0, price="0.50", size="9"),
        program=_program(),
    )

    assert score.qualifies is False
    assert score.raw_score == 0
