from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant.simulator.rewards import (
    TakerRebateProgramSchedule,
    TakerTier,
    TakerTradeEvidence,
    build_tier_snapshot,
    calculate_weighted_volume,
    estimate_daily_taker_rebate,
    resolve_taker_tier,
    rolling_weighted_volume,
)

NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)


def _program() -> TakerRebateProgramSchedule:
    return TakerRebateProgramSchedule(
        schedule_id="taker-program-1",
        effective_from=NOW - timedelta(days=100),
        effective_until=None,
        category_weights={"sports": Decimal(1), "politics": Decimal("1.3")},
        tiers=(
            TakerTier(0, "None", Decimal(0), Decimal(0), Decimal(0)),
            TakerTier(1, "Bronze", Decimal(2000), Decimal("0.03"), Decimal(10)),
            TakerTier(2, "Silver", Decimal(20000), Decimal("0.08"), Decimal(50)),
        ),
    )


def _event(
    fill_id: str,
    *,
    at: datetime,
    price: str = "0.40",
    shares: str = "1000",
    category: str = "Politics",
    fee: str = "10",
):
    return calculate_weighted_volume(
        TakerTradeEvidence(
            fill_id=fill_id,
            strategy_id="strategy-1",
            account_id="0xabc",
            fill_ts=at,
            category=category,
            price=Decimal(price),
            shares=Decimal(shares),
            platform_fee_paid=Decimal(fee),
        ),
        program=_program(),
    )


def test_weighted_volume_matches_official_formula() -> None:
    event = _event("fill-1", at=NOW)

    assert event.trade_notional == Decimal("400.00")
    assert event.upside_factor == Decimal("0.60")
    assert event.category_weight == Decimal("1.3")
    assert event.weighted_volume == Decimal("312.000")


def test_rolling_window_is_exactly_30_days_and_account_scoped() -> None:
    events = (
        _event("inside", at=NOW - timedelta(days=29)),
        _event("boundary", at=NOW - timedelta(days=30)),
        _event("outside", at=NOW - timedelta(days=30, microseconds=1)),
        _event("future", at=NOW),
    )

    assert rolling_weighted_volume(events, account_id="0xAbC", as_of=NOW) == Decimal(
        "624.000"
    )


def test_tier_resolution_and_level_up_bonus_are_point_in_time() -> None:
    program = _program()
    assert resolve_taker_tier(Decimal(1999), program=program).name == "None"
    assert resolve_taker_tier(Decimal(2000), program=program).name == "Bronze"
    assert resolve_taker_tier(Decimal(20000), program=program).name == "Silver"

    high_volume = _event("high", at=NOW - timedelta(hours=1), shares="10000")
    snapshot = build_tier_snapshot(
        (high_volume,),
        strategy_id="strategy-1",
        account_id="0xabc",
        calculated_at=NOW,
        activation_ts=NOW + timedelta(days=1),
        highest_tier_level_seen=0,
        program=program,
    )

    assert snapshot.tier.name == "Bronze"
    assert snapshot.level_up_bonus == Decimal(10)
    assert snapshot.activation_ts == NOW + timedelta(days=1)


def test_new_tier_is_forward_only_and_never_backfills_old_fills() -> None:
    program = _program()
    before = _event("before", at=NOW, fee="100")
    activation = NOW + timedelta(days=1)
    after = _event("after", at=activation + timedelta(hours=1), fee="10")
    snapshot = build_tier_snapshot(
        (_event("qualifying", at=NOW - timedelta(hours=1), shares="10000"),),
        strategy_id="strategy-1",
        account_id="0xabc",
        calculated_at=NOW,
        activation_ts=activation,
        highest_tier_level_seen=1,
        program=program,
    )
    estimate = estimate_daily_taker_rebate(
        (before, after),
        snapshot=snapshot,
        reward_date=after.fill_ts.date(),
        carry_in=Decimal(0),
        program=program,
    )

    assert estimate.fill_ids == ("after",)
    assert estimate.eligible_platform_fees == Decimal(10)
    assert estimate.trading_rebate == Decimal("0.30")
    assert estimate.level_up_bonus == 0
    assert estimate.payable_amount == 0
    assert estimate.carry_out == Decimal("0.30")
