"""Submit and verify a small, paper-only taker execution canary."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant.core.db import postgres_connection

from .authority import ControlPlanePostgresConnectionFactory
from .live_shadow_store import LiveShadowStore
from .paper_ledger import PostgresPaperLedgerSink

DEFAULT_OUTPUT = Path("runtime_outputs/paper_live_shadow/canary/latest.json")
CANARY_CANDIDATE_MAX_AGE_SECONDS = 60.0


def run_canary(
    *,
    strategy_id: str | None = None,
    wait_seconds: float = 30.0,
    output_path: Path = DEFAULT_OUTPUT,
    paper_account_id: str = "paper-account",
) -> dict[str, Any]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    strategy = strategy_id or f"p2-soak-{run_id}"
    risk_strategy = f"{strategy}-risk"
    ingress_connection_factory = _ingress_connection_factory()
    store = LiveShadowStore(ingress_connection_factory)
    store.ensure_schema()
    ledger = PostgresPaperLedgerSink(ingress_connection_factory)
    ledger.ensure_account(strategy)
    ledger.ensure_account(risk_strategy)
    refresh_requested_assets: list[str] = []
    candidates = _wait_candidates(
        limit=12,
        wait_seconds=wait_seconds,
        store=store,
        refresh_requested_assets=refresh_requested_assets,
        paper_account_id=paper_account_id,
    )
    attempts: list[dict[str, Any]] = []
    buy_result: dict[str, Any] | None = None
    buy_intent_id: int | None = None
    duplicate_intent_id: int | None = None
    selected: dict[str, Any] | None = None

    for index, candidate in enumerate(candidates):
        asset_id = str(candidate["asset_id"])
        tick = _decimal(candidate.get("tick_size")) or Decimal("0.001")
        minimum = _decimal(candidate.get("min_order_size")) or Decimal(1)
        if _decimal(candidate.get("best_ask")) is None:
            continue
        limit_price = Decimal(1) - tick
        client_order_id = f"{run_id}-buy-{index}"
        try:
            buy_intent_id = store.submit(
                strategy_id=strategy,
                client_order_id=client_order_id,
                asset_id=asset_id,
                side="BUY",
                time_in_force="FAK",
                limit_price=limit_price,
                size=minimum,
                post_only=False,
                decision_ts=datetime.now(timezone.utc),
            )
        except ValueError as exc:
            attempts.append(
                {
                    "asset_id": asset_id,
                    "limit_price": limit_price,
                    "size": minimum,
                    "submission_error": f"{type(exc).__name__}: {exc}",
                    "retryable_candidate_rejection": True,
                }
            )
            continue
        result = _wait_result(buy_intent_id, wait_seconds=wait_seconds)
        attempts.append({
            "asset_id": asset_id,
            "intent_id": buy_intent_id,
            "limit_price": limit_price,
            "size": minimum,
            "result": result,
        })
        if result and Decimal(str(result.get("filled_size") or 0)) > 0:
            buy_result = result
            selected = candidate
            duplicate_intent_id = store.submit(
                strategy_id=strategy,
                client_order_id=client_order_id,
                asset_id=asset_id,
                side="BUY",
                time_in_force="FAK",
                limit_price=limit_price,
                size=minimum,
                post_only=False,
                decision_ts=datetime.now(timezone.utc),
            )
        elif result is None:
            break
        if selected is not None:
            break

    risk_result: dict[str, Any] | None = None
    risk_intent_id: int | None = None
    no_fill_result: dict[str, Any] | None = None
    no_fill_intent_id: int | None = None
    no_fill_asset_id: str | None = None
    sell_result: dict[str, Any] | None = None
    sell_intent_id: int | None = None
    sell_attempts: list[dict[str, Any]] = []
    no_fill_attempts: list[dict[str, Any]] = []
    if selected is not None and buy_result is not None:
        asset_id = str(selected["asset_id"])
        tick = _decimal(selected.get("tick_size")) or Decimal("0.001")
        sell_limit = tick
        filled_size = Decimal(str(buy_result["filled_size"]))
        requested_sell_size = max(
            filled_size,
            _decimal(selected.get("min_order_size")) or Decimal(1),
        )
        position_ready = _wait_available_position(
            strategy_id=strategy,
            asset_id=asset_id,
            minimum_size=requested_sell_size,
            wait_seconds=wait_seconds,
        )
        if position_ready:
            sell_intent_id = store.submit(
                strategy_id=strategy,
                client_order_id=f"{run_id}-sell",
                asset_id=asset_id,
                side="SELL",
                time_in_force="FAK",
                limit_price=sell_limit,
                size=requested_sell_size,
                post_only=False,
                decision_ts=datetime.now(timezone.utc),
            )
            sell_result = _wait_result(sell_intent_id, wait_seconds=wait_seconds)
            sell_attempts.append({"intent_id": sell_intent_id, "result": sell_result})
        if sell_result and Decimal(str(sell_result.get("filled_size") or 0)) > 0:
            risk_intent_id = store.submit(
                strategy_id=risk_strategy,
                client_order_id=f"{run_id}-sell-without-position",
                asset_id=asset_id,
                side="SELL",
                time_in_force="FAK",
                limit_price=sell_limit,
                size=requested_sell_size,
                post_only=False,
                decision_ts=datetime.now(timezone.utc),
            )
            risk_result = _wait_result(risk_intent_id, wait_seconds=wait_seconds)
            no_fill_deadline = time.monotonic() + max(1.0, float(wait_seconds))
            for attempt in range(20):
                remaining = no_fill_deadline - time.monotonic()
                if remaining <= 0:
                    break
                fresh_candidates = _wait_candidates(
                    limit=12,
                    wait_seconds=min(remaining, 2.0),
                    store=store,
                    refresh_requested_assets=refresh_requested_assets,
                    paper_account_id=paper_account_id,
                )
                if not fresh_candidates:
                    continue
                no_fill_candidate = fresh_candidates[0]
                no_fill_asset_id = str(no_fill_candidate["asset_id"])
                no_fill_tick = _decimal(no_fill_candidate.get("tick_size")) or Decimal("0.001")
                no_fill_size = _decimal(no_fill_candidate.get("min_order_size")) or Decimal(1)
                no_fill_intent_id = store.submit(
                    strategy_id=strategy,
                    client_order_id=f"{run_id}-buy-not-marketable-{attempt}",
                    asset_id=no_fill_asset_id,
                    side="BUY",
                    time_in_force="FAK",
                    limit_price=no_fill_tick,
                    size=no_fill_size,
                    post_only=False,
                    decision_ts=datetime.now(timezone.utc),
                )
                no_fill_result = _wait_result(
                    no_fill_intent_id,
                    wait_seconds=max(1.0, no_fill_deadline - time.monotonic()),
                )
                no_fill_attempts.append({"intent_id": no_fill_intent_id, "result": no_fill_result})
                if no_fill_result and no_fill_result.get("reason") == "no_marketable_arrival_depth":
                    break
                time.sleep(0.25)

    passed = bool(
        buy_result
        and Decimal(str(buy_result.get("filled_size") or 0)) > 0
        and sell_result
        and Decimal(str(sell_result.get("filled_size") or 0)) > 0
        and risk_result
        and risk_result.get("status") in {"FAILED", "REJECTED"}
        and "insufficient_available_paper_position" in str(risk_result.get("reason") or "")
        and no_fill_result
        and no_fill_result.get("reason") == "no_marketable_arrival_depth"
        and buy_intent_id == duplicate_intent_id
    )
    report = _json_value({
        "status": "PASS" if passed else "FAIL",
        "generated_at": datetime.now(timezone.utc),
        "strategy_id": strategy,
        "candidate_count": len(candidates),
        "refresh_requested_assets": list(dict.fromkeys(refresh_requested_assets)),
        "selected_asset_id": selected.get("asset_id") if selected else None,
        "buy_intent_id": buy_intent_id,
        "duplicate_intent_id": duplicate_intent_id,
        "sell_intent_id": sell_intent_id,
        "risk_intent_id": risk_intent_id,
        "no_fill_intent_id": no_fill_intent_id,
        "no_fill_asset_id": no_fill_asset_id,
        "buy_result": buy_result,
        "sell_result": sell_result,
        "risk_result": risk_result,
        "no_fill_result": no_fill_result,
        "sell_attempts": sell_attempts,
        "no_fill_attempts": no_fill_attempts,
        "attempts": attempts,
        "portfolio": ledger.summary(strategy_id=strategy),
    })
    _write_json(output_path, report)
    return report


def _ingress_connection_factory() -> ControlPlanePostgresConnectionFactory:
    return ControlPlanePostgresConnectionFactory(postgres_connection)


def _candidates(
    *,
    limit: int,
    max_age_seconds: float | None = CANARY_CANDIDATE_MAX_AGE_SECONDS,
    paper_account_id: str = "paper-account",
) -> list[dict[str, Any]]:
    with postgres_connection(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH live_books AS MATERIALIZED (
                SELECT c.asset_id, c.best_bid, c.best_ask, c.observed_at
                FROM quant.paper_live_watchlist w
                JOIN quant.paper_live_current_books c USING (asset_id)
                WHERE w.enabled=TRUE
                  AND c.market_state='LIVE'
                  AND c.book_status='READY'
                  AND c.coverage_grade IN ('A_PLUS','A','B')
                  AND c.has_gap=FALSE
                  AND c.redundant_feed_match=TRUE
                  AND (
                      %s::double precision IS NULL
                      OR c.observed_at >= clock_timestamp() - make_interval(secs => %s)
                  )
                  AND c.best_bid > 0.02
                  AND c.best_ask < 0.98
                  AND c.best_bid < c.best_ask
                ORDER BY c.observed_at DESC, c.asset_id
                LIMIT 256
            )
            SELECT r.asset_id,
                   COALESCE(r.current_tick_size, 0.001) AS tick_size,
                   COALESCE(r.min_order_size, 1) AS min_order_size,
                   c.best_bid,
                   c.best_ask,
                   c.observed_at AS last_receive_ts
            FROM live_books c
            JOIN quant.paper_execution_market_catalog r USING (asset_id)
            WHERE r.market_state='LIVE'
              AND r.execution_eligible=TRUE
              AND r.active=TRUE AND r.closed=FALSE AND r.resolved=FALSE
              AND NOT EXISTS (
                  SELECT 1 FROM quant.simulator_oms_orders own_order
                  WHERE own_order.account_id=%s
                    AND own_order.asset_id=r.asset_id
                    AND own_order.state IN ('WORKING','PENDING_CANCEL')
                    AND own_order.remaining_size>0
              )
            ORDER BY c.observed_at DESC, r.asset_id
            LIMIT %s
            """,
            (
                None if max_age_seconds is None else max(0.1, float(max_age_seconds)),
                None if max_age_seconds is None else max(0.1, float(max_age_seconds)),
                str(paper_account_id),
                max(1, int(limit)),
            ),
        )
        return [dict(row) for row in cur.fetchall()]


def _wait_candidates(
    *,
    limit: int,
    wait_seconds: float,
    max_age_seconds: float = CANARY_CANDIDATE_MAX_AGE_SECONDS,
    store: LiveShadowStore | None = None,
    refresh_requested_assets: list[str] | None = None,
    paper_account_id: str = "paper-account",
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    refresh_requested = False
    while True:
        candidates = _candidates(
            limit=limit,
            max_age_seconds=max_age_seconds,
            paper_account_id=paper_account_id,
        )
        if candidates or time.monotonic() >= deadline:
            return candidates
        if store is not None and not refresh_requested:
            refresh_requested = True
            asset_ids = [
                str(candidate["asset_id"])
                for candidate in _candidates(
                    limit=limit,
                    max_age_seconds=None,
                    paper_account_id=paper_account_id,
                )
            ]
            request_batch = getattr(store, "request_book_refresh_batch", None)
            changed = (
                list(request_batch(asset_ids))
                if callable(request_batch)
                else [
                    asset_id
                    for asset_id in asset_ids
                    if store.request_book_refresh(asset_id)
                ]
            )
            if refresh_requested_assets is not None:
                refresh_requested_assets.extend(changed)
        time.sleep(0.25)


def _wait_result(intent_id: int, *, wait_seconds: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(1.0, float(wait_seconds))
    while time.monotonic() < deadline:
        with postgres_connection(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, result, last_error FROM quant.paper_live_order_intents WHERE intent_id=%s",
                (int(intent_id),),
            )
            row = cur.fetchone()
        if row and row["status"] in {"COMPLETED", "FAILED"}:
            if isinstance(row.get("result"), dict):
                return dict(row["result"])
            return {"status": row["status"], "reason": row.get("last_error")}
        time.sleep(0.2)
    return None


def _wait_available_position(
    *,
    strategy_id: str,
    asset_id: str,
    minimum_size: Decimal,
    wait_seconds: float,
) -> bool:
    deadline = time.monotonic() + max(1.0, float(wait_seconds))
    while time.monotonic() < deadline:
        with postgres_connection(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(quantity - reserved_quantity, 0) AS available
                FROM quant.paper_positions
                WHERE strategy_id=%s AND asset_id=%s
                """,
                (str(strategy_id), str(asset_id)),
            )
            row = cur.fetchone()
        if row and Decimal(str(row.get("available") or 0)) >= minimum_size:
            return True
        time.sleep(0.1)
    return False


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy-id")
    parser.add_argument("--wait-seconds", type=float, default=30)
    parser.add_argument(
        "--paper-account-id",
        default=os.getenv("PAPER_ACCOUNT_ID", "paper-account"),
    )
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_canary(
        strategy_id=args.strategy_id,
        wait_seconds=args.wait_seconds,
        output_path=args.json_out,
        paper_account_id=args.paper_account_id,
    )
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
