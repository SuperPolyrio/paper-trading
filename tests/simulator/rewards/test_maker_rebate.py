from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from quant.simulator.economics import FeeSchedule
from quant.simulator.rewards import (
    MakerFillEvidence,
    MakerRebateProgramSchedule,
    calculate_maker_fee_equivalent,
    estimate_daily_maker_rebate,
)

NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)
DAY = date(2026, 8, 18)


def _fee_schedule() -> FeeSchedule:
    return FeeSchedule(
        schedule_id="fee-schedule-1",
        asset_id="asset-1",
        condition_id="condition-1",
        effective_from=NOW,
        effective_until=None,
        platform_fee_rate=Decimal("0.04"),
        platform_fee_exponent=Decimal(1),
        source="CLOB_MARKET_TERMS",
    )


def _program() -> MakerRebateProgramSchedule:
    return MakerRebateProgramSchedule(
        schedule_id="maker-program-1",
        category="Politics",
        effective_from=NOW,
        effective_until=None,
        rebate_pool_fraction=Decimal("0.25"),
        minimum_payout=Decimal(1),
    )


def _equivalent(fill_id: str = "fill-1"):
    return calculate_maker_fee_equivalent(
        MakerFillEvidence(
            fill_id=fill_id,
            strategy_id="strategy-1",
            condition_id="condition-1",
            asset_id="asset-1",
            reward_date=DAY,
            category="Politics",
            price=Decimal("0.40"),
            shares=Decimal(100),
            fee_schedule=_fee_schedule(),
            source="PAPER_MAKER_FILL",
        ),
        program=_program(),
    )


def test_fee_equivalent_uses_authoritative_taker_fee_curve() -> None:
    equivalent = _equivalent()

    assert equivalent.fee_equivalent == Decimal("0.96000")
    assert equivalent.fee_schedule_id == "fee-schedule-1"
    assert equivalent.maker_rebate_schedule_id == "maker-program-1"


def test_per_market_pool_share_and_daily_minimum_carry() -> None:
    equivalent = _equivalent()
    day_one = estimate_daily_maker_rebate(
        (equivalent,),
        strategy_id="strategy-1",
        condition_id="condition-1",
        reward_date=DAY,
        category="Politics",
        market_fee_equivalent=Decimal("4.8"),
        collected_taker_fees=Decimal(10),
        carry_in=Decimal(0),
        program=_program(),
    )
    day_two = estimate_daily_maker_rebate(
        (equivalent,),
        strategy_id="strategy-1",
        condition_id="condition-1",
        reward_date=DAY,
        category="Politics",
        market_fee_equivalent=Decimal("4.8"),
        collected_taker_fees=Decimal(10),
        carry_in=Decimal("0.75"),
        program=_program(),
    )

    assert day_one.rebate_pool == Decimal("2.50")
    assert day_one.estimated_rebate == Decimal("0.500000")
    assert day_one.payable_amount == 0
    assert day_one.carry_out == Decimal("0.500000")
    assert day_two.payable_amount == Decimal("1.250000")
    assert day_two.carry_out == 0


def test_no_maker_fill_produces_no_rebate() -> None:
    estimate = estimate_daily_maker_rebate(
        (),
        strategy_id="strategy-1",
        condition_id="condition-1",
        reward_date=DAY,
        category="Politics",
        market_fee_equivalent=Decimal(10),
        collected_taker_fees=Decimal(10),
        carry_in=Decimal(0),
        program=_program(),
    )

    assert estimate.own_fee_equivalent == 0
    assert estimate.estimated_rebate == 0
    assert estimate.payable_amount == 0


def test_market_total_cannot_be_lower_than_own_contribution() -> None:
    with pytest.raises(ValueError, match="below own contribution"):
        estimate_daily_maker_rebate(
            (_equivalent(),),
            strategy_id="strategy-1",
            condition_id="condition-1",
            reward_date=DAY,
            category="Politics",
            market_fee_equivalent=Decimal("0.5"),
            collected_taker_fees=Decimal(1),
            carry_in=Decimal(0),
            program=_program(),
        )
