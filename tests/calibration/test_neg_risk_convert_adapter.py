from decimal import Decimal

import pytest

from quant.calibration.neg_risk_convert_adapter import (
    NegRiskTokenPair,
    _normalize_standard_event,
    neg_risk_conversion_deltas,
)
from quant.calibration.settlement_redeemer import SettlementPreflightError


def _event(*, augmented: bool = False):
    return {
        "slug": "fed-event",
        "active": True,
        "closed": False,
        "enableNegRisk": True,
        "negRiskAugmented": augmented,
        "negRiskMarketID": "0x" + "12" * 31 + "00",
        "markets": [
            {
                "conditionId": "condition-a",
                "negRisk": True,
                "clobTokenIds": '["a-yes","a-no"]',
                "outcomes": '["Yes","No"]',
            },
            {
                "conditionId": "condition-b",
                "negRisk": True,
                "clobTokenIds": '["b-yes","b-no"]',
                "outcomes": '["Yes","No"]',
            },
            {
                "conditionId": "condition-c",
                "negRisk": True,
                "clobTokenIds": '["c-yes","c-no"]',
                "outcomes": '["Yes","No"]',
            },
        ],
    }


def test_standard_event_normalization_preserves_binary_token_pairs() -> None:
    market_id, slug, pairs, payload_hash = _normalize_standard_event(_event())

    assert market_id[-1] == 0
    assert slug == "fed-event"
    assert [(pair.index, pair.yes_asset_id, pair.no_asset_id) for pair in pairs] == [
        (0, "a-yes", "a-no"),
        (1, "b-yes", "b-no"),
        (2, "c-yes", "c-no"),
    ]
    assert len(payload_hash) == 64


def test_augmented_event_is_rejected_by_standard_converter() -> None:
    with pytest.raises(SettlementPreflightError, match="not standard"):
        _normalize_standard_event(_event(augmented=True))


def test_single_no_conversion_has_zero_cash_and_only_other_yes_credits() -> None:
    pairs = (
        NegRiskTokenPair(0, "a", "a-yes", "a-no"),
        NegRiskTokenPair(1, "b", "b-yes", "b-no"),
        NegRiskTokenPair(2, "c", "c-yes", "c-no"),
    )

    deltas = neg_risk_conversion_deltas(
        pairs=pairs,
        source_index=1,
        amount=Decimal(1),
        amount_out=Decimal("0.99"),
    )

    assert deltas == {
        "b-no": Decimal(-1),
        "a-yes": Decimal("0.99"),
        "c-yes": Decimal("0.99"),
    }
    assert "pUSD" not in deltas
    assert "b-yes" not in deltas
