"""Build live position-level PnL snapshots from the paper shadow BookState."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .store import CalibrationStore, summarize_pnl_positions


def load_shadow_health(
    path: Path,
    *,
    connection_factory: Any | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] | None = None
    if connection_factory is not None:
        try:
            with connection_factory(readonly=True) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM quant.paper_live_shadow_health
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                payload = dict(row) if row is not None else None
        except Exception:  # noqa: BLE001
            payload = None
    try:
        payload = payload or json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        return {
            "status": "UNAVAILABLE",
            "reason": f"{exc.__class__.__name__}:{str(exc)[:200]}",
        }
    updated_at = _datetime(payload.get("updated_at"))
    age = (
        max(0.0, (datetime.now(timezone.utc) - updated_at).total_seconds())
        if updated_at is not None
        else None
    )
    route_states = payload.get("route_states")
    connected = bool(
        isinstance(route_states, Mapping)
        and route_states
        and all(value == "CONNECTED" for value in route_states.values())
    )
    ready = bool(
        payload.get("transport_state") == "REDUNDANT"
        and connected
        and age is not None
        and age <= 30
    )
    return {
        "status": "READY" if ready else "DEGRADED",
        "updated_at": payload.get("updated_at"),
        "age_seconds": age,
        "transport_state": payload.get("transport_state"),
        "route_states": route_states,
        "fresh_books": payload.get("fresh_books"),
        "ready_books": payload.get("ready_books"),
        "last_message_at": payload.get("last_message_at"),
        "last_error": payload.get("last_error"),
    }


def build_portfolio_snapshot(
    store: CalibrationStore,
    *,
    account_id: str | None,
    shadow_status_path: Path,
    rest_book_client: Any | None = None,
    health_connection_factory: Any | None = None,
) -> dict[str, Any]:
    positions = store.pnl_positions(account_id=account_id)
    rest_marking: dict[str, Any] = {
        "enabled": rest_book_client is not None,
        "attempted": 0,
        "marked": 0,
        "unavailable": 0,
        "errors": [],
    }
    if rest_book_client is not None:
        positions, rest_marking = apply_rest_book_fallback(
            positions,
            rest_book_client=rest_book_client,
        )
        summary = summarize_pnl_positions(positions)
    else:
        summary = store.pnl_summary(account_id=account_id)
    health = load_shadow_health(
        shadow_status_path,
        connection_factory=(
            health_connection_factory
            if health_connection_factory is not None
            else getattr(store, "connection_factory", None)
        ),
    )
    all_open_marked = summary["open_positions"] == summary["marked_open_positions"]
    return {
        "schema_version": "calibration_live_portfolio_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "READY"
            if health["status"] == "READY" and all_open_marked
            else "DEGRADED"
        ),
        "account_id": account_id,
        "shadow_health": health,
        "rest_marking": rest_marking,
        "summary": summary,
        "positions": positions,
    }


def apply_rest_book_fallback(
    positions: list[Mapping[str, Any]],
    *,
    rest_book_client: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fill reporting-only marks from CLOB REST without changing execution state."""

    observed_now = datetime.now(timezone.utc)
    output: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "enabled": True,
        "attempted": 0,
        "marked": 0,
        "unavailable": 0,
        "errors": [],
        "policy": "REST_BBO_FALLBACK is reporting-only and never changes execution eligibility",
    }
    candidates = [
        str(source.get("asset_id") or "")
        for source in positions
        if max(_decimal(source.get("real_quantity")), _decimal(source.get("paper_quantity")))
        > 0
        and source.get("mark_status") != "READY"
        and str(source.get("asset_id") or "")
    ]
    batch_reader = getattr(rest_book_client, "get_reporting_book_snapshots", None)
    batch_results: Mapping[str, Mapping[str, Any]] | None = None
    if callable(batch_reader) and candidates:
        try:
            batch_results = batch_reader(asset_ids=candidates)
            stats["batch_mode"] = True
        except Exception as exc:  # noqa: BLE001
            stats["batch_mode"] = True
            stats["batch_error"] = f"{exc.__class__.__name__}:{str(exc)[:200]}"
    for source in positions:
        row = dict(source)
        quantity = max(
            _decimal(row.get("real_quantity")),
            _decimal(row.get("paper_quantity")),
        )
        if quantity <= 0 or row.get("mark_status") == "READY":
            output.append(row)
            continue
        asset_id = str(row.get("asset_id") or "")
        stats["attempted"] += 1
        try:
            snapshot = (
                batch_results.get(asset_id, {"reporting_book_error": "asset_missing_from_batch"})
                if batch_results is not None
                else rest_book_client.get_book_snapshot(asset_id=asset_id)
            )
            if snapshot.get("reporting_book_error"):
                raise ValueError(str(snapshot["reporting_book_error"]))
            bid = _positive_price(snapshot.get("rest_best_bid"))
            ask = _positive_price(snapshot.get("rest_best_ask"))
            if bid is None and ask is None:
                raise ValueError("rest_book_has_no_sides")
            observed_at = _datetime(snapshot.get("rest_book_observed_at")) or observed_now
            no_bid = bid is None
            liquidation_mark = bid if bid is not None else Decimal("0")
            row.update(
                mark_bid=liquidation_mark,
                mark_status="READY",
                mark_source=(
                    "REST_BBO_FALLBACK"
                    if bid is not None
                    else "REST_NO_BID_CONSERVATIVE_ZERO"
                ),
                mark_observed_at=observed_at,
                mark_age_seconds=max(0.0, (observed_now - observed_at).total_seconds()),
                mark_quality=(
                    "REPORTING_ONLY" if bid is not None else "CONSERVATIVE_ZERO"
                ),
                rest_mark_attempted=True,
                rest_best_bid=bid,
                rest_best_ask=ask,
                rest_book_hash=str(snapshot.get("rest_book_hash") or "") or None,
                rest_no_executable_bid=no_bid,
            )
            row["real_liquidation_value"] = _decimal(row.get("real_quantity")) * liquidation_mark
            row["paper_liquidation_value"] = _decimal(row.get("paper_quantity")) * liquidation_mark
            row["real_unrealized_pnl"] = row["real_liquidation_value"] - _decimal(
                row.get("real_cost_basis")
            )
            row["paper_unrealized_pnl"] = row["paper_liquidation_value"] - _decimal(
                row.get("paper_cost_basis")
            )
            stats["marked"] += 1
            if no_bid:
                stats["conservative_zero_marks"] = int(
                    stats.get("conservative_zero_marks") or 0
                ) + 1
        except Exception as exc:  # noqa: BLE001
            row.update(
                rest_mark_attempted=True,
                rest_mark_error=f"{exc.__class__.__name__}:{str(exc)[:200]}",
            )
            stats["unavailable"] += 1
            stats["errors"].append(
                {
                    "asset_id": asset_id,
                    "error": row["rest_mark_error"],
                }
            )
        output.append(row)
    return output, stats


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _positive_price(value: Any) -> Decimal | None:
    price = _decimal(value)
    return price if Decimal("0") < price < Decimal("1") else None
