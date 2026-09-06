from decimal import Decimal

from quant.simulator.ctf import (
    CtfConservationDeltas,
    CtfMatchEvidence,
    CtfSettlementMatchType,
    build_ctf_settlement_audit,
    unknown_ctf_settlement_audit,
)


def _evidence(
    *,
    maker_side: str,
    taker_side: str,
    maker_asset: str,
    taker_asset: str,
    complementary: bool | None,
) -> CtfMatchEvidence:
    return CtfMatchEvidence(
        evidence_id="chain:match:1",
        evidence_source="CTF_EXCHANGE_ORDER_MATCHED",
        maker_side=maker_side,
        taker_side=taker_side,
        maker_asset_id=maker_asset,
        taker_asset_id=taker_asset,
        complementary_assets=complementary,
    )


def test_opposite_sides_same_asset_classify_as_complementary() -> None:
    audit = build_ctf_settlement_audit(
        _evidence(
            maker_side="SELL",
            taker_side="BUY",
            maker_asset="yes",
            taker_asset="yes",
            complementary=False,
        ),
        CtfConservationDeltas(
            quantity=Decimal("5"),
            collateral_delta=Decimal("0"),
            outcome_a_delta=Decimal("0"),
            outcome_b_delta=Decimal("0"),
        ),
    )

    assert audit.settlement_match_type is CtfSettlementMatchType.COMPLEMENTARY
    assert audit.conservation.status == "PASS"


def test_two_complementary_buys_classify_as_mint_and_conserve() -> None:
    audit = build_ctf_settlement_audit(
        _evidence(
            maker_side="BUY",
            taker_side="BUY",
            maker_asset="yes",
            taker_asset="no",
            complementary=True,
        ),
        CtfConservationDeltas(
            quantity=Decimal("2"),
            collateral_delta=Decimal("-2"),
            outcome_a_delta=Decimal("2"),
            outcome_b_delta=Decimal("2"),
        ),
    )

    assert audit.settlement_match_type is CtfSettlementMatchType.MINT
    assert audit.conservation.status == "PASS"


def test_two_complementary_sells_classify_as_merge_and_detect_mismatch() -> None:
    audit = build_ctf_settlement_audit(
        _evidence(
            maker_side="SELL",
            taker_side="SELL",
            maker_asset="yes",
            taker_asset="no",
            complementary=True,
        ),
        CtfConservationDeltas(
            quantity=Decimal("2"),
            collateral_delta=Decimal("2"),
            outcome_a_delta=Decimal("-2"),
            outcome_b_delta=Decimal("-1.9"),
        ),
    )

    assert audit.settlement_match_type is CtfSettlementMatchType.MERGE
    assert audit.conservation.status == "FAIL"


def test_l2_fill_without_counterparty_evidence_remains_explicitly_unknown() -> None:
    first = unknown_ctf_settlement_audit(evidence_id="paper:fill:1")
    second = unknown_ctf_settlement_audit(evidence_id="paper:fill:1")

    assert first.settlement_match_type is CtfSettlementMatchType.UNKNOWN
    assert first.conservation.status == "NOT_PROVABLE"
    assert first.conservation.conservation_hash == second.conservation.conservation_hash


def test_unverified_complementary_pair_does_not_get_guessed() -> None:
    audit = build_ctf_settlement_audit(
        _evidence(
            maker_side="BUY",
            taker_side="BUY",
            maker_asset="yes",
            taker_asset="unknown-token",
            complementary=None,
        ),
        None,
    )

    assert audit.settlement_match_type is CtfSettlementMatchType.UNKNOWN
    assert audit.conservation.status == "NOT_PROVABLE"
