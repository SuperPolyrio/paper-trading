from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from quant.simulator.rewards import (
    ReferralProgramSchedule,
    ReferralRelationship,
    ReferralTradeEvidence,
    assess_referral_trade,
    estimate_daily_referral_reward,
    bundled_program_rule_rows,
    official_referral_schedule,
    official_taker_rebate_schedule,
)


NOW = datetime(2026, 8, 19, 1, tzinfo=timezone.utc)


def _program() -> ReferralProgramSchedule:
    return ReferralProgramSchedule(
        schedule_id="referral-2026-05-28",
        effective_from=datetime(2026, 5, 28, tzinfo=timezone.utc),
        effective_until=None,
        minimum_owner_lifetime_volume=Decimal(10_000),
        direct_rate=Decimal("0.10"),
        indirect_rate=Decimal("0.05"),
        earning_window_days=30,
        platinum_tier_level=4,
        source="OFFICIAL_DOC_FIXTURE",
    )


def _evidence(
    evidence_id: str,
    *,
    relationship: ReferralRelationship = ReferralRelationship.DIRECT,
    trade_ts: datetime = NOW,
    tier: int = 2,
) -> ReferralTradeEvidence:
    return ReferralTradeEvidence(
        evidence_id=evidence_id,
        strategy_id="strategy",
        account_id="0xowner",
        referred_account_id=f"0x{evidence_id}",
        relationship=relationship,
        signup_ts=NOW - timedelta(days=10),
        trade_ts=trade_ts,
        gross_platform_fee=Decimal(10),
        referred_taker_rebate=Decimal(2),
        referred_tier_level=tier,
        source="OFFICIAL_REFERRED_TRADE",
    )


def test_referral_uses_net_fee_and_direct_indirect_rates() -> None:
    program = _program()
    direct = assess_referral_trade(
        _evidence("direct"), owner_lifetime_volume=Decimal(10_000), program=program
    )
    indirect = assess_referral_trade(
        _evidence("indirect", relationship=ReferralRelationship.INDIRECT),
        owner_lifetime_volume=Decimal(10_000),
        program=program,
    )

    result = estimate_daily_referral_reward(
        (direct, indirect),
        strategy_id="strategy",
        account_id="0xowner",
        reward_date=date(2026, 8, 19),
        owner_lifetime_volume=Decimal(10_000),
        program=program,
    )

    assert direct.estimated_reward == Decimal("0.80")
    assert indirect.estimated_reward == Decimal("0.40")
    assert result.estimated_reward == Decimal("1.20")
    assert result.direct_net_fees == Decimal(8)
    assert result.indirect_net_fees == Decimal(8)


def test_referral_enforces_owner_threshold_window_and_platinum_cutoff() -> None:
    program = _program()
    below = assess_referral_trade(
        _evidence("below"), owner_lifetime_volume=Decimal("9999.99"), program=program
    )
    expired = assess_referral_trade(
        ReferralTradeEvidence(
            **{
                **_evidence("expired").__dict__,
                "signup_ts": NOW - timedelta(days=31),
            }
        ),
        owner_lifetime_volume=Decimal(10_000),
        program=program,
    )
    platinum = assess_referral_trade(
        _evidence("platinum", tier=4),
        owner_lifetime_volume=Decimal(10_000),
        program=program,
    )

    assert below.ineligibility_reason == "OWNER_VOLUME_BELOW_THRESHOLD"
    assert expired.ineligibility_reason == "REFERRAL_WINDOW_EXPIRED"
    assert platinum.ineligibility_reason == "REFERRED_USER_REACHED_PLATINUM"
    assert below.estimated_reward == expired.estimated_reward == platinum.estimated_reward == 0


def test_bundled_official_rules_are_complete_and_preserve_holding_rate_conflict() -> None:
    taker = official_taker_rebate_schedule()
    referral = official_referral_schedule()
    rows = bundled_program_rule_rows()

    assert len(taker.tiers) == 7
    assert taker.tiers[-1].name == "Obsidian"
    assert taker.tiers[-1].rebate_rate == Decimal("0.50")
    assert referral.minimum_owner_lifetime_volume == Decimal(10_000)
    holding = [row for row in rows if row["_source"] == "OFFICIAL_DOC_HOLDING_REWARD"]
    assert {row["annual_rate"] for row in holding} == {"0.0325", "0.04"}
    assert any(row.get("requires_manual_authority_resolution") for row in holding)
