"""Deterministic collateral/token conservation checks for CTF match paths."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from .domain import (
    CtfConservationDeltas,
    CtfConservationResult,
    CtfSettlementMatchType,
)


def audit_ctf_conservation(
    match_type: CtfSettlementMatchType,
    deltas: CtfConservationDeltas | None,
    *,
    tolerance: Decimal = Decimal("0.000001"),
) -> CtfConservationResult:
    """Check the normalized aggregate participant deltas for a settlement path."""

    expected = _expected(match_type, deltas)
    observed = _observed(deltas)
    if match_type is CtfSettlementMatchType.UNKNOWN or deltas is None:
        status = "NOT_PROVABLE"
        reason = "counterparty settlement evidence unavailable"
    else:
        passed = all(
            abs(Decimal(observed[key]) - Decimal(value)) <= tolerance
            for key, value in expected.items()
        )
        status = "PASS" if passed else "FAIL"
        reason = "conservation signature matched" if passed else "conservation mismatch"
    payload = {
        "match_type": match_type.value,
        "status": status,
        "expected": expected,
        "observed": observed,
        "tolerance": format(tolerance, "f"),
    }
    return CtfConservationResult(
        status=status,
        reason=reason,
        conservation_hash=hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        expected=expected,
        observed=observed,
    )


def _expected(
    match_type: CtfSettlementMatchType,
    deltas: CtfConservationDeltas | None,
) -> dict[str, str]:
    if match_type is CtfSettlementMatchType.UNKNOWN or deltas is None:
        return {}
    quantity = Decimal(deltas.quantity)
    if match_type is CtfSettlementMatchType.COMPLEMENTARY:
        values = (Decimal(0), Decimal(0), Decimal(0))
    elif match_type is CtfSettlementMatchType.MINT:
        values = (-quantity, quantity, quantity)
    elif match_type is CtfSettlementMatchType.MERGE:
        values = (quantity, -quantity, -quantity)
    else:  # pragma: no cover - enum exhaustiveness
        raise ValueError(f"unsupported CTF settlement type: {match_type.value}")
    return {
        "collateral_delta": format(values[0], "f"),
        "outcome_a_delta": format(values[1], "f"),
        "outcome_b_delta": format(values[2], "f"),
    }


def _observed(deltas: CtfConservationDeltas | None) -> dict[str, str]:
    if deltas is None:
        return {}
    return {
        "collateral_delta": format(Decimal(deltas.collateral_delta), "f"),
        "outcome_a_delta": format(Decimal(deltas.outcome_a_delta), "f"),
        "outcome_b_delta": format(Decimal(deltas.outcome_b_delta), "f"),
    }
