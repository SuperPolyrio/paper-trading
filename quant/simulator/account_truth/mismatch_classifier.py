"""Field-aware mismatch construction for account truth."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from .models import AccountTruthMismatch, MismatchType

FIELD_MISMATCH_TYPES = {
    "size": MismatchType.POSITION_MISMATCH,
    "quantity": MismatchType.POSITION_MISMATCH,
    "avg_price": MismatchType.COST_BASIS_MISMATCH,
    "initial_value": MismatchType.COST_BASIS_MISMATCH,
    "gross_initial_value": MismatchType.COST_BASIS_MISMATCH,
    "entry_fees_usdc": MismatchType.FEE_MISMATCH,
    "realized_pnl": MismatchType.REALIZED_PNL_MISMATCH,
    "cash_pnl": MismatchType.UNREALIZED_PNL_MISMATCH,
    "current_value": MismatchType.UNREALIZED_PNL_MISMATCH,
    "cash_balance": MismatchType.CASH_MISMATCH,
    "equity": MismatchType.EQUITY_MISMATCH,
    "provisional_fill_count": MismatchType.FINALITY_MISMATCH,
}


def decimal_mismatch(
    *,
    comparison_type: str,
    comparison_key: str,
    field_name: str,
    official_value: Decimal,
    paper_value: Decimal,
    tolerance: Decimal,
    reason: str,
    retryable: bool = False,
    evidence: Mapping[str, Any] | None = None,
    mismatch_type: MismatchType | None = None,
) -> AccountTruthMismatch | None:
    delta = paper_value - official_value
    if abs(delta) <= abs(tolerance):
        return None
    kind = mismatch_type or FIELD_MISMATCH_TYPES.get(
        field_name, MismatchType.UNCLASSIFIED
    )
    return AccountTruthMismatch(
        mismatch_id=_mismatch_id(
            comparison_type,
            comparison_key,
            field_name,
            format(official_value, "f"),
            format(paper_value, "f"),
        ),
        mismatch_type=kind,
        comparison_type=comparison_type,
        comparison_key=comparison_key,
        field_name=field_name,
        official_value=format(official_value, "f"),
        paper_value=format(paper_value, "f"),
        delta=format(delta, "f"),
        tolerance=format(abs(tolerance), "f"),
        reason=reason,
        severity="WARNING" if retryable else "ERROR",
        retryable=retryable,
        evidence=dict(evidence or {}),
    )


def categorical_mismatch(
    *,
    mismatch_type: MismatchType,
    comparison_type: str,
    comparison_key: str,
    field_name: str,
    official_value: Any,
    paper_value: Any,
    reason: str,
    retryable: bool = False,
    evidence: Mapping[str, Any] | None = None,
) -> AccountTruthMismatch | None:
    if official_value == paper_value:
        return None
    official = None if official_value is None else str(official_value)
    paper = None if paper_value is None else str(paper_value)
    return AccountTruthMismatch(
        mismatch_id=_mismatch_id(
            comparison_type,
            comparison_key,
            field_name,
            official or "",
            paper or "",
        ),
        mismatch_type=mismatch_type,
        comparison_type=comparison_type,
        comparison_key=comparison_key,
        field_name=field_name,
        official_value=official,
        paper_value=paper,
        delta=None,
        tolerance="0",
        reason=reason,
        severity="WARNING" if retryable else "ERROR",
        retryable=retryable,
        evidence=dict(evidence or {}),
    )


def informational_mismatch(
    *,
    mismatch_type: MismatchType,
    comparison_type: str,
    comparison_key: str,
    field_name: str,
    reason: str,
    retryable: bool,
    evidence: Mapping[str, Any] | None = None,
) -> AccountTruthMismatch:
    return AccountTruthMismatch(
        mismatch_id=_mismatch_id(
            comparison_type, comparison_key, field_name, mismatch_type.value
        ),
        mismatch_type=mismatch_type,
        comparison_type=comparison_type,
        comparison_key=comparison_key,
        field_name=field_name,
        official_value=None,
        paper_value=None,
        delta=None,
        tolerance="0",
        reason=reason,
        severity="WARNING" if retryable else "ERROR",
        retryable=retryable,
        evidence=dict(evidence or {}),
    )


def _mismatch_id(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]
    return f"account-truth-item:{digest}"
