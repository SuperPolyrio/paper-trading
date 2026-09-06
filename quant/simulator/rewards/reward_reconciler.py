"""Pure reward estimate-versus-official reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .models import (
    RewardAccrual,
    RewardPayout,
    RewardReconciliation,
    deterministic_reward_id,
)


def reconcile_reward(
    accrual: RewardAccrual,
    payout: RewardPayout,
    *,
    tolerance: Decimal = Decimal("0.000001"),
    at: datetime | None = None,
) -> RewardReconciliation:
    if accrual.strategy_id != payout.strategy_id:
        raise ValueError("reward accrual and payout strategy mismatch")
    if accrual.account_id.lower() != payout.account_id.lower():
        raise ValueError("reward accrual and payout account mismatch")
    if accrual.reward_type != payout.reward_type:
        raise ValueError("reward accrual and payout type mismatch")
    if accrual.currency != payout.currency:
        raise ValueError("reward accrual and payout currency mismatch")
    delta = payout.amount - accrual.amount
    status = "PASS" if abs(delta) <= tolerance else "MISMATCH"
    allocated = min(accrual.amount, payout.amount)
    reconciled_at = at or datetime.now(timezone.utc)
    reconciliation_id = deterministic_reward_id(
        "reward-reconciliation",
        {
            "accrual_id": accrual.accrual_id,
            "payout_id": payout.payout_id,
            "tolerance": str(tolerance),
        },
    )
    return RewardReconciliation(
        reconciliation_id=reconciliation_id,
        accrual_id=accrual.accrual_id,
        payout_id=payout.payout_id,
        status=status,
        modeled_amount=accrual.amount,
        official_amount=payout.amount,
        allocated_amount=allocated,
        amount_delta=delta,
        tolerance=tolerance,
        reconciled_at=reconciled_at,
    )
