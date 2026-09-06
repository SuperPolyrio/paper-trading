"""Explain current Paper position cost from its immutable ledger history."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any


ZERO = Decimal("0")


def reconstruct_position_economics(
    entries: Iterable[Mapping[str, Any]],
    *,
    current_quantity: Decimal,
    current_gross_basis: Decimal,
) -> dict[str, Any]:
    """Rebuild remaining entry fee and fee-exclusive basis.

    The authoritative position table intentionally keeps gross cost basis.  This
    replay only decomposes that already-authoritative number for user display
    and official account reconciliation.  It never changes the ledger.
    """

    rows = tuple(entries)
    gross_before = ZERO
    fee_before = ZERO
    position_before = ZERO
    complete = True
    reason_codes: list[str] = []

    for row in rows:
        event_type = str(row.get("event_type") or "").upper()
        gross_after = _decimal(row.get("cost_basis_after"))
        position_after = _decimal(row.get("position_after"))
        shares_delta = _decimal(row.get("shares_delta"))
        event_fee = max(ZERO, _decimal(row.get("fee")))
        if gross_after < ZERO or position_after < ZERO:
            complete = False
            reason_codes.append("INVALID_LEDGER_STATE")
            continue

        if event_type == "POSITION_SEED" and gross_after > ZERO:
            complete = False
            reason_codes.append("LEGACY_SEED_FEE_UNATTRIBUTED")
            fee_before = ZERO
        elif event_type == "BUY" or (shares_delta > ZERO and event_fee > ZERO):
            fee_before += event_fee
        elif gross_after == ZERO or position_after == ZERO:
            fee_before = ZERO
        elif gross_after < gross_before:
            if gross_before <= ZERO:
                complete = False
                reason_codes.append("MISSING_GROSS_BASIS_ORIGIN")
                fee_before = ZERO
            else:
                fee_before *= gross_after / gross_before
        elif gross_after > gross_before:
            # Split/convert or another fee-free asset operation adds principal,
            # not an entry fee. BUY fees were handled above.
            fee_before += event_fee

        if fee_before > gross_after:
            complete = False
            reason_codes.append("FEE_EXCEEDS_GROSS_BASIS")
            fee_before = min(fee_before, gross_after)
        gross_before = gross_after
        position_before = position_after

    tolerance = Decimal("0.00000001")
    if abs(gross_before - current_gross_basis) > tolerance:
        complete = False
        reason_codes.append("LEDGER_GROSS_BASIS_MISMATCH")
    if abs(position_before - current_quantity) > tolerance:
        complete = False
        reason_codes.append("LEDGER_POSITION_MISMATCH")

    entry_fees = fee_before if rows else ZERO
    fee_exclusive = max(ZERO, current_gross_basis - entry_fees)
    return {
        "status": "EXACT" if complete else "INCOMPLETE",
        "fee_exclusive_basis": fee_exclusive,
        "entry_fees_usdc": entry_fees,
        "gross_initial_value": current_gross_basis,
        "avg_price_excluding_fee": (
            fee_exclusive / current_quantity if current_quantity > ZERO else ZERO
        ),
        "avg_gross_price": (
            current_gross_basis / current_quantity if current_quantity > ZERO else ZERO
        ),
        "source_entry_count": len(rows),
        "reason_codes": list(dict.fromkeys(reason_codes)),
    }


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value if value not in (None, "") else 0))
