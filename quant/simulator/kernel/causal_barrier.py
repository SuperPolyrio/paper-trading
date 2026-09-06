"""Prevent handler-emitted events from becoming visible before their cause."""

from __future__ import annotations

from .event import SimEvent


class CausalBarrier:
    """Enforce strict ordering of descendants relative to a processed event."""

    @staticmethod
    def validate(parent: SimEvent, child: SimEvent) -> None:
        if child.sort_key <= parent.sort_key:
            raise ValueError(
                "handler emitted an event at or before its causal parent: "
                f"parent={parent.event_id} child={child.event_id}"
            )
