"""Route-level health helpers for redundant paper market-data feeds."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def connected_route_keys(
    route_states: Mapping[str, Any],
    route_proxy_urls: Mapping[str, Any],
) -> set[str]:
    keys: set[str] = set()
    for source, state in route_states.items():
        if str(state).upper() != "CONNECTED":
            continue
        proxy_url = str(route_proxy_urls.get(source) or "direct://").strip().rstrip("/")
        keys.add(proxy_url.lower())
    return keys


def has_independent_connected_routes(
    route_states: Mapping[str, Any],
    route_proxy_urls: Mapping[str, Any],
    *,
    minimum: int = 2,
) -> bool:
    return len(connected_route_keys(route_states, route_proxy_urls)) >= minimum
