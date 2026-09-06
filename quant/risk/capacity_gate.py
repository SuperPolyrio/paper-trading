"""Calibrated-domain capacity classification and stress curves."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Iterable

from quant.execution.domain import CapacityStatus, ImpactMode


@dataclass(frozen=True)
class CapacityLimits:
    max_top1_ratio: Decimal = Decimal("0.25")
    max_top5_ratio: Decimal = Decimal("0.25")
    max_visible_ratio: Decimal = Decimal("0.25")
    max_trailing_volume_ratio: Decimal = Decimal("0.05")
    max_strategy_participation: Decimal = Decimal("0.05")
    max_account_participation: Decimal = Decimal("0.10")
    max_event_notional: Decimal = Decimal("100")
    reject_multiplier: Decimal = Decimal("2")


@dataclass(frozen=True)
class CapacitySnapshot:
    order_to_top1_depth: Decimal
    order_to_top5_depth: Decimal
    order_to_visible_eligible_depth: Decimal
    order_to_trailing_real_volume: Decimal
    strategy_market_window_participation: Decimal
    account_market_window_participation: Decimal
    event_level_notional: Decimal


@dataclass(frozen=True)
class CapacityDecision:
    status: CapacityStatus
    impact_mode: ImpactMode
    exceeded: tuple[str, ...]
    metrics: CapacitySnapshot

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "impact_mode": self.impact_mode.value,
            "exceeded": list(self.exceeded),
            "metrics": {
                key: format(value, "f") for key, value in asdict(self.metrics).items()
            },
        }


class CapacityGate:
    def __init__(self, limits: CapacityLimits | None = None) -> None:
        self.limits = limits or CapacityLimits()

    def evaluate(self, metrics: CapacitySnapshot) -> CapacityDecision:
        checks = {
            "order_to_top1_depth": (
                metrics.order_to_top1_depth,
                self.limits.max_top1_ratio,
            ),
            "order_to_top5_depth": (
                metrics.order_to_top5_depth,
                self.limits.max_top5_ratio,
            ),
            "order_to_visible_eligible_depth": (
                metrics.order_to_visible_eligible_depth,
                self.limits.max_visible_ratio,
            ),
            "order_to_trailing_real_volume": (
                metrics.order_to_trailing_real_volume,
                self.limits.max_trailing_volume_ratio,
            ),
            "strategy_market_window_participation": (
                metrics.strategy_market_window_participation,
                self.limits.max_strategy_participation,
            ),
            "account_market_window_participation": (
                metrics.account_market_window_participation,
                self.limits.max_account_participation,
            ),
            "event_level_notional": (
                metrics.event_level_notional,
                self.limits.max_event_notional,
            ),
        }
        exceeded = tuple(
            name for name, (value, limit) in checks.items() if value > limit
        )
        rejected = tuple(
            name
            for name, (value, limit) in checks.items()
            if value > limit * self.limits.reject_multiplier
        )
        if rejected:
            return CapacityDecision(
                CapacityStatus.REJECT_UNCALIBRATED_CAPACITY,
                ImpactMode.STRESS_IMPACT,
                rejected,
                metrics,
            )
        if exceeded:
            return CapacityDecision(
                CapacityStatus.CAPACITY_STRESSED,
                ImpactMode.CAPACITY_GATED_REPLAY,
                exceeded,
                metrics,
            )
        return CapacityDecision(
            CapacityStatus.IN_DOMAIN_CALIBRATED,
            ImpactMode.NO_IMPACT_REPLAY,
            (),
            metrics,
        )

    def capacity_curve(
        self,
        notionals: Iterable[Decimal],
        *,
        visible_depth_notional: Decimal,
    ) -> list[dict[str, str]]:
        depth = max(Decimal("0.00000001"), visible_depth_notional)
        rows = []
        for notional in notionals:
            ratio = max(Decimal("0"), notional) / depth
            stress = max(Decimal("0"), ratio - self.limits.max_visible_ratio)
            rows.append(
                {
                    "notional": format(notional, "f"),
                    "visible_depth_ratio": format(ratio, "f"),
                    "expected_extra_slippage_ticks": format(stress * Decimal("4"), "f"),
                    "fill_probability_haircut": format(
                        max(Decimal("0"), Decimal("1") - stress), "f"
                    ),
                }
            )
        return rows
