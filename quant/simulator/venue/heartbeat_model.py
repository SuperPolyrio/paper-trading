"""Account heartbeat tracking for fail-closed paper dead-man handling."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HeartbeatConfig:
    required: bool = False
    timeout_ns: int = 0

    def __post_init__(self) -> None:
        if self.timeout_ns < 0:
            raise ValueError("heartbeat timeout must be non-negative")
        if self.required and self.timeout_ns <= 0:
            raise ValueError("required heartbeat needs a positive timeout")


@dataclass
class HeartbeatTracker:
    config: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    _started_at: dict[str, int] = field(default_factory=dict, init=False)
    _last_heartbeat_at: dict[str, int] = field(default_factory=dict, init=False)
    _expired: set[str] = field(default_factory=set, init=False)

    def register_account(self, account_id: str, *, now_ts_ns: int) -> None:
        self._started_at.setdefault(str(account_id), int(now_ts_ns))

    def heartbeat(self, account_id: str, *, now_ts_ns: int) -> None:
        account = str(account_id)
        self.register_account(account, now_ts_ns=now_ts_ns)
        self._last_heartbeat_at[account] = int(now_ts_ns)
        self._expired.discard(account)

    def is_expired(self, account_id: str, *, now_ts_ns: int) -> bool:
        if not self.config.required:
            return False
        account = str(account_id)
        started = self._started_at.get(account)
        if started is None:
            return False
        reference = self._last_heartbeat_at.get(account, started)
        return int(now_ts_ns) - reference > self.config.timeout_ns

    def expire_due_accounts(self, account_ids: set[str], *, now_ts_ns: int) -> tuple[str, ...]:
        due = tuple(sorted(account for account in account_ids if self.is_expired(account, now_ts_ns=now_ts_ns)))
        self._expired.update(due)
        return due

    def accepts_new_risk(self, account_id: str, *, now_ts_ns: int) -> bool:
        return not self.is_expired(account_id, now_ts_ns=now_ts_ns)
