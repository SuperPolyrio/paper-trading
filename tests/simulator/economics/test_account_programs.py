from datetime import datetime, timezone
from decimal import Decimal

from quant.simulator.economics import (
    AccountCashflowType,
    BridgeDirection,
    BridgeTransfer,
    BridgeTransferState,
    DisputeBond,
    DisputeOutcome,
    SponsorCommitment,
)

NOW = datetime(2026, 8, 19, 15, tzinfo=timezone.utc)


def test_completed_bridge_generates_principal_and_fee_without_guessing_pending_cash() -> None:
    pending = BridgeTransfer(
        "bridge-1", "account", "strategy", BridgeDirection.DEPOSIT,
        Decimal("100"), Decimal("1"), "USDC", BridgeTransferState.PROCESSING,
        NOW, "source-1",
    ).account_operations()
    complete = BridgeTransfer(
        "bridge-1", "account", "strategy", BridgeDirection.DEPOSIT,
        Decimal("100"), Decimal("1"), "USDC", BridgeTransferState.COMPLETED,
        NOW, "source-1", transaction_hash="0xtx",
    ).account_operations()

    assert pending[0].cash_delta == Decimal("100")
    assert pending[0].state.value == "PROCESSING"
    assert [item.operation_type for item in complete] == [
        AccountCashflowType.BRIDGE_DEPOSIT,
        AccountCashflowType.BRIDGE_FEE,
    ]
    assert sum((item.cash_delta for item in complete), Decimal(0)) == Decimal("99")


def test_completed_bridge_requires_destination_transaction_hash() -> None:
    try:
        BridgeTransfer(
            "bridge-1", "account", "strategy", BridgeDirection.DEPOSIT,
            Decimal("100"), Decimal("1"), "USDC", BridgeTransferState.COMPLETED,
            NOW, "source-1",
        )
    except ValueError as exc:
        assert "destination tx hash" in str(exc)
    else:
        raise AssertionError("completed bridge without transaction evidence was accepted")


def test_sponsor_cancel_refund_starts_next_midnight_and_distributions_are_returns() -> None:
    sponsor = SponsorCommitment(
        "sponsor-1", "account", "strategy", "condition", Decimal("10"),
        "USDC", NOW, "source-sponsor", distributed_amount=Decimal("3"),
    )
    distribution = sponsor.distribution_operation(
        amount=Decimal("2"), distribution_date=NOW
    )
    refund = sponsor.cancellation_refund_operation(requested_at=NOW)

    assert sponsor.commitment_operation().cash_delta == Decimal("-10")
    assert distribution.cash_delta == 0
    assert distribution.return_delta == Decimal("-2")
    assert refund is not None
    assert refund.amount == Decimal("7")
    assert refund.state.value == "PROCESSING"
    assert refund.effective_ts.isoformat() == "2026-08-20T00:00:00+00:00"


def test_dispute_winner_receives_principal_return_and_separate_bounty() -> None:
    bond = DisputeBond(
        "dispute-1", "account", "strategy", "condition", Decimal("750"),
        "USDC", NOW, "source-dispute",
    )
    won = bond.resolution_operations(
        outcome=DisputeOutcome.WON,
        resolved_at=NOW,
        bounty_amount=Decimal("375"),
        transaction_hash="0xresolution",
    )
    lost = bond.resolution_operations(
        outcome=DisputeOutcome.LOST,
        resolved_at=NOW,
    )

    assert bond.post_operation().cash_delta == Decimal("-750")
    assert won[0].operation_type is AccountCashflowType.DISPUTE_BOND_RETURN
    assert won[0].return_delta == 0
    assert won[1].operation_type is AccountCashflowType.DISPUTE_BOUNTY
    assert won[1].return_delta == Decimal("375")
    assert lost[0].return_delta == Decimal("-750")
