from datetime import datetime, timezone
from decimal import Decimal

from quant.adapters.polymarket_data_trades_client import PolymarketDataTradesClient


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


def test_recent_public_trade_activity_filters_direction_price_and_time() -> None:
    session = _Session(
        [
            {
                "asset": "asset",
                "side": "SELL",
                "price": 0.40,
                "size": 3,
                "timestamp": 1000,
                "transactionHash": "0x1",
            },
            {
                "asset": "asset",
                "side": "SELL",
                "price": 0.41,
                "size": 5,
                "timestamp": 1010,
                "transactionHash": "0x2",
            },
            {
                "asset": "asset",
                "side": "BUY",
                "price": 0.39,
                "size": 100,
                "timestamp": 1010,
            },
            {
                "asset": "other",
                "side": "SELL",
                "price": 0.39,
                "size": 100,
                "timestamp": 1010,
            },
        ]
    )
    client = PolymarketDataTradesClient(session=session)

    result = client.summarize_compatible_maker_activity(
        condition_id="condition",
        asset_id="asset",
        maker_side="BUY",
        limit_price=Decimal("0.405"),
        lookback_seconds=60,
        observed_at=datetime.fromtimestamp(1020, tz=timezone.utc),
    )

    assert result["compatible_trade_count"] == 1
    assert result["compatible_trade_volume"] == "3"
    assert result["median_trade_size"] == "3"
    assert result["last_compatible_trade_age_seconds"] == "20.0"
    assert result["prediction_truth_claimed"] is False
    assert result["own_order_execution_truth_claimed"] is False
    assert session.calls[0][1]["params"]["takerOnly"] == "true"


def test_recent_public_trade_fetch_can_scan_the_global_tape() -> None:
    session = _Session([])
    client = PolymarketDataTradesClient(session=session)

    assert client.fetch_recent_taker_trades(limit=10) == ()
    params = session.calls[0][1]["params"]
    assert params == {"limit": 10, "takerOnly": "true"}
