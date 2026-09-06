"""Risk-bounded sizing for controlled Maker FULL and PARTIAL probes."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any


def plan_maker_probe_size(
    *,
    target_outcome: str,
    limit_price: Decimal,
    min_order_size: Decimal,
    median_trade_size: Decimal,
    max_gross_notional: Decimal,
    partial_multiplier: Decimal = Decimal(3),
) -> dict[str, Any]:
    target = str(target_outcome).upper()
    if target not in {"FULL", "PARTIAL"}:
        raise ValueError("Maker probe target must be FULL or PARTIAL")
    price = Decimal(limit_price)
    minimum = max(Decimal(0), Decimal(min_order_size))
    median = max(Decimal(0), Decimal(median_trade_size))
    notional_cap = max(Decimal(0), Decimal(max_gross_notional))
    if price <= 0 or price >= 1:
        raise ValueError("Maker probe limit price must be strictly between zero and one")
    cap_size = (notional_cap / price).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN
    )
    if target == "FULL":
        recommended = minimum
        status = "READY" if recommended > 0 and recommended <= cap_size else "BLOCKED"
        reason = (
            "minimum_size_maximizes_full_fill_observation_probability"
            if status == "READY"
            else "minimum_order_exceeds_notional_cap"
        )
    else:
        multiplier = min(Decimal(4), max(Decimal(2), Decimal(partial_multiplier)))
        recommended = max(minimum, median * multiplier)
        if median <= 0:
            recommended = min(cap_size, minimum * Decimal(3))
            status = (
                "READY_PROSPECTIVE"
                if minimum > 0 and recommended >= minimum * Decimal(2)
                else "BLOCKED"
            )
            reason = (
                "three_minimum_orders_for_prospective_partial_label"
                if status == "READY_PROSPECTIVE"
                else "notional_cap_cannot_fit_two_minimum_orders"
            )
        elif cap_size <= median:
            recommended = cap_size
            status = "BLOCKED"
            reason = "notional_cap_cannot_exceed_one_median_trade"
        elif recommended > cap_size:
            recommended = cap_size
            status = "READY_DEGRADED"
            reason = "recommended_partial_size_clipped_by_notional_cap"
        else:
            status = "READY"
            reason = "two_to_four_median_trade_partial_probe"
    return {
        "schema_version": "maker_probe_sizing_v1",
        "target_outcome": target,
        "status": status,
        "reason": reason,
        "limit_price": format(price, "f"),
        "min_order_size": format(minimum, "f"),
        "median_trade_size": format(median, "f"),
        "max_gross_notional": format(notional_cap, "f"),
        "max_size_under_notional_cap": format(cap_size, "f"),
        "recommended_size": format(max(Decimal(0), recommended), "f"),
        "maximum_loss_if_fully_filled": format(
            max(Decimal(0), recommended) * price, "f"
        ),
        "cancel_on_first_fill": target == "PARTIAL",
        "self_trade_forbidden": True,
    }


def validate_maker_probe_size(
    *, requested_size: Decimal, sizing_plan: dict[str, Any]
) -> tuple[str, ...]:
    requested = max(Decimal(0), Decimal(requested_size))
    issues: list[str] = []
    if not str(sizing_plan.get("status") or "").startswith("READY"):
        issues.append(str(sizing_plan.get("reason") or "maker_probe_sizing_not_ready"))
        return tuple(issues)
    minimum = Decimal(str(sizing_plan.get("min_order_size") or 0))
    cap_size = Decimal(str(sizing_plan.get("max_size_under_notional_cap") or 0))
    if requested < minimum:
        issues.append("requested_size_below_market_minimum")
    if requested > cap_size:
        issues.append("requested_size_exceeds_notional_cap")
    if str(sizing_plan.get("target_outcome") or "").upper() == "PARTIAL":
        median = Decimal(str(sizing_plan.get("median_trade_size") or 0))
        if median > 0 and requested < median * Decimal(2):
            issues.append("partial_probe_size_below_two_median_trades")
        if median > 0 and requested > median * Decimal(4) and requested > minimum:
            issues.append("partial_probe_size_above_four_median_trades")
    return tuple(issues)
