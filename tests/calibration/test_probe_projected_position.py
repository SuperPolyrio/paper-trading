from dataclasses import replace
from decimal import Decimal

from quant.calibration.probe_plan import load_probe_plan
from quant.calibration.probe_risk_guard import _projected_market_position


PLAN = load_probe_plan("config/calibration/taker_operation_fidelity_multi_dynamic_live.yaml")


def test_quote_buy_projects_acquired_shares() -> None:
    plan = replace(
        PLAN,
        execution=replace(
            PLAN.execution,
            amount=Decimal("2"),
            amount_unit="QUOTE",
        ),
    )

    projected = _projected_market_position(
        plan,
        {"best_ask": "0.014"},
        "BUY",
        current_position=Decimal("0"),
    )

    assert projected > plan.limits.max_position_per_market


def test_share_sell_projects_only_remaining_position() -> None:
    plan = replace(
        PLAN,
        execution=replace(
            PLAN.execution,
            amount=Decimal("5"),
            amount_unit="SHARES",
        ),
    )

    assert _projected_market_position(
        plan,
        {"best_bid": "0.013"},
        "SELL",
        current_position=Decimal("12"),
    ) == Decimal("7")
