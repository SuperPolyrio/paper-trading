from datetime import datetime, timezone
from decimal import Decimal
from contextlib import contextmanager

from quant.calibration.market_taxonomy import normalize_market_domain
from quant.calibration.representative_sampling import (
    apply_live_shadow_status,
    build_representative_sampling_report,
    decorate_sampling_candidate,
    select_representative_candidates,
)
from quant.calibration.representative_watchlist import (
    reconcile_representative_watchlist,
    validate_representative_plan,
)


def _candidate(
    market_id: str,
    category: str,
    *,
    bid: str = "0.49",
    ask: str = "0.50",
    tick: str = "0.01",
    activity: int = 10,
    ready: bool = True,
    prior: int = 0,
    event_slug: str | None = None,
    outcome_name: str = "YES",
) -> dict:
    return decorate_sampling_candidate(
        {
            "market_id": market_id,
            "asset_id": f"asset-{market_id}",
            "condition_id": f"condition-{market_id}",
            "market_slug": f"market-{market_id}",
            "market_title": f"Market {market_id}",
            "outcome_name": outcome_name,
            "event_slug": event_slug,
            "source_category": category,
            "best_bid": bid,
            "best_ask": ask,
            "tick_size": tick,
            "min_order_size": "1",
            "activity_event_count": activity,
            "watchlisted": ready,
            "rest_book_match": ready,
            "checkpoint_observed_at": "2026-07-23T00:00:00Z" if ready else None,
            "coverage_grade": "A",
            "redundant_feed_match": True,
            "prior_calibratable_count": prior,
            "prior_submitted_count": prior,
        }
    )


def test_market_taxonomy_normalizes_fine_grained_categories() -> None:
    assert normalize_market_domain("world-elections") == "politics"
    assert normalize_market_domain("geopolitics") == "politics"
    assert normalize_market_domain("soccer") == "sports"
    assert normalize_market_domain("temperature") == "weather"
    assert normalize_market_domain("ethereum") == "crypto"
    assert normalize_market_domain("unknown", market_title="Will Bitcoin exceed $200k?") == "crypto"
    assert normalize_market_domain("unknown", market_title="An unrelated market") == "other"


def test_candidate_decoration_builds_microstructure_buckets() -> None:
    candidate = _candidate("1", "weather", bid="0.08", ask="0.10", tick="0.01")

    assert candidate["category_group"] == "weather"
    assert candidate["midpoint"] == Decimal("0.09")
    assert candidate["price_bucket"] == "0.01-0.10"
    assert candidate["spread_bucket"] == "2_3_ticks"
    assert candidate["runtime_ready"] is True


def test_selector_covers_domains_and_avoids_reusing_markets() -> None:
    candidates = [
        _candidate("1", "politics", bid="0.04", ask="0.05", activity=200),
        _candidate("2", "politics", bid="0.49", ask="0.50", activity=100),
        _candidate("3", "politics", bid="0.93", ask="0.95", activity=20),
        _candidate("4", "soccer"),
        _candidate("5", "weather"),
        _candidate("6", "crypto"),
    ]

    selected = select_representative_candidates(
        candidates,
        domains=("politics", "sports", "weather", "crypto"),
        per_domain=1,
    )

    assert [row["category_group"] for row in selected] == [
        "politics",
        "sports",
        "weather",
        "crypto",
    ]
    assert len({row["market_id"] for row in selected}) == 4
    assert len({row["event_key"] for row in selected}) == 4


def test_selector_does_not_treat_sibling_markets_as_independent_events() -> None:
    candidates = [
        _candidate("1", "politics", event_slug="same-election", activity=500),
        _candidate("2", "politics", event_slug="same-election", activity=400),
        _candidate("3", "politics", event_slug="different-election", activity=10),
    ]

    selected = select_representative_candidates(
        candidates,
        domains=("politics",),
        per_domain=3,
    )

    assert len(selected) == 2
    assert {row["event_key"] for row in selected} == {
        "event:same-election",
        "event:different-election",
    }


def test_report_surfaces_domain_quota_shortfall_without_authorizing_orders() -> None:
    report = build_representative_sampling_report(
        [_candidate("1", "politics"), _candidate("2", "soccer")],
        per_domain=1,
    )

    assert report["exchange_order_submitted"] is False
    assert report["watchlist_modified"] is False
    assert report["domain_summary"]["politics"]["quota_status"] == "MET"
    assert report["domain_summary"]["sports"]["quota_status"] == "MET"
    assert report["domain_summary"]["weather"]["quota_status"] == "SHORTFALL"
    assert report["domain_summary"]["crypto"]["quota_status"] == "SHORTFALL"
    assert report["event_uniqueness_required"] is True


def test_watchlist_plan_validation_requires_distinct_markets_and_assets() -> None:
    report = build_representative_sampling_report(
        [
            _candidate("1", "politics"),
            _candidate("2", "soccer"),
            _candidate("3", "weather"),
            _candidate("4", "crypto"),
        ],
        per_domain=1,
    )

    rows = validate_representative_plan(report)

    assert len(rows) == 4
    assert {row["domain"] for row in rows} == {
        "politics",
        "sports",
        "weather",
        "crypto",
    }


def test_live_shadow_status_replaces_historical_checkpoint_readiness() -> None:
    candidate = _candidate("1", "weather", ready=True)
    now = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    status = {
        "updated_at": "2026-07-23T09:59:59Z",
        "transport_state": "REDUNDANT",
        "route_states": {"primary": "CONNECTED", "secondary": "CONNECTED"},
        "last_error": None,
        "fresh_book_sample": [
            {
                "asset_id": candidate["asset_id"],
                "observed_at": "2026-07-23T09:59:59Z",
                "book_status": "READY",
                "coverage_grade": "A",
                "has_gap": False,
                "best_bid": "0.40",
                "best_ask": "0.41",
                "checkpoint_id": "cp-live",
            }
        ],
    }

    ready = apply_live_shadow_status([candidate], status, now=now)[0]
    missing = apply_live_shadow_status(
        [candidate],
        {**status, "fresh_book_sample": []},
        now=now,
    )[0]

    assert ready["runtime_ready"] is True
    assert ready["shadow_checkpoint_id"] == "cp-live"
    assert ready["best_bid"] == "0.40"
    assert missing["runtime_ready"] is False
    assert missing["runtime_readiness_reason"] == "FRESH_TRUSTED_HEAD_MISSING"


def test_live_shadow_status_never_accepts_a_stale_transport_projection() -> None:
    candidate = _candidate("1", "weather", ready=True)
    now = datetime(2026, 7, 23, 10, 5, tzinfo=timezone.utc)
    status = {
        "updated_at": "2026-07-23T09:59:00Z",
        "transport_state": "REDUNDANT",
        "route_states": {"primary": "CONNECTED", "secondary": "CONNECTED"},
        "last_error": None,
        "fresh_book_sample": [],
    }

    observed = apply_live_shadow_status([candidate], status, now=now)[0]

    assert observed["runtime_ready"] is False
    assert observed["runtime_readiness_reason"] == "SHADOW_STATUS_STALE"


def test_watchlist_reconciliation_preserves_an_existing_owner(monkeypatch) -> None:
    report = build_representative_sampling_report(
        [_candidate("1", "politics")],
        domains=("politics",),
        per_domain=1,
    )

    class Cursor:
        def __init__(self) -> None:
            self.calls = []
            self.rows = []
            self.rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, params=()) -> None:
            self.calls.append((query, params))
            if "SELECT w.asset_id" in query:
                self.rows = [{
                    "asset_id": "asset-1",
                    "strategy_id": "another-strategy",
                    "reason": "owned-elsewhere",
                    "enabled": True,
                }]
            elif "SELECT asset_id, strategy_id" in query:
                self.rows = []
            else:
                self.rows = []
            self.rowcount = 0

        def fetchall(self):
            return list(self.rows)

    cursor = Cursor()

    class Connection:
        def cursor(self):
            return cursor

        def commit(self):
            return None

    @contextmanager
    def connection_factory(*, readonly):
        assert readonly is False
        yield Connection()

    monkeypatch.setattr(
        "quant.calibration.representative_watchlist.postgres_connection",
        connection_factory,
    )

    result = reconcile_representative_watchlist(report, apply=True)
    upsert = next(query for query, _params in cursor.calls if "ON CONFLICT (asset_id)" in query)

    assert "strategy_id=EXCLUDED.strategy_id" not in upsert
    assert "reason=EXCLUDED.reason" not in upsert
    assert result["ownership_preserved_count"] == 1
    assert result["seed_ownership_leased_count"] == 0
    assert result["ownership_replaced_count"] == 0
