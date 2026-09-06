from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant.execution.models.fee_rebate import FeeSchedule as CompatibilitySchedule
from quant.simulator.economics import (
    FeeEngine,
    FeeSchedule,
    FeeScheduleRegistry,
    LiquidityRole,
    maximum_order_fees,
    reconcile_fee_charge,
)
from quant.simulator.economics.fee_rounding import round_fee

NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)


def _schedule(**overrides) -> FeeSchedule:
    values = {
        "schedule_id": "schedule-1",
        "asset_id": "asset-1",
        "condition_id": "condition-1",
        "effective_from": NOW,
        "effective_until": NOW + timedelta(hours=1),
        "platform_fee_rate": Decimal("0.04"),
        "platform_fee_exponent": Decimal(1),
        "platform_taker_only": True,
        "source": "test_market_terms",
    }
    values.update(overrides)
    return FeeSchedule(**values)


def test_each_fill_is_rounded_before_order_total() -> None:
    schedule = _schedule()
    first = FeeEngine.calculate(
        fill_id="fill-1",
        schedule=schedule,
        liquidity_role="TAKER",
        price=Decimal("0.40"),
        shares=Decimal(50),
    )
    second = FeeEngine.calculate(
        fill_id="fill-2",
        schedule=schedule,
        liquidity_role="TAKER",
        price=Decimal("0.42"),
        shares=Decimal(50),
    )
    vwap = FeeEngine.calculate(
        fill_id="incorrect-vwap-model",
        schedule=schedule,
        liquidity_role="TAKER",
        price=Decimal("0.41"),
        shares=Decimal(100),
    )

    assert first.platform_fee == Decimal("0.48000")
    assert second.platform_fee == Decimal("0.48720")
    assert first.total_fee + second.total_fee == Decimal("0.96720")
    assert vwap.total_fee == Decimal("0.96760")


def test_fee_free_market_and_sub_unit_rounding_are_zero() -> None:
    free = FeeEngine.calculate(
        fill_id="free",
        schedule=_schedule(
            schedule_id="free",
            platform_fee_rate=Decimal(0),
            platform_fee_exponent=Decimal(0),
        ),
        liquidity_role="TAKER",
        price=Decimal("0.50"),
        shares=Decimal(100),
    )
    tiny = FeeEngine.calculate(
        fill_id="tiny",
        schedule=_schedule(platform_fee_rate=Decimal("0.000001")),
        liquidity_role="TAKER",
        price=Decimal("0.01"),
        shares=Decimal("0.01"),
    )

    assert free.total_fee == Decimal("0.00000")
    assert tiny.total_fee == Decimal("0.00000")


def test_v2_half_unit_fee_is_truncated_like_orderfilled_receipts() -> None:
    assert round_fee(Decimal("0.058275")) == Decimal("0.05827")
    assert round_fee(Decimal("0.060475")) == Decimal("0.06047")


def test_builder_fee_stacks_and_maker_platform_fee_remains_zero() -> None:
    schedule = _schedule(
        builder_code="builder-1",
        builder_taker_fee_bps=100,
        builder_maker_fee_bps=50,
    )
    taker = FeeEngine.calculate(
        fill_id="taker",
        schedule=schedule,
        liquidity_role=LiquidityRole.TAKER,
        price=Decimal("0.40"),
        shares=Decimal(10),
    )
    maker = FeeEngine.calculate(
        fill_id="maker",
        schedule=schedule,
        liquidity_role=LiquidityRole.MAKER,
        price=Decimal("0.40"),
        shares=Decimal(10),
    )

    assert taker.platform_fee == Decimal("0.09600")
    assert taker.builder_fee == Decimal("0.04000")
    assert taker.total_fee == Decimal("0.13600")
    assert maker.platform_fee == Decimal("0.00000")
    assert maker.builder_fee == Decimal("0.02000")
    assert maker.total_fee == Decimal("0.02000")


def test_all_in_reservation_includes_platform_and_builder_fees() -> None:
    schedule = _schedule(builder_taker_fee_bps=100)
    shares = maximum_order_fees(
        schedule=schedule,
        liquidity_role="TAKER",
        limit_price=Decimal("0.60"),
        shares=Decimal(2),
    )
    quote = maximum_order_fees(
        schedule=schedule,
        liquidity_role="TAKER",
        limit_price=Decimal("0.60"),
        quote_notional=Decimal(5),
    )

    assert shares.platform_fee == Decimal("0.02000")
    assert shares.builder_fee == Decimal("0.01200")
    assert quote.platform_fee == Decimal("0.20000")
    assert quote.builder_fee == Decimal("0.05000")


def test_post_only_reservation_uses_maker_builder_rate_not_taker_platform() -> None:
    schedule = _schedule(builder_taker_fee_bps=100, builder_maker_fee_bps=50)
    charge = maximum_order_fees(
        schedule=schedule,
        liquidity_role="MAKER",
        limit_price=Decimal("0.60"),
        shares=Decimal(2),
    )

    assert charge.platform_fee == Decimal("0.00000")
    assert charge.builder_fee == Decimal("0.00600")


def test_fee_reconciliation_reports_exact_and_mismatch() -> None:
    charge = FeeEngine.calculate(
        fill_id="fill",
        schedule=_schedule(builder_taker_fee_bps=100),
        liquidity_role="TAKER",
        price=Decimal("0.40"),
        shares=Decimal(10),
    )

    exact = reconcile_fee_charge(
        charge,
        official_platform_fee=Decimal("0.09600"),
        official_builder_fee=Decimal("0.04000"),
        official_total_fee=Decimal("0.13600"),
    )
    mismatch = reconcile_fee_charge(
        charge,
        official_total_fee=Decimal("0.14000"),
    )

    assert exact.status == "PASS"
    assert mismatch.status == "MISMATCH"
    assert mismatch.total_fee_delta == Decimal("-0.00400")


def test_schedule_identity_and_charge_identity_are_deterministic() -> None:
    schedule = _schedule()
    left = FeeEngine.calculate(
        fill_id="same",
        schedule=schedule,
        liquidity_role="TAKER",
        price=Decimal("0.40"),
        shares=Decimal(10),
    )
    right = FeeEngine.calculate(
        fill_id="same",
        schedule=schedule,
        liquidity_role="TAKER",
        price=Decimal("0.40"),
        shares=Decimal(10),
    )

    assert schedule.applies_at(NOW + timedelta(minutes=1))
    assert not schedule.applies_at(NOW + timedelta(hours=1))
    assert left == right
    assert left.fee_charge_id == right.fee_charge_id


def test_registry_resolves_point_in_time_and_rejects_id_collision() -> None:
    first = _schedule(effective_until=NOW + timedelta(minutes=30))
    second = _schedule(
        schedule_id="schedule-2",
        effective_from=NOW + timedelta(minutes=30),
        effective_until=NOW + timedelta(hours=1),
        platform_fee_rate=Decimal("0.05"),
    )
    registry = FeeScheduleRegistry((second, first, first))

    assert registry.resolve("asset-1", at=NOW + timedelta(minutes=1)) == first
    assert registry.resolve("asset-1", at=NOW + timedelta(minutes=31)) == second
    assert registry.history("asset-1") == (first, second)
    with pytest.raises(ValueError, match="collision"):
        registry.register(_schedule(platform_fee_rate=Decimal("0.06")))


def test_builder_limits_and_nonzero_subunit_exponent_fail_closed() -> None:
    with pytest.raises(ValueError, match="builder taker"):
        _schedule(builder_taker_fee_bps=101)
    with pytest.raises(ValueError, match="exponent"):
        _schedule(platform_fee_exponent=Decimal("0.5"))


def test_legacy_fee_schedule_delegates_to_dynamic_curve() -> None:
    schedule = CompatibilitySchedule("v1", NOW, Decimal("0.04"))
    assert schedule.fee(shares=Decimal(10), price=Decimal("0.40")) == Decimal("0.09600")
