"""Independent cash and token-position reconciliation for calibration accounts."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping


def reconcile_accounting(
    *,
    expected_cash: Any,
    actual_cash: Any,
    expected_positions: Mapping[str, Any],
    actual_positions: Mapping[str, Any],
    tolerance: Any = "0.00001",
) -> dict[str, Any]:
    allowed = abs(_decimal(tolerance))
    cash_delta = _decimal(actual_cash) - _decimal(expected_cash)
    assets = sorted(set(expected_positions) | set(actual_positions))
    position_deltas = {
        asset_id: _decimal(actual_positions.get(asset_id)) - _decimal(expected_positions.get(asset_id))
        for asset_id in assets
    }
    mismatches = {
        asset_id: format(delta, "f")
        for asset_id, delta in position_deltas.items()
        if abs(delta) > allowed
    }
    passed = abs(cash_delta) <= allowed and not mismatches
    return {
        "schema_version": "calibration_accounting_reconciliation_v1",
        "status": "PASS" if passed else "FAIL",
        "cash_delta": format(cash_delta, "f"),
        "position_deltas": {key: format(value, "f") for key, value in position_deltas.items()},
        "position_mismatches": mismatches,
        "tolerance": format(allowed, "f"),
        "accounting_reconciled": passed,
    }


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")
