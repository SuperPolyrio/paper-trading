from decimal import Decimal

from quant.execution.domain import CapacityStatus
from quant.risk.capacity_gate import CapacityGate, CapacitySnapshot


def _snapshot(ratio: str) -> CapacitySnapshot:
    value = Decimal(ratio)
    return CapacitySnapshot(
        order_to_top1_depth=value,
        order_to_top5_depth=value,
        order_to_visible_eligible_depth=value,
        order_to_trailing_real_volume=Decimal("0.01"),
        strategy_market_window_participation=Decimal("0.01"),
        account_market_window_participation=Decimal("0.01"),
        event_level_notional=Decimal("1"),
    )


def test_capacity_levels() -> None:
    gate = CapacityGate()
    assert (
        gate.evaluate(_snapshot("0.10")).status == CapacityStatus.IN_DOMAIN_CALIBRATED
    )
    assert gate.evaluate(_snapshot("0.30")).status == CapacityStatus.CAPACITY_STRESSED
    assert (
        gate.evaluate(_snapshot("0.60")).status
        == CapacityStatus.REJECT_UNCALIBRATED_CAPACITY
    )
