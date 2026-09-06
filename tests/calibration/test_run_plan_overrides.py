from decimal import Decimal

from quant.calibration.probe_plan import ProbeExecution
from quant.calibration.run_plan import _override_execution


def _execution() -> ProbeExecution:
    return ProbeExecution(
        count=1,
        sides=("BUY",),
        order_types=("FOK",),
        amount=Decimal("1"),
        amount_unit="QUOTE",
        interval_seconds=Decimal("0"),
    )


def test_sell_override_defaults_to_shares_and_preserves_amount() -> None:
    updated = _override_execution(_execution(), side="SELL")

    assert updated.sides == ("SELL",)
    assert updated.amount_unit == "SHARES"
    assert updated.amount == Decimal("1")


def test_tif_and_amount_overrides_are_frozen_into_execution() -> None:
    updated = _override_execution(
        _execution(),
        side="BUY",
        order_type="FAK",
        amount=Decimal("0.75"),
        amount_unit="QUOTE",
    )

    assert updated.order_types == ("FAK",)
    assert updated.amount == Decimal("0.75")
    assert updated.amount_unit == "QUOTE"
