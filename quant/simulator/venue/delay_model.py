"""Sports/game delay that can temporarily make an acknowledged order uncancelable."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SportsDelayModel:
    uncancelable_window_ns: int = 0

    def __post_init__(self) -> None:
        if self.uncancelable_window_ns < 0:
            raise ValueError("uncancelable_window_ns must be non-negative")

    def uncancelable_until(self, acknowledged_ts_ns: int) -> int:
        return int(acknowledged_ts_ns) + self.uncancelable_window_ns
