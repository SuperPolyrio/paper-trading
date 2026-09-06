"""Coverage gate for using local L2 archive in depth-fill backtests."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import duckdb


@dataclass(frozen=True)
class L2DepthGateDecision:
    asset_id: str
    hour_start: datetime
    allowed: bool
    reason: str
    manifest: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["hour_start"] = self.hour_start.isoformat()
        return payload


@dataclass(frozen=True)
class L2DepthIntervalGateDecision:
    asset_id: str
    start: datetime
    end: datetime
    allowed: bool
    reason: str
    manifests: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start"] = self.start.isoformat()
        payload["end"] = self.end.isoformat()
        payload["manifests"] = list(self.manifests)
        return payload


def evaluate_l2_depth_gate(
    *,
    asset_id: str,
    timestamp: datetime,
    manifest_path: Path | str,
) -> L2DepthGateDecision:
    hour_start = _hour_start(timestamp)
    path = Path(manifest_path)
    path = _manifest_file_for_hour(path, hour_start)
    if path is None:
        return L2DepthGateDecision(str(asset_id), hour_start, False, "manifest_missing", {})
    con = duckdb.connect(":memory:")
    try:
        row = con.execute(
            """
            SELECT *
            FROM read_parquet(?)
            WHERE asset_id = ?
              AND hour_start = ?
            LIMIT 1
            """,
            [str(path), str(asset_id), hour_start],
        ).fetchdf()
    finally:
        con.close()
    if row.empty:
        return L2DepthGateDecision(str(asset_id), hour_start, False, "token_hour_missing", {})
    manifest = dict(row.iloc[0].to_dict())
    if _has_dual_gap(manifest):
        allowed, reason = _active_active_interval_decision(
            manifest_path=path,
            asset_id=str(asset_id),
            timestamp=timestamp,
            manifest=manifest,
        )
        return L2DepthGateDecision(str(asset_id), hour_start, allowed, reason, manifest)
    allowed, reason = _decision_from_manifest(manifest)
    return L2DepthGateDecision(str(asset_id), hour_start, allowed, reason, manifest)


def evaluate_l2_depth_interval_gate(
    *,
    asset_id: str,
    start: datetime,
    end: datetime,
    manifest_path: Path | str,
) -> L2DepthIntervalGateDecision:
    """Allow an order interval only when every covered instant is replayable.

    Point-in-time taker checks should keep using ``evaluate_l2_depth_gate``.
    This interval form is for latency windows and resting orders, where a gap
    between two otherwise valid endpoints makes the fill outcome unknowable.
    """

    start_utc = _utc(start)
    end_utc = _utc(end)
    if end_utc < start_utc:
        raise ValueError("L2 gate interval end precedes start")
    if end_utc == start_utc:
        point = evaluate_l2_depth_gate(
            asset_id=asset_id,
            timestamp=start_utc,
            manifest_path=manifest_path,
        )
        return L2DepthIntervalGateDecision(
            str(asset_id), start_utc, end_utc, point.allowed, point.reason, (point.manifest,)
        )

    root = Path(manifest_path)
    manifests: list[dict[str, Any]] = []
    cursor = _hour_start(start_utc)
    final_hour = _hour_start(end_utc)
    while cursor <= final_hour:
        path = _manifest_file_for_hour(root, cursor)
        if path is None:
            return L2DepthIntervalGateDecision(
                str(asset_id), start_utc, end_utc, False, "manifest_missing", tuple(manifests)
            )
        manifest = _read_manifest_row(path, asset_id=str(asset_id), hour_start=cursor)
        if not manifest:
            return L2DepthIntervalGateDecision(
                str(asset_id), start_utc, end_utc, False, "token_hour_missing", tuple(manifests)
            )
        manifests.append(manifest)
        if _has_dual_gap(manifest):
            if not bool(manifest.get("fill_depth_ready_outside_gap")):
                reason = str(manifest.get("fill_depth_reason_outside_gap") or "coverage_not_ready")
                return L2DepthIntervalGateDecision(
                    str(asset_id), start_utc, end_utc, False, reason, tuple(manifests)
                )
            gap_path = path.with_name("active_active_gaps.parquet")
            if not gap_path.is_file():
                return L2DepthIntervalGateDecision(
                    str(asset_id), start_utc, end_utc, False, "dual_gap_intervals_missing", tuple(manifests)
                )
            if _gap_overlaps_range(gap_path, asset_id=str(asset_id), start=start_utc, end=end_utc):
                return L2DepthIntervalGateDecision(
                    str(asset_id), start_utc, end_utc, False, "indeterminate_dual_feed_gap", tuple(manifests)
                )
        else:
            allowed, reason = _decision_from_manifest(manifest)
            if not allowed:
                return L2DepthIntervalGateDecision(
                    str(asset_id), start_utc, end_utc, False, reason, tuple(manifests)
                )
        cursor += timedelta(hours=1)
    return L2DepthIntervalGateDecision(
        str(asset_id), start_utc, end_utc, True, "ready_interval", tuple(manifests)
    )


def evaluate_l2_depth_gate_row(*, asset_id: str, timestamp: datetime, manifest: Mapping[str, Any]) -> L2DepthGateDecision:
    hour_start = _hour_start(timestamp)
    payload = dict(manifest)
    allowed, reason = _decision_from_manifest(payload)
    return L2DepthGateDecision(str(asset_id), hour_start, allowed, reason, payload)


def _decision_from_manifest(row: Mapping[str, Any]) -> tuple[bool, str]:
    if "fill_depth_ready" in row and not bool(row.get("fill_depth_ready")):
        return False, str(row.get("fill_depth_reason") or "coverage_not_ready")
    if not bool(row.get("has_book")):
        return False, "no_book_baseline"
    if "has_two_sided_book" in row and not bool(row.get("has_two_sided_book")):
        return False, "one_sided_book"
    rest_seed_only = bool(row.get("rest_seed_only"))
    if rest_seed_only and "has_price_change_after_baseline" in row:
        if not bool(row.get("has_price_change_after_baseline")):
            return False, "rest_seed_without_ws_delta"
    elif rest_seed_only and not bool(row.get("has_price_change")):
        return False, "rest_seed_without_ws_delta"
    if int(row.get("ws_gap_unseeded_count") or 0) > 0:
        return False, "ws_gap_unseeded"
    if int(row.get("rest_error_count") or 0) > 0:
        return False, "rest_seed_error"
    return True, "ready"


def _active_active_interval_decision(
    *,
    manifest_path: Path,
    asset_id: str,
    timestamp: datetime,
    manifest: Mapping[str, Any],
) -> tuple[bool, str]:
    if not bool(manifest.get("fill_depth_ready_outside_gap")):
        return False, str(manifest.get("fill_depth_reason_outside_gap") or "coverage_not_ready")
    gaps_path = manifest_path.with_name("active_active_gaps.parquet")
    if not gaps_path.is_file():
        return False, "dual_gap_intervals_missing"
    observed_at = timestamp.astimezone(timezone.utc) if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    con = duckdb.connect(":memory:")
    try:
        overlap = con.execute(
            """
            SELECT count(*)
            FROM read_parquet(?)
            WHERE asset_id = ? AND gap_start <= ? AND recovered_at > ?
            """,
            [str(gaps_path), asset_id, observed_at, observed_at],
        ).fetchone()[0]
    finally:
        con.close()
    if int(overlap or 0) > 0:
        return False, "dual_feed_gap_overlap"
    return True, "ready_outside_dual_gap"


def _read_manifest_row(path: Path, *, asset_id: str, hour_start: datetime) -> dict[str, Any]:
    con = duckdb.connect(":memory:")
    try:
        row = con.execute(
            "SELECT * FROM read_parquet(?) WHERE asset_id = ? AND hour_start = ? LIMIT 1",
            [str(path), asset_id, hour_start],
        ).fetchdf()
    finally:
        con.close()
    return {} if row.empty else dict(row.iloc[0].to_dict())


def _gap_overlaps_range(path: Path, *, asset_id: str, start: datetime, end: datetime) -> bool:
    con = duckdb.connect(":memory:")
    try:
        count = con.execute(
            """
            SELECT count(*) FROM read_parquet(?)
            WHERE asset_id = ? AND gap_start < ? AND recovered_at > ?
            """,
            [str(path), asset_id, end, start],
        ).fetchone()[0]
    finally:
        con.close()
    return int(count or 0) > 0


def _has_dual_gap(manifest: Mapping[str, Any]) -> bool:
    try:
        return int(manifest.get("dual_gap_overlap_count") or 0) > 0
    except (TypeError, ValueError):
        return str(manifest.get("fill_depth_reason") or "") == "dual_feed_gap_overlap"


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _hour_start(value: datetime) -> datetime:
    ts = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return ts.replace(minute=0, second=0, microsecond=0)


def _manifest_file_for_hour(path: Path, hour_start: datetime) -> Path | None:
    if path.is_file():
        return path
    if not path.is_dir():
        return None
    partition = path / f"dt={hour_start:%Y-%m-%d}" / f"hour={hour_start:%H}"
    for name in ("active_active_coverage.parquet", "coverage.parquet"):
        candidate = partition / name
        if candidate.is_file():
            return candidate
    candidates = sorted(partition.glob("*.parquet"))
    return candidates[0] if len(candidates) == 1 else None
