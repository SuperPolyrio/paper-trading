"""In-memory overlap-safe catalog for deterministic regime lookup."""

from __future__ import annotations

from datetime import datetime

from .model import VenueRegimeSnapshot


class VenueRegimeCatalog:
    def __init__(self) -> None:
        self._snapshots: dict[str, VenueRegimeSnapshot] = {}

    def freeze(self, snapshot: VenueRegimeSnapshot) -> VenueRegimeSnapshot:
        existing = self._snapshots.get(snapshot.regime_id)
        if existing is not None:
            if existing.source_hash != snapshot.source_hash:
                raise ValueError("regime id is already frozen with different content")
            return existing
        for other in self._snapshots.values():
            if other.venue == snapshot.venue and _overlap(other, snapshot):
                raise ValueError("venue regime validity ranges must not overlap")
        self._snapshots[snapshot.regime_id] = snapshot
        return snapshot

    def effective_at(self, *, venue: str, at: datetime) -> VenueRegimeSnapshot:
        candidates = [
            snapshot
            for snapshot in self._snapshots.values()
            if snapshot.venue == venue
            and snapshot.valid_from <= at
            and (snapshot.valid_to is None or at < snapshot.valid_to)
        ]
        if len(candidates) != 1:
            raise LookupError(
                f"expected exactly one {venue} regime at {at.isoformat()}, found {len(candidates)}"
            )
        return candidates[0]


def _overlap(left: VenueRegimeSnapshot, right: VenueRegimeSnapshot) -> bool:
    left_end = left.valid_to
    right_end = right.valid_to
    return (left_end is None or left_end > right.valid_from) and (
        right_end is None or right_end > left.valid_from
    )
