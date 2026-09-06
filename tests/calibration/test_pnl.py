from decimal import Decimal

from quant.calibration.pnl import (
    PositionPnlState,
    apply_fill,
    apply_resolution,
    build_paired_pnl,
)


def _buy_row(*, paper_fee: str = "0.03800") -> dict:
    return {
        "side": "BUY",
        "prediction": {
            "filled_size": "20",
            "avg_fill_price": "0.05",
            "total_fee": paper_fee,
            "arrival_checkpoint_id": "checkpoint-1",
        },
        "market_snapshot": {
            "best_bid": "0.04",
            "best_ask": "0.05",
            "shadow_checkpoint_id": "checkpoint-1",
            "shadow_observed_at": "2026-07-22T08:55:05+00:00",
        },
        "reconciliation": {"actual_fee": "0.03800"},
    }


def _buy_truth() -> dict:
    return {
        "actual_matched_size": "20",
        "actual_avg_price": "0.05",
    }


def test_matching_buy_calculates_cost_and_conservative_unrealized_pnl() -> None:
    pnl, real_after, paper_after = build_paired_pnl(_buy_row(), _buy_truth())

    assert pnl["status"] == "PASS"
    assert pnl["pnl_reconciled"] is True
    assert pnl["mark"]["method"] == "CONSERVATIVE_EXECUTABLE_BID"
    assert pnl["real"]["cost_basis_after"] == "1.03800"
    assert pnl["real"]["liquidation_value"] == "0.80"
    assert pnl["real"]["unrealized_pnl"] == "-0.23800"
    assert pnl["real"]["realized_pnl_delta"] == "0"
    assert pnl["difference"]["total_pnl_error"] == "0.00000"
    assert real_after == paper_after


def test_real_quote_amount_overrides_venue_limit_price_for_pnl() -> None:
    row = _buy_row(paper_fee="0")
    row["reconciliation"]["actual_fee"] = "0"
    row["prediction"].update(
        filled_size="51.13636363636363636363636364",
        avg_fill_price="0.02151111111111111111111111111",
    )
    truth = {
        "actual_matched_size": "51.136362",
        "actual_avg_price": "0.022",
        "actual_quote_amount": "1.099999",
    }

    pnl, _, _ = build_paired_pnl(row, truth, tolerance="0.00001")

    assert pnl["real"]["notional"] == "1.099999000000000000000000000"
    assert pnl["real"]["average_price"] != "0.022"
    assert Decimal(pnl["difference"]["total_pnl_error"]) < Decimal("0.000001")
    assert pnl["status"] == "PASS"


def test_fee_difference_is_visible_as_total_pnl_mismatch() -> None:
    pnl, _, _ = build_paired_pnl(_buy_row(paper_fee="0.03000"), _buy_truth())

    assert pnl["status"] == "MISMATCH"
    assert pnl["pnl_reconciled"] is False
    assert pnl["difference"]["unrealized_pnl_error"] == "0.00800"
    assert pnl["difference"]["total_pnl_error"] == "0.00800"


def test_sell_uses_persisted_average_cost_for_realized_pnl() -> None:
    state = PositionPnlState(
        quantity=Decimal("10"),
        cost_basis=Decimal("4"),
        realized_pnl=Decimal("0"),
    )
    row = {
        "side": "SELL",
        "prediction": {
            "filled_size": "5",
            "avg_fill_price": "0.7",
            "total_fee": "0.1",
        },
        "market_snapshot": {"best_bid": "0.69", "best_ask": "0.70"},
        "reconciliation": {"actual_fee": "0.1"},
    }
    truth = {"actual_matched_size": "5", "actual_avg_price": "0.7"}

    pnl, real_after, paper_after = build_paired_pnl(
        row,
        truth,
        real_state=state,
        paper_state=state,
    )

    assert pnl["status"] == "PASS"
    assert pnl["scope"] == "PORTFOLIO_LEDGER"
    assert pnl["real"]["realized_pnl_delta"] == "1.4"
    assert real_after.quantity == Decimal("5")
    assert real_after.cost_basis == Decimal("2.0")
    assert real_after.realized_pnl == Decimal("1.4")
    assert real_after == paper_after


def test_isolated_sell_refuses_to_invent_cost_basis() -> None:
    row = {
        "side": "SELL",
        "prediction": {
            "filled_size": "1",
            "avg_fill_price": "0.7",
            "total_fee": "0",
        },
        "market_snapshot": {"best_bid": "0.69", "best_ask": "0.70"},
        "reconciliation": {"actual_fee": "0"},
    }
    truth = {"actual_matched_size": "1", "actual_avg_price": "0.7"}

    pnl, _, _ = build_paired_pnl(row, truth)

    assert pnl["status"] == "PENDING_COST_BASIS"
    assert pnl["pnl_reconciled"] is False


def test_resolution_realizes_winner_and_loser() -> None:
    state = PositionPnlState(
        quantity=Decimal("10"),
        cost_basis=Decimal("4"),
        realized_pnl=Decimal("1"),
    )

    winner, winner_payout, winner_delta = apply_resolution(state, winning=True)
    loser, loser_payout, loser_delta = apply_resolution(state, winning=False)

    assert winner_payout == Decimal("10")
    assert winner_delta == Decimal("6")
    assert winner.realized_pnl == Decimal("7")
    assert loser_payout == Decimal("0")
    assert loser_delta == Decimal("-4")
    assert loser.realized_pnl == Decimal("-3")


def test_fill_formula_matches_existing_paper_ledger_convention() -> None:
    after_buy = apply_fill(
        PositionPnlState(),
        side="BUY",
        size="10",
        price="0.1",
        fee="0.036",
    ).after
    sell = apply_fill(
        after_buy,
        side="SELL",
        size="10",
        price="0.2",
        fee="0.05",
    )

    assert after_buy.cost_basis == Decimal("1.036")
    assert sell.cash_delta == Decimal("1.95")
    assert sell.realized_pnl_delta == Decimal("0.914")
    assert sell.after.quantity == Decimal("0")
    assert sell.after.cost_basis == Decimal("0")
