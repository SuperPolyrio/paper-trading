import asyncio
from unittest.mock import patch

import pytest

from quant.maker.hot_preflight import (
    acquire_hot_preflight_candidate,
    align_hot_preflight_candidate,
    capture_targeted_ws_book,
)


class FakeWsClient:
    def __init__(self, messages):
        self.messages = list(messages)
        self.connected = False
        self.closed = False
        self.subscribed = []

    async def connect(self):
        self.connected = True

    async def subscribe(self, asset_ids, **kwargs):
        self.subscribed.append((list(asset_ids), dict(kwargs)))

    async def recv(self):
        return self.messages.pop(0)

    async def close(self):
        self.closed = True


def test_targeted_ws_capture_requires_native_two_sided_book() -> None:
    client = FakeWsClient(
        [
            [
                {
                    "event_type": "book",
                    "asset_id": "asset-1",
                    "timestamp": "1770000000000",
                    "hash": "book-hash",
                    "bids": [
                        {"price": "0.40", "size": "12"},
                        {"price": "0.39", "size": "5"},
                    ],
                    "asks": [{"price": "0.42", "size": "8"}],
                }
            ]
        ]
    )

    result = asyncio.run(
        capture_targeted_ws_book(
            asset_id="asset-1",
            proxy_url="http://127.0.0.1:18080",
            timeout_seconds=1,
            client=client,
        )
    )

    assert result["best_bid"] == "0.40"
    assert result["best_ask"] == "0.42"
    assert result["payload_sha256"]
    assert client.connected and client.closed
    assert client.subscribed[0][0] == ["asset-1"]


def test_targeted_ws_capture_records_current_activity_after_baseline() -> None:
    client = FakeWsClient(
        [
            [
                {
                    "event_type": "book",
                    "asset_id": "asset-1",
                    "bids": [{"price": "0.40", "size": "12"}],
                    "asks": [{"price": "0.42", "size": "8"}],
                }
            ],
            [
                {
                    "event_type": "price_change",
                    "price_changes": [
                        {
                            "asset_id": "asset-1",
                            "price": "0.40",
                            "side": "BUY",
                            "size": "13",
                        }
                    ],
                }
            ],
        ]
    )

    result = asyncio.run(
        capture_targeted_ws_book(
            asset_id="asset-1",
            proxy_url=None,
            timeout_seconds=1,
            activity_observation_seconds=0.5,
            client=client,
        )
    )

    assert result["activity_counts"]["price_change"] == 1
    assert len(result["activity_payload_sha256"]) == 1


def test_hot_preflight_promotes_only_current_probe_and_retains_registry_truth() -> None:
    result = align_hot_preflight_candidate(
        {
            "asset_id": "asset-1",
            "best_bid": "0.40",
            "best_ask": "0.42",
            "coverage_grade": "D",
            "has_gap": True,
            "redundant_feed_match": False,
            "last_receive_ts": "2026-08-28T00:00:00+00:00",
            "book_fingerprint": "gcp-hash",
            "transport_state": "REDUNDANT",
        },
        {
            "asset_id": "asset-1",
            "best_bid": "0.40",
            "best_ask": "0.42",
            "bids": [["0.40", "12"]],
            "asks": [["0.42", "8"]],
            "received_at": "2026-08-28T00:00:01+00:00",
            "payload_sha256": "ws-hash",
        },
    )

    assert result["coverage_grade"] == "A"
    assert result["has_gap"] is False
    assert result["redundant_feed_match"] is False
    assert result["registry_coverage_grade"] == "D"
    assert result["registry_has_gap"] is True
    assert result["hot_preflight"]["historical_continuity_claimed"] is False
    assert result["hot_preflight"]["gcp_ws_bbo_match"] is True


def test_hot_preflight_retains_stale_gcp_bbo_as_diagnostic_only() -> None:
    result = align_hot_preflight_candidate(
        {
            "asset_id": "asset-1",
            "best_bid": "0.40",
            "best_ask": "0.42",
        },
        {
            "asset_id": "asset-1",
            "best_bid": "0.39",
            "best_ask": "0.43",
            "bids": [["0.39", "1"]],
            "asks": [["0.43", "1"]],
        },
    )

    assert result["best_bid"] == "0.39"
    assert result["best_ask"] == "0.43"
    assert result["hot_preflight"]["gcp_ws_bbo_match"] is False
    assert result["hot_preflight"]["rest_bbo_match"] is False


def test_hot_preflight_uses_stale_gcp_book_only_for_identity() -> None:
    calls: list[float] = []

    def load(_store, *, asset_id, market_id, max_age_seconds):
        assert asset_id == "asset-1"
        assert market_id == "market-1"
        calls.append(max_age_seconds)
        if max_age_seconds < 3600:
            return None
        return {
            "asset_id": "asset-1",
            "best_bid": "0.40",
            "best_ask": "0.42",
            "coverage_grade": "D",
            "has_gap": True,
            "redundant_feed_match": False,
        }

    def capture(**_kwargs):
        return {
            "asset_id": "asset-1",
            "best_bid": "0.41",
            "best_ask": "0.43",
            "bids": [["0.41", "1"]],
            "asks": [["0.43", "1"]],
        }

    with patch(
        "quant.maker.hot_preflight.load_hot_preflight_candidate",
        side_effect=load,
    ):
        result = acquire_hot_preflight_candidate(
            object(),
            asset_id="asset-1",
            market_id="market-1",
            proxy_url=None,
            timeout_seconds=1,
            max_book_age_seconds=60,
            identity_book_grace_seconds=3600,
            capture=capture,
        )

    gcp = result["hot_preflight"]["gcp_current_book"]
    assert calls == [60, 3600, 3600]
    assert gcp["identity_only"] is True
    assert gcp["used_for_current_price"] is False
    assert gcp["freshness_policy_seconds"] == "60.000"
    assert gcp["identity_grace_seconds"] == "3600.000"
    assert result["hot_preflight"]["historical_continuity_claimed"] is False


def test_targeted_ws_capture_rejects_one_sided_book() -> None:
    client = FakeWsClient(
        [
            [
                {
                    "event_type": "book",
                    "asset_id": "asset-1",
                    "bids": [{"price": "0.40", "size": "12"}],
                    "asks": [],
                }
            ]
        ]
    )

    with pytest.raises(RuntimeError, match="not two-sided"):
        asyncio.run(
            capture_targeted_ws_book(
                asset_id="asset-1",
                proxy_url=None,
                timeout_seconds=1,
                client=client,
            )
        )
    assert client.closed
