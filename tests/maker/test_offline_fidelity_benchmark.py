from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from quant.maker.offline_fidelity_benchmark import (
    BenchmarkCandidate,
    BookState,
    ReplaySample,
    XueL2ArchiveAdapter,
    _apply_probability_odds_multiplier,
    _calibrate_empirical_bayes_model,
    _fault_injection_matrix,
    _fit_empirical_bayes_maker_prior,
    _historical_account_truth_evidence,
    _maker_probability_config,
    _maker_probability_research_gate,
    _maker_quote,
    _matrix_coverage,
    _parse_sshfs_source,
    _posterior_activity_forecast,
    _replay_book,
    _rolling_origin_maker_evaluation,
    _scenario_skip_reason,
    _select_baseline,
    _shadow_row,
    _small_order_size,
    split_candidates,
)
from quant.maker.offline_fidelity_benchmark import (
    _candidate as candidate_from_mapping,
)
from quant.maker.offline_shadow import CounterfactualMakerOrder, MakerEvidenceWindow

UTC = timezone.utc


def test_parse_sshfs_source_uses_active_mount_route() -> None:
    host, root = _parse_sshfs_source(
        "hy@10.7.7.223:/mnt/hdd22t/prediction-market-quant/lob_l2_archive_full"
    )

    assert host == "hy@10.7.7.223"
    assert root == Path(
        "/mnt/hdd22t/prediction-market-quant/lob_l2_archive_full"
    )


def _candidate(event_id: str, day: int) -> BenchmarkCandidate:
    return BenchmarkCandidate(
        asset_id=f"asset-{event_id}-{day}",
        market_id=f"market-{event_id}",
        condition_id=f"condition-{event_id}",
        event_id=event_id,
        title="test",
        outcome="YES",
        category="politics",
        hour_start=datetime(2026, 8, day, 12, tzinfo=UTC),
        archive_shard_ids=(1,),
        connection_shard_id=1,
        tick_size=Decimal("0.001"),
        min_order_size=Decimal(5),
        coverage_hash=f"hash-{event_id}-{day}",
        prior_trade_count=0,
        prior_trade_volume=Decimal(0),
    )


def _maker_sample(
    *,
    event_id: str,
    day: int,
    split: str,
    horizon_seconds: int,
    forecast_trade_count: int,
    compatible_trade_size: Decimal = Decimal(0),
) -> ReplaySample:
    candidate = _candidate(event_id, day)
    order = CounterfactualMakerOrder(
        shadow_order_id=f"maker-{event_id}-{horizon_seconds}s",
        event_id=event_id,
        asset_id=candidate.asset_id,
        side="BUY",
        price=Decimal("0.4"),
        size=Decimal(5),
        queue_ahead_estimate=Decimal(0),
        horizon_seconds=Decimal(horizon_seconds),
        quote_position="AT_BEST",
        trial_id=f"maker-{event_id}",
    )
    observed_pre_volume = Decimal(forecast_trade_count) * Decimal(10)
    return ReplaySample(
        candidate=candidate,
        split=split,
        order=order,
        evidence=MakerEvidenceWindow(
            compatible_trade_size=compatible_trade_size,
            terminal_reason="horizon_elapsed",
            observed_seconds=Decimal(horizon_seconds),
        ),
        forecast_trade_volume=(
            observed_pre_volume / Decimal(300) * Decimal(horizon_seconds)
        ),
        taker_case={"case_id": event_id},
        taker_result={"case_id": event_id},
        source_files=(),
        source_hashes={},
        forecast_trade_count=forecast_trade_count,
        forecast_window_seconds=300,
    )


def test_split_drops_event_crossing_date_boundaries() -> None:
    candidates = [
        _candidate("shared", 1),
        _candidate("train-only", 1),
        _candidate("calibration-only", 2),
        _candidate("shared", 3),
        _candidate("holdout-only", 3),
    ]

    mapping, manifest = split_candidates(candidates)

    assert manifest["status"] == "PASS"
    assert manifest["event_leakage_count"] == 0
    assert manifest["excluded_cross_boundary_events"] == ["shared"]
    assert all(event_id != "shared" for event_id, _ in mapping)


def test_split_honors_frozen_holdout_start() -> None:
    candidates = [
        _candidate("train", 1),
        _candidate("calibration", 2),
        _candidate("unseen-holdout", 3),
    ]

    mapping, manifest = split_candidates(
        candidates,
        holdout_start=date(2026, 8, 3),
    )

    assert mapping[("train", date(2026, 8, 1))] == "train"
    assert mapping[("calibration", date(2026, 8, 2))] == "calibration"
    assert mapping[("unseen-holdout", date(2026, 8, 3))] == "holdout"
    assert manifest["holdout_start"] == "2026-08-03"
    assert manifest["status"] == "PASS"


def test_missing_routed_archive_is_an_explicit_abstention() -> None:
    reason = _scenario_skip_reason(
        FileNotFoundError(
            "no routed XUE parquet for asset at 2026-08-17T14:00:00+00:00"
        )
    )

    assert reason == "MISSING_ROUTED_ARCHIVE_EVIDENCE"


def test_empirical_bayes_prior_keeps_zero_count_window_probabilistic() -> None:
    train = [
        _maker_sample(
            event_id=f"train-{index}",
            day=1,
            split="train",
            horizon_seconds=300,
            forecast_trade_count=2,
            compatible_trade_size=Decimal(10 if index == 0 else 0),
        )
        for index in range(3)
    ]
    target = _maker_sample(
        event_id="holdout-zero",
        day=3,
        split="holdout",
        horizon_seconds=300,
        forecast_trade_count=0,
    )
    prior = _fit_empirical_bayes_maker_prior([*train, target])
    config = _maker_probability_config(
        prior,
        activity_multiplier=Decimal(1),
        prior_exposure_seconds=Decimal(300),
        raw_probability_weight=Decimal("0.5"),
    )

    volume, arrival_probability, context = _posterior_activity_forecast(
        target, prior, config
    )
    row = _shadow_row(target, prior, config)

    assert volume > 0
    assert arrival_probability > 0
    assert context["activity_bucket"] == "GLOBAL"
    assert row.prediction.fill_probability > 0
    assert row.prediction.domain_status == "RESEARCH_EMPIRICAL_BAYES_CANDIDATE"


def test_probability_odds_multiplier_calibrates_without_changing_boundaries() -> None:
    assert _apply_probability_odds_multiplier(Decimal(0), Decimal("1.5")) == 0
    assert _apply_probability_odds_multiplier(Decimal(1), Decimal("1.5")) == 1
    assert _apply_probability_odds_multiplier(Decimal("0.2"), Decimal(2)) == Decimal(
        "0.3333333333333333333333333333"
    )


def test_probability_odds_multiplier_is_frozen_in_config_hash() -> None:
    samples = [
        _maker_sample(
            event_id=f"train-{index}",
            day=1,
            split="train",
            horizon_seconds=300,
            forecast_trade_count=1,
        )
        for index in range(3)
    ]
    prior = _fit_empirical_bayes_maker_prior(samples)
    first = _maker_probability_config(
        prior,
        activity_multiplier=Decimal(1),
        prior_exposure_seconds=Decimal(120),
        raw_probability_weight=Decimal("0.5"),
        probability_odds_multiplier=Decimal(1),
    )
    second = _maker_probability_config(
        prior,
        activity_multiplier=Decimal(1),
        prior_exposure_seconds=Decimal(120),
        raw_probability_weight=Decimal("0.5"),
        probability_odds_multiplier=Decimal("1.5"),
    )

    assert first.decision_hash != second.decision_hash


def test_empirical_bayes_fit_and_selection_ignore_holdout_labels() -> None:
    train = [
        _maker_sample(
            event_id=f"train-{index}",
            day=1,
            split="train",
            horizon_seconds=300,
            forecast_trade_count=index % 3,
            compatible_trade_size=Decimal(10 if index == 0 else 0),
        )
        for index in range(6)
    ]
    calibration = [
        _maker_sample(
            event_id=f"calibration-{index}",
            day=2,
            split="calibration",
            horizon_seconds=300,
            forecast_trade_count=index % 2,
            compatible_trade_size=Decimal(10 if index == 0 else 0),
        )
        for index in range(4)
    ]
    holdout_no_fill = _maker_sample(
        event_id="holdout",
        day=3,
        split="holdout",
        horizon_seconds=300,
        forecast_trade_count=0,
    )
    holdout_fill = _maker_sample(
        event_id="holdout",
        day=3,
        split="holdout",
        horizon_seconds=300,
        forecast_trade_count=0,
        compatible_trade_size=Decimal(10),
    )
    first_samples = [*train, *calibration, holdout_no_fill]
    second_samples = [*train, *calibration, holdout_fill]

    first_prior = _fit_empirical_bayes_maker_prior(first_samples)
    second_prior = _fit_empirical_bayes_maker_prior(second_samples)
    first_config, _ = _calibrate_empirical_bayes_model(first_samples, first_prior)
    second_config, _ = _calibrate_empirical_bayes_model(second_samples, second_prior)

    assert first_prior.artifact_hash == second_prior.artifact_hash
    assert first_config.decision_hash == second_config.decision_hash


def test_rolling_origin_excludes_frozen_final_holdout() -> None:
    samples = [
        _maker_sample(
            event_id=f"event-{day}",
            day=day,
            split="holdout",
            horizon_seconds=300,
            forecast_trade_count=day % 2,
            compatible_trade_size=Decimal(10 if day in {1, 3} else 0),
        )
        for day in range(1, 6)
    ]

    report = _rolling_origin_maker_evaluation(
        samples,
        final_holdout_start=date(2026, 8, 5),
    )

    assert report["fold_count"] == 1
    assert report["folds"][0]["target_date"] == "2026-08-04"
    assert report["final_holdout_start"] == "2026-08-05"
    assert all(fold.get("target_date") != "2026-08-05" for fold in report["folds"])


def test_candidate_manifest_round_trip_preserves_frozen_candidate() -> None:
    candidate = _candidate("round-trip", 1)

    restored = candidate_from_mapping(
        json.loads(json.dumps(asdict(candidate), default=str))
    )

    assert restored == candidate


def test_select_baseline_falls_back_to_liquid_side() -> None:
    hour = datetime(2026, 8, 1, 12, tzinfo=UTC)
    rows = [
        {
            "event_type": "book",
            "timestamp_received": hour + timedelta(minutes=10),
            "bids": '[["0.40","100"]]',
            "asks": '[["0.60","10"]]',
        }
    ]

    baseline, side = _select_baseline(
        rows,
        hour,
        60,
        preferred_side="BUY",
        minimum_size=Decimal(5),
    )

    assert baseline == rows[0]
    assert side == "SELL"


def test_select_baseline_rejects_shallow_book() -> None:
    hour = datetime(2026, 8, 1, 12, tzinfo=UTC)
    rows = [
        {
            "event_type": "book",
            "timestamp_received": hour + timedelta(minutes=10),
            "bids": '[["0.40","49"]]',
            "asks": '[["0.60","49"]]',
        }
    ]

    with pytest.raises(ValueError, match="small-order domain"):
        _select_baseline(
            rows,
            hour,
            60,
            preferred_side="BUY",
            minimum_size=Decimal(5),
        )


def test_replay_book_commits_exchange_timestamp_batch_atomically() -> None:
    hour = datetime(2026, 8, 27, 2, tzinfo=UTC)
    exchange_at = hour + timedelta(minutes=34, seconds=39, milliseconds=430)
    rows = [
        {
            "event_type": "book",
            "timestamp_received": hour,
            "timestamp_exchange": hour,
            "bids": '[["0.74","10"]]',
            "asks": '[["0.75","10"],["0.76","10"]]',
            "raw_connection_generation": 1,
            "raw_frame_seq": 1,
        },
        {
            "event_type": "price_change",
            "timestamp_received": exchange_at + timedelta(microseconds=100),
            "timestamp_exchange": exchange_at,
            "side": "SELL",
            "price": "0.76",
            "size": "0",
            "raw_connection_generation": 1,
            "raw_frame_seq": 2,
        },
        {
            "event_type": "price_change",
            "timestamp_received": exchange_at + timedelta(microseconds=200),
            "timestamp_exchange": exchange_at,
            "side": "SELL",
            "price": "0.75",
            "size": "0",
            "raw_connection_generation": 1,
            "raw_frame_seq": 3,
        },
        {
            "event_type": "price_change",
            "timestamp_received": exchange_at + timedelta(microseconds=300),
            "timestamp_exchange": exchange_at,
            "side": "SELL",
            "price": "0.77",
            "size": "10",
            "raw_connection_generation": 1,
            "raw_frame_seq": 4,
        },
    ]

    before_batch_commit = _replay_book(
        rows,
        until=exchange_at + timedelta(microseconds=250),
    )
    after_batch_commit = _replay_book(
        rows,
        until=exchange_at + timedelta(microseconds=300),
    )

    assert min(before_batch_commit.asks) == Decimal("0.75")
    assert min(after_batch_commit.asks) == Decimal("0.77")
    assert Decimal("0.75") not in after_batch_commit.asks
    assert Decimal("0.76") not in after_batch_commit.asks


def test_small_order_respects_minimum_and_participation_cap() -> None:
    assert _small_order_size(Decimal(100), Decimal(5)) == Decimal("5.000000")
    assert _small_order_size(Decimal(49), Decimal(5)) == 0


def test_maker_quote_positions_remain_post_only() -> None:
    book = BookState(
        bids={Decimal("0.40"): Decimal(10), Decimal("0.39"): Decimal(4)},
        asks={Decimal("0.43"): Decimal(12), Decimal("0.44"): Decimal(5)},
        observed_at=datetime(2026, 8, 1, tzinfo=UTC),
        generation=1,
    )

    assert _maker_quote(
        book,
        side="BUY",
        tick_size=Decimal("0.01"),
        quote_position="AT_BEST",
    ) == (Decimal("0.40"), Decimal(10))
    assert _maker_quote(
        book,
        side="BUY",
        tick_size=Decimal("0.01"),
        quote_position="ONE_TICK_BEHIND",
    ) == (Decimal("0.39"), Decimal(4))
    assert _maker_quote(
        book,
        side="BUY",
        tick_size=Decimal("0.01"),
        quote_position="ONE_TICK_INSIDE_SPREAD",
    ) == (Decimal("0.41"), Decimal(0))
    with pytest.raises(ValueError, match="cross or lock"):
        _maker_quote(
            BookState(
                bids={Decimal("0.40"): Decimal(10)},
                asks={Decimal("0.41"): Decimal(12)},
                observed_at=book.observed_at,
                generation=1,
            ),
            side="BUY",
            tick_size=Decimal("0.01"),
            quote_position="ONE_TICK_INSIDE_SPREAD",
        )


def test_matrix_coverage_requires_positions_horizons_sides_and_queue_buckets() -> None:
    samples: list[ReplaySample] = []
    for index, (side, position, horizon, queue) in enumerate(
        (
            ("BUY", "AT_BEST", 30, Decimal(0)),
            ("SELL", "ONE_TICK_BEHIND", 120, Decimal(10)),
            ("BUY", "ONE_TICK_INSIDE_SPREAD", 300, Decimal(100)),
        )
    ):
        candidate = _candidate(f"event-{index}", index + 1)
        samples.append(
            ReplaySample(
                candidate=candidate,
                split="holdout",
                order=CounterfactualMakerOrder(
                    shadow_order_id=f"order-{index}",
                    event_id=candidate.event_id,
                    asset_id=candidate.asset_id,
                    side=side,
                    price=Decimal("0.4"),
                    size=Decimal(5),
                    queue_ahead_estimate=queue,
                    horizon_seconds=Decimal(horizon),
                    quote_position=position,
                ),
                evidence=MakerEvidenceWindow(),
                forecast_trade_volume=Decimal(0),
                taker_case={"case_id": f"case-{index}"},
                taker_result={"case_id": f"case-{index}"},
                source_files=(),
                source_hashes={},
            )
        )

    report = _matrix_coverage(
        samples,
        requested_quote_positions=(
            "AT_BEST",
            "ONE_TICK_BEHIND",
            "ONE_TICK_INSIDE_SPREAD",
        ),
        requested_horizons=(30, 120, 300),
    )

    assert report["status"] == "PASS"
    assert all(report["checks"].values())


def test_fault_injection_matrix_is_deterministic() -> None:
    report = _fault_injection_matrix()

    assert report["status"] == "PASS"
    assert all(report["checks"].values())


def test_probability_research_gate_blocks_high_false_positive_uncertainty() -> None:
    report = _maker_probability_research_gate(
        {
            "strict_confirmed_fill_count": 12,
            "observed_tape_no_fill_count": 60,
            "metrics": {
                "brier_score_observed_tape_proxy": "0.08",
                "ece_observed_tape_proxy": "0.05",
                "brier_skill_score_independent_trial_proxy": "0.25",
                "reliability_bins_independent_trial_proxy": [
                    {
                        "sample_count": 72,
                        "mean_predicted_probability": "0.20",
                        "observed_fill_wilson_95": {
                            "lower": 0.10,
                            "upper": 0.30,
                        },
                    }
                ],
                "false_positive_fill_proxy_upper_95": {
                    "total": 20,
                    "upper": 0.60,
                },
            },
        }
    )

    assert report["status"] == "BLOCKED"
    assert report["classification"] == "RESEARCH_CALIBRATION_NOT_PROMOTED"
    assert report["high_probability_domain"]["status"] == "BLOCKED_UNSAFE"


def test_probability_research_gate_accepts_bounded_proxy_error() -> None:
    report = _maker_probability_research_gate(
        {
            "strict_confirmed_fill_count": 30,
            "observed_tape_no_fill_count": 70,
            "metrics": {
                "brier_score_observed_tape_proxy": "0.10",
                "ece_observed_tape_proxy": "0.05",
                "brier_skill_score_independent_trial_proxy": "0.25",
                "reliability_bins_independent_trial_proxy": [
                    {
                        "sample_count": 100,
                        "mean_predicted_probability": "0.30",
                        "observed_fill_wilson_95": {
                            "lower": 0.20,
                            "upper": 0.40,
                        },
                    }
                ],
                "false_positive_fill_proxy_upper_95": {
                    "total": 40,
                    "upper": 0.20,
                },
            },
        }
    )

    assert report["status"] == "PASS"
    assert report["classification"] == "RESEARCH_CALIBRATED"
    assert all(report["checks"].values())
    assert report["high_probability_domain"]["status"] == "CALIBRATED"


def test_probability_gate_accepts_only_low_probability_domain_when_sparse() -> None:
    report = _maker_probability_research_gate(
        {
            "independent_strict_confirmed_fill_count": 7,
            "independent_observed_tape_no_fill_count": 152,
            "independent_trial_count": 188,
            "metrics": {
                "brier_score_independent_trial_proxy": "0.025",
                "ece_independent_trial_proxy": "0.03",
                "brier_skill_score_independent_trial_proxy": "0.35",
                "reliability_bins_independent_trial_proxy": [
                    {
                        "sample_count": 159,
                        "mean_predicted_probability": "0.04",
                        "observed_fill_wilson_95": {
                            "lower": 0.02,
                            "upper": 0.07,
                        },
                    }
                ],
                "false_positive_fill_independent_trial_upper_95": {
                    "total": 3,
                    "upper": 0.56,
                },
            },
        }
    )

    assert report["status"] == "PASS"
    assert report["classification"] == "RESEARCH_CALIBRATED_LOW_PROBABILITY_DOMAIN"
    assert report["calibrated_probability_domain"] == "[0,0.5)"
    assert (
        report["high_probability_domain"]["status"] == "DISABLED_INSUFFICIENT_EVIDENCE"
    )


def test_probability_gate_blocks_miscalibrated_low_probability_bin() -> None:
    report = _maker_probability_research_gate(
        {
            "independent_strict_confirmed_fill_count": 7,
            "independent_observed_tape_no_fill_count": 152,
            "metrics": {
                "brier_score_independent_trial_proxy": "0.025",
                "ece_independent_trial_proxy": "0.03",
                "brier_skill_score_independent_trial_proxy": "0.35",
                "reliability_bins_independent_trial_proxy": [
                    {
                        "sample_count": 159,
                        "mean_predicted_probability": "0.001",
                        "observed_fill_wilson_95": {
                            "lower": 0.01,
                            "upper": 0.07,
                        },
                    }
                ],
                "false_positive_fill_independent_trial_upper_95": {
                    "total": 3,
                    "upper": 0.56,
                },
            },
        }
    )

    assert report["status"] == "BLOCKED"
    assert report["checks"]["reliability_bins_calibrated"] is False
    assert report["reliability_gate"]["failed_bin_count"] == 1


def test_probability_gate_prefers_independent_trial_metrics() -> None:
    report = _maker_probability_research_gate(
        {
            "strict_confirmed_fill_count": 80,
            "observed_tape_no_fill_count": 80,
            "independent_strict_confirmed_fill_count": 1,
            "independent_observed_tape_no_fill_count": 1,
            "independent_trial_count": 2,
            "metrics": {
                "brier_score_observed_tape_proxy": "0.01",
                "false_positive_fill_proxy_upper_95": {
                    "total": 100,
                    "upper": 0.10,
                },
                "brier_score_independent_trial_proxy": "0.30",
                "ece_independent_trial_proxy": "0.20",
                "brier_skill_score_independent_trial_proxy": "-0.1",
                "reliability_bins_independent_trial_proxy": [],
                "false_positive_fill_independent_trial_upper_95": {
                    "total": 2,
                    "upper": 0.90,
                },
            },
        }
    )

    assert report["status"] == "BLOCKED"
    assert report["proxy_count"] == 2
    assert report["predicted_positive_trials"] == 2
    assert report["row_strict_confirmed_fill_count"] == 80
    assert report["checks"]["minimum_independent_proxy_trials"] is False
    assert report["checks"]["minimum_observed_fill_trials"] is False


def test_xue_adapter_uses_only_sha_verified_filtered_cache(tmp_path) -> None:
    archive_root = tmp_path / "archive"
    cache_root = tmp_path / "cache"
    archive_root.mkdir()
    filtered = cache_root / "filtered"
    filtered.mkdir(parents=True)
    candidate = _candidate("cached", 1)
    target = filtered / f"{candidate.coverage_hash[:16]}-evidence.parquet"
    target.write_bytes(b"verified-cache")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".parquet.cache.json").write_text(
        json.dumps(
            {
                "asset_id": candidate.asset_id,
                "hour_start": candidate.hour_start.isoformat(),
                "filtered_sha256": digest,
                "source_files": [
                    {"path": "dt=2026-08-01/hour=12/source.parquet", "sha256": "a" * 64}
                ],
            }
        )
    )
    adapter = XueL2ArchiveAdapter(archive_root, cache_root=cache_root)

    assert adapter._cached_filtered(candidate) == target
    assert (
        adapter._source_hashes[
            str(archive_root / "dt=2026-08-01/hour=12/source.parquet")
        ]
        == "a" * 64
    )

    target.write_bytes(b"tampered")
    with pytest.raises(FileNotFoundError, match="verified filtered cache"):
        adapter._cached_filtered(candidate)


def test_xue_adapter_filters_a_shared_batch_cache_by_asset(tmp_path) -> None:
    archive_root = tmp_path / "archive"
    cache_root = tmp_path / "cache"
    filtered = cache_root / "filtered"
    batches = filtered / "batches"
    archive_root.mkdir()
    batches.mkdir(parents=True)
    candidate = _candidate("batch", 1)
    other_asset = "asset-other"
    relative_source = Path("dt=2026-08-01/hour=12/source.parquet")
    target = batches / "shared.parquet"
    target_sql = str(target).replace("'", "''")
    source_sql = str(Path("/remote/archive") / relative_source).replace("'", "''")
    con = duckdb.connect(":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (?, ?, ?, 'book', '[[\"0.4\",\"10\"]]',
                 '[[\"0.6\",\"10\"]]', NULL, NULL, NULL, '0.4', '0.6',
                 'target-payload', 'target-book', 'ws', 1, 1, 0, 1, 1,
                 '{source_sql}'),
                (?, ?, ?, 'book', '[[\"0.3\",\"10\"]]',
                 '[[\"0.7\",\"10\"]]', NULL, NULL, NULL, '0.3', '0.7',
                 'other-payload', 'other-book', 'ws', 2, 1, 0, 1, 2,
                 '{source_sql}')
              ) AS rows(
                asset_id, timestamp_received, timestamp_exchange, event_type,
                bids, asks, price, size, side, best_bid, best_ask, payload_hash,
                book_hash, source, collector_seq, sequence_in_message,
                change_index, raw_connection_generation, raw_frame_seq,
                source_file
              )
            ) TO '{target_sql}' (FORMAT PARQUET)
            """,
            [
                candidate.asset_id,
                candidate.hour_start,
                candidate.hour_start,
                other_asset,
                candidate.hour_start,
                candidate.hour_start,
            ],
        )
    finally:
        con.close()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    metadata_path = (
        filtered / f"{candidate.coverage_hash[:16]}-shared-batch.parquet.cache.json"
    )
    metadata_path.write_text(
        json.dumps(
            {
                "asset_id": candidate.asset_id,
                "batch_asset_count": 2,
                "filtered_path": str(target),
                "filtered_sha256": digest,
                "hour_start": candidate.hour_start.isoformat(),
                "source_files": [
                    {"path": relative_source.as_posix(), "sha256": "a" * 64}
                ],
            }
        )
    )
    adapter = XueL2ArchiveAdapter(
        archive_root,
        cache_root=cache_root,
        remote_root=Path("/remote/archive"),
    )

    rows, source_files, source_hashes = adapter.read_candidate(candidate)

    expected_source = str(archive_root / relative_source)
    assert [row["payload_hash"] for row in rows] == ["target-payload"]
    assert source_files == (expected_source,)
    assert source_hashes == {expected_source: "a" * 64}


def test_xue_batch_skips_one_missing_asset_without_dropping_hour(
    tmp_path, monkeypatch
) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    adapter = XueL2ArchiveAdapter(archive_root, cache_root=tmp_path / "cache")
    available = [_candidate("available-a", 1), _candidate("available-b", 1)]
    missing = _candidate("missing", 1)
    captured: list[BenchmarkCandidate] = []

    def no_cache(_candidate):
        raise FileNotFoundError

    def source_files(_start, _end, candidate):
        if candidate is missing:
            raise FileNotFoundError
        return [str(archive_root / f"{candidate.asset_id}.parquet")]

    def extract(
        _files, *, candidates, candidate_source_files, start, end
    ):
        del candidate_source_files, start, end
        captured.extend(candidates)

    monkeypatch.setattr(adapter, "_cached_filtered", no_cache)
    monkeypatch.setattr(adapter, "_files", source_files)
    monkeypatch.setattr(adapter, "_extract_filtered_batch", extract)

    adapter.prepare_candidates([*available, missing])

    assert captured == available


def test_xue_empty_batch_scan_is_a_cached_abstention(tmp_path) -> None:
    archive_root = tmp_path / "archive"
    filtered = tmp_path / "cache" / "filtered"
    archive_root.mkdir()
    filtered.mkdir(parents=True)
    candidate = _candidate("empty-scan", 1)
    batch = filtered / "batch.parquet"
    batch.write_bytes(b"batch-evidence")
    digest = hashlib.sha256(batch.read_bytes()).hexdigest()
    metadata = filtered / (
        f"{candidate.coverage_hash[:16]}-batch-v2.parquet.cache.json"
    )
    metadata.write_text(
        json.dumps(
            {
                "asset_id": candidate.asset_id,
                "batch_filtered_path": str(batch),
                "batch_filtered_sha256": digest,
                "empty_scan": True,
                "hour_start": candidate.hour_start.isoformat(),
                "source_files": [
                    {"path": "dt=2026-08-01/hour=12/source.parquet", "sha256": "a" * 64}
                ],
            }
        )
    )
    adapter = XueL2ArchiveAdapter(
        archive_root, cache_root=tmp_path / "cache"
    )

    with pytest.raises(RuntimeError, match="no routed XUE parquet"):
        adapter.read_candidate(candidate)


def test_historical_account_truth_requires_five_matched_fields(
    tmp_path,
) -> None:
    report_dir = tmp_path / "calrun-test" / "latest"
    report_dir.mkdir(parents=True)
    (report_dir / "reconciliation-summary.json").write_text(
        """{
          "as_of": "2026-08-23T12:46:29+00:00",
          "account_truth_gate": "PASS_WITH_TIMING_LAG",
          "official_source_status": "PASS_WITH_TIMING_LAG",
          "comparison_scope": "CALIBRATION_DELTA",
          "comparison_item_count": 5,
          "summary": {"material_mismatch_count": 0, "retryable_mismatch_count": 2}
        }"""
    )
    rows = [
        {
            "record_type": "FIELD_COMPARISON",
            "field_name": field,
            "status": "MATCH",
        }
        for field in (
            "size",
            "gross_initial_value",
            "entry_fees_usdc",
            "realized_pnl",
            "cash_balance",
        )
    ]
    (report_dir / "reconciliation-items.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n"
    )

    result = _historical_account_truth_evidence(tmp_path)

    assert result["status"] == "PASS"
    assert result["material_mismatch_count"] == 0
    assert set(result["matched_fields"]) == {row["field_name"] for row in rows}
