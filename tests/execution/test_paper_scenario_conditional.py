from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from quant.paper.conditional_orders import TriggerObservation, evaluate_trigger
from quant.paper.scenario_service import evaluate_scenario

NOW = datetime(2026, 8, 16, 8, tzinfo=timezone.utc)


def observation(
    price: str, *, quality: str = "A", gap: bool = False
) -> TriggerObservation:
    return TriggerObservation(
        asset_id="asset-yes",
        event_ts=NOW,
        source="BOOK_DELTA",
        data_quality=quality,
        price=Decimal(price),
        has_gap=gap,
    )


def test_price_trigger_refuses_gapped_evidence() -> None:
    order = {
        "order_type": "STOP_LIMIT",
        "trigger_kind": "PRICE",
        "trigger_operator": "LTE",
        "trigger_value": "0.40",
        "child_order": {"side": "SELL"},
    }
    triggered, price, watermark, reason = evaluate_trigger(
        order, observation("0.35", gap=True)
    )
    assert not triggered
    assert price is None
    assert watermark is None
    assert reason == "UNTRUSTED_OR_GAPPED_OBSERVATION"


def test_trailing_stop_uses_causal_watermark() -> None:
    order = {
        "order_type": "TRAILING_STOP",
        "trigger_kind": "PRICE",
        "trigger_operator": "LTE",
        "trigger_value": "0.01",
        "trailing_percent": "0.10",
        "trailing_offset": None,
        "watermark": "0.70",
        "child_order": {"side": "SELL"},
    }
    triggered, _, watermark, _ = evaluate_trigger(order, observation("0.75"))
    assert not triggered
    assert watermark == Decimal("0.75")
    order["watermark"] = watermark
    triggered, price, watermark, _ = evaluate_trigger(order, observation("0.67"))
    assert triggered
    assert price == Decimal("0.67")
    assert watermark == Decimal("0.75")


def test_resolution_scenario_is_complete_and_does_not_write_ledger() -> None:
    result = evaluate_scenario(
        cash_balance=Decimal(90),
        positions=[
            {
                "asset_id": "asset-yes",
                "quantity": "10",
                "cost_basis": "5",
                "liquidation_mark": "0.55",
            }
        ],
        inputs={"payout_by_asset": {"asset-yes": "1"}},
    )
    assert result["classification"] == "SCENARIO_NOT_EXECUTION"
    assert result["writes_to_paper_ledger"] is False
    assert result["scenario_nav"] == "100"
    assert result["scenario_pnl_delta"] == "4.50"
    assert result["scenario_complete"] is True


def test_depth_haircut_leaves_uncovered_position_unmarked() -> None:
    result = evaluate_scenario(
        cash_balance=Decimal(90),
        positions=[
            {
                "asset_id": "asset-yes",
                "quantity": "10",
                "cost_basis": "5",
                "liquidation_mark": "0.55",
            }
        ],
        inputs={"book_depth_haircut": "0.25"},
    )
    assert result["scenario_nav"] is None
    assert result["scenario_complete"] is False
    assert result["unmarked_assets"] == ["asset-yes"]
    assert result["positions"][0]["unmarked_quantity"] == "2.50"
