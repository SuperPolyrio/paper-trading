from datetime import datetime, timedelta, timezone

from quant.paper.watchdog_health import evaluate_worker_health

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _payload() -> dict[str, object]:
    recent = (NOW - timedelta(seconds=10)).isoformat()
    return {
        "updated_at": recent,
        "transport_state": "REDUNDANT",
        "route_states": {"primary": "CONNECTED", "secondary": "CONNECTED"},
        "route_last_transport_at": {"primary": recent, "secondary": recent},
        "persistent_kernel_state": "READY",
    }


def test_healthy_worker_uses_transport_heartbeats() -> None:
    result = evaluate_worker_health(_payload(), now=NOW)

    assert result["healthy"] is True
    assert result["reason_codes"] == []
    assert result["transport_timestamp_source"] == "route_last_transport_at"


def test_live_process_with_stalled_feed_and_recovering_kernel_is_unhealthy() -> None:
    payload = _payload()
    stale = (NOW - timedelta(hours=5)).isoformat()
    payload["route_last_transport_at"] = {
        "primary": stale,
        "secondary": stale,
    }
    payload["persistent_kernel_state"] = "RECOVERING"

    result = evaluate_worker_health(payload, now=NOW)

    assert result["healthy"] is False
    assert result["reason_codes"] == [
        "ROUTE_TRANSPORT_STALE:primary",
        "ROUTE_TRANSPORT_STALE:secondary",
        "PERSISTENT_KERNEL_RECOVERING",
    ]


def test_old_status_falls_back_to_market_message_times() -> None:
    payload = _payload()
    payload.pop("route_last_transport_at")
    recent = (NOW - timedelta(seconds=20)).isoformat()
    payload["route_last_message_at"] = {
        "primary": recent,
        "secondary": recent,
    }

    result = evaluate_worker_health(payload, now=NOW)

    assert result["healthy"] is True
    assert result["transport_timestamp_source"] == "route_last_message_at"


def test_status_timestamp_and_missing_routes_fail_closed() -> None:
    payload = _payload()
    payload["updated_at"] = (NOW - timedelta(minutes=10)).isoformat()
    payload["route_states"] = {}

    result = evaluate_worker_health(
        payload,
        now=NOW,
        max_status_age_seconds=420,
    )

    assert result["healthy"] is False
    assert result["reason_codes"] == ["STATUS_STALE", "NO_CONNECTED_ROUTE"]
