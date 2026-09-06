from decimal import Decimal

import pytest

from quant.settlement.neg_risk_convert import plan_no_to_other_yes


def test_augmented_conversion_requires_source_outcome_and_excludes_its_yes() -> None:
    with pytest.raises(ValueError, match="source outcome YES"):
        plan_no_to_other_yes(
            source_no_asset_id="a-no",
            event_yes_asset_ids=("a-yes", "b-yes", "other-yes"),
            quantity=Decimal(2),
            augmented_neg_risk=True,
        )

    planned = plan_no_to_other_yes(
        source_no_asset_id="a-no",
        source_yes_asset_id="a-yes",
        event_yes_asset_ids=("a-yes", "b-yes", "other-yes"),
        quantity=Decimal(2),
        augmented_neg_risk=True,
    )

    assert planned.status == "PLANNED_AUGMENTED"
    assert planned.collateral_delta == 0
    assert planned.yes_deltas == {
        "b-yes": Decimal(2),
        "other-yes": Decimal(2),
    }


def test_standard_single_no_conversion_has_no_cash_delta_and_excludes_source_yes() -> None:
    planned = plan_no_to_other_yes(
        source_no_asset_id="a-no",
        source_yes_asset_id="a-yes",
        event_yes_asset_ids=("a-yes", "b-yes", "other-yes"),
        quantity=Decimal(1),
        augmented_neg_risk=False,
    )

    assert planned.status == "PLANNED"
    assert planned.collateral_delta == 0
    assert planned.yes_deltas == {
        "b-yes": Decimal(1),
        "other-yes": Decimal(1),
    }
