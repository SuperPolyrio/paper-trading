from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from quant.core.metadata import derive_clickhouse_token_id_hex
from quant.maker.trade_evidence import (
    CachedMakerTradeEvidenceClient,
    maker_evidence_request_key,
)
from quant.paper.paired_probe import OrderFilledEvidenceClient


class FakeClickHouseClient:
    settings = SimpleNamespace(orderfilled_table="order_filled")

    def query_json_rows(self, query: str, **_kwargs):
        if "ORDER BY block_number DESC" in query:
            return [
                {
                    "block_number": 123,
                    "block_time": "2026-08-26T00:01:00Z",
                }
            ]
        asset_key = derive_clickhouse_token_id_hex("1")
        return [
            {
                "asset_key": asset_key,
                "side_code": 2,
                "price": "0.35",
                "size": "3",
                "block_time": "2026-08-25T23:58:00Z",
            },
            {
                "asset_key": asset_key,
                "side_code": 2,
                "price": "0.45",
                "size": "7",
                "block_time": "2026-08-25T23:59:00Z",
            },
            {
                "asset_key": asset_key,
                "side_code": 1,
                "price": "0.65",
                "size": "5",
                "block_time": "2026-08-25T23:59:30Z",
            },
            {
                "asset_key": asset_key,
                "side_code": 1,
                "price": "0.55",
                "size": "11",
                "block_time": "2026-08-25T23:59:45Z",
            },
        ]


def test_batch_evidence_keeps_side_and_price_levels_independent() -> None:
    buy_key = maker_evidence_request_key(
        asset_id="1", maker_side="BUY", limit_price=Decimal("0.40")
    )
    sell_key = maker_evidence_request_key(
        asset_id="1", maker_side="SELL", limit_price=Decimal("0.60")
    )
    summaries = OrderFilledEvidenceClient(
        client=FakeClickHouseClient()
    ).summarize_compatible_maker_volume_batch(
        [
            {
                "request_key": buy_key,
                "asset_id": "1",
                "maker_side": "BUY",
                "limit_price": "0.40",
            },
            {
                "request_key": sell_key,
                "asset_id": "1",
                "maker_side": "SELL",
                "limit_price": "0.60",
            },
        ],
        start=datetime(2026, 8, 25, 23, 55, tzinfo=timezone.utc),
        end=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )

    assert summaries[buy_key]["source_ready"] is True
    assert summaries[buy_key]["trade_count"] == 1
    assert summaries[buy_key]["compatible_trade_volume"] == "3"
    assert summaries[sell_key]["trade_count"] == 1
    assert summaries[sell_key]["compatible_trade_volume"] == "5"

    cached = CachedMakerTradeEvidenceClient(summaries)
    buy = cached.summarize_compatible_maker_volume(
        asset_id="1", maker_side="BUY", limit_price=Decimal("0.400")
    )
    sell = cached.summarize_compatible_maker_volume(
        asset_id="1", maker_side="SELL", limit_price=Decimal("0.6000")
    )
    assert buy["compatible_trade_volume"] == "3"
    assert sell["compatible_trade_volume"] == "5"


def test_orderfilled_window_batch_partitions_rows_by_asset() -> None:
    rows = OrderFilledEvidenceClient(
        client=FakeClickHouseClient()
    ).fetch_windows(
        asset_ids=["1", "2"],
        start=datetime(2026, 8, 25, 23, 55, tzinfo=timezone.utc),
        end=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )

    assert len(rows["1"]) == 4
    assert rows["2"] == []
    assert all("asset_key" not in row for row in rows["1"])


def test_cached_evidence_fails_closed_for_unrequested_level() -> None:
    cached = CachedMakerTradeEvidenceClient({})

    result = cached.summarize_compatible_maker_volume(
        asset_id="1", maker_side="BUY", limit_price=Decimal("0.40")
    )

    assert result["source_ready"] is False
    assert result["coverage_reason"] == "maker_level_missing_from_batch"
