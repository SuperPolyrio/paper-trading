from decimal import Decimal

import pytest

from quant.calibration.live_probe_runner import _validated_sell_seed


TRACKED = {
    "real_quantity": Decimal("3.125"),
    "paper_quantity": Decimal("3.125"),
    "real_cost_basis": Decimal("1.00000"),
    "paper_cost_basis": Decimal("1.00000"),
}


def test_sell_seed_requires_and_preserves_the_tracked_cost_basis() -> None:
    state = _validated_sell_seed(
        observed_quantity=Decimal("3.125"),
        tracked=TRACKED,
        requested_size=Decimal("2"),
    )

    assert state == {
        "quantity": Decimal("3.125"),
        "cost_basis": Decimal("1.00000"),
    }


@pytest.mark.parametrize(
    ("tracked", "message"),
    [
        ({**TRACKED, "paper_quantity": Decimal("3")}, "position baselines differ"),
        ({**TRACKED, "paper_cost_basis": Decimal("0")}, "cost-basis baselines differ"),
    ],
)
def test_sell_seed_fails_closed_on_real_paper_baseline_disagreement(
    tracked: dict,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _validated_sell_seed(
            observed_quantity=Decimal("3.125"),
            tracked=tracked,
            requested_size=Decimal("2"),
        )


def test_sell_seed_fails_closed_when_size_exceeds_observed_position() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        _validated_sell_seed(
            observed_quantity=Decimal("3.125"),
            tracked=TRACKED,
            requested_size=Decimal("4"),
        )
