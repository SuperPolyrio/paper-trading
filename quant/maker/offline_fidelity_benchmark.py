"""Evidence-safe offline execution fidelity benchmark over the XUE L2 archive.

The benchmark never submits an order. Public L2 and on-chain ``OrderFilled``
events can establish a conservative counterfactual fill lower bound, but cannot
establish an authenticated user-order NO_FILL. Reports preserve that boundary.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import duckdb

from quant.core.db import postgres_connection
from quant.execution.models.maker_probability_calibration import (
    LOW_PROBABILITY_CLASSIFICATION,
    MakerProbabilityCalibrationArtifact,
)
from quant.execution.models.maker_queue import (
    MakerFillPrediction,
    MakerQueueEngine,
    MakerQueueState,
    QueueModel,
    poisson_arrival_probability,
)
from quant.maker.offline_shadow import (
    CounterfactualMakerOrder,
    MakerEvidenceWindow,
    ShadowEvaluationRow,
    evaluate_shadow_predictions,
    label_counterfactual,
)
from quant.orderbook.subscriptions import token_shard
from quant.paper.paired_probe import OrderFilledEvidenceClient
from quant.paper.taker_execution import (
    ArrivalBookCheckpoint,
    OrderIntent,
    PaperBookLevel,
    PaperLatencyModel,
    TakerExecutionConfig,
    TakerOnlyPaperExecutionEngine,
)

UTC = timezone.utc
DEFAULT_ARCHIVE = Path("/data/jiahuaiyu/prediction-market-quant/lob_l2_archive_xue")
DEFAULT_OUTPUT = Path("runtime_outputs/offline_execution_fidelity/latest")
DEFAULT_CACHE = Path(".tmp/offline_fidelity_xue_cache")
DEFAULT_XUE_HOST = "hy@10.7.7.223"
DEFAULT_XUE_REMOTE_ARCHIVE = Path(
    "/mnt/hdd22t/prediction-market-quant/lob_l2_archive_full"
)
DEFAULT_XUE_IDENTITY = Path(
    "/home/jiahuaiyu/.ssh/prediction_market_quant_xue_archive_ed25519"
)
GCP_COVERAGE = "quant.clob_l2_token_hour_coverage_gcp_batch"
ACTIVE_ACTIVE_COVERAGE = "quant.clob_l2_active_active_token_hour_coverage"
TARGET_CATEGORIES = ("politics", "sports", "weather", "crypto")
CATEGORY_VALUES = {
    "politics": ("politics", "trump", "elections"),
    "sports": (
        "sports",
        "tennis",
        "mlb",
        "baseball",
        "nba",
        "nfl",
        "soccer",
        "cricket",
        "esports",
    ),
    "weather": ("weather",),
    "crypto": (
        "crypto",
        "crypto-prices",
        "bitcoin",
        "ethereum",
        "solana",
        "up-or-down",
        "5m",
        "15m",
        "1h",
        "4h",
    ),
}


class RoutedArchiveEmptyEvidence(RuntimeError):
    """A SHA-bound routed-source scan completed without rows for the token."""


ACTIVITY_MULTIPLIERS = (
    Decimal("0.1"),
    Decimal("0.25"),
    Decimal("0.5"),
    Decimal(1),
    Decimal(2),
    Decimal(4),
)
EMPIRICAL_BAYES_PRIOR_EXPOSURES_SECONDS = (
    Decimal(60),
    Decimal(120),
    Decimal(300),
    Decimal(600),
)
EMPIRICAL_BAYES_RAW_PROBABILITY_WEIGHTS = (
    Decimal("0.25"),
    Decimal("0.5"),
    Decimal("0.75"),
    Decimal(1),
)
EMPIRICAL_BAYES_ODDS_MULTIPLIERS = (
    Decimal("0.75"),
    Decimal(1),
    Decimal("1.25"),
    Decimal("1.5"),
    Decimal(2),
)
EMPIRICAL_BAYES_HIERARCHY_EXPOSURE_SECONDS = Decimal(1200)
EMPIRICAL_BAYES_FILL_PRIOR_STRENGTH = Decimal(10)
MIN_EMPIRICAL_BAYES_ACTIVITY_TRIALS = 20
MIN_RESEARCH_PROXY_ROWS = 60
MIN_RESEARCH_OBSERVED_FILL_TRIALS = 5
MIN_RESEARCH_PREDICTED_POSITIVES = 20
MAX_RESEARCH_PROXY_BRIER = Decimal("0.20")
MAX_RESEARCH_PROXY_ECE = Decimal("0.10")
MAX_RESEARCH_FALSE_POSITIVE_UPPER_95 = Decimal("0.25")
DEFAULT_MAKER_QUOTE_POSITIONS = (
    "AT_BEST",
    "ONE_TICK_BEHIND",
    "ONE_TICK_INSIDE_SPREAD",
)
DEFAULT_MAKER_HORIZONS_SECONDS = (30, 120, 300, 900)


@dataclass(frozen=True)
class BenchmarkCandidate:
    asset_id: str
    market_id: str
    condition_id: str
    event_id: str
    title: str
    outcome: str
    category: str
    hour_start: datetime
    archive_shard_ids: tuple[int, ...]
    connection_shard_id: int | None
    tick_size: Decimal
    min_order_size: Decimal
    coverage_hash: str
    prior_trade_count: int
    prior_trade_volume: Decimal


@dataclass(frozen=True)
class ReplaySample:
    candidate: BenchmarkCandidate
    split: str
    order: CounterfactualMakerOrder
    evidence: MakerEvidenceWindow
    forecast_trade_volume: Decimal
    taker_case: Mapping[str, Any]
    taker_result: Mapping[str, Any]
    source_files: tuple[str, ...]
    source_hashes: Mapping[str, str]
    forecast_trade_count: int = 0
    forecast_window_seconds: int = 300


@dataclass(frozen=True)
class EmpiricalBayesMakerPrior:
    """Train-only activity and fill priors used by the research model."""

    schema_version: str
    source_split: str
    source_trial_count: int
    source_event_count: int
    global_arrival_rate_per_second: Decimal
    global_mean_trade_size: Decimal
    activity_buckets: Mapping[str, Mapping[str, Any]]
    horizon_fill_probabilities: Mapping[int, Decimal]
    horizon_fill_evidence: Mapping[int, Mapping[str, Any]]
    artifact_hash: str


@dataclass(frozen=True)
class EmpiricalBayesMakerConfig:
    """Calibration-only hyperparameters frozen before holdout evaluation."""

    schema_version: str
    activity_multiplier: Decimal
    prior_exposure_seconds: Decimal
    raw_probability_weight: Decimal
    probability_odds_multiplier: Decimal
    prior_artifact_hash: str
    decision_hash: str


@dataclass(frozen=True)
class BookState:
    bids: Mapping[Decimal, Decimal]
    asks: Mapping[Decimal, Decimal]
    observed_at: datetime
    generation: int


class HistoricalUniverseAdapter:
    """Select PIT token-hours that have complete, usable archive evidence."""

    def select(
        self,
        *,
        start: datetime,
        end: datetime,
        max_days: int,
        per_category_per_day: int,
    ) -> list[BenchmarkCandidate]:
        orderfilled = OrderFilledEvidenceClient()
        with postgres_connection(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT (hour_start AT TIME ZONE 'UTC')::date AS utc_day,
                       max(hour_start) AS hour_start
                FROM {ACTIVE_ACTIVE_COVERAGE}
                WHERE hour_start >= %s AND hour_start < %s
                  AND fill_depth_ready_outside_gap=TRUE
                  AND COALESCE(dual_gap_overlap_count, 0)=0
                GROUP BY 1
                ORDER BY 1 DESC
                LIMIT %s
                """,
                (_utc(start), _utc(end), max(3, int(max_days))),
            )
            hours = sorted(row["hour_start"] for row in cur.fetchall())
            if not hours:
                return []
            selected: list[BenchmarkCandidate] = []
            selected_events: set[str] = set()
            for hour in hours:
                try:
                    prior_activity = orderfilled.fetch_active_assets(
                        start=hour - timedelta(hours=1),
                        end=hour,
                    )
                except Exception:  # noqa: BLE001 - selection falls back deterministically
                    prior_activity = {}
                cur.execute(
                    f"""
                    SELECT g.asset_id, g.hour_start, g.condition_id,
                           g.connection_shard_id, g.archive_shard_ids,
                           g.archive_row_count, g.baseline_received_at,
                           g.fill_depth_reason, r.market_id, r.market_title,
                           r.outcome_name, r.current_tick_size, r.min_order_size
                    FROM {GCP_COVERAGE} g
                    JOIN {ACTIVE_ACTIVE_COVERAGE} aa
                      ON aa.asset_id=g.asset_id AND aa.hour_start=g.hour_start
                    JOIN quant.paper_market_registry_tokens r
                      ON r.asset_id=g.asset_id
                    WHERE g.hour_start=%s
                      AND g.fill_depth_ready=TRUE
                      AND g.has_ws_book=TRUE
                      AND g.ws_book_count > 0
                      AND g.baseline_received_at >= g.hour_start
                      AND g.baseline_received_at < g.hour_start + interval '55 minutes'
                      AND g.has_two_sided_book=TRUE
                      AND g.has_price_change_after_baseline=TRUE
                      AND COALESCE(g.ws_gap_unseeded_count, 0)=0
                      AND COALESCE(g.rest_error_count, 0)=0
                      AND g.archive_row_count > 0
                      AND r.market_id IS NOT NULL
                      AND aa.fill_depth_ready_outside_gap=TRUE
                      AND COALESCE(aa.dual_gap_overlap_count, 0)=0
                    LIMIT 20000
                    """,
                    (hour,),
                )
                pool = [dict(row) for row in cur.fetchall()]
                market_ids = sorted({int(row["market_id"]) for row in pool})
                if not market_ids:
                    continue
                cur.execute(
                    """
                    SELECT DISTINCT ON (member.market_id)
                           member.market_id, m.event_id, m.event_slug,
                           m.event_category
                    FROM quant.market_event_members member
                    JOIN quant.market_event_metadata m
                      ON m.event_slug=member.event_slug
                    WHERE member.market_id=ANY(%s::bigint[])
                    ORDER BY member.market_id, m.updated_at DESC, m.event_slug
                    """,
                    (market_ids,),
                )
                metadata = {int(row["market_id"]): dict(row) for row in cur.fetchall()}
                buckets: dict[str, list[BenchmarkCandidate]] = defaultdict(list)
                for row in pool:
                    meta = metadata.get(int(row["market_id"]))
                    category = _normalize_category(
                        meta.get("event_category") if meta else None
                    )
                    if category is None:
                        continue
                    buckets[category].append(
                        _candidate_from_pool(
                            row,
                            meta or {},
                            category,
                            prior_activity.get(str(row["asset_id"]), {}),
                        )
                    )
                for category in TARGET_CATEGORIES:
                    ordered = sorted(
                        buckets[category],
                        key=lambda item: (
                            0 if item.prior_trade_count > 0 else 1,
                            -item.prior_trade_count,
                            -item.prior_trade_volume,
                            hashlib.sha256(
                                f"{item.asset_id}|{item.hour_start.isoformat()}".encode()
                            ).hexdigest(),
                        ),
                    )
                    chosen: list[BenchmarkCandidate] = []
                    for item in ordered:
                        if item.event_id in selected_events:
                            continue
                        chosen.append(item)
                        selected_events.add(item.event_id)
                        if len(chosen) >= max(1, int(per_category_per_day)):
                            break
                    selected.extend(chosen)
            return sorted(
                selected,
                key=lambda item: (
                    item.hour_start,
                    item.category,
                    item.event_id,
                    item.asset_id,
                ),
            )


def _parse_sshfs_source(source: str) -> tuple[str | None, Path | None]:
    source = source.removeprefix("sshfs#")
    host, separator, remote_path = source.partition(":/")
    if not separator or not host or not remote_path:
        return None, None
    return host, Path(f"/{remote_path}")


def _mounted_sshfs_remote(root: Path) -> tuple[str | None, Path | None]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, None
    payload = json.loads(result.stdout or "{}")
    filesystems = payload.get("filesystems") or []
    source = str(filesystems[0].get("source") or "") if filesystems else ""
    return _parse_sshfs_source(source)


class XueL2ArchiveAdapter:
    """Read only the token's routed shard files from the mounted WD archive."""

    def __init__(
        self,
        root: Path | str,
        *,
        cache_root: Path | str,
        remote_host: str | None = None,
        remote_root: Path | str | None = None,
        identity_file: Path | str = DEFAULT_XUE_IDENTITY,
    ) -> None:
        self.root = Path(root).resolve()
        self.cache_root = Path(cache_root).resolve()
        mounted_host, mounted_root = _mounted_sshfs_remote(self.root)
        self.remote_host = remote_host or mounted_host or DEFAULT_XUE_HOST
        self.remote_root = Path(
            remote_root or mounted_root or DEFAULT_XUE_REMOTE_ARCHIVE
        )
        self.identity_file = Path(identity_file).expanduser().resolve()
        self._source_hashes: dict[str, str] = {}
        self._batch_files: set[Path] = set()
        self._verified_cache_hashes: dict[Path, tuple[int, int, str]] = {}

    def mount_evidence(self) -> dict[str, Any]:
        result = subprocess.run(
            ["findmnt", "-J", "-T", str(self.root)],
            check=False,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout or "{}") if result.returncode == 0 else {}
        filesystems = payload.get("filesystems") or []
        fs = filesystems[0] if filesystems else {}
        options = str(fs.get("options") or "")
        return {
            "root": str(self.root),
            "exists": self.root.is_dir(),
            "findmnt_ok": result.returncode == 0,
            "source": fs.get("source"),
            "filesystem": fs.get("fstype"),
            "read_only": "ro" in {part.strip() for part in options.split(",")},
            "options": options,
        }

    def read_candidate(
        self, candidate: BenchmarkCandidate
    ) -> tuple[list[dict[str, Any]], tuple[str, ...], dict[str, str]]:
        start = candidate.hour_start
        end = start + timedelta(hours=1)
        try:
            filtered_file = self._cached_filtered(candidate)
        except FileNotFoundError:
            source_files = self._files(start, end, candidate)
            filtered_file = self._extract_filtered(
                source_files, candidate=candidate, start=start, end=end
            )
        con = duckdb.connect(":memory:", config={"threads": "2"})
        try:
            columns = {
                str(row[0])
                for row in con.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?, union_by_name=true)",
                    [str(filtered_file)],
                ).fetchall()
            }
            exchange_column = (
                "timestamp_exchange" if "timestamp_exchange" in columns else "timestamp"
            )
            where = (
                "WHERE CAST(asset_id AS VARCHAR)=?"
                if filtered_file in self._batch_files
                else ""
            )
            frame = con.execute(
                f"""
                SELECT timestamp_received,
                       {exchange_column} AS timestamp_exchange,
                       event_type, bids, asks, price, size,
                       side, best_bid, best_ask, payload_hash, book_hash, source,
                       collector_seq, sequence_in_message, change_index,
                       raw_connection_generation, raw_frame_seq,
                       source_file AS filename
                FROM read_parquet(?, union_by_name=true)
                {where}
                ORDER BY timestamp_received, collector_seq, sequence_in_message,
                         change_index, raw_frame_seq
                """,
                [str(filtered_file), candidate.asset_id]
                if where
                else [str(filtered_file)],
            ).fetchdf()
        finally:
            con.close()
        rows = []
        for _, row in frame.iterrows():
            event = _event(dict(row))
            remote_source = Path(event["filename"])
            try:
                relative = remote_source.relative_to(self.remote_root)
            except ValueError:
                pass
            else:
                event["filename"] = str(self.root / relative)
            rows.append(event)
        rows = _dedupe_events(rows)
        used_files = tuple(sorted({str(row["filename"]) for row in rows}))
        hashes = {path: self._source_hashes[path] for path in used_files}
        return rows, used_files, hashes

    def prepare_candidates(self, candidates: Sequence[BenchmarkCandidate]) -> None:
        """Batch uncached assets by hour so XUE scans each shard file once."""

        missing_by_hour: dict[datetime, list[BenchmarkCandidate]] = defaultdict(list)
        for candidate in candidates:
            try:
                self._cached_filtered(candidate)
            except RoutedArchiveEmptyEvidence:
                continue
            except FileNotFoundError:
                missing_by_hour[candidate.hour_start].append(candidate)
        for hour_start, rows in sorted(missing_by_hour.items()):
            if len(rows) < 2:
                continue
            source_files: set[str] = set()
            available_rows: list[BenchmarkCandidate] = []
            candidate_source_files: dict[str, list[str]] = {}
            try:
                for candidate in rows:
                    try:
                        candidate_files = self._files(
                            hour_start,
                            hour_start + timedelta(hours=1),
                            candidate,
                        )
                    except FileNotFoundError:
                        continue
                    source_files.update(candidate_files)
                    available_rows.append(candidate)
                    candidate_source_files[candidate.asset_id] = candidate_files
                if len(available_rows) < 2:
                    continue
                self._extract_filtered_batch(
                    sorted(source_files),
                    candidates=available_rows,
                    candidate_source_files=candidate_source_files,
                    start=hour_start,
                    end=hour_start + timedelta(hours=1),
                )
            except Exception as exc:  # noqa: BLE001 - per-candidate fallback remains
                print(
                    f"XUE batch extract fallback for {hour_start.isoformat()}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    def _cached_filtered(self, candidate: BenchmarkCandidate) -> Path:
        """Use an immutable SHA-checked extraction when the SSHFS route is stale."""

        filtered_root = self.cache_root / "filtered"
        pattern = f"{candidate.coverage_hash[:16]}-*.parquet.cache.json"
        for metadata_path in sorted(filtered_root.glob(pattern)):
            metadata = _read_json(metadata_path)
            if (
                str(metadata.get("asset_id") or "") != candidate.asset_id
                or str(metadata.get("hour_start") or "")
                != candidate.hour_start.isoformat()
            ):
                continue
            if metadata.get("empty_scan") is True:
                batch_target = Path(str(metadata.get("batch_filtered_path") or ""))
                expected_batch = str(metadata.get("batch_filtered_sha256") or "")
                source_files = metadata.get("source_files")
                actual_batch: str | None = None
                if batch_target.is_file() and batch_target.stat().st_size > 0:
                    stat = batch_target.stat()
                    cached = self._verified_cache_hashes.get(batch_target)
                    if cached is not None and cached[:2] == (
                        stat.st_size,
                        stat.st_mtime_ns,
                    ):
                        actual_batch = cached[2]
                    else:
                        actual_batch = _sha256(batch_target)
                        self._verified_cache_hashes[batch_target] = (
                            stat.st_size,
                            stat.st_mtime_ns,
                            actual_batch,
                        )
                if (
                    batch_target.is_file()
                    and batch_target.stat().st_size > 0
                    and len(expected_batch) == 64
                    and actual_batch == expected_batch
                    and isinstance(source_files, list)
                    and source_files
                    and all(
                        isinstance(row, Mapping)
                        and str(row.get("path") or "")
                        and len(str(row.get("sha256") or "")) == 64
                        for row in source_files
                    )
                ):
                    raise RoutedArchiveEmptyEvidence(
                        "no routed XUE parquet for "
                        f"{candidate.asset_id} at {candidate.hour_start.isoformat()}"
                    )
                continue
            target = Path(
                str(
                    metadata.get("filtered_path")
                    or str(metadata_path).removesuffix(".cache.json")
                )
            )
            expected = str(metadata.get("filtered_sha256") or "")
            actual: str | None = None
            if target.is_file() and target.stat().st_size > 0:
                stat = target.stat()
                cached = self._verified_cache_hashes.get(target)
                if cached is not None and cached[:2] == (
                    stat.st_size,
                    stat.st_mtime_ns,
                ):
                    actual = cached[2]
                else:
                    actual = _sha256(target)
                    self._verified_cache_hashes[target] = (
                        stat.st_size,
                        stat.st_mtime_ns,
                        actual,
                    )
            if (
                not target.is_file()
                or target.stat().st_size <= 0
                or len(expected) != 64
                or actual != expected
            ):
                continue
            source_files = metadata.get("source_files")
            if not isinstance(source_files, list) or not source_files:
                continue
            valid_sources = True
            for source_row in source_files:
                if not isinstance(source_row, Mapping):
                    valid_sources = False
                    break
                relative = str(source_row.get("path") or "")
                digest = str(source_row.get("sha256") or "")
                if not relative or len(digest) != 64:
                    valid_sources = False
                    break
                self._source_hashes[str(self.root / relative)] = digest
            if valid_sources:
                if int(metadata.get("batch_asset_count") or 1) > 1:
                    self._batch_files.add(target)
                return target
        raise FileNotFoundError(
            "no routed XUE parquet or verified filtered cache for "
            f"{candidate.asset_id} at {candidate.hour_start.isoformat()}"
        )

    def _extract_filtered(
        self,
        source_files: Sequence[str],
        *,
        candidate: BenchmarkCandidate,
        start: datetime,
        end: datetime,
    ) -> Path:
        source_manifest, remote_files, evidence_hash = self._source_evidence(
            source_files
        )
        target = (
            self.cache_root
            / "filtered"
            / f"{candidate.coverage_hash[:16]}-{evidence_hash[:16]}-atomic-v2.parquet"
        )
        metadata_path = target.with_suffix(".parquet.cache.json")
        metadata = _read_json(metadata_path)
        if (
            target.is_file()
            and target.stat().st_size > 0
            and metadata.get("source_evidence_hash") == evidence_hash
        ):
            return target
        self._remote_extract(
            remote_files,
            asset_ids=(candidate.asset_id,),
            start=start,
            end=end,
            target=target,
            output_key=f"{candidate.coverage_hash[:16]}-{evidence_hash[:16]}",
        )
        filtered_hash = _sha256(target)
        stat = target.stat()
        self._verified_cache_hashes[target] = (
            stat.st_size,
            stat.st_mtime_ns,
            filtered_hash,
        )
        _write_json(
            metadata_path,
            {
                "asset_id": candidate.asset_id,
                "hour_start": start.isoformat(),
                "source_evidence_hash": evidence_hash,
                "filtered_schema_version": "xue-maker-filtered-v2-atomic-timestamp",
                "source_files": source_manifest,
                "filtered_sha256": filtered_hash,
            },
        )
        return target

    def _extract_filtered_batch(
        self,
        source_files: Sequence[str],
        *,
        candidates: Sequence[BenchmarkCandidate],
        candidate_source_files: Mapping[str, Sequence[str]],
        start: datetime,
        end: datetime,
    ) -> Path:
        source_manifest, remote_files, evidence_hash = self._source_evidence(
            source_files
        )
        asset_ids = tuple(sorted({candidate.asset_id for candidate in candidates}))
        batch_hash = hashlib.sha256(
            json.dumps(
                {
                    "asset_ids": asset_ids,
                    "end": end.isoformat(),
                    "source_evidence_hash": evidence_hash,
                    "start": start.isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        target = (
            self.cache_root
            / "filtered"
            / "batches"
            / f"{start:%Y%m%dT%H}-{batch_hash[:24]}-atomic-v1.parquet"
        )
        if not target.is_file() or target.stat().st_size <= 0:
            self._remote_extract(
                remote_files,
                asset_ids=asset_ids,
                start=start,
                end=end,
                target=target,
                output_key=f"batch-{start:%Y%m%dT%H}-{batch_hash[:16]}",
            )
        partitioned = self._partition_filtered_batch(target, asset_ids)
        batch_filtered_hash = _sha256(target)
        source_manifest_by_path = {
            str(self.remote_root / str(row["path"])): row for row in source_manifest
        }
        for candidate in candidates:
            candidate_target = partitioned.get(candidate.asset_id)
            candidate_manifest = [
                source_manifest_by_path[
                    str(
                        self.remote_root
                        / Path(path).relative_to(self.root)
                    )
                ]
                for path in candidate_source_files.get(candidate.asset_id, ())
                if str(
                    self.remote_root / Path(path).relative_to(self.root)
                )
                in source_manifest_by_path
            ]
            metadata_path = (
                self.cache_root
                / "filtered"
                / (
                    f"{candidate.coverage_hash[:16]}-{batch_hash[:16]}-"
                    "batch-v2.parquet.cache.json"
                )
            )
            if candidate_target is None:
                _write_json(
                    metadata_path,
                    {
                        "asset_id": candidate.asset_id,
                        "batch_asset_count": len(asset_ids),
                        "batch_filtered_path": str(target),
                        "batch_filtered_sha256": batch_filtered_hash,
                        "empty_scan": True,
                        "filtered_schema_version": (
                            "xue-maker-filtered-batch-v2-empty-scan"
                        ),
                        "hour_start": start.isoformat(),
                        "source_evidence_hash": evidence_hash,
                        "source_files": candidate_manifest,
                    },
                )
                continue
            filtered_hash = _sha256(candidate_target)
            stat = candidate_target.stat()
            self._verified_cache_hashes[candidate_target] = (
                stat.st_size,
                stat.st_mtime_ns,
                filtered_hash,
            )
            con = duckdb.connect(":memory:")
            try:
                used_remote_files = {
                    str(row[0])
                    for row in con.execute(
                        "SELECT DISTINCT source_file FROM read_parquet(?)",
                        [str(candidate_target)],
                    ).fetchall()
                }
            finally:
                con.close()
            _write_json(
                metadata_path,
                {
                    "asset_id": candidate.asset_id,
                    "batch_asset_count": 1,
                    "filtered_path": str(candidate_target),
                    "filtered_schema_version": (
                        "xue-maker-filtered-batch-v2-partitioned-atomic-timestamp"
                    ),
                    "filtered_sha256": filtered_hash,
                    "hour_start": start.isoformat(),
                    "source_batch_asset_count": len(asset_ids),
                    "source_evidence_hash": evidence_hash,
                    "source_files": [
                        source_manifest_by_path[path]
                        for path in sorted(used_remote_files)
                        if path in source_manifest_by_path
                    ]
                    or candidate_manifest,
                },
            )
        return target

    def _partition_filtered_batch(
        self, target: Path, asset_ids: Sequence[str]
    ) -> dict[str, Path]:
        partition_root = target.with_suffix(".assets")
        if not partition_root.is_dir():
            with tempfile.TemporaryDirectory(
                dir=target.parent, prefix=f".{target.stem}-"
            ) as temporary:
                temporary_root = Path(temporary) / "assets"
                output_sql = str(temporary_root).replace("'", "''")
                con = duckdb.connect(":memory:", config={"threads": "4"})
                try:
                    con.execute(
                        f"""
                        COPY (
                          SELECT * FROM read_parquet(?)
                        ) TO '{output_sql}' (
                          FORMAT PARQUET,
                          PARTITION_BY (asset_id),
                          COMPRESSION ZSTD
                        )
                        """,
                        [str(target)],
                    )
                finally:
                    con.close()
                os.replace(temporary_root, partition_root)
        result: dict[str, Path] = {}
        for asset_id in asset_ids:
            files = sorted((partition_root / f"asset_id={asset_id}").glob("*.parquet"))
            if len(files) == 1:
                result[asset_id] = files[0]
        return result

    def _source_evidence(
        self, source_files: Sequence[str]
    ) -> tuple[list[dict[str, str]], list[str], str]:
        source_manifest: list[dict[str, str]] = []
        remote_files: list[str] = []
        for source_text in source_files:
            source = Path(source_text)
            relative = source.relative_to(self.root)
            sidecar_payload = _read_json(Path(str(source) + ".manifest.json"))
            digest = str(sidecar_payload.get("sha256") or "")
            if len(digest) != 64:
                raise ValueError(f"missing source SHA256 sidecar for {source}")
            self._source_hashes[str(source)] = digest
            source_manifest.append({"path": relative.as_posix(), "sha256": digest})
            remote_files.append(str(self.remote_root / relative))
        evidence_hash = hashlib.sha256(
            json.dumps(source_manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return source_manifest, remote_files, evidence_hash

    def _remote_extract(
        self,
        remote_files: Sequence[str],
        *,
        asset_ids: Sequence[str],
        start: datetime,
        end: datetime,
        target: Path,
        output_key: str,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        remote_output = (
            f"/tmp/prediction_market_quant_offline_extract/{output_key}.parquet"
        )
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "files": remote_files,
                    "output": remote_output,
                    "asset_ids": list(asset_ids),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                separators=(",", ":"),
            ).encode()
        ).decode()
        remote_script = """
import base64, json
from pathlib import Path
import duckdb, sys
p=json.loads(base64.urlsafe_b64decode(sys.stdin.read()).decode())
out=Path(p['output']); out.parent.mkdir(parents=True, exist_ok=True)
escaped='['+','.join("'"+x.replace("'", "''")+"'" for x in p['files'])+']'
assets='['+','.join("'"+x.replace("'", "''")+"'" for x in p['asset_ids'])+']'
out_sql=str(out).replace("'", "''")
sql=f'''COPY (
SELECT CAST(asset_id AS VARCHAR) AS asset_id,
       timestamp_received,timestamp AS timestamp_exchange,
       event_type,bids,asks,price,size,side,best_bid,best_ask,
       payload_hash,book_hash,source,collector_seq,sequence_in_message,change_index,
       raw_connection_generation,raw_frame_seq,filename AS source_file
FROM read_parquet({escaped},filename=true,union_by_name=true)
WHERE CAST(asset_id AS VARCHAR) IN (SELECT unnest({assets}))
  AND timestamp_received>=? AND timestamp_received<?
) TO '{out_sql}' (FORMAT PARQUET, COMPRESSION ZSTD)'''
c=duckdb.connect(':memory:', config={'threads':'4'})
c.execute(sql,[p['start'],p['end']]); c.close()
print(json.dumps({'output':str(out),'size':out.stat().st_size}))
""".strip()
        command = "python3 -c " + shlex.quote(remote_script)
        print(
            f"extracting {len(asset_ids)} assets from "
            f"{len(remote_files)} XUE shard files",
            file=sys.stderr,
            flush=True,
        )
        extract = subprocess.run(
            [
                "ssh",
                "-i",
                str(self.identity_file),
                "-o",
                "BatchMode=yes",
                self.remote_host,
                command,
            ],
            check=False,
            capture_output=True,
            input=payload,
            text=True,
            timeout=300,
        )
        if extract.returncode != 0:
            raise RuntimeError(f"XUE remote extract failed: {extract.stderr[-1000:]}")
        temporary = target.with_suffix(".parquet.part")
        transfer = subprocess.run(
            [
                "rsync",
                "-a",
                "--no-perms",
                "--no-owner",
                "--no-group",
                "-e",
                f"ssh -i {self.identity_file} -o BatchMode=yes",
                f"{self.remote_host}:{remote_output}",
                str(temporary),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if transfer.returncode != 0:
            raise RuntimeError(
                f"XUE filtered transfer failed: {transfer.stderr[-1000:]}"
            )
        os.replace(temporary, target)

    def _files(
        self,
        start: datetime,
        end: datetime,
        candidate: BenchmarkCandidate,
    ) -> list[str]:
        shards = set(candidate.archive_shard_ids)
        if candidate.connection_shard_id is not None:
            shards.add(candidate.connection_shard_id)
        if not shards:
            shards.add(token_shard(candidate.asset_id, shard_count=48))
        files: list[str] = []
        hour = start.replace(minute=0, second=0, microsecond=0)
        while hour < end:
            directory = self.root / f"dt={hour:%Y-%m-%d}" / f"hour={hour:%H}"
            hour_files: list[str] = []
            shardall_files: list[str] = []
            for path in directory.glob("*.parquet"):
                if path.name.endswith(".tmp.parquet"):
                    continue
                if any(path.stem.endswith(f"_shard{shard}") for shard in shards):
                    hour_files.append(str(path))
                elif path.stem.endswith("_shardall"):
                    shardall_files.append(str(path))
            files.extend(hour_files or shardall_files)
            hour += timedelta(hours=1)
        if not files:
            raise FileNotFoundError(
                f"no routed XUE parquet for {candidate.asset_id} at {start.isoformat()}"
            )
        return sorted(set(files))


def run_benchmark(
    *,
    archive_root: Path,
    cache_root: Path,
    output_root: Path,
    start: datetime,
    end: datetime,
    max_days: int = 9,
    per_category_per_day: int = 1,
    latency_ms: int = 250,
    maker_horizon_seconds: int | None = None,
    maker_horizons_seconds: Sequence[int] = DEFAULT_MAKER_HORIZONS_SECONDS,
    maker_quote_positions: Sequence[str] = DEFAULT_MAKER_QUOTE_POSITIONS,
    max_samples: int = 48,
    nautilus_env: str = "polymonitor-nautilus312",
    reuse_candidate_manifest: bool = True,
    maker_holdout_start: date | None = None,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    archive = XueL2ArchiveAdapter(archive_root, cache_root=cache_root)
    mount = archive.mount_evidence()
    if not mount["exists"] or not mount["findmnt_ok"]:
        raise RuntimeError("XUE L2 archive mount is not available")
    candidate_manifest_path = output_root / "candidate-manifest.json"
    selection_inputs = {
        "start": _utc(start).isoformat(),
        "end": _utc(end).isoformat(),
        "max_days": int(max_days),
        "per_category_per_day": int(per_category_per_day),
        "max_samples": int(max_samples),
    }
    selection_hash = hashlib.sha256(
        json.dumps(selection_inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing_manifest = _read_json(candidate_manifest_path)
    candidate_rows = existing_manifest.get("candidates")
    manifest_reused = bool(
        reuse_candidate_manifest
        and existing_manifest.get("selection_hash") == selection_hash
        and isinstance(candidate_rows, list)
        and candidate_rows
    )
    if manifest_reused:
        candidates = [_candidate(row) for row in candidate_rows]
    else:
        candidates = HistoricalUniverseAdapter().select(
            start=start,
            end=end,
            max_days=max_days,
            per_category_per_day=per_category_per_day,
        )[: max(1, int(max_samples))]
        _write_json(
            candidate_manifest_path,
            {
                "schema_version": "offline_fidelity_candidate_manifest_v1",
                "generated_at": datetime.now(UTC).isoformat(),
                "selection_inputs": selection_inputs,
                "selection_hash": selection_hash,
                "candidates": [asdict(candidate) for candidate in candidates],
            },
        )
    split_map, split_manifest = split_candidates(
        candidates,
        holdout_start=maker_holdout_start,
    )
    archive.prepare_candidates(
        [
            candidate
            for candidate in candidates
            if (candidate.event_id, candidate.hour_start.date()) in split_map
        ]
    )
    orderfilled = OrderFilledEvidenceClient()
    watermark = orderfilled.coverage_watermark()
    chain_rows_by_candidate: dict[
        tuple[str, datetime], list[dict[str, Any]]
    ] = {}
    candidates_by_hour: dict[datetime, list[BenchmarkCandidate]] = defaultdict(list)
    for candidate in candidates:
        if (candidate.event_id, candidate.hour_start.date()) in split_map:
            candidates_by_hour[candidate.hour_start].append(candidate)
    for hour_start, hour_candidates in sorted(candidates_by_hour.items()):
        try:
            rows_by_asset = orderfilled.fetch_windows(
                asset_ids=(candidate.asset_id for candidate in hour_candidates),
                start=hour_start - timedelta(minutes=5),
                end=hour_start + timedelta(hours=1),
                limit=200_000,
            )
        except Exception as exc:  # noqa: BLE001 - safe per-candidate fallback
            print(
                f"OrderFilled batch fallback for {hour_start.isoformat()}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            continue
        for asset_id, chain_rows in rows_by_asset.items():
            chain_rows_by_candidate[(asset_id, hour_start)] = chain_rows
    horizons = _normalize_horizons(
        (maker_horizon_seconds,)
        if maker_horizon_seconds is not None
        else maker_horizons_seconds
    )
    quote_positions = _normalize_quote_positions(maker_quote_positions)
    samples: list[ReplaySample] = []
    failures: list[dict[str, Any]] = []
    scenario_skips: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        split = split_map.get((candidate.event_id, candidate.hour_start.date()))
        if split is None:
            continue
        try:
            print(
                f"replaying candidate {index}/{len(candidates)} "
                f"{candidate.category} {candidate.asset_id}",
                file=sys.stderr,
                flush=True,
            )
            rows, source_files, source_hashes = archive.read_candidate(candidate)
            chain_key = (candidate.asset_id, candidate.hour_start)
            chain_rows = chain_rows_by_candidate.get(chain_key)
            if chain_rows is None:
                chain_rows = orderfilled.fetch_window(
                    asset_id=candidate.asset_id,
                    start=candidate.hour_start - timedelta(minutes=5),
                    end=candidate.hour_start + timedelta(hours=1),
                    limit=10_000,
                )
            exchange_batches = _exchange_timestamp_batches(rows)
            for horizon_seconds in horizons:
                for quote_position in quote_positions:
                    try:
                        samples.append(
                            _build_sample(
                                candidate,
                                split=split,
                                rows=rows,
                                source_files=source_files,
                                source_hashes=source_hashes,
                                orderfilled=orderfilled,
                                watermark=watermark,
                                latency_ms=latency_ms,
                                horizon_seconds=horizon_seconds,
                                quote_position=quote_position,
                                chain_rows=chain_rows,
                                exchange_batches=exchange_batches,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        row = {
                            "asset_id": candidate.asset_id,
                            "event_id": candidate.event_id,
                            "category": candidate.category,
                            "hour_start": candidate.hour_start.isoformat(),
                            "quote_position": quote_position,
                            "horizon_seconds": horizon_seconds,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                        skip_reason = _scenario_skip_reason(exc)
                        if skip_reason is None:
                            failures.append(row)
                        else:
                            scenario_skips.append({**row, "skip_reason": skip_reason})
        except Exception as exc:  # noqa: BLE001 - preserve unavailable evidence
            row = {
                "asset_id": candidate.asset_id,
                "event_id": candidate.event_id,
                "category": candidate.category,
                "hour_start": candidate.hour_start.isoformat(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            skip_reason = _scenario_skip_reason(exc)
            if skip_reason is None:
                failures.append(row)
            else:
                scenario_skips.append({**row, "skip_reason": skip_reason})
    empirical_bayes_prior = _fit_empirical_bayes_maker_prior(samples)
    probability_config, calibration_grid = _calibrate_empirical_bayes_model(
        samples,
        empirical_bayes_prior,
    )
    rows = [
        _shadow_row(sample, empirical_bayes_prior, probability_config)
        for sample in samples
    ]
    holdout_rows = [
        row
        for row, sample in zip(rows, samples, strict=True)
        if sample.split == "holdout"
    ]
    maker_all = evaluate_shadow_predictions(rows)
    maker_holdout = evaluate_shadow_predictions(holdout_rows)
    maker_metrics = _maker_error_metrics(holdout_rows)
    probability_research_gate = _maker_probability_research_gate(maker_holdout)
    walk_forward = _rolling_origin_maker_evaluation(
        samples,
        final_holdout_start=maker_holdout_start,
    )
    stratified_holdout = _stratified_maker_report(
        [
            (sample, row)
            for sample, row in zip(samples, rows, strict=True)
            if sample.split == "holdout"
        ]
    )
    taker = _taker_report(samples)
    corpus_path = output_root / "book-differential-corpus.json"
    corpus = {
        "schema_version": "offline_book_corpus_v1",
        "cases": _unique_taker_cases(samples),
    }
    _write_json(corpus_path, corpus)
    differential = _run_differential(
        corpus_path=corpus_path,
        output_root=output_root,
        nautilus_env=nautilus_env,
    )
    fault_matrix = _fault_injection_matrix()
    account = _accounting_evidence()
    category_counts = {
        category: sum(sample.candidate.category == category for sample in samples)
        for category in TARGET_CATEGORIES
    }
    classifications = {
        "taker": (
            "OFFLINE_CALIBRATED_SMALL_ORDER"
            if taker["status"] == "PASS" and differential["status"] == "PASS"
            else "BLOCKED"
        ),
        "maker_strict": (
            "STRICT_LOWER_BOUND_VALIDATED"
            if int(maker_holdout.get("strict_confirmed_fill_count") or 0) > 0
            else "BLOCKED_NO_HOLDOUT_STRICT_CONFIRMED_FILL"
        ),
        "maker_probability": (str(probability_research_gate["classification"])),
        "maker_probability_walk_forward": str(
            walk_forward["probability_research_gate"]["classification"]
        ),
        "accounting": account["classification"],
    }
    matrix_coverage = _matrix_coverage(
        samples,
        requested_quote_positions=quote_positions,
        requested_horizons=horizons,
    )
    data_gate = (
        all(category_counts.values())
        and bool(samples)
        and not failures
        and matrix_coverage["status"] == "PASS"
    )
    report = {
        "schema_version": "offline_execution_fidelity_benchmark_v3",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": (
            "PASS_OFFLINE"
            if data_gate
            and taker["status"] == "PASS"
            and differential["status"] == "PASS"
            and fault_matrix["status"] == "PASS"
            else "BLOCKED"
        ),
        "evidence_scope": "OFFLINE_COUNTERFACTUAL_NO_LIVE_SUBMISSION",
        "live_submission_performed": False,
        "claims_not_established": [
            "LIVE_MAKER_CALIBRATED",
            "AUTHENTICATED_COUNTERFACTUAL_NO_FILL",
            "LARGE_ORDER_MARKET_IMPACT_FIDELITY",
            "EXACT_POLYMARKET_FIFO_POSITION",
        ],
        "classifications": classifications,
        "archive_mount": mount,
        "window": {"start": _utc(start).isoformat(), "end": _utc(end).isoformat()},
        "orderfilled_watermark": _json_value(watermark),
        "candidate_count": len(candidates),
        "candidate_selection": {
            "method": "previous_hour_orderfilled_activity_then_stable_hash",
            "point_in_time": True,
            "label_window_used_for_selection": False,
            "manifest_path": str(candidate_manifest_path.resolve()),
            "manifest_sha256": _sha256(candidate_manifest_path),
            "manifest_reused": manifest_reused,
            "selection_hash": selection_hash,
        },
        "sample_count": len(samples),
        "failure_count": len(failures),
        "scenario_skip_count": len(scenario_skips),
        "scenario_skip_counts": _count_values(scenario_skips, "skip_reason"),
        "category_counts": category_counts,
        "split_manifest": split_manifest,
        "activity_calibration": {
            "schema_version": "maker_empirical_bayes_calibration_v1",
            "selected_config": _json_value(asdict(probability_config)),
            "train_prior": _json_value(asdict(empirical_bayes_prior)),
            "grid": calibration_grid,
            "fit_splits": ["train"],
            "selection_splits": ["calibration"],
            "holdout_rows_used_for_fit": 0,
            "event_and_date_isolation": split_manifest["status"],
            "truth_scope": "PUBLIC_ORDERFILLED_TAPE_PROXY",
        },
        "taker": taker,
        "maker": {
            "all": maker_all,
            "holdout": maker_holdout,
            "holdout_error_metrics": maker_metrics,
            "probability_research_gate": probability_research_gate,
            "walk_forward": walk_forward,
            "stratified_holdout": stratified_holdout,
            "matrix_coverage": matrix_coverage,
        },
        "differential": differential,
        "fault_injection": fault_matrix,
        "accounting": account,
        "failures": failures,
        "scenario_skips": scenario_skips,
        "samples": [
            _sample_json(
                sample,
                row,
                empirical_bayes_prior,
                probability_config,
            )
            for sample, row in zip(samples, rows, strict=True)
        ],
    }
    artifact_status: dict[str, Any]
    if (
        classifications.get("maker_probability_walk_forward")
        == LOW_PROBABILITY_CLASSIFICATION
    ):
        artifact = MakerProbabilityCalibrationArtifact.from_benchmark_report(report)
        artifact_path = artifact.write(
            output_root / "maker_probability_calibration.json"
        )
        artifact_status = {
            "status": "ENABLED_RESEARCH_ONLY",
            "path": artifact_path.name,
            "sha256": _sha256(artifact_path),
            "artifact_hash": artifact.artifact_hash,
            "calibration_domain": artifact.calibration_domain,
            "probability_domain": "[0,0.5)",
            "authoritative_pnl": False,
        }
    else:
        artifact_status = {
            "status": "DISABLED_NOT_CALIBRATED",
            "path": None,
            "sha256": None,
            "artifact_hash": None,
            "authoritative_pnl": False,
        }
    report["maker_probability_calibration_artifact"] = artifact_status
    _write_json(output_root / "summary.json", report)
    (output_root / "summary.md").write_text(_markdown(report), encoding="utf-8")
    return report


def split_candidates(
    candidates: Sequence[BenchmarkCandidate],
    *,
    holdout_start: date | None = None,
) -> tuple[dict[tuple[str, date], str], dict[str, Any]]:
    dates = sorted({candidate.hour_start.date() for candidate in candidates})
    if len(dates) < 3:
        return {}, {"status": "BLOCKED", "reason": "fewer_than_three_utc_dates"}
    if holdout_start is not None:
        development_dates = [day for day in dates if day < holdout_start]
        final_holdout_dates = [day for day in dates if day >= holdout_start]
        if len(development_dates) < 2 or not final_holdout_dates:
            return {}, {
                "status": "BLOCKED",
                "reason": "frozen_holdout_requires_development_and_holdout_dates",
                "holdout_start": holdout_start.isoformat(),
            }
        train_end = max(1, int(len(development_dates) * 0.6))
        train_end = min(train_end, len(development_dates) - 1)
        date_split = {
            day: (
                "holdout"
                if day >= holdout_start
                else "train"
                if index < train_end
                else "calibration"
            )
            for index, day in enumerate(dates)
        }
        split_rule = "frozen_holdout_start_with_chronological_development_split"
    else:
        train_end = max(1, int(len(dates) * 0.6))
        calibration_end = max(train_end + 1, int(len(dates) * 0.8))
        calibration_end = min(calibration_end, len(dates) - 1)
        date_split = {
            day: (
                "train"
                if index < train_end
                else "calibration"
                if index < calibration_end
                else "holdout"
            )
            for index, day in enumerate(dates)
        }
        split_rule = "chronological_utc_date_split"
    event_splits: dict[str, set[str]] = defaultdict(set)
    for candidate in candidates:
        event_splits[candidate.event_id].add(date_split[candidate.hour_start.date()])
    leaking_events = sorted(
        event for event, values in event_splits.items() if len(values) > 1
    )
    mapping = {
        (candidate.event_id, candidate.hour_start.date()): date_split[
            candidate.hour_start.date()
        ]
        for candidate in candidates
        if candidate.event_id not in leaking_events
    }
    return mapping, {
        "status": "PASS",
        "utc_dates": [day.isoformat() for day in dates],
        "date_assignment": {
            day.isoformat(): split for day, split in date_split.items()
        },
        "event_leakage_count": 0,
        "excluded_cross_boundary_event_count": len(leaking_events),
        "excluded_cross_boundary_events": leaking_events,
        "holdout_start": holdout_start.isoformat() if holdout_start else None,
        "rule": (f"{split_rule}_and_drop_events_crossing_split_boundaries"),
    }


def _build_sample(
    candidate: BenchmarkCandidate,
    *,
    split: str,
    rows: list[dict[str, Any]],
    source_files: tuple[str, ...],
    source_hashes: Mapping[str, str],
    orderfilled: OrderFilledEvidenceClient,
    watermark: Mapping[str, Any],
    latency_ms: int,
    horizon_seconds: int,
    quote_position: str = "AT_BEST",
    chain_rows: Sequence[Mapping[str, Any]] | None = None,
    exchange_batches: Sequence[
        tuple[datetime, tuple[dict[str, Any], ...]]
    ]
    | None = None,
) -> ReplaySample:
    preferred_side = (
        "BUY"
        if int(hashlib.sha256(candidate.asset_id.encode()).hexdigest()[:2], 16) % 2 == 0
        else "SELL"
    )
    baseline, side = _select_baseline(
        rows,
        candidate.hour_start,
        horizon_seconds,
        preferred_side=preferred_side,
        minimum_size=candidate.min_order_size,
        exchange_batches=exchange_batches,
    )
    decision_ts = baseline["timestamp_received"]
    arrival_ts = decision_ts + timedelta(milliseconds=max(0, int(latency_ms)))
    end = arrival_ts + timedelta(seconds=max(1, int(horizon_seconds)))
    decision_book = _replay_book(
        rows, until=decision_ts, exchange_batches=exchange_batches
    )
    arrival_book = _replay_book(
        rows, until=arrival_ts, exchange_batches=exchange_batches
    )
    taker_levels = arrival_book.asks if side == "BUY" else arrival_book.bids
    if not taker_levels:
        raise ValueError("arrival book is one-sided")
    best_price = min(taker_levels) if side == "BUY" else max(taker_levels)
    best_size = taker_levels[best_price]
    size = _small_order_size(best_size, candidate.min_order_size)
    if size <= 0 or size > best_size * Decimal("0.10") + Decimal("0.000001"):
        raise ValueError("small-order domain cannot be satisfied")
    decision_checkpoint = _checkpoint(
        candidate, decision_book, decision_ts, source_files
    )
    arrival_checkpoint = _checkpoint(candidate, arrival_book, arrival_ts, source_files)
    intent = OrderIntent(
        strategy_id="offline-fidelity",
        market_id=candidate.market_id,
        condition_id=candidate.condition_id,
        asset_id=candidate.asset_id,
        side=side,
        order_type="FOK",
        limit_price=best_price,
        size=size,
        post_only=False,
        decision_ts=decision_ts,
        client_order_id=f"offline-{candidate.asset_id[:12]}-{int(decision_ts.timestamp())}",
        amount_unit="SHARES",
        tick_size=candidate.tick_size,
        min_order_size=None,
    )
    engine = TakerOnlyPaperExecutionEngine(
        TakerExecutionConfig(
            latency=PaperLatencyModel(order_delay_ms=max(0, int(latency_ms))),
            max_book_age_ms=max(2_000, int(latency_ms) + 1_000),
        )
    )
    result = engine.execute(
        intent,
        decision_checkpoint=decision_checkpoint,
        arrival_checkpoint=arrival_checkpoint,
        arrival_ts_override=arrival_ts,
    )
    reference_fills = _walk_book(
        arrival_book,
        side=side,
        size=size,
        limit_price=best_price,
    )
    reference_size = sum((quantity for _, quantity in reference_fills), Decimal(0))
    reference_notional = sum(
        (price * quantity for price, quantity in reference_fills), Decimal(0)
    )
    case_id = hashlib.sha256(
        f"{candidate.asset_id}|{arrival_ts.isoformat()}|{side}|{size}|{best_price}".encode()
    ).hexdigest()[:24]
    taker_case = {
        "case_id": case_id,
        "category": candidate.category,
        "event_id": candidate.event_id,
        "asset_id": candidate.asset_id,
        "side": side,
        "size": str(size),
        "limit_price": str(best_price),
        "tick_size": str(candidate.tick_size),
        "bids": [
            [str(price), str(quantity)]
            for price, quantity in sorted(arrival_book.bids.items(), reverse=True)
        ],
        "asks": [
            [str(price), str(quantity)]
            for price, quantity in sorted(arrival_book.asks.items())
        ],
        "reference_filled_size": str(reference_size),
        "reference_filled_notional": str(reference_notional),
    }
    taker_result = {
        "case_id": case_id,
        "status": result.status,
        "filled_size": str(result.filled_size),
        "filled_notional": str(result.filled_notional),
        "reference_filled_size": str(reference_size),
        "reference_filled_notional": str(reference_notional),
        "exact_match": result.filled_size == reference_size
        and result.filled_notional == reference_notional,
        "arrival_ts": arrival_ts.isoformat(),
        "book_checkpoint_id": arrival_checkpoint.checkpoint_id,
        "visible_top_level_participation": str(size / best_size),
    }

    maker_price, queue_ahead = _maker_quote(
        arrival_book,
        side=side,
        tick_size=candidate.tick_size,
        quote_position=quote_position,
    )
    maker_size = max(Decimal(5), candidate.min_order_size).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN
    )
    order = CounterfactualMakerOrder(
        shadow_order_id=(
            f"maker-{case_id}-{quote_position.lower()}-{horizon_seconds}s"
        ),
        event_id=candidate.event_id,
        asset_id=candidate.asset_id,
        side=side,
        price=maker_price,
        size=maker_size,
        queue_ahead_estimate=queue_ahead,
        horizon_seconds=Decimal(horizon_seconds),
        quote_position=quote_position,
        trial_id=f"maker-{case_id}-{quote_position.lower()}",
    )
    observed_chain_rows = (
        list(chain_rows)
        if chain_rows is not None
        else orderfilled.fetch_window(
            asset_id=candidate.asset_id,
            start=arrival_ts - timedelta(minutes=5),
            end=end,
            limit=10_000,
        )
    )
    pre = _compatible_trades(
        observed_chain_rows, order, arrival_ts - timedelta(minutes=5), arrival_ts
    )
    post = _compatible_trades(observed_chain_rows, order, arrival_ts, end)
    forecast = (
        sum((item[1] for item in pre), Decimal(0))
        / Decimal(300)
        * Decimal(horizon_seconds)
    )
    compatible = sum((item[1] for item in post), Decimal(0))
    observed_seconds = _queue_consumption_time(
        post, queue_ahead, arrival_ts, horizon_seconds
    )
    removed, replenished, crossed, epoch_stable, l2_complete = _maker_l2_evidence(
        rows,
        arrival_book,
        side=side,
        price=maker_price,
        start=arrival_ts,
        end=end,
        exchange_batches=exchange_batches,
    )
    watermark_at = watermark.get("block_time")
    orderfilled_complete = bool(
        isinstance(watermark_at, datetime)
        and _utc(watermark_at) >= end
        and len(observed_chain_rows) < 10_000
    )
    evidence = MakerEvidenceWindow(
        compatible_trade_size=compatible,
        book_level_removed_size=removed,
        book_level_replenished_size=replenished,
        market_crossed_through_price=crossed,
        terminal_reason="horizon_elapsed"
        if end <= candidate.hour_start + timedelta(hours=1)
        else None,
        l2_complete=l2_complete,
        orderfilled_complete=orderfilled_complete,
        queue_epoch_stable=epoch_stable,
        observed_seconds=observed_seconds,
    )
    return ReplaySample(
        candidate=candidate,
        split=split,
        order=order,
        evidence=evidence,
        forecast_trade_volume=forecast,
        taker_case=taker_case,
        taker_result=taker_result,
        source_files=source_files,
        source_hashes=dict(source_hashes),
        forecast_trade_count=len(pre),
        forecast_window_seconds=300,
    )


def _normalize_horizons(values: Sequence[int | None]) -> tuple[int, ...]:
    horizons = sorted({int(value) for value in values if value is not None})
    if not horizons or any(value <= 0 or value > 1_800 for value in horizons):
        raise ValueError("maker horizons must be between 1 and 1800 seconds")
    return tuple(horizons)


def _normalize_quote_positions(values: Sequence[str]) -> tuple[str, ...]:
    allowed = set(DEFAULT_MAKER_QUOTE_POSITIONS)
    positions = tuple(dict.fromkeys(str(value).strip().upper() for value in values))
    if not positions or any(value not in allowed for value in positions):
        raise ValueError(
            "maker quote positions must be AT_BEST, ONE_TICK_BEHIND, "
            "or ONE_TICK_INSIDE_SPREAD"
        )
    return positions


def _maker_quote(
    book: BookState,
    *,
    side: str,
    tick_size: Decimal,
    quote_position: str,
) -> tuple[Decimal, Decimal]:
    position = str(quote_position).upper()
    if not book.bids or not book.asks:
        raise ValueError("maker quote requires a two-sided book")
    best_bid = max(book.bids)
    best_ask = min(book.asks)
    if side == "BUY":
        if position == "AT_BEST":
            price = best_bid
        elif position == "ONE_TICK_BEHIND":
            price = best_bid - tick_size
        else:
            price = best_bid + tick_size
            if price >= best_ask:
                raise ValueError("inside-spread BUY would cross or lock")
        levels = book.bids
    else:
        if position == "AT_BEST":
            price = best_ask
        elif position == "ONE_TICK_BEHIND":
            price = best_ask + tick_size
        else:
            price = best_ask - tick_size
            if price <= best_bid:
                raise ValueError("inside-spread SELL would cross or lock")
        levels = book.asks
    if price <= 0 or price >= 1:
        raise ValueError("maker quote is outside the binary-option price domain")
    return price, max(Decimal(0), levels.get(price, Decimal(0)))


def _queue_bucket(sample: ReplaySample) -> str:
    if sample.order.queue_ahead_estimate <= 0:
        return "Q0_FRONT"
    ratio = sample.order.queue_ahead_estimate / max(
        Decimal("0.000001"), sample.order.size
    )
    if ratio <= 1:
        return "Q1_LE_1X"
    if ratio <= 5:
        return "Q2_1_TO_5X"
    if ratio <= 20:
        return "Q3_5_TO_20X"
    return "Q4_GT_20X"


def _activity_bucket_keys(sample: ReplaySample) -> tuple[str, ...]:
    category = sample.candidate.category
    side = sample.order.side
    position = sample.order.quote_position
    return (
        f"CATEGORY_POSITION:{category}|{side}|{position}",
        f"POSITION:{side}|{position}",
        f"CATEGORY:{category}|{side}",
        "GLOBAL",
    )


def _pre_window_trade_volume(sample: ReplaySample) -> Decimal:
    horizon = max(Decimal("0.000001"), sample.order.horizon_seconds)
    return (
        sample.forecast_trade_volume * Decimal(sample.forecast_window_seconds) / horizon
    )


def _independent_activity_rows(
    samples: Sequence[ReplaySample],
    *,
    split: str,
) -> list[ReplaySample]:
    selected: dict[str, ReplaySample] = {}
    for sample in samples:
        if sample.split != split:
            continue
        trial_id = sample.order.trial_id or sample.order.shadow_order_id
        current = selected.get(trial_id)
        if (
            current is None
            or sample.order.horizon_seconds < current.order.horizon_seconds
        ):
            selected[trial_id] = sample
    return [selected[key] for key in sorted(selected)]


def _fit_empirical_bayes_maker_prior(
    samples: Sequence[ReplaySample],
) -> EmpiricalBayesMakerPrior:
    """Fit activity and fill priors from train rows only."""

    activity_rows = _independent_activity_rows(samples, split="train")
    if not activity_rows:
        raise ValueError("empirical Bayes prior requires train rows")

    raw_buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "trial_count": 0,
            "event_ids": set(),
            "exposure_seconds": Decimal(0),
            "trade_count": Decimal(0),
            "trade_volume": Decimal(0),
        }
    )
    for sample in activity_rows:
        for key in _activity_bucket_keys(sample):
            bucket = raw_buckets[key]
            bucket["trial_count"] += 1
            bucket["event_ids"].add(sample.candidate.event_id)
            bucket["exposure_seconds"] += Decimal(sample.forecast_window_seconds)
            bucket["trade_count"] += Decimal(sample.forecast_trade_count)
            bucket["trade_volume"] += _pre_window_trade_volume(sample)

    global_bucket = raw_buckets["GLOBAL"]
    global_exposure = max(Decimal("0.000001"), global_bucket["exposure_seconds"])
    global_count = global_bucket["trade_count"]
    global_rate = global_count / global_exposure
    global_mean_size = (
        global_bucket["trade_volume"] / global_count if global_count > 0 else Decimal(5)
    )
    pseudo_arrivals = max(
        Decimal(1),
        global_rate * EMPIRICAL_BAYES_HIERARCHY_EXPOSURE_SECONDS,
    )
    activity_buckets: dict[str, Mapping[str, Any]] = {}
    for key, raw in sorted(raw_buckets.items()):
        is_global = key == "GLOBAL"
        exposure = raw["exposure_seconds"]
        count = raw["trade_count"]
        volume = raw["trade_volume"]
        rate = (
            global_rate
            if is_global
            else (count + global_rate * EMPIRICAL_BAYES_HIERARCHY_EXPOSURE_SECONDS)
            / (exposure + EMPIRICAL_BAYES_HIERARCHY_EXPOSURE_SECONDS)
        )
        mean_size = (
            global_mean_size
            if is_global
            else (volume + global_mean_size * pseudo_arrivals)
            / (count + pseudo_arrivals)
        )
        activity_buckets[key] = {
            "trial_count": int(raw["trial_count"]),
            "event_count": len(raw["event_ids"]),
            "exposure_seconds": str(exposure),
            "trade_count": str(count),
            "trade_volume": str(volume),
            "posterior_arrival_rate_per_second": str(rate),
            "posterior_mean_trade_size": str(mean_size),
        }

    horizon_labels: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"fill_count": 0, "no_fill_count": 0, "event_ids": set()}
    )
    all_fill_count = 0
    all_no_fill_count = 0
    seen_horizon_trials: set[tuple[str, int]] = set()
    for sample in samples:
        if sample.split != "train":
            continue
        horizon = int(sample.order.horizon_seconds)
        trial_id = sample.order.trial_id or sample.order.shadow_order_id
        trial_key = (trial_id, horizon)
        if trial_key in seen_horizon_trials:
            continue
        seen_horizon_trials.add(trial_key)
        label_class = label_counterfactual(sample.order, sample.evidence).label_class
        if label_class not in {
            "STRICT_CONFIRMED_FILL",
            "OBSERVED_TAPE_NO_FILL",
        }:
            continue
        bucket = horizon_labels[horizon]
        bucket["event_ids"].add(sample.candidate.event_id)
        if label_class == "STRICT_CONFIRMED_FILL":
            bucket["fill_count"] += 1
            all_fill_count += 1
        else:
            bucket["no_fill_count"] += 1
            all_no_fill_count += 1

    total_labels = all_fill_count + all_no_fill_count
    global_fill_rate = (
        (Decimal(all_fill_count) + Decimal("0.5"))
        / (Decimal(total_labels) + Decimal(1))
        if total_labels > 0
        else Decimal("0.01")
    )
    horizon_fill_probabilities: dict[int, Decimal] = {}
    horizon_fill_evidence: dict[int, Mapping[str, Any]] = {}
    previous_probability = Decimal(0)
    for horizon, raw in sorted(horizon_labels.items()):
        fill_count = int(raw["fill_count"])
        no_fill_count = int(raw["no_fill_count"])
        label_count = fill_count + no_fill_count
        posterior = (
            Decimal(fill_count) + global_fill_rate * EMPIRICAL_BAYES_FILL_PRIOR_STRENGTH
        ) / (Decimal(label_count) + EMPIRICAL_BAYES_FILL_PRIOR_STRENGTH)
        posterior = max(previous_probability, posterior)
        previous_probability = posterior
        horizon_fill_probabilities[horizon] = posterior
        horizon_fill_evidence[horizon] = {
            "fill_count": fill_count,
            "no_fill_count": no_fill_count,
            "label_count": label_count,
            "event_count": len(raw["event_ids"]),
            "posterior_fill_probability": str(posterior),
        }

    payload = {
        "schema_version": "maker_empirical_bayes_prior_v1",
        "source_split": "train",
        "source_trial_count": len(activity_rows),
        "source_event_count": len(
            {sample.candidate.event_id for sample in activity_rows}
        ),
        "global_arrival_rate_per_second": str(global_rate),
        "global_mean_trade_size": str(global_mean_size),
        "activity_buckets": activity_buckets,
        "horizon_fill_probabilities": {
            str(key): str(value)
            for key, value in sorted(horizon_fill_probabilities.items())
        },
        "horizon_fill_evidence": {
            str(key): value for key, value in sorted(horizon_fill_evidence.items())
        },
    }
    artifact_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return EmpiricalBayesMakerPrior(
        schema_version=str(payload["schema_version"]),
        source_split="train",
        source_trial_count=len(activity_rows),
        source_event_count=int(payload["source_event_count"]),
        global_arrival_rate_per_second=global_rate,
        global_mean_trade_size=global_mean_size,
        activity_buckets=activity_buckets,
        horizon_fill_probabilities=horizon_fill_probabilities,
        horizon_fill_evidence=horizon_fill_evidence,
        artifact_hash=artifact_hash,
    )


def _stratified_maker_report(
    pairs: Sequence[tuple[ReplaySample, ShadowEvaluationRow]],
) -> dict[str, Any]:
    dimensions: dict[str, dict[str, Any]] = {}
    extractors = {
        "category": lambda sample: sample.candidate.category,
        "side": lambda sample: sample.order.side,
        "quote_position": lambda sample: sample.order.quote_position,
        "horizon_seconds": lambda sample: str(int(sample.order.horizon_seconds)),
        "queue_bucket": _queue_bucket,
    }
    for dimension, extractor in extractors.items():
        grouped: dict[str, list[ShadowEvaluationRow]] = defaultdict(list)
        for sample, row in pairs:
            grouped[str(extractor(sample))].append(row)
        dimensions[dimension] = {
            value: _proxy_stratum_summary(rows)
            for value, rows in sorted(grouped.items())
        }

    joint: dict[tuple[str, ...], list[ShadowEvaluationRow]] = defaultdict(list)
    for sample, row in pairs:
        key = (
            sample.candidate.category,
            sample.order.side,
            sample.order.quote_position,
            str(int(sample.order.horizon_seconds)),
            _queue_bucket(sample),
        )
        joint[key].append(row)
    return {
        "evidence_scope": "PUBLIC_ORDERFILLED_TAPE_PROXY_NOT_OWN_ORDER_TRUTH",
        "dimensions": dimensions,
        "joint_strata": [
            {
                "category": key[0],
                "side": key[1],
                "quote_position": key[2],
                "horizon_seconds": key[3],
                "queue_bucket": key[4],
                **_proxy_stratum_summary(rows),
            }
            for key, rows in sorted(joint.items())
        ],
    }


def _proxy_stratum_summary(rows: Sequence[ShadowEvaluationRow]) -> dict[str, Any]:
    evaluation = evaluate_shadow_predictions(rows)
    proxy_count = int(evaluation.get("strict_confirmed_fill_count") or 0) + int(
        evaluation.get("observed_tape_no_fill_count") or 0
    )
    return {
        "status": (
            "PROXY_METRICS_AVAILABLE"
            if len(rows) >= 2 and proxy_count > 0
            else "INSUFFICIENT_PROXY_EVIDENCE"
        ),
        "sample_count": len(rows),
        "strict_confirmed_fill_count": evaluation.get("strict_confirmed_fill_count"),
        "observed_tape_no_fill_count": evaluation.get("observed_tape_no_fill_count"),
        "ambiguous_abstain_count": evaluation.get("ambiguous_abstain_count"),
        "metrics": evaluation.get("metrics"),
    }


def _matrix_coverage(
    samples: Sequence[ReplaySample],
    *,
    requested_quote_positions: Sequence[str],
    requested_horizons: Sequence[int],
) -> dict[str, Any]:
    quote_counts = {
        position: sum(sample.order.quote_position == position for sample in samples)
        for position in requested_quote_positions
    }
    horizon_counts = {
        str(horizon): sum(
            int(sample.order.horizon_seconds) == horizon for sample in samples
        )
        for horizon in requested_horizons
    }
    side_counts = {
        side: sum(sample.order.side == side for sample in samples)
        for side in ("BUY", "SELL")
    }
    queue_counts = {
        bucket: sum(_queue_bucket(sample) == bucket for sample in samples)
        for bucket in (
            "Q0_FRONT",
            "Q1_LE_1X",
            "Q2_1_TO_5X",
            "Q3_5_TO_20X",
            "Q4_GT_20X",
        )
    }
    checks = {
        "all_requested_quote_positions_observed": all(quote_counts.values()),
        "all_requested_horizons_observed": all(horizon_counts.values()),
        "both_sides_observed": all(side_counts.values()),
        "multiple_queue_buckets_observed": sum(
            count > 0 for count in queue_counts.values()
        )
        >= 2,
    }
    return {
        "status": "PASS" if all(checks.values()) else "BLOCKED",
        "checks": checks,
        "quote_position_counts": quote_counts,
        "horizon_counts": horizon_counts,
        "side_counts": side_counts,
        "queue_bucket_counts": queue_counts,
    }


def _unique_taker_cases(samples: Sequence[ReplaySample]) -> list[dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for sample in samples:
        cases[str(sample.taker_case["case_id"])] = dict(sample.taker_case)
    return list(cases.values())


def _scenario_skip_reason(exc: Exception) -> str | None:
    message = str(exc)
    if message.startswith("no routed XUE parquet for "):
        return "MISSING_ROUTED_ARCHIVE_EVIDENCE"
    if message in {
        "inside-spread BUY would cross or lock",
        "inside-spread SELL would cross or lock",
    }:
        return "QUOTE_POSITION_NOT_APPLICABLE"
    if message in {
        "no full WS book baseline satisfies horizon and small-order domain",
        "small-order domain cannot be satisfied",
        "maker quote is outside the binary-option price domain",
    }:
        return "SCENARIO_OUTSIDE_CALIBRATION_DOMAIN"
    return None


def _count_values(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    values = sorted({str(row.get(key) or "") for row in rows})
    return {
        value: sum(str(row.get(key) or "") == value for row in rows)
        for value in values
        if value
    }


def _maker_probability_config(
    prior: EmpiricalBayesMakerPrior,
    *,
    activity_multiplier: Decimal,
    prior_exposure_seconds: Decimal,
    raw_probability_weight: Decimal,
    probability_odds_multiplier: Decimal = Decimal(1),
) -> EmpiricalBayesMakerConfig:
    payload = {
        "schema_version": "maker_empirical_bayes_config_v1",
        "activity_multiplier": str(activity_multiplier),
        "prior_exposure_seconds": str(prior_exposure_seconds),
        "raw_probability_weight": str(raw_probability_weight),
        "probability_odds_multiplier": str(probability_odds_multiplier),
        "prior_artifact_hash": prior.artifact_hash,
        "fit_split": "train",
        "selection_split": "calibration",
    }
    decision_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return EmpiricalBayesMakerConfig(
        schema_version=str(payload["schema_version"]),
        activity_multiplier=activity_multiplier,
        prior_exposure_seconds=prior_exposure_seconds,
        raw_probability_weight=raw_probability_weight,
        probability_odds_multiplier=probability_odds_multiplier,
        prior_artifact_hash=prior.artifact_hash,
        decision_hash=decision_hash,
    )


def _resolve_activity_bucket(
    sample: ReplaySample,
    prior: EmpiricalBayesMakerPrior,
) -> tuple[Decimal, Decimal, str, Mapping[str, Any]]:
    for key in _activity_bucket_keys(sample):
        raw = prior.activity_buckets.get(key)
        if not isinstance(raw, Mapping):
            continue
        if key != "GLOBAL" and int(raw.get("trial_count") or 0) < (
            MIN_EMPIRICAL_BAYES_ACTIVITY_TRIALS
        ):
            continue
        return (
            Decimal(str(raw["posterior_arrival_rate_per_second"])),
            Decimal(str(raw["posterior_mean_trade_size"])),
            key,
            raw,
        )
    raise ValueError("empirical Bayes prior has no global activity bucket")


def _horizon_fill_probability(
    horizon_seconds: Decimal,
    prior: EmpiricalBayesMakerPrior,
) -> Decimal:
    if not prior.horizon_fill_probabilities:
        return Decimal(0)
    target = int(horizon_seconds)
    nearest = min(
        prior.horizon_fill_probabilities,
        key=lambda value: (abs(value - target), value),
    )
    return prior.horizon_fill_probabilities[nearest]


def _posterior_activity_forecast(
    sample: ReplaySample,
    prior: EmpiricalBayesMakerPrior,
    config: EmpiricalBayesMakerConfig,
) -> tuple[Decimal, Decimal, Mapping[str, Any]]:
    prior_rate, prior_mean_size, bucket_key, bucket = _resolve_activity_bucket(
        sample, prior
    )
    lookback = max(Decimal("0.000001"), Decimal(sample.forecast_window_seconds))
    exposure = max(Decimal(0), config.prior_exposure_seconds)
    observed_count = max(Decimal(0), Decimal(sample.forecast_trade_count))
    observed_volume = max(Decimal(0), _pre_window_trade_volume(sample))
    posterior_rate = (observed_count + prior_rate * exposure) / (lookback + exposure)
    prior_arrivals = max(Decimal(1), prior_rate * exposure)
    posterior_mean_size = (observed_volume + prior_mean_size * prior_arrivals) / (
        observed_count + prior_arrivals
    )
    expected_arrivals = (
        posterior_rate * sample.order.horizon_seconds * config.activity_multiplier
    )
    arrival_probability = poisson_arrival_probability(
        observed_count=posterior_rate * lookback,
        lookback_seconds=lookback,
        horizon_seconds=sample.order.horizon_seconds,
        rate_multiplier=config.activity_multiplier,
    )
    conditional_arrivals = (
        expected_arrivals / arrival_probability
        if arrival_probability > 0
        else Decimal(0)
    )
    conditional_trade_volume = conditional_arrivals * posterior_mean_size
    return (
        conditional_trade_volume,
        arrival_probability,
        {
            "activity_bucket": bucket_key,
            "activity_bucket_trial_count": int(bucket.get("trial_count") or 0),
            "posterior_arrival_rate_per_second": str(posterior_rate),
            "posterior_mean_trade_size": str(posterior_mean_size),
            "expected_arrivals": str(expected_arrivals),
            "arrival_probability": str(arrival_probability),
            "conditional_trade_volume": str(conditional_trade_volume),
        },
    )


def _calibrate_empirical_bayes_model(
    samples: Sequence[ReplaySample],
    prior: EmpiricalBayesMakerPrior,
) -> tuple[EmpiricalBayesMakerConfig, list[dict[str, Any]]]:
    """Select hyperparameters on calibration rows without reading holdout labels."""

    calibration = [sample for sample in samples if sample.split == "calibration"]
    if not calibration:
        raise ValueError("empirical Bayes calibration requires calibration rows")
    grid: list[dict[str, Any]] = []
    best: tuple[Decimal, Decimal, EmpiricalBayesMakerConfig] | None = None
    for exposure in EMPIRICAL_BAYES_PRIOR_EXPOSURES_SECONDS:
        for multiplier in ACTIVITY_MULTIPLIERS:
            for raw_weight in EMPIRICAL_BAYES_RAW_PROBABILITY_WEIGHTS:
                for odds_multiplier in EMPIRICAL_BAYES_ODDS_MULTIPLIERS:
                    config = _maker_probability_config(
                        prior,
                        activity_multiplier=multiplier,
                        prior_exposure_seconds=exposure,
                        raw_probability_weight=raw_weight,
                        probability_odds_multiplier=odds_multiplier,
                    )
                    rows = [
                        _shadow_row(sample, prior, config) for sample in calibration
                    ]
                    report = evaluate_shadow_predictions(rows)
                    metrics = report.get("metrics")
                    metric_rows = dict(metrics) if isinstance(metrics, Mapping) else {}
                    brier_raw = metric_rows.get("brier_score_independent_trial_proxy")
                    ece_raw = metric_rows.get("ece_independent_trial_proxy")
                    brier = Decimal(str(brier_raw)) if brier_raw is not None else None
                    ece = Decimal(str(ece_raw)) if ece_raw is not None else None
                    grid.append(
                        {
                            "activity_multiplier": str(multiplier),
                            "prior_exposure_seconds": str(exposure),
                            "raw_probability_weight": str(raw_weight),
                            "probability_odds_multiplier": str(odds_multiplier),
                            "proxy_brier": (str(brier) if brier is not None else None),
                            "proxy_ece": str(ece) if ece is not None else None,
                            "independent_proxy_count": int(
                                report.get("independent_proxy_count") or 0
                            ),
                            "config_decision_hash": config.decision_hash,
                        }
                    )
                    if brier is None:
                        continue
                    rank = (brier, ece if ece is not None else Decimal(1))
                    if best is None or rank < best[:2]:
                        best = (rank[0], rank[1], config)
    if best is None:
        raise ValueError("calibration rows have no usable maker proxy labels")
    return best[2], grid


def _rolling_origin_maker_evaluation(
    samples: Sequence[ReplaySample],
    *,
    final_holdout_start: date | None,
) -> dict[str, Any]:
    """Aggregate day-forward predictions whose target day was never fitted."""

    eligible_dates = sorted(
        {
            sample.candidate.hour_start.date()
            for sample in samples
            if final_holdout_start is None
            or sample.candidate.hour_start.date() < final_holdout_start
        }
    )
    aggregate_rows: list[ShadowEvaluationRow] = []
    folds: list[dict[str, Any]] = []
    for target_index in range(3, len(eligible_dates)):
        target_date = eligible_dates[target_index]
        development_dates = eligible_dates[:target_index]
        train_end = max(1, int(len(development_dates) * 0.6))
        train_end = min(train_end, len(development_dates) - 1)
        train_dates = set(development_dates[:train_end])
        calibration_dates = set(development_dates[train_end:])
        partition_by_event: dict[str, set[str]] = defaultdict(set)
        for sample in samples:
            sample_date = sample.candidate.hour_start.date()
            if sample_date in train_dates:
                partition_by_event[sample.candidate.event_id].add("train")
            elif sample_date in calibration_dates:
                partition_by_event[sample.candidate.event_id].add("calibration")
            elif sample_date == target_date:
                partition_by_event[sample.candidate.event_id].add("holdout")
        leaking_events = {
            event_id
            for event_id, partitions in partition_by_event.items()
            if len(partitions) > 1
        }
        fold_samples: list[ReplaySample] = []
        for sample in samples:
            if sample.candidate.event_id in leaking_events:
                continue
            sample_date = sample.candidate.hour_start.date()
            if sample_date in train_dates:
                split = "train"
            elif sample_date in calibration_dates:
                split = "calibration"
            elif sample_date == target_date:
                split = "holdout"
            else:
                continue
            fold_samples.append(replace(sample, split=split))
        try:
            prior = _fit_empirical_bayes_maker_prior(fold_samples)
            config, _ = _calibrate_empirical_bayes_model(fold_samples, prior)
        except ValueError as exc:
            folds.append(
                {
                    "target_date": target_date.isoformat(),
                    "status": "SKIPPED_INSUFFICIENT_PRIOR_OR_CALIBRATION",
                    "reason": str(exc),
                    "excluded_cross_boundary_event_count": len(leaking_events),
                }
            )
            continue
        target_rows = [
            _shadow_row(sample, prior, config)
            for sample in fold_samples
            if sample.split == "holdout"
        ]
        evaluation = evaluate_shadow_predictions(target_rows)
        aggregate_rows.extend(target_rows)
        folds.append(
            {
                "target_date": target_date.isoformat(),
                "status": "EVALUATED",
                "train_dates": [day.isoformat() for day in sorted(train_dates)],
                "calibration_dates": [
                    day.isoformat() for day in sorted(calibration_dates)
                ],
                "target_row_count": len(target_rows),
                "independent_proxy_count": int(
                    evaluation.get("independent_proxy_count") or 0
                ),
                "independent_strict_confirmed_fill_count": int(
                    evaluation.get("independent_strict_confirmed_fill_count") or 0
                ),
                "independent_observed_tape_no_fill_count": int(
                    evaluation.get("independent_observed_tape_no_fill_count") or 0
                ),
                "prior_artifact_hash": prior.artifact_hash,
                "config_decision_hash": config.decision_hash,
                "calibrated_config": asdict(config),
                "excluded_cross_boundary_event_count": len(leaking_events),
            }
        )
    evaluation = evaluate_shadow_predictions(aggregate_rows)
    gate = _maker_probability_research_gate(evaluation)
    return {
        "schema_version": "maker_rolling_origin_evaluation_v1",
        "status": "PASS" if folds and aggregate_rows else "BLOCKED",
        "evidence_scope": "PUBLIC_ORDERFILLED_DAY_FORWARD_PROXY",
        "final_holdout_excluded_from_all_folds": True,
        "final_holdout_start": (
            final_holdout_start.isoformat() if final_holdout_start else None
        ),
        "fold_count": sum(fold["status"] == "EVALUATED" for fold in folds),
        "folds": folds,
        "aggregate": evaluation,
        "probability_research_gate": gate,
    }


def _maker_probability_research_gate(
    evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep an error-prone public-tape proxy out of the selected research model."""

    row_strict_count = int(evaluation.get("strict_confirmed_fill_count") or 0)
    row_no_fill_count = int(evaluation.get("observed_tape_no_fill_count") or 0)
    strict_count = int(
        evaluation.get("independent_strict_confirmed_fill_count")
        if evaluation.get("independent_strict_confirmed_fill_count") is not None
        else row_strict_count
    )
    no_fill_count = int(
        evaluation.get("independent_observed_tape_no_fill_count")
        if evaluation.get("independent_observed_tape_no_fill_count") is not None
        else row_no_fill_count
    )
    metrics = evaluation.get("metrics")
    metric_rows = dict(metrics) if isinstance(metrics, Mapping) else {}
    interval_raw = metric_rows.get(
        "false_positive_fill_independent_trial_upper_95",
        metric_rows.get("false_positive_fill_proxy_upper_95"),
    )
    interval = dict(interval_raw) if isinstance(interval_raw, Mapping) else {}
    brier_raw = metric_rows.get(
        "brier_score_independent_trial_proxy",
        metric_rows.get("brier_score_observed_tape_proxy"),
    )
    ece_raw = metric_rows.get(
        "ece_independent_trial_proxy",
        metric_rows.get("ece_observed_tape_proxy"),
    )
    skill_raw = metric_rows.get("brier_skill_score_independent_trial_proxy")
    reliability_raw = metric_rows.get("reliability_bins_independent_trial_proxy")
    reliability_rows = (
        [dict(row) for row in reliability_raw if isinstance(row, Mapping)]
        if isinstance(reliability_raw, list)
        else []
    )
    assessable_reliability_rows = [
        row for row in reliability_rows if int(row.get("sample_count") or 0) >= 30
    ]
    failed_reliability_rows = []
    for row in assessable_reliability_rows:
        observed_interval = row.get("observed_fill_wilson_95")
        interval_row = (
            dict(observed_interval) if isinstance(observed_interval, Mapping) else {}
        )
        predicted_raw = row.get("mean_predicted_probability")
        lower_raw = interval_row.get("lower")
        upper_raw = interval_row.get("upper")
        if predicted_raw is None or lower_raw is None or upper_raw is None:
            failed_reliability_rows.append(row)
            continue
        predicted = Decimal(str(predicted_raw))
        if not Decimal(str(lower_raw)) <= predicted <= Decimal(str(upper_raw)):
            failed_reliability_rows.append(row)
    brier = Decimal(str(brier_raw)) if brier_raw is not None else None
    ece = Decimal(str(ece_raw)) if ece_raw is not None else None
    skill = Decimal(str(skill_raw)) if skill_raw is not None else None
    upper_raw = interval.get("upper")
    upper = Decimal(str(upper_raw)) if upper_raw is not None else None
    proxy_count = strict_count + no_fill_count
    predicted_positive_trials = int(interval.get("total") or 0)
    checks = {
        "positive_and_negative_proxy_labels": strict_count > 0 and no_fill_count > 0,
        "minimum_independent_proxy_trials": proxy_count >= MIN_RESEARCH_PROXY_ROWS,
        "minimum_observed_fill_trials": (
            strict_count >= MIN_RESEARCH_OBSERVED_FILL_TRIALS
        ),
        "proxy_brier": brier is not None and brier <= MAX_RESEARCH_PROXY_BRIER,
        "proxy_ece": ece is not None and ece <= MAX_RESEARCH_PROXY_ECE,
        "positive_brier_skill": skill is not None and skill > 0,
        "reliability_bins_calibrated": bool(assessable_reliability_rows)
        and not failed_reliability_rows,
    }
    high_probability_estimable = (
        predicted_positive_trials >= MIN_RESEARCH_PREDICTED_POSITIVES
    )
    high_probability_safe = bool(
        high_probability_estimable
        and upper is not None
        and upper <= MAX_RESEARCH_FALSE_POSITIVE_UPPER_95
    )
    high_probability_status = (
        "CALIBRATED"
        if high_probability_safe
        else (
            "BLOCKED_UNSAFE"
            if high_probability_estimable
            else "DISABLED_INSUFFICIENT_EVIDENCE"
        )
    )
    promoted = all(checks.values()) and high_probability_status != "BLOCKED_UNSAFE"
    classification = (
        "RESEARCH_CALIBRATED"
        if promoted and high_probability_status == "CALIBRATED"
        else (
            "RESEARCH_CALIBRATED_LOW_PROBABILITY_DOMAIN"
            if promoted
            else "RESEARCH_CALIBRATION_NOT_PROMOTED"
        )
    )
    return {
        "status": "PASS" if promoted else "BLOCKED",
        "classification": classification,
        "evidence_scope": "PUBLIC_ORDERFILLED_TAPE_PROXY_NOT_OWN_ORDER_TRUTH",
        "checks": checks,
        "high_probability_domain": {
            "status": high_probability_status,
            "minimum_probability": "0.5",
            "predicted_positive_trials": predicted_positive_trials,
            "minimum_predicted_positive_trials": (MIN_RESEARCH_PREDICTED_POSITIVES),
            "false_positive_upper_95": (str(upper) if upper is not None else None),
            "max_false_positive_upper_95": str(MAX_RESEARCH_FALSE_POSITIVE_UPPER_95),
        },
        "calibrated_probability_domain": (
            "[0,1]" if high_probability_safe else "[0,0.5)"
        ),
        "reliability_gate": {
            "minimum_bin_samples": 30,
            "assessable_bin_count": len(assessable_reliability_rows),
            "failed_bin_count": len(failed_reliability_rows),
            "failed_bins": failed_reliability_rows,
        },
        "strict_confirmed_fill_count": strict_count,
        "observed_tape_no_fill_count": no_fill_count,
        "row_strict_confirmed_fill_count": row_strict_count,
        "row_observed_tape_no_fill_count": row_no_fill_count,
        "independent_trial_count": int(
            evaluation.get("independent_trial_count") or proxy_count
        ),
        "proxy_count": proxy_count,
        "predicted_positive_trials": predicted_positive_trials,
        "proxy_brier": str(brier) if brier is not None else None,
        "proxy_ece": str(ece) if ece is not None else None,
        "brier_skill_score": str(skill) if skill is not None else None,
        "false_positive_upper_95": str(upper) if upper is not None else None,
        "thresholds": {
            "minimum_independent_proxy_trials": MIN_RESEARCH_PROXY_ROWS,
            "minimum_observed_fill_trials": MIN_RESEARCH_OBSERVED_FILL_TRIALS,
            "minimum_predicted_positive_trials": (MIN_RESEARCH_PREDICTED_POSITIVES),
            "max_proxy_brier": str(MAX_RESEARCH_PROXY_BRIER),
            "max_proxy_ece": str(MAX_RESEARCH_PROXY_ECE),
            "max_false_positive_upper_95": str(MAX_RESEARCH_FALSE_POSITIVE_UPPER_95),
        },
    }


def _apply_probability_odds_multiplier(
    probability: Decimal,
    multiplier: Decimal,
) -> Decimal:
    """Apply a calibration-only log-odds intercept without changing ranking."""

    bounded = min(Decimal(1), max(Decimal(0), probability))
    if bounded in {Decimal(0), Decimal(1)}:
        return bounded
    if multiplier <= 0:
        raise ValueError("probability odds multiplier must be positive")
    weighted_odds = multiplier * bounded / (Decimal(1) - bounded)
    return weighted_odds / (Decimal(1) + weighted_odds)


def _calibrated_prediction(
    prediction: MakerFillPrediction,
    *,
    fill_probability: Decimal,
    order_size: Decimal,
    horizon_seconds: Decimal,
    prior: EmpiricalBayesMakerPrior,
    config: EmpiricalBayesMakerConfig,
) -> MakerFillPrediction:
    calibrated = min(Decimal(1), max(Decimal(0), fill_probability))
    raw = prediction.fill_probability
    if raw > 0:
        p_full = calibrated * prediction.p_full / raw
        p_partial = calibrated * prediction.p_partial / raw
    else:
        p_full = Decimal(0)
        p_partial = calibrated
    expected = order_size * (p_full + p_partial * Decimal("0.5"))
    first = prediction.expected_time_to_first_fill_seconds
    if first is None and calibrated > 0:
        first = horizon_seconds / Decimal(2)
    return replace(
        prediction,
        p_no_fill=Decimal(1) - calibrated,
        p_partial=p_partial,
        p_full=p_full,
        expected_filled_size=expected,
        filled_size_p50=order_size if p_full >= Decimal("0.5") else expected,
        filled_size_p90=min(order_size, expected * Decimal("1.5")),
        expected_time_to_first_fill_seconds=first,
        expected_time_to_full_fill_seconds=(horizon_seconds if p_full > 0 else None),
        domain_status="RESEARCH_EMPIRICAL_BAYES_CANDIDATE",
        fill_probability=calibrated,
        expected_time_to_fill_seconds=first,
        time_to_fill_p90_seconds=(horizon_seconds if calibrated > 0 else None),
        calibration_domain=(
            "OFFLINE_TRAIN_CALIBRATION_ONLY:"
            f"{prior.artifact_hash[:12]}:{config.decision_hash[:12]}"
        ),
    )


def _shadow_row(
    sample: ReplaySample,
    prior: EmpiricalBayesMakerPrior,
    config: EmpiricalBayesMakerConfig,
) -> ShadowEvaluationRow:
    state = MakerQueueState(
        paper_order_id=sample.order.shadow_order_id,
        asset_id=sample.order.asset_id,
        side=sample.order.side,
        price_tick=sample.order.price,
        queue_model_version="offline-probabilistic-v3-empirical-bayes",
        displayed_size_at_accept=sample.order.queue_ahead_estimate,
        own_orders_ahead=Decimal(0),
        estimated_external_queue_ahead=sample.order.queue_ahead_estimate,
        order_size=sample.order.size,
    )
    forecast_volume, arrival_probability, _ = _posterior_activity_forecast(
        sample, prior, config
    )
    raw_prediction = MakerQueueEngine(
        QueueModel.PROBABILISTIC_QUEUE,
        cancel_ahead_probability=Decimal("0.25"),
    ).predict(
        state,
        forecast_trade_volume=forecast_volume,
        horizon_seconds=sample.order.horizon_seconds,
        aggressor_arrival_probability=arrival_probability,
    )
    fill_prior = _horizon_fill_probability(sample.order.horizon_seconds, prior)
    final_probability = (
        raw_prediction.fill_probability * config.raw_probability_weight
        + fill_prior * (Decimal(1) - config.raw_probability_weight)
    )
    final_probability = _apply_probability_odds_multiplier(
        final_probability,
        config.probability_odds_multiplier,
    )
    prediction = _calibrated_prediction(
        raw_prediction,
        fill_probability=final_probability,
        order_size=sample.order.size,
        horizon_seconds=sample.order.horizon_seconds,
        prior=prior,
        config=config,
    )
    return ShadowEvaluationRow(
        sample.order,
        prediction,
        label_counterfactual(sample.order, sample.evidence),
    )


def _taker_report(samples: Sequence[ReplaySample]) -> dict[str, Any]:
    by_case: dict[str, dict[str, Any]] = {}
    for sample in samples:
        row = dict(sample.taker_result)
        by_case[str(row["case_id"])] = row
    rows = list(by_case.values())
    mismatches = [row for row in rows if not row["exact_match"]]
    out_of_domain = [
        row
        for row in rows
        if Decimal(str(row["visible_top_level_participation"])) > Decimal("0.10")
    ]
    return {
        "status": "PASS"
        if rows and not mismatches and not out_of_domain
        else "BLOCKED",
        "classification": "OFFLINE_SMALL_ORDER_L2_REFERENCE_DIFFERENTIAL",
        "sample_count": len(rows),
        "exact_match_count": len(rows) - len(mismatches),
        "mismatch_count": len(mismatches),
        "out_of_domain_count": len(out_of_domain),
        "rows": rows,
    }


def _run_differential(
    *,
    corpus_path: Path,
    output_root: Path,
    nautilus_env: str,
) -> dict[str, Any]:
    worker = (
        Path(__file__).resolve().parents[2]
        / "scripts/offline_book_differential_worker.py"
    )
    commands = {
        "reference": [sys.executable, str(worker), "--engine", "reference"],
        "hftbacktest": [sys.executable, str(worker), "--engine", "hftbacktest"],
        "nautilus": [
            "conda",
            "run",
            "-n",
            nautilus_env,
            "python",
            str(worker),
            "--engine",
            "nautilus",
        ],
    }
    payloads: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    for name, prefix in commands.items():
        output = output_root / f"book-differential-{name}.json"
        command = [*prefix, "--input", str(corpus_path), "--output", str(output)]
        process = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=300
        )
        if process.returncode != 0:
            failures.append(
                {
                    "engine": name,
                    "returncode": process.returncode,
                    "stderr": process.stderr[-2_000:],
                }
            )
            continue
        payloads[name] = json.loads(output.read_text(encoding="utf-8"))
    reference = {
        row["case_id"]: row for row in payloads.get("reference", {}).get("rows", [])
    }
    comparisons: list[dict[str, Any]] = []
    for engine in ("hftbacktest", "nautilus"):
        for row in payloads.get(engine, {}).get("rows", []):
            expected = reference.get(row["case_id"])
            size_error = (
                abs(Decimal(row["filled_size"]) - Decimal(expected["filled_size"]))
                if expected
                else None
            )
            notional_error = (
                abs(
                    Decimal(row["filled_notional"])
                    - Decimal(expected["filled_notional"])
                )
                if expected
                else None
            )
            passed = bool(
                expected
                and size_error is not None
                and size_error <= Decimal("0.000001")
                and notional_error is not None
                and notional_error <= Decimal("0.000001")
            )
            comparisons.append(
                {
                    "engine": engine,
                    "case_id": row["case_id"],
                    "status": "PASS" if passed else "MISMATCH",
                    "filled_size_error": str(size_error)
                    if size_error is not None
                    else None,
                    "filled_notional_error": str(notional_error)
                    if notional_error is not None
                    else None,
                }
            )
    expected_comparisons = len(reference) * 2
    return {
        "status": (
            "PASS"
            if not failures
            and len(comparisons) == expected_comparisons
            and all(row["status"] == "PASS" for row in comparisons)
            else "BLOCKED"
        ),
        "scope": "STATIC_SMALL_TAKER_L2_MODEL_CONTRACT_NOT_VENUE_TRUTH",
        "case_count": len(reference),
        "comparison_count": len(comparisons),
        "expected_comparison_count": expected_comparisons,
        "failures": failures,
        "comparisons": comparisons,
    }


def _fault_injection_matrix() -> dict[str, Any]:
    start = datetime(2026, 8, 1, 23, 59, 59, 900_000, tzinfo=UTC)
    base = [
        _synthetic_event(
            start, "book", bids='[["0.4","10"]]', asks='[["0.6","8"]]', payload_hash="a"
        ),
        _synthetic_event(
            start + timedelta(milliseconds=50),
            "price_change",
            side="BUY",
            price="0.4",
            size="9",
            payload_hash="b",
        ),
        _synthetic_event(
            start + timedelta(milliseconds=150),
            "price_change",
            side="SELL",
            price="0.6",
            size="7",
            payload_hash="c",
        ),
    ]
    expected = _replay_book(base, until=start + timedelta(seconds=1))
    duplicate = _replay_book(base + [dict(base[1])], until=start + timedelta(seconds=1))
    out_of_order = _replay_book(
        list(reversed(base)), until=start + timedelta(seconds=1)
    )
    first = _replay_book(base[:2], until=start + timedelta(milliseconds=100))
    resumed_rows = [
        _synthetic_event(
            start + timedelta(milliseconds=100),
            "book",
            bids=json.dumps([[str(p), str(q)] for p, q in first.bids.items()]),
            asks=json.dumps([[str(p), str(q)] for p, q in first.asks.items()]),
            payload_hash="restart",
        ),
        base[2],
    ]
    resumed = _replay_book(resumed_rows, until=start + timedelta(seconds=1))
    gap_label = label_counterfactual(
        CounterfactualMakerOrder(
            "fault",
            "event",
            "asset",
            "BUY",
            Decimal("0.4"),
            Decimal(1),
            Decimal(1),
            Decimal(60),
        ),
        MakerEvidenceWindow(l2_complete=False, terminal_reason="disconnect"),
    )
    checks = {
        "duplicate_event_idempotent": _same_book(expected, duplicate),
        "out_of_order_input_deterministic": _same_book(expected, out_of_order),
        "restart_checkpoint_deterministic": _same_book(expected, resumed),
        "disconnect_forces_abstain": gap_label.label_class == "AMBIGUOUS_ABSTAIN",
        "cross_hour_replay_ordered": expected.observed_at.date() > start.date(),
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "evidence_class": "DETERMINISTIC_FAULT_INJECTION",
        "checks": checks,
    }


def _accounting_evidence() -> dict[str, Any]:
    deterministic_paths = (
        Path("reports/simulator_acceptance/paper-position-operations-latest.json"),
        Path("reports/simulator_acceptance/paper-fee-engine-20260818.json"),
        Path("reports/simulator_acceptance/paper-ctf-settlement-latest.json"),
        Path("reports/simulator_acceptance/paper-account-return-20260818.json"),
        Path(
            "reports/simulator_acceptance/paper-complete-set-cost-basis-20260818.json"
        ),
    )
    deterministic = []
    for path in deterministic_paths:
        payload = _read_json(path)
        deterministic.append(
            {
                "path": str(path.resolve()),
                "sha256": _sha256(path) if path.exists() else None,
                "status": payload.get("status", "MISSING"),
                "live_submission_performed": payload.get(
                    "live_submission_performed", False
                ),
            }
        )
    official_path = Path(
        "runtime_outputs/account_truth/latest/reconciliation-summary.json"
    )
    official = _read_json(official_path)
    historical = _historical_account_truth_evidence(
        Path("runtime_outputs/account_truth")
    )
    deterministic_pass = all(item["status"] == "PASS" for item in deterministic)
    return {
        "classification": "DETERMINISTIC_ACCOUNTING_PASS"
        if deterministic_pass
        else "BLOCKED",
        "deterministic_artifacts": deterministic,
        "official_account_truth": {
            "path": str(official_path.resolve()),
            "sha256": _sha256(official_path) if official_path.exists() else None,
            "account_truth_gate": official.get("account_truth_gate", "MISSING"),
            "official_source_status": official.get("official_source_status", "MISSING"),
            "comparison_scope": official.get("comparison_scope"),
            "comparison_item_count": official.get("comparison_item_count"),
        },
        "historical_calibration_delta": historical,
        "boundary": (
            "deterministic paper accounting is accepted; historical official "
            "calibration "
            "is linked without replacing the current independent account-truth gate"
        ),
    }


def _historical_account_truth_evidence(root: Path) -> dict[str, Any]:
    required_fields = {
        "size",
        "gross_initial_value",
        "entry_fees_usdc",
        "realized_pnl",
        "cash_balance",
    }
    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    for path in root.glob("*/latest/reconciliation-summary.json"):
        payload = _read_json(path)
        if payload.get("comparison_scope") != "CALIBRATION_DELTA":
            continue
        if int(payload.get("comparison_item_count") or 0) <= 0:
            continue
        candidates.append((str(payload.get("as_of") or ""), path, payload))
    if not candidates:
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "reason": "no historical CALIBRATION_DELTA account-truth report",
        }

    _, path, payload = max(candidates, key=lambda item: (item[0], str(item[1])))
    items_path = path.with_name("reconciliation-items.jsonl")
    rows: list[dict[str, Any]] = []
    if items_path.exists():
        for line in items_path.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    matched_fields = {
        str(row.get("field_name"))
        for row in rows
        if row.get("record_type") == "FIELD_COMPARISON" and row.get("status") == "MATCH"
    }
    summary = payload.get("summary") or {}
    operation_delta_pass = (
        required_fields <= matched_fields
        and int(summary.get("material_mismatch_count") or 0) == 0
    )
    return {
        "status": "PASS" if operation_delta_pass else "INSUFFICIENT_EVIDENCE",
        "path": str(path.resolve()),
        "sha256": _sha256(path),
        "items_path": str(items_path.resolve()),
        "items_sha256": _sha256(items_path) if items_path.exists() else None,
        "account_truth_gate": payload.get("account_truth_gate"),
        "official_source_status": payload.get("official_source_status"),
        "comparison_scope": payload.get("comparison_scope"),
        "comparison_item_count": payload.get("comparison_item_count"),
        "matched_fields": sorted(matched_fields),
        "material_mismatch_count": int(summary.get("material_mismatch_count") or 0),
        "retryable_mismatch_count": int(summary.get("retryable_mismatch_count") or 0),
        "boundary": (
            "operation deltas match; official mark/value timing-lag rows remain "
            "independent source observations"
        ),
    }


def _maker_error_metrics(rows: Sequence[ShadowEvaluationRow]) -> dict[str, Any]:
    strict = [row for row in rows if row.label.label_class == "STRICT_CONFIRMED_FILL"]
    size_errors = [
        abs(row.prediction.expected_filled_size - row.label.filled_size_lower)
        for row in strict
    ]
    time_errors = [
        abs(
            row.prediction.expected_time_to_fill_seconds
            - row.label.observed_time_to_fill_seconds
        )
        for row in strict
        if row.prediction.expected_time_to_fill_seconds is not None
        and row.label.observed_time_to_fill_seconds is not None
    ]
    return {
        "strict_fill_size_mae": _decimal_mean(size_errors),
        "strict_time_to_fill_mae_seconds": _decimal_mean(time_errors),
        "strict_metric_count": len(strict),
        "time_metric_count": len(time_errors),
    }


def _select_baseline(
    rows: Sequence[Mapping[str, Any]],
    hour_start: datetime,
    horizon_seconds: int,
    *,
    preferred_side: str,
    minimum_size: Decimal,
    exchange_batches: Sequence[
        tuple[datetime, tuple[dict[str, Any], ...]]
    ]
    | None = None,
) -> tuple[Mapping[str, Any], str]:
    latest = hour_start + timedelta(hours=1) - timedelta(seconds=horizon_seconds + 1)
    candidates: list[tuple[Mapping[str, Any], tuple[str, ...]]] = []
    required_visible = max(Decimal("0.000001"), minimum_size) / Decimal("0.10")
    batches = exchange_batches or _exchange_timestamp_batches(rows)
    for effective_at, batch in batches:
        if effective_at > latest:
            break
        for row in batch:
            if row["event_type"] != "book":
                continue
            bids = _parse_levels(row.get("bids"))
            asks = _parse_levels(row.get("asks"))
            if not bids or not asks:
                continue
            eligible: list[str] = []
            if asks[min(asks)] >= required_visible:
                eligible.append("BUY")
            if bids[max(bids)] >= required_visible:
                eligible.append("SELL")
            if eligible:
                candidate_row = dict(row)
                candidate_row["timestamp_received"] = effective_at
                candidates.append((candidate_row, tuple(eligible)))
    if not candidates:
        raise ValueError(
            "no full WS book baseline satisfies horizon and small-order domain"
        )
    row, eligible = candidates[-1]
    side = preferred_side if preferred_side in eligible else eligible[0]
    return row, side


def _replay_book(
    rows: Sequence[Mapping[str, Any]],
    *,
    until: datetime,
    exchange_batches: Sequence[
        tuple[datetime, tuple[dict[str, Any], ...]]
    ]
    | None = None,
) -> BookState:
    bids: dict[Decimal, Decimal] = {}
    asks: dict[Decimal, Decimal] = {}
    generation = 0
    observed_at: datetime | None = None
    batches = exchange_batches or _exchange_timestamp_batches(rows)
    for effective_at, batch in batches:
        if effective_at > until:
            break
        for row in batch:
            event_type = str(row["event_type"])
            if event_type == "book":
                bids = _parse_levels(row.get("bids"))
                asks = _parse_levels(row.get("asks"))
                generation += 1
                observed_at = effective_at
            elif event_type == "price_change" and generation:
                target = _side_book(str(row.get("side") or ""), bids, asks)
                price = _decimal(row.get("price"))
                size = _decimal(row.get("size"))
                if target is None or price is None or size is None:
                    continue
                if size <= 0:
                    target.pop(price, None)
                else:
                    target[price] = size
                observed_at = effective_at
    if generation <= 0 or observed_at is None:
        raise ValueError("book baseline not available before checkpoint")
    return BookState(dict(bids), dict(asks), observed_at, generation)


def _checkpoint(
    candidate: BenchmarkCandidate,
    book: BookState,
    at: datetime,
    source_files: Sequence[str],
) -> ArrivalBookCheckpoint:
    checkpoint_id = hashlib.sha256(
        f"{candidate.asset_id}|{at.isoformat()}|{book.generation}|{book.observed_at.isoformat()}".encode()
    ).hexdigest()[:24]
    return ArrivalBookCheckpoint(
        checkpoint_id=checkpoint_id,
        asset_id=candidate.asset_id,
        market_id=candidate.market_id,
        condition_id=candidate.condition_id,
        observed_at=book.observed_at,
        generation=book.generation,
        coverage_grade="A",
        bids=tuple(
            PaperBookLevel(price, size)
            for price, size in sorted(book.bids.items(), reverse=True)
        ),
        asks=tuple(
            PaperBookLevel(price, size) for price, size in sorted(book.asks.items())
        ),
        has_gap=False,
        source_files=tuple(source_files),
    )


def _walk_book(
    book: BookState, *, side: str, size: Decimal, limit_price: Decimal
) -> list[tuple[Decimal, Decimal]]:
    levels = book.asks if side == "BUY" else book.bids
    ordered = sorted(levels.items(), reverse=side == "SELL")
    remaining = size
    fills: list[tuple[Decimal, Decimal]] = []
    for price, available in ordered:
        if (side == "BUY" and price > limit_price) or (
            side == "SELL" and price < limit_price
        ):
            break
        fill = min(remaining, available)
        if fill > 0:
            fills.append((price, fill))
            remaining -= fill
        if remaining <= 0:
            break
    return fills


def _compatible_trades(
    rows: Sequence[Mapping[str, Any]],
    order: CounterfactualMakerOrder,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, Decimal]]:
    result: list[tuple[datetime, Decimal]] = []
    for row in rows:
        ts = _parse_datetime(row.get("block_time"))
        if ts is None or ts < start or ts > end:
            continue
        side_code = int(row.get("side_code") or 0)
        price = Decimal(str(row.get("price") or 0))
        compatible = (
            order.side == "BUY" and side_code == 2 and price <= order.price
        ) or (order.side == "SELL" and side_code == 1 and price >= order.price)
        if compatible:
            result.append((ts, Decimal(str(row.get("size") or 0))))
    return sorted(result)


def _queue_consumption_time(
    trades: Sequence[tuple[datetime, Decimal]],
    queue_ahead: Decimal,
    start: datetime,
    horizon_seconds: int,
) -> Decimal:
    cumulative = Decimal(0)
    for ts, size in trades:
        cumulative += size
        if cumulative > queue_ahead:
            return Decimal(str(max(0.0, (ts - start).total_seconds())))
    return Decimal(horizon_seconds)


def _maker_l2_evidence(
    rows: Sequence[Mapping[str, Any]],
    initial: BookState,
    *,
    side: str,
    price: Decimal,
    start: datetime,
    end: datetime,
    exchange_batches: Sequence[
        tuple[datetime, tuple[dict[str, Any], ...]]
    ]
    | None = None,
) -> tuple[Decimal, Decimal, bool, bool, bool]:
    bids = dict(initial.bids)
    asks = dict(initial.asks)
    target = bids if side == "BUY" else asks
    removed = Decimal(0)
    replenished = Decimal(0)
    crossed = False
    epoch_stable = True
    l2_complete = True
    batches = exchange_batches or _exchange_timestamp_batches(rows)
    for effective_at, batch in batches:
        if effective_at <= start:
            continue
        if effective_at > end:
            break
        for row in batch:
            event_type = str(row["event_type"])
            if event_type == "book":
                epoch_stable = False
                continue
            if event_type in {
                "connection_disconnected",
                "ws_gap",
                "gap",
                "reconnect",
            }:
                l2_complete = False
                continue
            if event_type != "price_change":
                continue
            changed = _side_book(str(row.get("side") or ""), bids, asks)
            changed_price = _decimal(row.get("price"))
            changed_size = _decimal(row.get("size"))
            if changed is None or changed_price is None or changed_size is None:
                continue
            old_size = changed.get(changed_price, Decimal(0))
            if changed is target and changed_price == price:
                if changed_size < old_size:
                    removed += old_size - max(Decimal(0), changed_size)
                elif changed_size > old_size:
                    replenished += changed_size - old_size
            if changed_size <= 0:
                changed.pop(changed_price, None)
            else:
                changed[changed_price] = changed_size
        if side == "BUY" and asks and min(asks) <= price:
            crossed = True
        if side == "SELL" and bids and max(bids) >= price:
            crossed = True
    return removed, replenished, crossed, epoch_stable, l2_complete


def _small_order_size(visible: Decimal, minimum: Decimal) -> Decimal:
    cap = min(Decimal(5), visible * Decimal("0.10"))
    required = max(Decimal("0.000001"), minimum)
    if required > cap:
        return Decimal(0)
    return required.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


def _normalize_category(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    return next(
        (
            category
            for category, values in CATEGORY_VALUES.items()
            if normalized in values
        ),
        None,
    )


def _candidate_from_pool(
    row: Mapping[str, Any],
    metadata: Mapping[str, Any],
    category: str,
    prior_activity: Mapping[str, Any],
) -> BenchmarkCandidate:
    payload = dict(row)
    payload["event_id"] = (
        metadata.get("event_id")
        or metadata.get("event_slug")
        or row.get("condition_id")
        or row["market_id"]
    )
    payload["category"] = category
    coverage_payload = "|".join(
        str(row.get(key) or "")
        for key in (
            "asset_id",
            "hour_start",
            "archive_row_count",
            "baseline_received_at",
            "fill_depth_reason",
        )
    )
    payload["coverage_hash"] = hashlib.sha256(coverage_payload.encode()).hexdigest()
    payload["prior_trade_count"] = int(prior_activity.get("trade_count") or 0)
    payload["prior_trade_volume"] = Decimal(
        str(prior_activity.get("trade_volume") or 0)
    )
    return _candidate(payload)


def _candidate(row: Mapping[str, Any]) -> BenchmarkCandidate:
    hour_start = _parse_datetime(row.get("hour_start"))
    if hour_start is None:
        raise ValueError("benchmark candidate hour_start is invalid")
    return BenchmarkCandidate(
        asset_id=str(row["asset_id"]),
        market_id=str(row["market_id"]),
        condition_id=str(row.get("condition_id") or ""),
        event_id=str(row.get("event_id") or row["market_id"]),
        title=str(row.get("market_title") or row.get("title") or ""),
        outcome=str(row.get("outcome_name") or row.get("outcome") or ""),
        category=str(row["category"]),
        hour_start=hour_start,
        archive_shard_ids=tuple(
            sorted(int(value) for value in (row.get("archive_shard_ids") or ()))
        ),
        connection_shard_id=(
            int(row["connection_shard_id"])
            if row.get("connection_shard_id") is not None
            else None
        ),
        tick_size=Decimal(
            str(row.get("current_tick_size") or row.get("tick_size") or "0.001")
        ),
        min_order_size=Decimal(str(row.get("min_order_size") or "5")),
        coverage_hash=str(row["coverage_hash"]),
        prior_trade_count=int(row.get("prior_trade_count") or 0),
        prior_trade_volume=Decimal(str(row.get("prior_trade_volume") or 0)),
    )


def _event(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["timestamp_received"] = _utc(row["timestamp_received"])
    result["timestamp_exchange"] = (
        _parse_datetime(row.get("timestamp_exchange")) or result["timestamp_received"]
    )
    result["event_type"] = str(row.get("event_type") or "")
    result["filename"] = str(row.get("filename") or "")
    return result


def _dedupe_events(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (
            row["timestamp_received"],
            _int_or_zero(row.get("collector_seq")),
            _int_or_zero(row.get("sequence_in_message")),
            _int_or_zero(row.get("change_index")),
            _int_or_zero(row.get("raw_frame_seq")),
        ),
    )
    for row in ordered:
        key = (
            row.get("payload_hash"),
            row.get("event_type"),
            row.get("price"),
            row.get("size"),
            row.get("side"),
            row.get("change_index"),
            row.get("timestamp_received"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _exchange_timestamp_batches(
    rows: Iterable[Mapping[str, Any]],
) -> list[tuple[datetime, tuple[dict[str, Any], ...]]]:
    """Commit one exchange-timestamp group only after its final row arrived."""

    grouped: dict[tuple[int, datetime], list[dict[str, Any]]] = defaultdict(list)
    for row in _dedupe_events(rows):
        received_at = _utc(row["timestamp_received"])
        exchange_at = _parse_datetime(row.get("timestamp_exchange")) or received_at
        key = (_int_or_zero(row.get("raw_connection_generation")), exchange_at)
        grouped[key].append(row)
    batches: list[tuple[datetime, tuple[dict[str, Any], ...]]] = []
    for rows_at_timestamp in grouped.values():
        ordered = tuple(
            sorted(
                rows_at_timestamp,
                key=lambda row: (
                    _int_or_zero(row.get("raw_frame_seq")),
                    _int_or_zero(row.get("sequence_in_message")),
                    _int_or_zero(row.get("change_index")),
                    _int_or_zero(row.get("collector_seq")),
                ),
            )
        )
        effective_at = max(_utc(row["timestamp_received"]) for row in ordered)
        batches.append((effective_at, ordered))
    return sorted(
        batches,
        key=lambda item: (
            item[0],
            _int_or_zero(item[1][0].get("raw_frame_seq")),
        ),
    )


def _parse_levels(value: Any) -> dict[Decimal, Decimal]:
    if value is None or str(value).strip() in {"", "<NA>", "nan"}:
        return {}
    parsed = json.loads(str(value))
    result: dict[Decimal, Decimal] = {}
    for row in parsed if isinstance(parsed, list) else ():
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            price = _decimal(row[0])
            size = _decimal(row[1])
            if price is not None and size is not None and price > 0 and size > 0:
                result[price] = size
    return result


def _side_book(
    side: str, bids: dict[Decimal, Decimal], asks: dict[Decimal, Decimal]
) -> dict[Decimal, Decimal] | None:
    normalized = side.upper()
    if normalized in {"BUY", "BID", "BIDS"}:
        return bids
    if normalized in {"SELL", "ASK", "ASKS"}:
        return asks
    return None


def _sample_json(
    sample: ReplaySample,
    row: ShadowEvaluationRow,
    prior: EmpiricalBayesMakerPrior,
    config: EmpiricalBayesMakerConfig,
) -> dict[str, Any]:
    _, _, activity_context = _posterior_activity_forecast(sample, prior, config)
    return {
        "candidate": _json_value(asdict(sample.candidate)),
        "split": sample.split,
        "maker_order": _json_value(asdict(sample.order)),
        "maker_evidence": _json_value(asdict(sample.evidence)),
        "maker_prediction": _json_value(asdict(row.prediction)),
        "maker_label": {
            **_json_value(asdict(row.label)),
            "label_class": row.label.label_class,
        },
        "calibration_stratum": {
            "category": sample.candidate.category,
            "side": sample.order.side,
            "quote_position": sample.order.quote_position,
            "horizon_seconds": str(int(sample.order.horizon_seconds)),
            "queue_bucket": _queue_bucket(sample),
            "independent_trial_id": sample.order.trial_id,
            "forecast_trade_count": sample.forecast_trade_count,
            "forecast_window_seconds": sample.forecast_window_seconds,
            "empirical_bayes_prior_hash": prior.artifact_hash,
            "empirical_bayes_config_hash": config.decision_hash,
            "empirical_bayes_activity": activity_context,
            "horizon_fill_prior_probability": str(
                _horizon_fill_probability(sample.order.horizon_seconds, prior)
            ),
        },
        "taker": _json_value(sample.taker_result),
        "source_files": list(sample.source_files),
        "source_hashes": dict(sample.source_hashes),
    }


def _synthetic_event(
    ts: datetime,
    event_type: str,
    *,
    bids: str | None = None,
    asks: str | None = None,
    side: str | None = None,
    price: str | None = None,
    size: str | None = None,
    payload_hash: str,
) -> dict[str, Any]:
    return {
        "timestamp_received": ts,
        "event_type": event_type,
        "bids": bids,
        "asks": asks,
        "side": side,
        "price": price,
        "size": size,
        "payload_hash": payload_hash,
        "collector_seq": 0,
        "sequence_in_message": 0,
        "change_index": 0,
        "raw_frame_seq": 0,
    }


def _same_book(left: BookState, right: BookState) -> bool:
    return left.bids == right.bids and left.asks == right.asks


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value) in {"", "<NA>", "nan"}:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    try:
        if value is None or str(value) in {"", "<NA>", "nan"}:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _utc(value)
    text = str(value).replace("Z", "+00:00")
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _decimal_mean(values: Sequence[Decimal]) -> str | None:
    return str(sum(values, Decimal(0)) / Decimal(len(values))) if values else None


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_value(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _markdown(report: Mapping[str, Any]) -> str:
    split = report["split_manifest"]
    maker = report["maker"]["holdout"]
    walk_forward = report["maker"]["walk_forward"]
    walk_gate = walk_forward["probability_research_gate"]
    walk_metrics = walk_forward["aggregate"].get("metrics") or {}
    holdout_metrics = maker.get("metrics") or {}
    holdout_gate = report["maker"]["probability_research_gate"]
    watermark = report.get("orderfilled_watermark") or {}
    accounting = report["accounting"]
    matrix = report["maker"]["matrix_coverage"]
    lines = [
        "# Offline Execution Fidelity Benchmark",
        "",
        f"- Status: `{report['status']}`",
        f"- Evidence: `{report['evidence_scope']}`",
        "- Live submission performed: `false`",
        f"- Samples: `{report['sample_count']}`",
        f"- Failures: `{report['failure_count']}`",
        f"- Out-of-domain scenario skips: `{report['scenario_skip_count']}`",
        "",
        "## Classifications",
        "",
        *(f"- {name}: `{value}`" for name, value in report["classifications"].items()),
        "",
        "## Honest Boundary",
        "",
        *(
            f"- Not established: `{claim}`"
            for claim in report["claims_not_established"]
        ),
        "",
        "## Coverage",
        "",
        *(
            f"- {category}: `{count}`"
            for category, count in report["category_counts"].items()
        ),
        "",
        f"- Taker differential: `{report['taker']['status']}`",
        f"- External model differential: `{report['differential']['status']}`",
        f"- Fault injection: `{report['fault_injection']['status']}`",
        f"- UTC/event split: `{split['status']}`",
        f"- Event leakage: `{split['event_leakage_count']}`",
        f"- Maker quote/horizon matrix: `{matrix['status']}`",
        f"- Quote positions: `{matrix['quote_position_counts']}`",
        f"- Resting horizons: `{matrix['horizon_counts']}`",
        f"- Queue buckets: `{matrix['queue_bucket_counts']}`",
        "",
        "## Maker Rolling-Origin",
        "",
        f"- Evaluated folds: `{walk_forward['fold_count']}`",
        f"- Independent proxy trials: `{walk_gate['proxy_count']}`",
        f"- Strict fill proxies: `{walk_gate['strict_confirmed_fill_count']}`",
        f"- Brier: `{walk_gate['proxy_brier']}`",
        f"- Brier skill: `{walk_gate['brier_skill_score']}`",
        f"- ECE: `{walk_gate['proxy_ece']}`",
        f"- Reliability bins calibrated: `{walk_gate['checks']['reliability_bins_calibrated']}`",
        f"- Accepted probability domain: `{walk_gate['calibrated_probability_domain']}`",
        f"- High-probability domain: `{walk_gate['high_probability_domain']['status']}`",
        (
            "- Naive base fill rate: "
            f"`{walk_metrics.get('base_fill_rate_independent_trial_proxy')}`"
        ),
        "",
        "## Frozen Final Holdout",
        "",
        f"- Samples: `{maker['sample_count']}`",
        f"- Independent trials: `{maker['independent_trial_count']}`",
        f"- Independent proxy labels: `{maker['independent_proxy_count']}`",
        f"- Strict confirmed fills: `{maker['strict_confirmed_fill_count']}`",
        f"- Observed-tape proxy no-fills: `{maker['observed_tape_no_fill_count']}`",
        f"- Ambiguous abstentions: `{maker['ambiguous_abstain_count']}`",
        (
            "- Base fill rate: "
            f"`{holdout_metrics.get('base_fill_rate_independent_trial_proxy')}`"
        ),
        (f"- Brier: `{holdout_metrics.get('brier_score_independent_trial_proxy')}`"),
        f"- ECE: `{holdout_metrics.get('ece_independent_trial_proxy')}`",
        f"- Probability promotion gate: `{holdout_gate['status']}`",
        f"- Frozen holdout starts: `{split.get('holdout_start')}`",
        f"- OrderFilled watermark: `{watermark.get('block_time')}`",
        (
            "- Truth status: `WAITING_FOR_ORDERFILLED_WATERMARK`"
            if int(maker.get("independent_proxy_count") or 0) == 0
            else (
                "- Truth status: `NEGATIVE_ONLY_HOLDOUT`"
                if int(maker.get("independent_strict_confirmed_fill_count") or 0) == 0
                else "- Truth status: `POSITIVE_AND_NEGATIVE_PROXY_LABELS_AVAILABLE`"
            )
        ),
        "",
        "## Accounting Evidence",
        "",
        f"- Deterministic accounting: `{accounting['classification']}`",
        (
            "- Historical official calibration delta: "
            f"`{accounting['historical_calibration_delta']['status']}`"
        ),
        (
            "- Current official-only snapshot: "
            f"`{accounting['official_account_truth']['account_truth_gate']}`"
        ),
    ]
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--max-days", type=int, default=9)
    parser.add_argument("--per-category-per-day", type=int, default=1)
    parser.add_argument("--max-samples", type=int, default=48)
    parser.add_argument("--latency-ms", type=int, default=250)
    parser.add_argument(
        "--maker-holdout-start",
        default="",
        help=(
            "UTC date whose rows and later rows are frozen final holdout; "
            "earlier rows are split into train/calibration."
        ),
    )
    parser.add_argument(
        "--maker-horizon-seconds",
        type=int,
        default=None,
        help="Legacy single horizon override; otherwise the horizon matrix is used.",
    )
    parser.add_argument(
        "--maker-horizons-seconds",
        default=",".join(str(value) for value in DEFAULT_MAKER_HORIZONS_SECONDS),
        help="Comma-separated resting-horizon matrix.",
    )
    parser.add_argument(
        "--maker-quote-positions",
        default=",".join(DEFAULT_MAKER_QUOTE_POSITIONS),
        help="Comma-separated AT_BEST/ONE_TICK_BEHIND/ONE_TICK_INSIDE_SPREAD.",
    )
    parser.add_argument("--nautilus-env", default="polymonitor-nautilus312")
    parser.add_argument(
        "--refresh-candidates",
        action="store_true",
        help="Replace the matching immutable candidate manifest.",
    )
    parser.add_argument(
        "--print-full-report",
        action="store_true",
        help="Print every sample row instead of the compact acceptance summary.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    end = _parse_datetime(args.end) if args.end else datetime.now(UTC)
    assert end is not None
    start = _parse_datetime(args.start) if args.start else end - timedelta(days=14)
    assert start is not None
    report = run_benchmark(
        archive_root=args.archive_root,
        cache_root=args.cache_root,
        output_root=args.output_root,
        start=start,
        end=end,
        max_days=args.max_days,
        per_category_per_day=args.per_category_per_day,
        latency_ms=args.latency_ms,
        maker_horizon_seconds=args.maker_horizon_seconds,
        maker_horizons_seconds=tuple(
            int(value.strip())
            for value in str(args.maker_horizons_seconds).split(",")
            if value.strip()
        ),
        maker_quote_positions=tuple(
            value.strip()
            for value in str(args.maker_quote_positions).split(",")
            if value.strip()
        ),
        max_samples=args.max_samples,
        nautilus_env=args.nautilus_env,
        reuse_candidate_manifest=not args.refresh_candidates,
        maker_holdout_start=(
            date.fromisoformat(str(args.maker_holdout_start))
            if str(args.maker_holdout_start).strip()
            else None
        ),
    )
    printable = report
    if not args.print_full_report:
        printable = {
            "status": report["status"],
            "evidence_scope": report["evidence_scope"],
            "live_submission_performed": report["live_submission_performed"],
            "classifications": report["classifications"],
            "candidate_count": report["candidate_count"],
            "sample_count": report["sample_count"],
            "failure_count": report["failure_count"],
            "scenario_skip_count": report["scenario_skip_count"],
            "scenario_skip_counts": report["scenario_skip_counts"],
            "category_counts": report["category_counts"],
            "split_status": report["split_manifest"]["status"],
            "taker_status": report["taker"]["status"],
            "differential_status": report["differential"]["status"],
            "fault_injection_status": report["fault_injection"]["status"],
            "maker_matrix_status": report["maker"]["matrix_coverage"]["status"],
            "maker_matrix_counts": report["maker"]["matrix_coverage"],
            "claims_not_established": report["claims_not_established"],
            "summary_json": str((args.output_root / "summary.json").resolve()),
            "summary_markdown": str((args.output_root / "summary.md").resolve()),
        }
    print(json.dumps(_json_value(printable), indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS_OFFLINE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
