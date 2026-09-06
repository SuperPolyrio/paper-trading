"""Deterministic professional PnL curves for Paper accounts."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


ZERO = Decimal("0")
ONE = Decimal("1")
CURVE_NAMES = (
    "official_mark",
    "research_mid",
    "liquidation",
    "confirmed_return",
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("PnL timestamps must include a timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ProfessionalNavPoint:
    observed_at: datetime
    official_mark: Decimal | None
    research_mid: Decimal | None
    liquidation: Decimal | None
    confirmed_return: Decimal | None
    external_capital_flow: Decimal = ZERO
    unpriced_quantity: Decimal = ZERO
    complete: bool = True
    source: str = "paper_nav"

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        if self.unpriced_quantity < 0:
            raise ValueError("unpriced_quantity cannot be negative")

    def value(self, curve: str) -> Decimal | None:
        if curve not in CURVE_NAMES:
            raise ValueError(f"unknown PnL curve: {curve}")
        return getattr(self, curve)


def nav_point_from_snapshot(row: Mapping[str, Any]) -> ProfessionalNavPoint:
    """Normalize one persisted NAV row without inventing unavailable values."""

    metadata = row.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    views = metadata.get("valuation_views")
    views = views if isinstance(views, Mapping) else {}
    official = views.get("official_mark_nav")
    research = views.get("mid_mark_nav", row.get("equity"))
    liquidation = views.get(
        "walk_book_liquidation_nav", row.get("conservative_equity")
    )
    confirmed = views.get("confirmed_nav")
    if confirmed is None and metadata.get("finality_complete") is True:
        confirmed = row.get("equity")
    observed_at = row.get("observed_at")
    if not isinstance(observed_at, datetime):
        raise ValueError("NAV row observed_at must be a datetime")
    return ProfessionalNavPoint(
        observed_at=observed_at,
        official_mark=_decimal(official),
        research_mid=_decimal(research),
        liquidation=_decimal(liquidation),
        confirmed_return=_decimal(confirmed),
        external_capital_flow=_decimal(metadata.get("external_capital_flow")) or ZERO,
        unpriced_quantity=_decimal(
            views.get("unliquidated_quantity", metadata.get("unpriced_quantity"))
        )
        or ZERO,
        complete=bool(row.get("nav_complete", False)),
        source=str(metadata.get("mark_source") or "paper_nav"),
    )


def build_professional_pnl_report(
    points: Iterable[ProfessionalNavPoint],
    *,
    initial_nav: Decimal,
) -> dict[str, Any]:
    """Return capital-flow-adjusted curves, periods and risk metrics."""

    ordered = sorted(points, key=lambda item: item.observed_at)
    if initial_nav <= 0:
        raise ValueError("initial_nav must be positive")
    if not ordered:
        return {
            "schema_version": "professional_pnl_v1",
            "status": "NO_DATA",
            "initial_nav": initial_nav,
            "points": [],
            "curves": {},
            "cumulative_external_capital_flow": ZERO,
            "quality": {
                "point_count": 0,
                "complete_point_count": 0,
                "incomplete_point_count": 0,
                "unpriced_quantity": ZERO,
            },
        }

    cumulative_flow = ZERO
    normalized: list[dict[str, Any]] = []
    for point in ordered:
        cumulative_flow += point.external_capital_flow
        values: dict[str, Decimal | None] = {}
        pnl: dict[str, Decimal | None] = {}
        for curve in CURVE_NAMES:
            value = point.value(curve)
            values[curve] = value
            pnl[curve] = (
                value - initial_nav - cumulative_flow if value is not None else None
            )
        normalized.append(
            {
                "observed_at": point.observed_at,
                "values": values,
                "pnl": pnl,
                "external_capital_flow": point.external_capital_flow,
                "cumulative_external_capital_flow": cumulative_flow,
                "unpriced_quantity": point.unpriced_quantity,
                "complete": point.complete,
                "source": point.source,
            }
        )

    curve_reports = {
        curve: _curve_report(ordered, normalized, curve=curve, initial_nav=initial_nav)
        for curve in CURVE_NAMES
    }
    complete_points = sum(1 for point in ordered if point.complete)
    maximum_unpriced = max((point.unpriced_quantity for point in ordered), default=ZERO)
    status = "COMPLETE" if complete_points == len(ordered) and maximum_unpriced == 0 else "DEGRADED"
    return {
        "schema_version": "professional_pnl_v1",
        "status": status,
        "initial_nav": initial_nav,
        "points": normalized,
        "curves": curve_reports,
        "cumulative_external_capital_flow": cumulative_flow,
        "quality": {
            "point_count": len(ordered),
            "complete_point_count": complete_points,
            "incomplete_point_count": len(ordered) - complete_points,
            "unpriced_quantity": maximum_unpriced,
            "available_curves": [
                curve
                for curve, report in curve_reports.items()
                if report["point_count"] > 0
            ],
        },
    }


def _curve_report(
    points: Sequence[ProfessionalNavPoint],
    normalized: Sequence[Mapping[str, Any]],
    *,
    curve: str,
    initial_nav: Decimal,
) -> dict[str, Any]:
    available = [
        (point, row, point.value(curve))
        for point, row in zip(points, normalized, strict=True)
        if point.value(curve) is not None
    ]
    if not available:
        return {
            "status": "UNAVAILABLE",
            "point_count": 0,
            "current_nav": None,
            "pnl": None,
            "return": None,
            "time_weighted_return": None,
            "max_drawdown": None,
            "max_drawdown_pct": None,
            "daily_volatility": None,
            "annualized_sharpe": None,
            "annualized_sortino": None,
            "periods": {},
        }

    current = available[-1][2]
    assert current is not None
    cumulative_flow = Decimal(
        str(available[-1][1]["cumulative_external_capital_flow"])
    )
    pnl = current - initial_nav - cumulative_flow
    adjusted_return = pnl / initial_nav
    twr = _time_weighted_return(available, curve)
    modified_dietz = _modified_dietz_return(
        available, initial_nav=initial_nav, ending_nav=current
    )
    max_dd, max_dd_pct = _max_drawdown([item[2] for item in available])
    daily_values = _daily_closes(available)
    daily_returns = _simple_returns(daily_values)
    volatility = _sample_stddev(daily_returns)
    downside = _sample_stddev([value for value in daily_returns if value < 0])
    return {
        "status": "COMPLETE" if all(item[0].complete for item in available) else "DEGRADED",
        "point_count": len(available),
        "current_nav": current,
        "pnl": pnl,
        "return": adjusted_return,
        "time_weighted_return": twr,
        "modified_dietz_return": modified_dietz,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "daily_volatility": volatility,
        "annualized_sharpe": _annualized_ratio(daily_returns, volatility),
        "annualized_sortino": _annualized_ratio(daily_returns, downside),
        "periods": {
            "day": _period_change(available, days=1),
            "7d": _period_change(available, days=7),
            "30d": _period_change(available, days=30),
            "all": {"pnl": pnl, "return": adjusted_return},
        },
    }


def _modified_dietz_return(
    available: Sequence[tuple[ProfessionalNavPoint, Mapping[str, Any], Decimal | None]],
    *,
    initial_nav: Decimal,
    ending_nav: Decimal,
) -> Decimal | None:
    """Return a cash-flow-aware Modified Dietz return for irregular NAV points."""

    if not available:
        return None
    start = available[0][0].observed_at
    end = available[-1][0].observed_at
    duration = Decimal(str(max(0.0, (end - start).total_seconds())))
    weighted_flows = ZERO
    total_flows = ZERO
    for point, _, _ in available:
        flow = point.external_capital_flow
        total_flows += flow
        if duration == ZERO:
            weight = ONE
        else:
            elapsed = Decimal(str(max(0.0, (point.observed_at - start).total_seconds())))
            weight = max(ZERO, min(ONE, ONE - elapsed / duration))
        weighted_flows += flow * weight
    denominator = initial_nav + weighted_flows
    if denominator <= ZERO:
        return None
    return (ending_nav - initial_nav - total_flows) / denominator


def _time_weighted_return(
    available: Sequence[tuple[ProfessionalNavPoint, Mapping[str, Any], Decimal | None]],
    curve: str,
) -> Decimal | None:
    if len(available) < 2:
        return ZERO
    product = ONE
    previous = available[0][2]
    assert previous is not None
    for point, _, value in available[1:]:
        assert value is not None
        if previous <= 0:
            return None
        segment = (value - point.external_capital_flow) / previous
        product *= segment
        previous = value
    return product - ONE


def _max_drawdown(values: Sequence[Decimal | None]) -> tuple[Decimal, Decimal]:
    peak: Decimal | None = None
    maximum = ZERO
    maximum_pct = ZERO
    for value in values:
        if value is None:
            continue
        peak = value if peak is None else max(peak, value)
        drawdown = peak - value
        drawdown_pct = drawdown / peak if peak > 0 else ZERO
        maximum = max(maximum, drawdown)
        maximum_pct = max(maximum_pct, drawdown_pct)
    return maximum, maximum_pct


def _daily_closes(
    available: Sequence[tuple[ProfessionalNavPoint, Mapping[str, Any], Decimal | None]],
) -> list[Decimal]:
    closes: dict[object, Decimal] = {}
    for point, _, value in available:
        assert value is not None
        closes[point.observed_at.date()] = value
    return list(closes.values())


def _simple_returns(values: Sequence[Decimal]) -> list[Decimal]:
    results: list[Decimal] = []
    for previous, current in zip(values, values[1:]):
        if previous > 0:
            results.append(current / previous - ONE)
    return results


def _sample_stddev(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    floats = [float(value) for value in values]
    mean = sum(floats) / len(floats)
    variance = sum((value - mean) ** 2 for value in floats) / (len(floats) - 1)
    return Decimal(str(math.sqrt(variance)))


def _annualized_ratio(
    returns: Sequence[Decimal], denominator: Decimal | None
) -> Decimal | None:
    if not returns or denominator in (None, ZERO):
        return None
    mean = sum(returns, ZERO) / Decimal(len(returns))
    return mean / denominator * Decimal(str(math.sqrt(365)))


def _period_change(
    available: Sequence[tuple[ProfessionalNavPoint, Mapping[str, Any], Decimal | None]],
    *,
    days: int,
) -> dict[str, Decimal | None]:
    end_point, _, end_value = available[-1]
    assert end_value is not None
    cutoff = end_point.observed_at.timestamp() - days * 86400
    candidates = [item for item in available if item[0].observed_at.timestamp() <= cutoff]
    if not candidates:
        return {"pnl": None, "return": None}
    _, _, start_value = candidates[-1]
    assert start_value is not None
    change = end_value - start_value
    return {
        "pnl": change,
        "return": change / start_value if start_value > 0 else None,
    }


__all__ = [
    "CURVE_NAMES",
    "ProfessionalNavPoint",
    "build_professional_pnl_report",
    "nav_point_from_snapshot",
]
