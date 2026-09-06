from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quant.paper.replay_service import (
    REPORT_SCHEMA_VERSION,
    ReplayValidationError,
    build_strategy_report,
    parse_replay_timestamp,
)


def test_strategy_report_is_deterministic_and_uses_frozen_evidence() -> None:
    report_input = {
        "nav": [
            {
                "observed_at": "2026-08-01T00:00:00+00:00",
                "equity": "100",
                "conservative_equity": "100",
                "realized_pnl": "0",
                "unrealized_pnl": "0",
                "gross_exposure": "1",
                "drawdown": "0",
                "drawdown_pct": "0",
                "nav_complete": True,
            },
            {
                "observed_at": "2026-08-01T01:00:00+00:00",
                "equity": "105",
                "conservative_equity": "103",
                "realized_pnl": "2",
                "unrealized_pnl": "3",
                "gross_exposure": "7",
                "drawdown": "1",
                "drawdown_pct": "0.01",
                "nav_complete": True,
            },
        ],
        "tca": [
            {
                "fee_cost": "0.1",
                "implementation_shortfall": "0.2",
                "delay_cost": "0.03",
                "requested_size": "10",
                "filled_size": "8",
                "capacity_status": "WITHIN_CAPACITY",
                "fidelity_level": "TAKER_L2_UNCALIBRATED",
            }
        ],
        "fills": [{"notional": "4.5"}],
        "ledger": [
            {
                "market_id": "market-1",
                "event_id": "event-1",
                "category": "politics",
                "realized_pnl_delta": "2.5",
                "fee": "0.1",
            }
        ],
        "lifecycle": [
            {"to_state": "MATCHED_PROVISIONAL"},
            {"to_state": "REJECTED"},
        ],
    }

    first = build_strategy_report(report_input, benchmark="CASH")
    second = build_strategy_report(report_input, benchmark="CASH")

    assert first == second
    assert first["schema_version"] == REPORT_SCHEMA_VERSION
    assert first["performance"]["absolute_return"] == "5"
    assert first["performance"]["return_pct"] == "0.05"
    assert first["performance"]["fee_cost"] == "0.1"
    assert first["performance"]["fill_ratio"] == "0.8"
    assert first["performance"]["latency_slippage"] == "0.03"
    assert first["performance"]["turnover"] is not None
    assert first["risk"]["max_drawdown_pct"] == "0.01"
    assert first["risk"]["completed_order_events"] == 1
    assert first["risk"]["rejected_order_events"] == 1
    assert first["benchmark_comparison"]["status"] == "OUTPERFORMED"
    assert first["data_quality"]["nav_complete"] is True
    assert first["data_quality"]["confidence_coverage"] == "1"
    assert first["attribution"]["category"] == [
        {
            "category": "politics",
            "event_count": 1,
            "fees": "0.1",
            "realized_pnl": "2.5",
        }
    ]
    assert len(first["report_hash"]) == 64
    assert "RECORDED_PAPER_LIFECYCLE_NOT_L2_REMATCH" in first["limitations"]


def test_conservative_nav_benchmark_and_incomplete_quality_are_explicit() -> None:
    report = build_strategy_report(
        {
            "nav": [
                {
                    "observed_at": "2026-08-01T00:00:00+00:00",
                    "equity": "100",
                    "conservative_equity": "100",
                    "nav_complete": True,
                },
                {
                    "observed_at": "2026-08-01T01:00:00+00:00",
                    "equity": "102",
                    "conservative_equity": "103",
                    "nav_complete": False,
                },
            ]
        },
        benchmark="CONSERVATIVE_NAV",
    )

    comparison = report["benchmark_comparison"]
    assert comparison["strategy_return_pct"] == "0.02"
    assert comparison["benchmark_return_pct"] == "0.03"
    assert comparison["excess_return_pct"] == "-0.01"
    assert comparison["status"] == "UNDERPERFORMED"
    assert report["data_quality"]["nav_complete"] is False
    assert report["risk"]["incomplete_nav_snapshots"] == 1


def test_replay_timestamp_requires_timezone_and_normalizes_utc() -> None:
    selected = parse_replay_timestamp(
        "2026-08-01T08:00:00+08:00", field="start_ts"
    )
    assert selected == datetime(2026, 8, 1, tzinfo=timezone.utc)

    with pytest.raises(ReplayValidationError, match="timezone"):
        parse_replay_timestamp("2026-08-01T08:00:00", field="start_ts")
