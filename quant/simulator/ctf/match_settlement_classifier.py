"""Classify CTF settlement only when counterparty evidence is sufficient."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .domain import (
    CtfConservationDeltas,
    CtfMatchEvidence,
    CtfSettlementAudit,
    CtfSettlementMatchType,
)
from .settlement_conservation import audit_ctf_conservation


def classify_ctf_match(evidence: CtfMatchEvidence) -> CtfSettlementMatchType:
    maker_side = evidence.maker_side.upper()
    taker_side = evidence.taker_side.upper()
    same_asset = evidence.maker_asset_id == evidence.taker_asset_id
    if same_asset and maker_side != taker_side:
        return CtfSettlementMatchType.COMPLEMENTARY
    if evidence.complementary_assets is not True or same_asset:
        return CtfSettlementMatchType.UNKNOWN
    if maker_side == taker_side == "BUY":
        return CtfSettlementMatchType.MINT
    if maker_side == taker_side == "SELL":
        return CtfSettlementMatchType.MERGE
    return CtfSettlementMatchType.UNKNOWN


def build_ctf_settlement_audit(
    evidence: CtfMatchEvidence,
    deltas: CtfConservationDeltas | None,
) -> CtfSettlementAudit:
    match_type = classify_ctf_match(evidence)
    return CtfSettlementAudit(
        evidence_id=evidence.evidence_id,
        evidence_source=evidence.evidence_source,
        settlement_match_type=match_type,
        conservation=audit_ctf_conservation(match_type, deltas),
        evidence={
            "maker_side": evidence.maker_side.upper(),
            "taker_side": evidence.taker_side.upper(),
            "maker_asset_id": evidence.maker_asset_id,
            "taker_asset_id": evidence.taker_asset_id,
            "complementary_assets": evidence.complementary_assets,
            **dict(evidence.payload),
        },
    )


def unknown_ctf_settlement_audit(
    *,
    evidence_id: str,
    evidence_source: str = "PAPER_L2_NO_COUNTERPARTY",
    evidence: Mapping[str, Any] | None = None,
) -> CtfSettlementAudit:
    match_type = CtfSettlementMatchType.UNKNOWN
    return CtfSettlementAudit(
        evidence_id=evidence_id,
        evidence_source=evidence_source,
        settlement_match_type=match_type,
        conservation=audit_ctf_conservation(match_type, None),
        evidence=dict(evidence or {}),
    )
