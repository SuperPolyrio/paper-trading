from datetime import datetime, timezone
from decimal import Decimal

from quant.simulator.economics import (
    AccountCashflowEvent,
    OperationReturnEvent,
    RewardReturnEvent,
    TradeReturnEvent,
    build_account_return_report,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)


def _report():
    return build_account_return_report(
        strategy_id="strategy-1",
        account_id="account-1",
        as_of=NOW,
        trades=(
            TradeReturnEvent(
                "confirmed",
                Decimal("1.50"),
                Decimal("0.10"),
                Decimal("0.02"),
                "CONFIRMED_FINAL",
            ),
            TradeReturnEvent(
                "provisional",
                Decimal("0.50"),
                Decimal("0.03"),
                Decimal(0),
                "MATCHED_PROVISIONAL",
            ),
            TradeReturnEvent(
                "voided",
                Decimal("9.00"),
                Decimal("0.20"),
                Decimal(0),
                "REVERSAL_APPLIED",
            ),
        ),
        operations=(
            OperationReturnEvent("merge", "MERGE", Decimal("0.40")),
            OperationReturnEvent("redeem", "REDEEM", Decimal("0.70")),
        ),
        rewards=(
            RewardReturnEvent(
                "maker-model",
                "MAKER_REBATE",
                modeled_amount=Decimal("0.30"),
                allocated_to_received=Decimal("0.20"),
            ),
            RewardReturnEvent(
                "maker-received",
                "MAKER_REBATE",
                received_amount=Decimal("0.20"),
            ),
            RewardReturnEvent(
                "holding-model",
                "HOLDING_REWARD",
                modeled_amount=Decimal("0.05"),
            ),
        ),
        cashflows=(
            AccountCashflowEvent(
                "operation-cost", "OPERATION_COST", Decimal("0.04"), "CONFIRMED"
            ),
            AccountCashflowEvent("deposit", "DEPOSIT", Decimal(3), "RECEIVED"),
        ),
    )


def test_confirmed_trading_fee_decomposition_excludes_provisional_and_voided() -> None:
    report = _report()

    assert report.gross_execution_pnl == Decimal("1.62")
    assert report.platform_taker_fee_paid == Decimal("0.10")
    assert report.builder_fee_paid == Decimal("0.02")
    assert report.net_trading_pnl == Decimal("1.50")
    assert report.provisional_net_trading_pnl == Decimal("0.50")
    assert report.failed_or_voided_net_trading_pnl == Decimal("9.00")


def test_confirmed_and_estimated_returns_do_not_double_count_rewards() -> None:
    report = _report()

    assert report.reward_received["MAKER_REBATE"] == Decimal("0.20")
    assert report.reward_estimated["MAKER_REBATE"] == Decimal("0.10")
    assert report.reward_estimated["HOLDING_REWARD"] == Decimal("0.05")
    assert report.confirmed_account_return == Decimal("2.76")
    assert report.estimated_account_return == Decimal("2.91")


def test_capital_flow_is_visible_but_not_silently_added_to_return() -> None:
    report = _report()

    assert report.unmodeled_cashflows == ()
    assert report.capital_flows == {"DEPOSIT": Decimal("3")}
    assert report.confirmed_external_cashflow_return == 0
    assert report.reward_coverage == {
        "MAKER_REBATE": "OFFICIAL_RECEIVED",
        "TAKER_REBATE": "NO_EVIDENCE",
        "LIQUIDITY_REWARD": "NO_EVIDENCE",
        "HOLDING_REWARD": "MODEL_ESTIMATED_ONLY",
    }


def test_same_inputs_produce_same_report_hash() -> None:
    first = _report()
    second = _report()

    assert first.report_hash == second.report_hash
    assert first.report_id == second.report_id
