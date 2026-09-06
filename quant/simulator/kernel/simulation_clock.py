"""Monotonic integer-nanosecond clock for a simulation run."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SimulationClock:
    current_ts_ns: int | None = None

    def advance_to(self, event_ts_ns: int) -> int:
        target = int(event_ts_ns)
        if target < 0:
            raise ValueError("simulation clock cannot advance to a negative timestamp")
        if self.current_ts_ns is not None and target < self.current_ts_ns:
            raise ValueError("simulation clock cannot move backwards")
        self.current_ts_ns = target
        return target
