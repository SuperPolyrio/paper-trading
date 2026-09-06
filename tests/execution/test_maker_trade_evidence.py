from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant.maker.trade_evidence import (
    BestAvailableMakerTradeEvidenceClient,
    PersistedMakerTradeEvidenceClient,
)

NOW = datetime(2026, 8, 16, 4, 0, tzinfo=timezone.utc)


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))

    def fetchone(self):
        return self.rows.pop(0)


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _factory(cursor):
    @contextmanager
    def connection_factory(*, readonly: bool):
        assert readonly is True
        yield _Connection(cursor)

    return connection_factory


def test_persisted_evidence_uses_aggressor_and_price_compatible_with_maker_buy():
    start = NOW - timedelta(hours=1)
    cursor = _Cursor(
        [
            {
                "trade_count": 2,
                "compatible_trade_volume": Decimal("12.5"),
                "last_trade_at": NOW - timedelta(seconds=5),
            },
            {
                "worker_id": "worker-a",
                "transport_state": "REDUNDANT",
                "updated_at": NOW,
            },
            {
                "active": True,
                "assigned_at": start,
                "updated_at": NOW,
            },
            {"stream_event_count": 25},
            {
                "first_at": start,
                "last_at": NOW,
                "max_gap_seconds": Decimal("15"),
                "degraded_samples": 0,
            },
        ]
    )
    client = PersistedMakerTradeEvidenceClient(_factory(cursor))

    result = client.summarize_compatible_maker_volume(
        asset_id="asset-1",
        maker_side="BUY",
        limit_price=Decimal("0.42"),
        start=start,
        end=NOW,
    )

    aggregate_sql, aggregate_params = cursor.calls[0]
    assert "aggressor_side=%s" in aggregate_sql
    assert "price <= %s" in aggregate_sql
    assert aggregate_params == (
        "asset-1",
        start,
        NOW,
        "SELL",
        Decimal("0.42"),
    )
    assert result["source"] == "paper_live_ws_last_trade_price"
    assert result["source_ready"] is True
    assert result["trade_count"] == 2
    assert result["compatible_trade_volume"] == "12.5"
    assert result["coverage_stream_event_count"] == 25
    assert result["asset_assignment_ready"] is True


def test_persisted_evidence_rejects_asset_not_assigned_for_full_window():
    start = NOW - timedelta(hours=1)
    cursor = _Cursor(
        [
            {
                "trade_count": 0,
                "compatible_trade_volume": Decimal(0),
                "last_trade_at": None,
            },
            {
                "worker_id": "worker-a",
                "transport_state": "REDUNDANT",
                "updated_at": NOW,
            },
            {
                "active": True,
                "assigned_at": NOW - timedelta(minutes=5),
                "updated_at": NOW,
            },
        ]
    )
    client = PersistedMakerTradeEvidenceClient(_factory(cursor))

    result = client.summarize_compatible_maker_volume(
        asset_id="asset-never-watched",
        maker_side="BUY",
        limit_price=Decimal("0.42"),
        start=start,
        end=NOW,
    )

    assert result["source_ready"] is False
    assert result["coverage_reason"] == "asset_not_assigned_for_full_window"
    assert result["asset_assignment_ready"] is False


def test_best_available_retains_incomplete_live_counts_for_label_collection():
    class Live:
        def summarize_compatible_maker_volume(self, **_kwargs):
            return {
                "source_ready": False,
                "trade_count": 2,
                "compatible_trade_volume": "7",
                "median_trade_size": "3",
                "coverage_reason": "asset_not_assigned_for_full_window",
            }

    class Delayed:
        def summarize_compatible_maker_volume(self, **_kwargs):
            return {"source_ready": False, "trade_count": 0}

    result = BestAvailableMakerTradeEvidenceClient(
        live_client=Live(),
        delayed_client=Delayed(),
    ).summarize_compatible_maker_volume()

    assert result["source_ready"] is False
    assert result["trade_count"] == 2
    assert result["median_trade_size"] == "3"
    assert result["source"].endswith("incomplete_window")


class _Evidence:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def summarize_compatible_maker_volume(self, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return dict(self.payload)


def test_best_available_evidence_prefers_complete_hot_window():
    hot = _Evidence(
        {
            "source": "paper_live_ws_last_trade_price",
            "source_ready": True,
            "trade_count": 0,
            "compatible_trade_volume": "0",
        }
    )
    delayed = _Evidence({"trade_count": 99, "compatible_trade_volume": "99"})

    result = BestAvailableMakerTradeEvidenceClient(
        hot,
        delayed,
    ).summarize_compatible_maker_volume()

    assert result["source"] == "paper_live_ws_last_trade_price"
    assert result["trade_count"] == 0
    assert delayed.calls == 0


def test_best_available_evidence_marks_empty_delayed_fallback_unready():
    hot = _Evidence(error=RuntimeError("hot table unavailable"))
    delayed = _Evidence(
        {
            "trade_count": 0,
            "compatible_trade_volume": "0",
            "last_trade_at": "",
        }
    )

    result = BestAvailableMakerTradeEvidenceClient(
        hot,
        delayed,
    ).summarize_compatible_maker_volume()

    assert result["source"] == "clickhouse_orderfilled_delayed"
    assert result["source_ready"] is False
    assert result["live_source_error"] == "RuntimeError:hot table unavailable"
