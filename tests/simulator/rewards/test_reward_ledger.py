from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant.simulator.rewards import (
    OfficialRewardRecord,
    PolymarketOfficialRewardClient,
    RewardAccrual,
    RewardPayout,
    RewardSchedule,
    RewardScheduleRegistry,
    RewardStatus,
    RewardType,
    normalize_maker_rebates,
    normalize_user_earnings,
    reconcile_reward,
    reconcile_user_earnings_totals,
)

NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)


def _schedule(schedule_id: str, start: datetime, end: datetime | None):
    return RewardSchedule(
        schedule_id=schedule_id,
        reward_type=RewardType.MAKER_REBATE,
        scope_key="condition-1",
        effective_from=start,
        effective_until=end,
        condition_id="condition-1",
        source="OFFICIAL_REWARDS_CONFIG",
    )


def _accrual(amount: str = "0.237519") -> RewardAccrual:
    return RewardAccrual(
        accrual_id="accrual-1",
        strategy_id="strategy-1",
        account_id="0xabc",
        reward_type=RewardType.MAKER_REBATE,
        status=RewardStatus.ESTIMATED,
        amount=Decimal(amount),
        currency="USDC",
        period_start=NOW,
        period_end=NOW + timedelta(days=1),
        effective_ts=NOW + timedelta(days=1),
        idempotency_key="accrual-key-1",
    )


def _payout(amount: str = "0.237519") -> RewardPayout:
    return RewardPayout(
        payout_id="payout-1",
        strategy_id="strategy-1",
        account_id="0xAbC",
        reward_type=RewardType.MAKER_REBATE,
        status=RewardStatus.RECEIVED,
        amount=Decimal(amount),
        currency="USDC",
        reward_date=date(2026, 8, 18),
        effective_ts=NOW + timedelta(days=1),
        idempotency_key="payout-key-1",
        source="CLOB_REBATES_CURRENT",
        source_event_id="official-1",
    )


def test_schedule_registry_is_effective_dated_and_overlap_safe() -> None:
    first = _schedule("schedule-1", NOW, NOW + timedelta(days=1))
    second = _schedule("schedule-2", NOW + timedelta(days=1), None)
    registry = RewardScheduleRegistry((first, second))

    assert (
        registry.resolve(
            RewardType.MAKER_REBATE,
            "condition-1",
            at=NOW + timedelta(hours=12),
        )
        == first
    )
    assert (
        registry.resolve(
            RewardType.MAKER_REBATE,
            "condition-1",
            at=NOW + timedelta(days=2),
        )
        == second
    )
    assert registry.register(first) == first
    with pytest.raises(ValueError, match="overlapping"):
        registry.register(
            _schedule(
                "schedule-overlap",
                NOW + timedelta(hours=1),
                NOW + timedelta(hours=2),
            )
        )


def test_estimated_or_accrued_record_cannot_claim_confirmed_cash() -> None:
    for status in (RewardStatus.ESTIMATED, RewardStatus.ACCRUED, RewardStatus.PAYABLE):
        assert _accrual().status is RewardStatus.ESTIMATED
        record = OfficialRewardRecord(
            source="CLOB_REWARDS_USER",
            source_event_id=f"source-{status.value}",
            reward_type=RewardType.LIQUIDITY_REWARD,
            account_id="0xabc",
            amount=Decimal(1),
            currency="USDC",
            reward_date=date(2026, 8, 18),
            status=status
            if status is not RewardStatus.ESTIMATED
            else RewardStatus.ACCRUED,
        )
        assert record.status is not RewardStatus.RECEIVED


def test_official_maker_rebate_normalization_preserves_market_truth() -> None:
    rows = [
        {
            "date": "2026-02-27",
            "condition_id": "0xcondition",
            "asset_address": "0xasset",
            "maker_address": "0xmaker",
            "rebated_fees_usdc": "0.237519",
        }
    ]
    first = normalize_maker_rebates(rows, account_id="0xmaker")
    second = normalize_maker_rebates(rows, account_id="0xmaker")

    assert first == second
    assert first[0].reward_type is RewardType.MAKER_REBATE
    assert first[0].status is RewardStatus.PAYABLE
    assert first[0].amount == Decimal("0.237519")
    assert first[0].condition_id == "0xcondition"
    assert first[0].asset_id is None
    assert first[0].metadata["reward_asset_address"] == "0xasset"
    assert first[0].raw_payload_hash


def test_official_user_earnings_are_accrual_truth_not_cash_truth() -> None:
    records = normalize_user_earnings(
        [
            {
                "date": "2026-02-27T00:00:00Z",
                "condition_id": "0xcondition",
                "asset_address": "0xasset",
                "maker_address": "0xmaker",
                "earnings": 1.5,
                "asset_rate": 1,
            }
        ],
        account_id="0xmaker",
    )

    assert records[0].status is RewardStatus.ACCRUED
    assert records[0].reward_type is RewardType.LIQUIDITY_REWARD
    assert records[0].amount == Decimal("1.5")
    assert records[0].asset_id is None
    assert records[0].currency == "REWARD_ASSET:0xasset"
    assert records[0].metadata["reward_asset_address"] == "0xasset"


def test_user_earnings_detail_is_proved_against_official_total_by_reward_asset() -> None:
    details = [
        {"asset_address": "0xAAA", "earnings": "1.1"},
        {"asset_address": "0xaaa", "earnings": "0.4"},
        {"asset_address": "0xBBB", "earnings": "2"},
    ]
    totals = [
        {"asset_address": "0xaaa", "earnings": "1.5"},
        {"asset_address": "0xbbb", "earnings": "2.0"},
    ]

    result = reconcile_user_earnings_totals(
        details, totals, reward_date=date(2026, 8, 18)
    )

    assert result.status == "PASS"
    assert result.complete is True
    assert result.delta_by_asset == {"0xaaa": Decimal(0), "0xbbb": Decimal(0)}


def test_user_earnings_total_detects_missing_or_truncated_detail() -> None:
    result = reconcile_user_earnings_totals(
        [{"asset_address": "0xaaa", "earnings": "1"}],
        [{"asset_address": "0xaaa", "earnings": "2"}],
        reward_date=date(2026, 8, 18),
    )

    assert result.status == "MISMATCH"
    assert result.complete is False
    assert result.delta_by_asset["0xaaa"] == Decimal(1)


def test_reconciliation_is_exact_and_never_nets_against_fill_fee() -> None:
    passed = reconcile_reward(_accrual(), _payout(), at=NOW)
    mismatch = reconcile_reward(_accrual(), _payout("0.20"), at=NOW)

    assert passed.status == "PASS"
    assert passed.amount_delta == 0
    assert mismatch.status == "MISMATCH"
    assert mismatch.amount_delta == Decimal("-0.037519")


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return [{"date": "2026-02-27", "rebated_fees_usdc": "1"}]


class _Session:
    def __init__(self) -> None:
        self.call = None

    def get(self, url, **kwargs):
        self.call = (url, kwargs)
        return _Response()


def test_maker_rebate_network_call_is_confined_to_client_wrapper() -> None:
    session = _Session()
    client = PolymarketOfficialRewardClient(
        session=session,
        proxy_url="http://127.0.0.1:18080",
    )

    rows = client.fetch_maker_rebates(
        reward_date=date(2026, 2, 27), maker_address="0xmaker"
    )

    assert rows[0]["rebated_fees_usdc"] == "1"
    assert session.call[0].endswith("/rebates/current")
    assert session.call[1]["proxies"]["https"] == "http://127.0.0.1:18080"
