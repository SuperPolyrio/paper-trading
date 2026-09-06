from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from quant.simulator.rewards import (
    HoldingRewardProgramSchedule,
    PositionValueObservation,
    deterministic_hourly_sample_ts,
    estimate_daily_holding_reward,
    sample_holding_position,
)

DAY = date(2026, 8, 18)
NOW = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)


def _program(*, rate: str = "0.04") -> HoldingRewardProgramSchedule:
    return HoldingRewardProgramSchedule(
        schedule_id=f"holding-{rate}",
        effective_from=NOW - timedelta(days=1),
        effective_until=None,
        annual_rate=Decimal(rate),
        eligible_condition_ids=frozenset({"eligible"}),
        minimum_payout=Decimal(1),
        source_url_hash="official-snapshot-hash",
    )


def _observation(condition: str, *, sample_ts: datetime = NOW):
    return PositionValueObservation(
        strategy_id="strategy-1",
        account_id="account-1",
        condition_id=condition,
        asset_id=f"asset-{condition}",
        sample_ts=sample_ts,
        quantity=Decimal(100),
        mark_price=Decimal("0.75"),
    )


def test_hourly_sample_timestamp_is_deterministic_and_inside_hour() -> None:
    first = deterministic_hourly_sample_ts(DAY, 7, seed="research-1")
    second = deterministic_hourly_sample_ts(DAY, 7, seed="research-1")

    assert first == second
    assert first.hour == 7


def test_eligible_position_value_uses_effective_schedule_rate() -> None:
    sample = sample_holding_position(_observation("eligible"), program=_program())

    assert sample.position_value == Decimal(75)
    assert sample.expected_hourly_reward == Decimal(75) * Decimal("0.04") / Decimal(
        365 * 24
    )
    assert sample.eligible is True


def test_ineligible_market_is_audited_but_earns_zero() -> None:
    sample = sample_holding_position(_observation("ineligible"), program=_program())

    assert sample.position_value == Decimal(75)
    assert sample.eligible is False
    assert sample.expected_hourly_reward == 0


def test_daily_expected_reward_carries_below_minimum() -> None:
    program = _program()
    samples = tuple(
        sample_holding_position(
            _observation(
                "eligible",
                sample_ts=deterministic_hourly_sample_ts(DAY, hour, seed="research-1"),
            ),
            program=program,
        )
        for hour in range(24)
    )
    estimate = estimate_daily_holding_reward(
        samples,
        strategy_id="strategy-1",
        account_id="account-1",
        reward_date=DAY,
        carry_in=Decimal(0),
        program=program,
    )

    assert estimate.sample_count == 24
    assert estimate.eligible_sample_count == 24
    assert estimate.estimated_reward == sum(
        (sample.expected_hourly_reward for sample in samples), Decimal(0)
    )
    assert estimate.payable_amount == 0
    assert estimate.carry_out == estimate.estimated_reward
    assert estimate.evidence_class == "EXPECTED_HOLDING_REWARD"


def test_rate_is_not_hardcoded() -> None:
    four = sample_holding_position(
        _observation("eligible"), program=_program(rate="0.04")
    )
    lower = sample_holding_position(
        _observation("eligible"), program=_program(rate="0.0325")
    )

    assert four.expected_hourly_reward > lower.expected_hourly_reward
