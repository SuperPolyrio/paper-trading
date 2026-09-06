from datetime import datetime, timezone
from decimal import Decimal

from quant.maker.candidate_planner import (
    enrich_candidates_with_gamma_activity,
    enrich_candidates_with_recent_public_trades,
    plan_stratified_holdout_campaign,
    preselect_maker_candidates,
    rank_maker_candidates,
    reconcile_candidates_with_rest_books,
)


def _candidate(asset_id: str, queue: str) -> dict[str, object]:
    return {
        "asset_id": asset_id,
        "market_id": f"market-{asset_id}",
        "condition_id": f"condition-{asset_id}",
        "market_title": f"Market {asset_id}",
        "outcome_name": "YES",
        "tick_size": "0.01",
        "min_order_size": "5",
        "best_bid": "0.10",
        "best_ask": "0.11",
        "bids": [["0.10", queue]],
        "asks": [["0.11", "10"]],
    }


class Evidence:
    volumes = {
        "no-fill": "300",
        "partial": "720",
        "full": "1200",
    }

    def summarize_compatible_maker_volume(self, *, asset_id: str, **kwargs):
        return {
            "trade_count": 5,
            "compatible_trade_volume": self.volumes[asset_id],
            "last_trade_at": "2026-08-16T00:00:00Z",
        }


class UnreadyEvidence:
    def summarize_compatible_maker_volume(self, **_kwargs):
        return {
            "source": "clickhouse_orderfilled_delayed",
            "source_ready": False,
            "trade_count": 0,
            "compatible_trade_volume": "0",
            "last_trade_at": None,
            "live_coverage_reason": "redundant_health_window_incomplete",
        }


def test_candidate_planner_targets_all_maker_outcomes_without_submit() -> None:
    report = rank_maker_candidates(
        [
            _candidate("no-fill", "10"),
            _candidate("partial", "10"),
            _candidate("full", "10"),
        ],
        evidence_client=Evidence(),
        side="BUY",
        placement="AT_BEST",
        order_size=Decimal("5"),
        resting_seconds=Decimal("60"),
        observed_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )

    assert report["status"] == "FORECAST_READY"
    assert report["evidence_status"] == "FORECAST_ONLY_LIVE_ORDER_REQUIRED"
    assert report["exchange_submit_called"] is False
    assert report["predicted_outcome_counts"] == {
        "NO_FILL": 1,
        "PARTIAL": 1,
        "FULL": 1,
    }
    assert report["recommended_predictions"]["NO_FILL"]["asset_id"] == "no-fill"
    assert report["recommended_predictions"]["PARTIAL"]["asset_id"] == "partial"
    assert report["recommended_predictions"]["FULL"]["asset_id"] == "full"
    assert all(row["prediction_is_live_truth"] is False for row in report["candidates"])


def test_preselection_prioritizes_recent_direction_compatible_public_trade() -> None:
    observed = datetime(2026, 8, 16, tzinfo=timezone.utc)
    candidates = enrich_candidates_with_recent_public_trades(
        [_candidate("quiet", "0"), _candidate("active", "0")],
        [
            {
                "asset": "active",
                "side": "SELL",
                "price": "0.10",
                "size": "9",
                "timestamp": int(observed.timestamp()) - 10,
                "transactionHash": "0xtrade",
            }
        ],
        observed_at=observed,
        lookback_seconds=60,
    )

    selected, rejected = preselect_maker_candidates(
        candidates,
        side="BUY",
        placement="AT_BEST",
        order_size=Decimal(5),
        max_notional_usd=Decimal(5),
        limit=2,
    )

    assert rejected == []
    assert selected[0]["asset_id"] == "active"
    assert selected[0]["recent_public_compatible_trade_count"] == 1
    assert selected[0]["recent_public_compatible_trade_volume"] == Decimal(9)


def test_stratified_campaign_balances_matrix_without_submit() -> None:
    candidates = [
        _candidate("no-fill", "10"),
        _candidate("partial", "10"),
        _candidate("full", "10"),
    ]
    for candidate in candidates:
        candidate.update(
            category="politics",
            best_bid="0.10",
            best_ask="0.13",
            bids=[["0.10", "10"], ["0.09", "8"]],
            asks=[["0.13", "10"], ["0.14", "8"]],
        )

    report = plan_stratified_holdout_campaign(
        candidates,
        evidence_client=Evidence(),
        sides=("BUY", "SELL"),
        placements=(
            "AT_BEST",
            "ONE_TICK_BEHIND",
            "ONE_TICK_INSIDE_SPREAD",
        ),
        horizons_seconds=(60,),
        candidates_per_stratum=10,
        observed_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )

    assert report["status"] == "CAMPAIGN_READY"
    assert report["exchange_submit_called"] is False
    assert report["automatic_submission_allowed"] is False
    assert all(report["checks"].values())
    assert report["predicted_outcome_counts"]["PARTIAL"] > 0
    assert report["predicted_outcome_counts"]["FULL"] > 0
    assert all(
        row["authenticated_own_order_truth_required"] is True
        and row["predicted_outcome_is_truth"] is False
        for row in report["candidates"]
    )


def test_candidate_planner_rejects_minimum_size_above_notional_limit() -> None:
    candidate = _candidate("too-large", "10")
    candidate["min_order_size"] = "20"

    report = rank_maker_candidates(
        [candidate],
        evidence_client=Evidence(),
        order_size=Decimal("5"),
        observed_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )

    assert report["status"] == "NO_CANDIDATES"
    assert report["rejected"][0]["reason"] == "minimum_size_exceeds_notional_limit"


def test_candidate_planner_rejects_incomplete_trade_evidence_window() -> None:
    report = rank_maker_candidates(
        [_candidate("not-ready", "10")],
        evidence_client=UnreadyEvidence(),
        order_size=Decimal("5"),
        observed_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )

    assert report["status"] == "NO_CANDIDATES"
    assert report["rejected"][0] == {
        "asset_id": "not-ready",
        "market_id": "market-not-ready",
        "reason": "maker_trade_evidence_not_ready",
        "forecast_source": "clickhouse_orderfilled_delayed",
        "forecast_status": "EVIDENCE_NOT_READY",
        "coverage_reason": "redundant_health_window_incomplete",
        "live_source_error": None,
    }


def test_rest_reconciliation_replaces_stale_catalog_tick_and_book() -> None:
    candidate = _candidate("asset", "10")
    candidate["tick_size"] = "0.001"
    candidate["best_bid"] = "0.830"
    candidate["best_ask"] = "0.840"

    [reconciled] = reconcile_candidates_with_rest_books(
        [candidate],
        {
            "asset": {
                "asset_id": "asset",
                "market": "condition",
                "timestamp": "1",
                "hash": "book-hash",
                "tick_size": "0.01",
                "min_order_size": "5",
                "bids": [{"price": "0.83", "size": "7"}],
                "asks": [{"price": "0.84", "size": "8"}],
            }
        },
        observed_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )

    assert reconciled["local_tick_size"] == "0.001"
    assert reconciled["tick_size"] == "0.01"
    assert reconciled["best_bid"] == Decimal("0.83")
    assert reconciled["best_ask"] == Decimal("0.84")
    assert reconciled["rest_reconciled"] is True


def test_candidate_planner_rejects_missing_rest_book() -> None:
    [candidate] = reconcile_candidates_with_rest_books([_candidate("asset", "10")], {})

    report = rank_maker_candidates(
        [candidate],
        evidence_client=Evidence(),
        observed_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )

    assert report["status"] == "NO_CANDIDATES"
    assert report["rejected"][0]["reason"] == "rest_book_missing"


def test_gamma_activity_prioritizes_traded_inside_spread_candidate() -> None:
    quiet = _candidate("quiet", "1")
    quiet.update(best_bid="0.10", best_ask="0.13")
    active = _candidate("active", "100")
    active.update(best_bid="0.20", best_ask="0.23")
    rows = enrich_candidates_with_gamma_activity(
        [quiet, active],
        [
            {
                "id": "market-active",
                "active": True,
                "closed": False,
                "enableOrderBook": True,
                "acceptingOrders": True,
                "volume24hrClob": 5000,
                "liquidityClob": 2000,
            }
        ],
    )

    selected, rejected = preselect_maker_candidates(
        rows,
        side="BUY",
        placement="ADAPTIVE_FRONT",
        order_size=Decimal(5),
        max_notional_usd=Decimal(5),
        limit=2,
    )

    assert not rejected
    assert selected[0]["asset_id"] == "active"
    assert selected[0]["resolved_placement"] == "ONE_TICK_INSIDE_SPREAD"
    assert selected[0]["gamma_volume_24h"] == Decimal(5000)


def test_adaptive_preselection_uses_best_when_spread_is_one_tick() -> None:
    candidate = _candidate("asset", "10")
    candidate["rest_reconciled"] = True

    selected, rejected = preselect_maker_candidates(
        [candidate],
        side="BUY",
        placement="ADAPTIVE_FRONT",
        order_size=Decimal("5"),
        max_notional_usd=Decimal("1"),
        limit=10,
    )

    assert rejected == []
    assert selected[0]["resolved_placement"] == "AT_BEST"
    assert selected[0]["preselection_limit_price"] == Decimal("0.10")
    assert selected[0]["preselection_queue_ahead"] == Decimal("10")
