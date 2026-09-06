from decimal import Decimal

from quant.maker.probe_sizing import (
    plan_maker_probe_size,
    validate_maker_probe_size,
)


def test_full_probe_uses_the_market_minimum_size() -> None:
    plan = plan_maker_probe_size(
        target_outcome="FULL",
        limit_price=Decimal("0.40"),
        min_order_size=Decimal(5),
        median_trade_size=Decimal(20),
        max_gross_notional=Decimal(5),
    )

    assert plan["status"] == "READY"
    assert plan["recommended_size"] == "5"
    assert plan["maximum_loss_if_fully_filled"] == "2.00"
    assert plan["cancel_on_first_fill"] is False


def test_partial_probe_uses_three_median_trades_and_cancels_on_first_fill() -> None:
    plan = plan_maker_probe_size(
        target_outcome="PARTIAL",
        limit_price=Decimal("0.20"),
        min_order_size=Decimal(5),
        median_trade_size=Decimal(6),
        max_gross_notional=Decimal(5),
    )

    assert plan["status"] == "READY"
    assert plan["recommended_size"] == "18"
    assert plan["cancel_on_first_fill"] is True
    assert validate_maker_probe_size(
        requested_size=Decimal(18), sizing_plan=plan
    ) == ()


def test_partial_probe_uses_bounded_minimum_multiple_without_distribution() -> None:
    plan = plan_maker_probe_size(
        target_outcome="PARTIAL",
        limit_price=Decimal("0.40"),
        min_order_size=Decimal(5),
        median_trade_size=Decimal(0),
        max_gross_notional=Decimal(5),
    )

    assert plan["status"] == "READY_PROSPECTIVE"
    assert plan["recommended_size"] == "12.500000"
    assert plan["maximum_loss_if_fully_filled"] == "5.00000000"
    assert validate_maker_probe_size(
        requested_size=Decimal("12.5"), sizing_plan=plan
    ) == ()


def test_partial_probe_never_exceeds_the_full_fill_loss_cap() -> None:
    plan = plan_maker_probe_size(
        target_outcome="PARTIAL",
        limit_price=Decimal("0.80"),
        min_order_size=Decimal(5),
        median_trade_size=Decimal(10),
        max_gross_notional=Decimal(5),
    )

    assert plan["status"] == "BLOCKED"
    assert Decimal(plan["maximum_loss_if_fully_filled"]) <= Decimal(5)
