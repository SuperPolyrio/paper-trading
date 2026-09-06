from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant.calibration.real_live_adapter import (
    ClobV2AdapterError,
    PreparedLiveOrder,
    PolymarketV2LiveAdapter,
    SignedOrderAudit,
    VenueMaintenance,
    WriteRouteRestricted,
    _official_market_category,
)


def _adapter(
    monkeypatch,
    *,
    attempts=3,
    proxies=("proxy-a", "proxy-b"),
    write_proxy="",
):
    adapter = object.__new__(PolymarketV2LiveAdapter)
    adapter.plan = SimpleNamespace(
        network=SimpleNamespace(
            read_proxy_urls=proxies,
            read_attempts=attempts,
            read_retry_seconds=Decimal("0"),
            proxy_url=write_proxy,
        )
    )
    selected = []
    monkeypatch.setattr(
        adapter,
        "_configure_sdk_http",
        lambda proxy_url, **_kwargs: selected.append(proxy_url),
    )
    return adapter, selected


def test_read_retries_on_next_proxy(monkeypatch):
    adapter, selected = _adapter(monkeypatch)
    calls = 0

    def callback():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("temporary")
        return "ok"

    assert adapter._read_with_failover("book", callback) == "ok"
    assert selected == ["proxy-a", "proxy-b", "proxy-a"]


def test_read_does_not_retry_permanent_http_error(monkeypatch):
    adapter, selected = _adapter(monkeypatch)

    class Unauthorized(Exception):
        status_code = 401

    with pytest.raises(ClobV2AdapterError, match="after 1 read attempt"):
        adapter._read_with_failover("account", lambda: (_ for _ in ()).throw(Unauthorized()))
    assert selected == ["proxy-a"]


def test_read_retry_is_bounded(monkeypatch):
    adapter, selected = _adapter(monkeypatch, attempts=2)

    with pytest.raises(ClobV2AdapterError, match="after 2 read attempt"):
        adapter._read_with_failover("book", lambda: (_ for _ in ()).throw(TimeoutError()))
    assert selected == ["proxy-a", "proxy-b"]


def test_read_uses_write_route_after_auxiliary_pool_times_out(monkeypatch):
    adapter, selected = _adapter(
        monkeypatch,
        attempts=2,
        proxies=("proxy-a", "proxy-b"),
        write_proxy="proxy-write",
    )
    calls = 0

    def callback():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("temporary")
        return "ok"

    assert adapter._read_with_failover("book", callback) == "ok"
    assert selected == ["proxy-a", "proxy-b", "proxy-write"]


def test_reporting_books_batch_uses_bounded_failover(monkeypatch):
    adapter, _selected = _adapter(monkeypatch, proxies=("proxy-a", "proxy-b"))
    adapter.plan.network.clob_host = "https://clob.polymarket.com"
    adapter.plan.network.read_retry_seconds = Decimal("0")
    seen = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "asset_id": "token-1",
                    "bids": [{"price": "0.4"}],
                    "asks": [{"price": "0.5"}],
                }
            ]

    class Client:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, endpoint, json):
            seen.append((endpoint, json))
            return Response()

    monkeypatch.setattr("quant.calibration.real_live_adapter.httpx.Client", Client)

    result = adapter.get_reporting_book_snapshots(
        asset_ids=["token-1"], batch_size=4, timeout_seconds=1, attempts=1
    )

    assert result["token-1"]["rest_best_bid"] == "0.4"
    assert result["token-1"]["rest_best_ask"] == "0.5"
    assert seen[0]["proxy"] == "proxy-a"
    assert seen[1] == (
        "https://clob.polymarket.com/books",
        [{"token_id": "token-1"}],
    )


def test_order_reconciliation_returns_only_exact_order_trades(monkeypatch):
    adapter, _selected = _adapter(monkeypatch)
    adapter.plan.account = SimpleNamespace(expected_funder_address="0xwallet")

    class Params:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    adapter._sdk = SimpleNamespace(TradeParams=Params, OpenOrderParams=Params)

    class Client:
        def get_order(self, order_id):
            return {"id": order_id, "status": "MATCHED"}

        def get_trades(self, _params, *, only_first_page):
            assert only_first_page is False
            return [
                {
                    "id": "trade-buy",
                    "taker_order_id": "order-buy",
                    "transaction_hash": "0xbuy",
                },
                {
                    "id": "trade-later-sell",
                    "taker_order_id": "order-sell",
                    "transaction_hash": "0xsell",
                },
            ]

        def get_open_orders(self, _params, *, only_first_page):
            assert only_first_page is False
            return []

    monkeypatch.setattr(adapter, "_client", lambda **_kwargs: Client())

    result = adapter._get_order_reconciliation_snapshot_once(
        order_id="order-buy",
        condition_id="condition-1",
        asset_id="asset-1",
        after=1,
        before=2,
    )

    assert [row["id"] for row in result["trades"]] == ["trade-buy"]
    assert result["raw_trade_count"] == 2
    assert result["correlated_trade_count"] == 1


def test_market_snapshot_includes_official_lifecycle_metadata(monkeypatch):
    adapter, _selected = _adapter(monkeypatch)

    class Client:
        def get_clob_market_info(self, _condition_id):
            return {"fd": {"r": 0.05, "e": 1, "to": True}, "mos": 5}

        def get_market(self, _condition_id):
            return {
                "active": True,
                "closed": False,
                "accepting_orders": True,
                "end_date_iso": "2026-09-07T00:00:00Z",
                "question": "Will there be 9 earthquakes?",
                "market_slug": "will-there-be-9-earthquakes",
                "category": "weather",
                "tags": ["Weather"],
                "events": [{"title": "Earthquakes in September"}],
            }

        def get_tick_size(self, _asset_id):
            return "0.01"

        def get_neg_risk(self, _asset_id):
            return True

        def get_fee_rate_bps(self, _asset_id):
            return 1000

        def get_ok(self):
            return {"ok": True}

    monkeypatch.setattr(adapter, "_client", lambda **_kwargs: Client())
    monkeypatch.setattr(
        adapter,
        "_book_snapshot",
        lambda _client, *, asset_id: {
            "rest_best_bid": "0.10",
            "rest_best_ask": "0.16",
            "tick_size": "0.01",
            "min_order_size": "5",
            "neg_risk": True,
        },
    )
    monkeypatch.setattr(adapter, "_server_clock_offset_ms", lambda _client: 0)

    snapshot = adapter._get_market_snapshot_once(
        asset_id="asset-1",
        condition_id="condition-1",
    )

    assert snapshot["end_date"] == "2026-09-07T00:00:00Z"
    assert snapshot["market_state"] == "LIVE"
    assert snapshot["execution_eligible"] is True
    assert snapshot["official_market_info"]["market_slug"] == (
        "will-there-be-9-earthquakes"
    )
    assert snapshot["category"] == "weather"
    assert snapshot["event_title"] == "Earthquakes in September"


def test_market_snapshot_recovers_category_from_official_tags():
    assert _official_market_category({"tags": ["Solana", "Crypto"]}) == "crypto"


def test_write_route_geoblock_is_pinned_and_fail_closed(monkeypatch):
    adapter, _selected = _adapter(monkeypatch)
    adapter.plan.network.proxy_url = "proxy-write"
    observed = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"blocked": True, "country": "BR", "region": "SP", "ip": "203.0.113.1"}

    class Client:
        def __init__(self, **kwargs):
            observed.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url):
            observed.append(url)
            return Response()

    monkeypatch.setattr("quant.calibration.real_live_adapter.httpx.Client", Client)

    with pytest.raises(WriteRouteRestricted, match="country=BR"):
        adapter.require_write_route_allowed()
    assert observed[0]["proxy"] == "proxy-write"
    assert observed[1] == "https://polymarket.com/api/geoblock"


def test_write_route_geoblock_allows_explicit_unblocked_response(monkeypatch):
    adapter, _selected = _adapter(monkeypatch)
    adapter.plan.network.proxy_url = "proxy-write"

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"blocked": False, "country": "JP", "region": "13", "ip": "203.0.113.2"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url):
            return Response()

    monkeypatch.setattr("quant.calibration.real_live_adapter.httpx.Client", Client)

    assert adapter.require_write_route_allowed()["blocked"] is False


def test_write_route_geoblock_retries_only_the_pinned_route(monkeypatch):
    adapter, _selected = _adapter(
        monkeypatch,
        attempts=3,
        proxies=("proxy-read-a", "proxy-read-b"),
        write_proxy="proxy-write",
    )
    observed = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"blocked": False, "country": "HK", "region": "", "ip": "ip"}

    class Client:
        def __init__(self, **kwargs):
            observed.append(kwargs["proxy"])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url):
            if len(observed) < 3:
                raise TimeoutError("temporary")
            return Response()

    monkeypatch.setattr("quant.calibration.real_live_adapter.httpx.Client", Client)

    assert adapter.get_write_route_geoblock_snapshot()["blocked"] is False
    assert observed == ["proxy-write", "proxy-write", "proxy-write"]


def test_write_route_geoblock_retry_remains_fail_closed(monkeypatch):
    adapter, _selected = _adapter(
        monkeypatch,
        attempts=2,
        write_proxy="proxy-write",
    )
    calls = 0

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url):
            nonlocal calls
            calls += 1
            raise TimeoutError("temporary")

    monkeypatch.setattr("quant.calibration.real_live_adapter.httpx.Client", Client)

    with pytest.raises(WriteRouteRestricted, match="after 2 attempt"):
        adapter.get_write_route_geoblock_snapshot()
    assert calls == 2


def test_write_route_close_only_allows_sell(monkeypatch):
    adapter, _selected = _adapter(monkeypatch)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"blocked": True, "country": "BR", "region": "SP", "ip": "203.0.113.3"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url):
            return Response()

    monkeypatch.setattr("quant.calibration.real_live_adapter.httpx.Client", Client)

    result = adapter.require_write_route_allowed(
        side="SELL",
        asset_id="asset-1",
        exposure_before=Decimal("2"),
        exposure_after=Decimal("1"),
    )

    assert result["admission"]["jurisdiction_mode"] == "CLOSE_ONLY"
    assert result["admission"]["allowed"] is True


def test_write_route_close_only_sell_without_exposure_fails_closed(monkeypatch):
    adapter, _selected = _adapter(monkeypatch)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"blocked": True, "country": "BR", "region": "SP", "ip": "203.0.113.3"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url):
            return Response()

    monkeypatch.setattr("quant.calibration.real_live_adapter.httpx.Client", Client)

    with pytest.raises(WriteRouteRestricted, match="mode=CLOSE_ONLY"):
        adapter.require_write_route_allowed(side="SELL")


def test_write_route_full_block_rejects_sell(monkeypatch):
    adapter, _selected = _adapter(monkeypatch)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"blocked": True, "country": "IR", "region": "", "ip": "203.0.113.4"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url):
            return Response()

    monkeypatch.setattr("quant.calibration.real_live_adapter.httpx.Client", Client)

    with pytest.raises(WriteRouteRestricted, match="mode=BLOCK_COMPLETELY"):
        adapter.require_write_route_allowed(side="SELL")


def test_reconciliation_treats_canceled_order_404_as_missing_not_transport_failure():
    adapter = object.__new__(PolymarketV2LiveAdapter)
    adapter.plan = SimpleNamespace(
        account=SimpleNamespace(expected_funder_address="0xmaker")
    )
    observed = {}

    class NotFound(Exception):
        status_code = 404

    class Client:
        def get_order(self, _order_id):
            raise NotFound()

        def get_trades(self, params, only_first_page=False):
            assert only_first_page is False
            observed.update(params)
            return []

        def get_open_orders(self, _params, only_first_page=False):
            assert only_first_page is False
            return []

    adapter._sdk = type(
        "SDK",
        (),
        {
            "TradeParams": lambda **kwargs: kwargs,
            "OpenOrderParams": lambda **kwargs: kwargs,
        },
    )
    adapter._client = lambda **_kwargs: Client()

    snapshot = adapter._get_order_reconciliation_snapshot_once(
        order_id="order",
        condition_id="condition",
        asset_id="asset",
        after=None,
        before=None,
    )

    assert snapshot["order"] == {}
    assert snapshot["order_lookup_error"] == "HTTP_404_ORDER_NOT_FOUND"
    assert snapshot["open_orders"] == []
    assert observed == {
        "market": "condition",
        "maker_address": "0xmaker",
        "after": None,
        "before": None,
    }


def test_authenticated_trade_history_uses_maker_filter_and_all_pages():
    adapter = object.__new__(PolymarketV2LiveAdapter)
    observed = {}

    class Client:
        def get_trades(self, params, only_first_page=False):
            observed.update(params=params, only_first_page=only_first_page)
            return [{"id": "trade-1", "trader_side": "MAKER"}]

    adapter._sdk = type(
        "SDK",
        (),
        {"TradeParams": lambda **kwargs: kwargs},
    )
    adapter._client = lambda **_kwargs: Client()

    trades = adapter._get_authenticated_trades_once(
        maker_address="0xmaker",
        after=10,
        before=20,
    )

    assert trades == [{"id": "trade-1", "trader_side": "MAKER"}]
    assert observed == {
        "params": {"maker_address": "0xmaker", "after": 10, "before": 20},
        "only_first_page": False,
    }


def test_post_only_submission_does_not_enable_deferred_execution(monkeypatch):
    adapter = object.__new__(PolymarketV2LiveAdapter)
    adapter.plan = SimpleNamespace(network=SimpleNamespace(proxy_url="proxy"))
    adapter.submit_calls = 0
    observed = {}

    class Client:
        def post_order(self, signed, order_type, post_only=False, defer_exec=False):
            observed.update(
                signed=signed,
                order_type=order_type,
                post_only=post_only,
                defer_exec=defer_exec,
            )
            return {"success": True, "orderID": "order-1", "status": "live"}

    monkeypatch.setattr(adapter, "_require_live_authorization", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_configure_sdk_http", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(adapter, "_client", lambda **_kwargs: Client())
    audit = SignedOrderAudit(
        sdk_name="sdk",
        sdk_version="1",
        signed_at=datetime.now(timezone.utc),
        sign_duration_ms=1,
        order_hash="hash",
        signed_order_fingerprint="signed",
        signature_fingerprint="signature",
        maker="maker",
        signer="signer",
        signature_type=2,
        token_id="asset",
        side="BUY",
        order_type="GTC",
        amount="5",
        amount_unit="SHARES",
        worst_price="0.1",
        maker_amount="500000",
        taker_amount="5000000",
        timestamp="1",
        metadata="metadata",
        builder="builder",
        neg_risk=False,
        tick_size="0.001",
        post_only=True,
    )

    submitted, response = adapter.submit_prepared_once(
        PreparedLiveOrder(signed_order="signed-order", audit=audit),
        run_id="run",
        risk_passed=True,
    )

    assert submitted.exchange_submit_called is True
    assert response["orderID"] == "order-1"
    assert observed == {
        "signed": "signed-order",
        "order_type": "GTC",
        "post_only": True,
        "defer_exec": False,
    }


def test_engine_not_ready_rejection_preserves_submitted_audit(monkeypatch):
    adapter = object.__new__(PolymarketV2LiveAdapter)
    adapter.plan = SimpleNamespace(network=SimpleNamespace(proxy_url="proxy"))
    adapter.submit_calls = 0

    class EngineNotReady(Exception):
        status_code = 425
        error_msg = {"error": "order manager not ready, please retry"}

    class Client:
        def post_order(self, *_args, **_kwargs):
            raise EngineNotReady()

    monkeypatch.setattr(adapter, "_require_live_authorization", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_configure_sdk_http", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(adapter, "_client", lambda **_kwargs: Client())
    audit = SignedOrderAudit(
        sdk_name="sdk",
        sdk_version="1",
        signed_at=datetime.now(timezone.utc),
        sign_duration_ms=1,
        order_hash="0xhash",
        signed_order_fingerprint="signed",
        signature_fingerprint="signature",
        maker="maker",
        signer="signer",
        signature_type=2,
        token_id="asset",
        side="BUY",
        order_type="GTC",
        amount="5",
        amount_unit="SHARES",
        worst_price="0.1",
        maker_amount="500000",
        taker_amount="5000000",
        timestamp="1",
        metadata="metadata",
        builder="builder",
        neg_risk=False,
        tick_size="0.001",
        post_only=True,
    )

    with pytest.raises(VenueMaintenance) as caught:
        adapter.submit_prepared_once(
            PreparedLiveOrder(signed_order="signed-order", audit=audit),
            run_id="run",
            risk_passed=True,
        )

    assert caught.value.audit.exchange_submit_called is True
    assert caught.value.response["venue_error_code"] == "ENGINE_RESTART"
