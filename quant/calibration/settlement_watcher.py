"""Discover resolved calibration positions and preflight redemption without submit."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .settlement_redeemer import SafeSettlementRedeemer, SettlementRedeemError
from .store import CalibrationStore


RedeemerFactory = Callable[[], SafeSettlementRedeemer]


def run_settlement_watch(
    store: CalibrationStore,
    *,
    account_id: str | None,
    redeemer_factory: RedeemerFactory | None,
    redeemer_factories: Sequence[RedeemerFactory] | None = None,
    max_size: Decimal = Decimal("20"),
) -> dict[str, Any]:
    before = store.settlement_watch_positions(account_id=account_id)
    newly_settled = store.settle_resolved_pnl_positions(limit=1000)
    pending = store.pending_settlements(account_id=account_id)
    zero_payout_reconciliations: list[dict[str, Any]] = []
    for settlement in pending:
        if Decimal(str(settlement.get("expected_real_payout") or "0")) != 0:
            continue
        zero_payout_reconciliations.append(
            store.record_observed_settlement_payout(
                settlement_key=str(settlement["settlement_key"]),
                observed_payout=Decimal("0"),
                evidence={
                    "source": "resolution_truth_zero_payout",
                    "winning_asset_id": settlement.get("winning_asset_id"),
                    "held_asset_id": settlement.get("asset_id"),
                    "redeem_required": False,
                },
            )
        )
    if zero_payout_reconciliations:
        pending = store.pending_settlements(account_id=account_id)
    preflights: list[dict[str, Any]] = []
    redeemable_by_asset: dict[str, Any] = {}
    discovery_error: str | None = None
    discovery_attempts: list[dict[str, Any]] = []
    factories = list(redeemer_factories or ())
    if not factories and redeemer_factory is not None:
        factories.append(redeemer_factory)
    for attempt, factory in enumerate(factories, start=1):
        try:
            with factory() as redeemer:
                redeemable_by_asset = {
                    row.asset_id: row
                    for row in redeemer.list_redeemable_positions()
                    if row.size <= max_size
                }
                discovery_attempts.append({"attempt": attempt, "status": "PASS"})
                for settlement in pending:
                    asset_id = str(settlement["asset_id"])
                    candidate = redeemable_by_asset.get(asset_id)
                    if candidate is None:
                        continue
                    try:
                        preflight = redeemer.preflight(
                            candidate,
                            negative_risk_amounts=(
                                _negative_risk_amounts(candidate)
                                if candidate.negative_risk
                                else None
                            ),
                            require_auth=False,
                        )
                        preflights.append(
                            {
                                "settlement_key": settlement["settlement_key"],
                                "status": "PREFLIGHT_PASS",
                                "preflight": preflight.as_dict(),
                                "submit_called": False,
                            }
                        )
                    except SettlementRedeemError as exc:
                        preflights.append(
                            {
                                "settlement_key": settlement["settlement_key"],
                                "status": "PREFLIGHT_BLOCKED",
                                "error": f"{exc.__class__.__name__}:{str(exc)[:500]}",
                                "submit_called": False,
                            }
                        )
            discovery_error = None
            break
        except Exception as exc:  # noqa: BLE001
            discovery_error = f"{exc.__class__.__name__}:{str(exc)[:500]}"
            discovery_attempts.append(
                {
                    "attempt": attempt,
                    "status": "FAIL",
                    "error": discovery_error,
                }
            )
    resolved_waiting = [
        row
        for row in before
        if row.get("resolved") and row.get("winning_asset_id")
    ]
    return {
        "schema_version": "calibration_settlement_watch_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "REDEEM_PREFLIGHT_READY"
            if any(row["status"] == "PREFLIGHT_PASS" for row in preflights)
            else "ZERO_PAYOUT_RECONCILED"
            if zero_payout_reconciliations and not pending
            else "WATCHING"
            if not pending and not discovery_error
            else "WAITING_FOR_RESOLUTION_OR_REDEEMABLE_POSITION"
            if not discovery_error
            else "DISCOVERY_DEGRADED"
        ),
        "submit_called": False,
        "open_position_count": len(before),
        "resolved_position_count": len(resolved_waiting),
        "newly_economically_settled": newly_settled,
        "zero_payout_reconciliations": zero_payout_reconciliations,
        "pending_redemption_count": len(pending),
        "api_redeemable_asset_count": len(redeemable_by_asset),
        "discovery_attempts": discovery_attempts,
        "positions": before,
        "pending_settlements": pending,
        "redeem_preflights": preflights,
        "discovery_error": discovery_error,
        "real_redeem_policy": (
            "Never submitted by this watcher. Execute redeem_calibration_position.py "
            "--execute only after a fresh full preflight."
        ),
    }


def _negative_risk_amounts(candidate: Any) -> tuple[int, int]:
    """Return exact [YES, NO] base-unit balances for binary neg-risk redeem."""

    amount = candidate.size * Decimal("1000000")
    if amount != amount.to_integral_value():
        raise ValueError("negative-risk token size is not exact to six decimals")
    outcome = str(candidate.outcome or "").strip().upper()
    if outcome == "YES":
        return int(amount), 0
    if outcome == "NO":
        return 0, int(amount)
    raise ValueError(f"unsupported negative-risk outcome: {candidate.outcome}")
