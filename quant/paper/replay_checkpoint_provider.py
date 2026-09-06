"""Adapt local L2 Parquet replay into auditable paper book checkpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
from typing import Iterable

from quant.core.db import postgres_connection
from quant.orderbook.l2_active_active import (
    EXECUTION_REDUNDANT_FEED_SOURCE,
    HOT_STANDBY_FEED_SOURCE,
    PRIMARY_FEED_SOURCE,
    SECONDARY_FEED_SOURCE,
)
from quant.orderbook.l2_coverage_gate import (
    evaluate_l2_depth_gate,
    evaluate_l2_depth_gate_row,
    evaluate_l2_depth_interval_gate,
)
from quant.orderbook.l2_coverage_manifest import coverage_table_sql
from quant.orderbook.l2_replay import L2ArchiveReplayReader, L2ReplaySnapshot, L2StateCheckpoint

from .taker_execution import ArrivalBookCheckpoint, PaperBookLevel


class ArchivePaperCheckpointProvider:
    def __init__(
        self,
        *,
        archive_dir: Path | str = Path("runtime_outputs/lob_l2_archive_full"),
        coverage_manifest: Path | str = Path("runtime_outputs/lob_l2_active_active_coverage"),
        primary_shard_count: int = 48,
        secondary_shard_count: int = 2,
        hot_standby_shard_count: int = 48,
        redundant_shard_count: int = 1,
        coverage_table: str | None = None,
        shard_count: int | None = None,
    ) -> None:
        self.coverage_table = coverage_table_sql(coverage_table) if coverage_table else None
        if self.coverage_table:
            self.reader = L2ArchiveReplayReader(
                archive_dir,
                shard_count=shard_count,
                coverage_table=self.coverage_table,
            )
        else:
            self.reader = L2ArchiveReplayReader(
                archive_dir,
                active_active_sources=(
                    PRIMARY_FEED_SOURCE,
                    SECONDARY_FEED_SOURCE,
                    HOT_STANDBY_FEED_SOURCE,
                    EXECUTION_REDUNDANT_FEED_SOURCE,
                ),
                active_active_shard_counts=(
                    primary_shard_count,
                    secondary_shard_count,
                    hot_standby_shard_count,
                    redundant_shard_count,
                ),
            )
        self.coverage_manifest = Path(coverage_manifest)

    def checkpoint_at(
        self,
        *,
        asset_id: str,
        timestamp: datetime,
        market_id: str,
        condition_id: str,
        market_state: str,
    ) -> ArrivalBookCheckpoint:
        gate_allowed, gate_manifest = self._point_gate(asset_id=asset_id, timestamp=timestamp)
        replay = self.reader.snapshot_at(
            asset_id=asset_id,
            timestamp=timestamp,
            allow_rest_seed_only=True,
            allow_one_sided=True,
        )
        return _checkpoint_from_replay(
            replay,
            asset_id=str(asset_id),
            market_id=str(market_id),
            condition_id=str(condition_id),
            market_state=market_state,
            target_timestamp=timestamp,
            gate_allowed=gate_allowed,
            gate_manifest=gate_manifest,
        )

    def checkpoint_pair(
        self,
        *,
        asset_id: str,
        decision_timestamp: datetime,
        arrival_timestamp: datetime,
        market_id: str,
        condition_id: str,
        market_state: str,
    ) -> tuple[ArrivalBookCheckpoint, ArrivalBookCheckpoint]:
        """Build decision and arrival books with one baseline replay."""
        if _utc(arrival_timestamp) < _utc(decision_timestamp):
            raise ValueError("arrival timestamp precedes decision timestamp")
        decision_allowed, decision_manifest = self._point_gate(
            asset_id=asset_id,
            timestamp=decision_timestamp,
        )
        arrival_allowed, arrival_manifest = self._interval_gate(
            asset_id=asset_id,
            start=decision_timestamp,
            end=arrival_timestamp,
        )
        state = self.reader.create_checkpoint(asset_id=asset_id, timestamp=decision_timestamp)
        arrival = self.reader.snapshot_from_checkpoint(
            state,
            timestamp=arrival_timestamp,
            allow_one_sided=True,
        )
        decision = _checkpoint_from_state(
            state,
            asset_id=str(asset_id),
            market_id=str(market_id),
            condition_id=str(condition_id),
            market_state=market_state,
            gate_allowed=decision_allowed,
            gate_manifest=decision_manifest,
        )
        return decision, _checkpoint_from_replay(
            arrival,
            asset_id=str(asset_id),
            market_id=str(market_id),
            condition_id=str(condition_id),
            market_state=market_state,
            target_timestamp=arrival_timestamp,
            gate_allowed=arrival_allowed,
            gate_manifest=arrival_manifest,
        )

    def _point_gate(self, *, asset_id: str, timestamp: datetime) -> tuple[bool, dict]:
        if not self.coverage_table:
            decision = evaluate_l2_depth_gate(
                asset_id=asset_id,
                timestamp=timestamp,
                manifest_path=self.coverage_manifest,
            )
            return decision.allowed, decision.manifest
        row = self._coverage_row(asset_id=asset_id, timestamp=timestamp)
        if not row:
            return False, {}
        decision = evaluate_l2_depth_gate_row(
            asset_id=asset_id,
            timestamp=timestamp,
            manifest=row,
        )
        return decision.allowed, decision.manifest

    def _interval_gate(
        self,
        *,
        asset_id: str,
        start: datetime,
        end: datetime,
    ) -> tuple[bool, dict]:
        if not self.coverage_table:
            decision = evaluate_l2_depth_interval_gate(
                asset_id=asset_id,
                start=start,
                end=end,
                manifest_path=self.coverage_manifest,
            )
            return decision.allowed, decision.manifests[-1] if decision.manifests else {}
        cursor = _utc(start).replace(minute=0, second=0, microsecond=0)
        final_hour = _utc(end).replace(minute=0, second=0, microsecond=0)
        latest: dict = {}
        while cursor <= final_hour:
            row = self._coverage_row(asset_id=asset_id, timestamp=cursor)
            if not row:
                return False, latest
            decision = evaluate_l2_depth_gate_row(
                asset_id=asset_id,
                timestamp=cursor,
                manifest=row,
            )
            latest = decision.manifest
            if not decision.allowed:
                return False, latest
            cursor += timedelta(hours=1)
        return True, latest

    def _coverage_row(self, *, asset_id: str, timestamp: datetime) -> dict:
        assert self.coverage_table is not None
        with postgres_connection(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                FROM {self.coverage_table}
                WHERE asset_id=%s
                  AND hour_start=date_trunc('hour', %s::timestamptz)
                LIMIT 1
                """,
                (str(asset_id), _utc(timestamp)),
            )
            row = cur.fetchone()
        return dict(row) if row else {}


def _checkpoint_from_state(
    state: L2StateCheckpoint,
    *,
    asset_id: str,
    market_id: str,
    condition_id: str,
    market_state: str,
    gate_allowed: bool,
    gate_manifest: dict,
) -> ArrivalBookCheckpoint:
    bids = tuple(PaperBookLevel(price, size) for price, size in state.bids)
    asks = tuple(PaperBookLevel(price, size) for price, size in state.asks)
    observed_at = state.latest_received_at or state.timestamp
    return ArrivalBookCheckpoint(
        checkpoint_id=_paper_checkpoint_id(asset_id, observed_at, state.generation, bids, asks),
        asset_id=asset_id,
        market_id=market_id,
        condition_id=condition_id,
        observed_at=observed_at,
        generation=state.generation,
        coverage_grade=_coverage_grade(gate_allowed, gate_manifest),
        bids=bids,
        asks=asks,
        market_state=market_state,
        book_status=_book_status(bids, asks),
        has_gap=not gate_allowed,
        source_manifest_ids=_manifest_ids(state.source_files),
        source_files=state.source_files,
        source_event_start="row:0",
        source_event_end=f"row:{max(0, state.row_count - 1)}",
    )


def _checkpoint_from_replay(
    replay: L2ReplaySnapshot,
    *,
    asset_id: str,
    market_id: str,
    condition_id: str,
    market_state: str,
    target_timestamp: datetime,
    gate_allowed: bool,
    gate_manifest: dict,
) -> ArrivalBookCheckpoint:
    checkpoint = replay.checkpoint
    bids = tuple(PaperBookLevel(level.price, level.size) for level in replay.snapshot.bids)
    asks = tuple(PaperBookLevel(level.price, level.size) for level in replay.snapshot.asks)
    observed_at = checkpoint.latest_received_at or target_timestamp
    return ArrivalBookCheckpoint(
        checkpoint_id=_paper_checkpoint_id(asset_id, observed_at, checkpoint.generation, bids, asks),
        asset_id=asset_id,
        market_id=market_id,
        condition_id=condition_id,
        observed_at=observed_at,
        generation=checkpoint.generation,
        coverage_grade=_coverage_grade(gate_allowed, gate_manifest),
        bids=bids,
        asks=asks,
        market_state=market_state,
        book_status="READY" if checkpoint.book_quality == "READY_TWO_SIDED" else checkpoint.book_quality,
        has_gap=not gate_allowed,
        source_manifest_ids=_manifest_ids(checkpoint.source_files),
        source_files=checkpoint.source_files,
        source_event_start="row:0",
        source_event_end=f"row:{max(0, checkpoint.row_count - 1)}",
    )


def _coverage_grade(allowed: bool, row: dict) -> str:
    if not allowed:
        return "D"
    if bool(row.get("rest_seed_only")) or int(row.get("rest_seed_book_count") or 0) > 0:
        return "B"
    return "A"


def _paper_checkpoint_id(
    asset_id: str,
    observed_at: datetime,
    generation: int,
    bids: Iterable[PaperBookLevel],
    asks: Iterable[PaperBookLevel],
) -> str:
    """Identify an exchange book generation, independent of replay query time."""
    digest = hashlib.sha256()
    digest.update(str(asset_id).encode("ascii"))
    digest.update(b"|")
    digest.update(_utc(observed_at).isoformat().encode("ascii"))
    digest.update(b"|")
    digest.update(str(int(generation)).encode("ascii"))
    for side, levels in ((b"B", bids), (b"A", asks)):
        digest.update(b"|")
        digest.update(side)
        for level in levels:
            digest.update(format(Decimal(level.price), "f").encode("ascii"))
            digest.update(b":")
            digest.update(format(Decimal(level.size), "f").encode("ascii"))
            digest.update(b";")
    return digest.hexdigest()[:32]


def _book_status(bids: tuple[PaperBookLevel, ...], asks: tuple[PaperBookLevel, ...]) -> str:
    if bids and asks:
        return "READY"
    if bids or asks:
        return "ONE_SIDED"
    return "EMPTY"


def _utc(value: datetime) -> datetime:
    observed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc)


def _manifest_ids(paths: tuple[str, ...]) -> tuple[int, ...]:
    if not paths:
        return ()
    with postgres_connection(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT manifest_id
            FROM quant.clob_l2_archive_manifest
            WHERE path = ANY(%s)
            ORDER BY manifest_id
            """,
            (list(paths),),
        )
        return tuple(int(row["manifest_id"]) for row in cur.fetchall())
