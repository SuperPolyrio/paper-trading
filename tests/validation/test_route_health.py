from types import SimpleNamespace

from quant.paper.acceptance import _dual_transport_available
from quant.paper.live_shadow_service import LivePaperShadowService
from quant.paper.route_health import (
    connected_route_keys,
    has_independent_connected_routes,
)


def test_same_proxy_endpoint_is_not_independent_redundancy() -> None:
    states = {"primary": "CONNECTED", "secondary": "CONNECTED"}
    urls = {
        "primary": "http://127.0.0.1:17982",
        "secondary": "http://127.0.0.1:17982/",
    }

    assert connected_route_keys(states, urls) == {"http://127.0.0.1:17982"}
    assert not has_independent_connected_routes(states, urls)
    assert not _dual_transport_available(
        "REDUNDANT",
        states,
        {"primary": 10, "secondary": 10},
        route_proxy_urls=urls,
    )


def test_distinct_connected_proxy_endpoints_are_independent() -> None:
    states = {"primary": "CONNECTED", "secondary": "CONNECTED"}
    urls = {
        "primary": "http://127.0.0.1:17982",
        "secondary": "http://127.0.0.1:17980",
    }

    assert has_independent_connected_routes(states, urls)
    assert _dual_transport_available(
        "REDUNDANT",
        states,
        {"primary": 10, "secondary": 10},
        route_proxy_urls=urls,
    )


def test_disconnected_route_does_not_count_as_independent() -> None:
    states = {"primary": "CONNECTED", "secondary": "RECONNECTING"}
    urls = {
        "primary": "http://127.0.0.1:17982",
        "secondary": "http://127.0.0.1:17980",
    }

    assert not has_independent_connected_routes(states, urls)


def test_live_service_only_reports_redundant_for_distinct_endpoints() -> None:
    service = object.__new__(LivePaperShadowService)
    service.clients = {"primary": object(), "secondary": object()}
    service.stats = SimpleNamespace(
        route_states={"primary": "CONNECTED", "secondary": "CONNECTED"},
        route_proxy_urls={
            "primary": "http://127.0.0.1:17982",
            "secondary": "http://127.0.0.1:17982",
        },
        transport_state="STARTING",
        last_error="starting",
    )

    service._update_transport_state()
    assert service.stats.transport_state == "DEGRADED"

    service.stats.route_proxy_urls["secondary"] = "http://127.0.0.1:17980"
    service._update_transport_state()
    assert service.stats.transport_state == "REDUNDANT"
