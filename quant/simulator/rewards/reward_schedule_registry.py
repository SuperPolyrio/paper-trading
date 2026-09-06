"""Effective-dated reward schedule registry."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .models import RewardSchedule, RewardType


class RewardScheduleRegistry:
    def __init__(self, schedules: Iterable[RewardSchedule] = ()) -> None:
        self._by_id: dict[str, RewardSchedule] = {}
        self._by_scope: dict[tuple[RewardType, str], list[RewardSchedule]] = {}
        for schedule in schedules:
            self.register(schedule)

    def register(self, schedule: RewardSchedule) -> RewardSchedule:
        existing = self._by_id.get(schedule.schedule_id)
        if existing is not None:
            if existing != schedule:
                raise ValueError(
                    f"reward schedule id collision: {schedule.schedule_id}"
                )
            return existing
        key = (schedule.reward_type, schedule.scope_key)
        history = self._by_scope.setdefault(key, [])
        for item in history:
            if _overlaps(item, schedule):
                raise ValueError(
                    "overlapping reward schedules for "
                    f"{schedule.reward_type.value}:{schedule.scope_key}"
                )
        history.append(schedule)
        history.sort(key=lambda item: (item.effective_from, item.schedule_id))
        self._by_id[schedule.schedule_id] = schedule
        return schedule

    def resolve(
        self,
        reward_type: RewardType | str,
        scope_key: str,
        *,
        at: datetime,
    ) -> RewardSchedule:
        key = (RewardType(str(getattr(reward_type, "value", reward_type))), scope_key)
        matches = [item for item in self._by_scope.get(key, ()) if item.applies_at(at)]
        if len(matches) != 1:
            raise LookupError(
                f"expected one reward schedule for {key[0].value}:{scope_key} "
                f"at {at.isoformat()}, found {len(matches)}"
            )
        return matches[0]

    def history(
        self, reward_type: RewardType | str, scope_key: str
    ) -> tuple[RewardSchedule, ...]:
        normalized = RewardType(str(getattr(reward_type, "value", reward_type)))
        return tuple(self._by_scope.get((normalized, scope_key), ()))


def _overlaps(left: RewardSchedule, right: RewardSchedule) -> bool:
    left_end = left.effective_until or datetime.max.replace(
        tzinfo=left.effective_from.tzinfo
    )
    right_end = right.effective_until or datetime.max.replace(
        tzinfo=right.effective_from.tzinfo
    )
    return left.effective_from < right_end and right.effective_from < left_end
