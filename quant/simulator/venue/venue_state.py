"""Venue mode contract, including restart and post-only recovery windows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .command import CommandType, GatewayCommand


class VenueMode(str, Enum):
    NORMAL = "NORMAL"
    RESTARTING_425 = "RESTARTING_425"
    POST_ONLY = "POST_ONLY"
    CANCEL_ONLY = "CANCEL_ONLY"
    TRADING_HALTED = "TRADING_HALTED"
    STREAM_RECONCILING = "STREAM_RECONCILING"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class VenuePermission:
    accepted: bool
    reason: str


@dataclass
class VenueStateMachine:
    mode: VenueMode = VenueMode.NORMAL
    restart_until_ts_ns: int | None = None
    post_only_until_ts_ns: int | None = None

    def set_mode(self, mode: VenueMode | str) -> None:
        self.mode = mode if isinstance(mode, VenueMode) else VenueMode(str(mode))
        if self.mode is not VenueMode.RESTARTING_425:
            self.restart_until_ts_ns = None
        if self.mode is not VenueMode.POST_ONLY:
            self.post_only_until_ts_ns = None

    def start_restart(
        self,
        *,
        now_ts_ns: int,
        retry_after_ns: int,
        post_only_window_ns: int,
    ) -> None:
        if retry_after_ns < 0 or post_only_window_ns < 0:
            raise ValueError("restart windows must be non-negative")
        self.mode = VenueMode.RESTARTING_425
        self.restart_until_ts_ns = int(now_ts_ns) + int(retry_after_ns)
        self.post_only_until_ts_ns = self.restart_until_ts_ns + int(post_only_window_ns)

    def mode_at(self, now_ts_ns: int) -> VenueMode:
        now = int(now_ts_ns)
        if self.mode is VenueMode.RESTARTING_425 and self.restart_until_ts_ns is not None and now >= self.restart_until_ts_ns:
            self.mode = VenueMode.POST_ONLY if self.post_only_until_ts_ns and now < self.post_only_until_ts_ns else VenueMode.NORMAL
        if self.mode is VenueMode.POST_ONLY and self.post_only_until_ts_ns is not None and now >= self.post_only_until_ts_ns:
            self.mode = VenueMode.NORMAL
        return self.mode

    def permits(self, command: GatewayCommand, *, now_ts_ns: int) -> VenuePermission:
        mode = self.mode_at(now_ts_ns)
        if mode is VenueMode.NORMAL:
            return VenuePermission(True, "normal")
        if mode is VenueMode.RESTARTING_425:
            return VenuePermission(False, "venue_restarting_425")
        if mode is VenueMode.POST_ONLY:
            allowed = command.command_type is CommandType.CANCEL or (
                command.increases_risk and command.post_only
            )
            return VenuePermission(allowed, "post_only" if allowed else "venue_post_only")
        if mode in {VenueMode.CANCEL_ONLY, VenueMode.TRADING_HALTED, VenueMode.STREAM_RECONCILING}:
            allowed = command.command_type is CommandType.CANCEL
            return VenuePermission(allowed, "cancel_allowed" if allowed else f"venue_{mode.value.lower()}")
        return VenuePermission(False, "venue_degraded")
