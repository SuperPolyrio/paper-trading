from datetime import datetime, timezone
from decimal import Decimal

from quant.simulator.economics import AccountCashflowState, AccountCashflowType
from quant.simulator.rewards import (
    PolymarketOfficialRewardClient,
    RewardStatus,
    RewardType,
    activity_source_event_id,
    normalize_activity_cashflow,
    normalize_activity_reward,
)

NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)
WALLET = "0x1111111111111111111111111111111111111111"


def _activity(activity_type: str, *, tx_hash: str | None = "0xtx"):
    return {
        "proxyWallet": WALLET,
        "timestamp": int(NOW.timestamp()),
        "conditionId": "0xcondition",
        "type": activity_type,
        "usdcSize": "1.25",
        "size": "2.5",
        "transactionHash": tx_hash,
        "asset": "asset-1",
        "outcomeIndex": 1,
    }


def test_reward_activity_requires_transaction_hash_before_cash_confirmation() -> None:
    received = normalize_activity_reward(_activity("MAKER_REBATE"), account_id=WALLET)
    accrued = normalize_activity_reward(
        _activity("TAKER_REBATE", tx_hash=None), account_id=WALLET
    )

    assert received is not None
    assert received.reward_type is RewardType.MAKER_REBATE
    assert received.status is RewardStatus.RECEIVED
    assert received.amount == Decimal("1.25")
    assert accrued is not None
    assert accrued.reward_type is RewardType.TAKER_REBATE
    assert accrued.status is RewardStatus.ACCRUED
    assert activity_source_event_id(_activity("MAKER_REBATE"), WALLET) == received.source_event_id


def test_deposit_and_withdrawal_are_capital_flow_operations() -> None:
    deposit = normalize_activity_cashflow(
        _activity("DEPOSIT"), account_id=WALLET, strategy_id="strategy"
    )
    withdrawal = normalize_activity_cashflow(
        _activity("WITHDRAWAL", tx_hash=None),
        account_id=WALLET,
        strategy_id="strategy",
    )

    assert deposit is not None
    assert deposit.operation_type is AccountCashflowType.DEPOSIT
    assert deposit.state is AccountCashflowState.CONFIRMED
    assert deposit.cash_delta == Decimal("1.25")
    assert deposit.return_delta == 0
    assert withdrawal is not None
    assert withdrawal.state is AccountCashflowState.PROCESSING


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "/activity" in url:
            offset = kwargs["params"]["offset"]
            rows = [
                {**_activity("DEPOSIT"), "timestamp": int(NOW.timestamp()) + index}
                for index in range(3)
            ]
            return _Response(rows[offset : offset + kwargs["params"]["limit"]])
        cursor = kwargs["params"].get("cursor")
        if cursor is None:
            return _Response(
                {"transactions": [{"status": "PROCESSING", "createdTimeMs": 1}], "nextCursor": "next"}
            )
        return _Response(
            {"transactions": [{"status": "COMPLETED", "createdTimeMs": 2, "txHash": "0x2"}], "nextCursor": None}
        )


def test_activity_offset_and_bridge_cursor_are_walked_to_completion() -> None:
    session = _Session()
    client = PolymarketOfficialRewardClient(session=session)

    activity = client.fetch_user_activities(
        user_address=WALLET,
        activity_types=("DEPOSIT",),
        start=int(NOW.timestamp()),
        end=int(NOW.timestamp()) + 10,
        page_size=2,
    )
    bridge = client.fetch_bridge_transactions(bridge_address="0xbridge", limit=100)

    assert len(activity) == 3
    assert session.calls[0][1]["params"]["excludeDepositsWithdrawals"] == "false"
    assert [row["status"] for row in bridge] == ["PROCESSING", "COMPLETED"]
    assert session.calls[-1][1]["params"]["cursor"] == "next"
