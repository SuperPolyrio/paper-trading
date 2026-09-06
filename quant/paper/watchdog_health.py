"""Content-level liveness check for the colocated GCP paper worker."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def evaluate_worker_health(
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_status_age_seconds: float = 420.0,
    max_transport_idle_seconds: float = 180.0,
) -> dict[str, Any]:
    observed_now = _utc(now or datetime.now(timezone.utc))
    reasons: list[str] = []
    updated_at = _datetime(payload.get("updated_at") or payload.get("sampled_at"))
    status_age = _age_seconds(observed_now, updated_at)
    if status_age is None:
        reasons.append("STATUS_TIMESTAMP_MISSING")
    elif status_age < -1 or status_age > max(1.0, max_status_age_seconds):
        reasons.append("STATUS_STALE")

    route_states = payload.get("route_states")
    connected_routes = [
        str(source)
        for source, state in (route_states.items() if isinstance(route_states, Mapping) else ())
        if str(state) == "CONNECTED"
    ]
    if not connected_routes:
        reasons.append("NO_CONNECTED_ROUTE")

    transport_times = payload.get("route_last_transport_at")
    transport_source = "route_last_transport_at"
    if not isinstance(transport_times, Mapping) or not transport_times:
        transport_times = payload.get("route_last_message_at")
        transport_source = "route_last_message_at"
    if not isinstance(transport_times, Mapping):
        transport_times = {}

    route_idle_seconds: dict[str, float | None] = {}
    for source in connected_routes:
        idle = _age_seconds(observed_now, _datetime(transport_times.get(source)))
        route_idle_seconds[source] = idle
        if (
            idle is None
            or idle < -1
            or idle > max(1.0, max_transport_idle_seconds)
        ):
            reasons.append(f"ROUTE_TRANSPORT_STALE:{source}")

    persistent_state = str(payload.get("persistent_kernel_state") or "")
    if persistent_state and persistent_state not in {"READY", "NOT_CONFIGURED"}:
        reasons.append(f"PERSISTENT_KERNEL_{persistent_state}")

    return {
        "healthy": not reasons,
        "reason_codes": reasons,
        "status_age_seconds": status_age,
        "route_idle_seconds": route_idle_seconds,
        "transport_timestamp_source": transport_source,
        "persistent_kernel_state": persistent_state or None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-path", type=Path, required=True)
    parser.add_argument("--max-status-age-seconds", type=float, default=420.0)
    parser.add_argument("--max-transport-idle-seconds", type=float, default=180.0)
    parser.add_argument("--reason-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.status_path.read_text(encoding="utf-8"))
        result = evaluate_worker_health(
            payload,
            max_status_age_seconds=args.max_status_age_seconds,
            max_transport_idle_seconds=args.max_transport_idle_seconds,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        result = {
            "healthy": False,
            "reason_codes": [f"STATUS_UNREADABLE:{exc.__class__.__name__}"],
        }
    if args.reason_only:
        print(",".join(result["reason_codes"]) or "HEALTHY")
    else:
        print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result["healthy"] else 2


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if value in (None, ""):
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _age_seconds(now: datetime, value: datetime | None) -> float | None:
    return (now - value).total_seconds() if value is not None else None


if __name__ == "__main__":
    raise SystemExit(main())
