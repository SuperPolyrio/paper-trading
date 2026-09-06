#!/usr/bin/env python3
"""Plan a no-submit Maker holdout campaign over the existing live LOB path."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant.adapters.polymarket_clob_client import PolymarketClobClient
from quant.adapters.polymarket_data_trades_client import PolymarketDataTradesClient
from quant.adapters.polymarket_gamma_client import PolymarketGammaClient
from quant.core.db import postgres_connection
from quant.maker.candidate_planner import (
    CAMPAIGN_HORIZONS_SECONDS,
    CAMPAIGN_PLACEMENTS,
    CAMPAIGN_SIDES,
    enrich_candidates_with_gamma_activity,
    enrich_candidates_with_recent_public_trades,
    load_maker_candidate_pool,
    load_maker_discovery_pool,
    load_recent_maker_trade_pool,
    plan_stratified_holdout_campaign,
    preselect_maker_candidates,
    reconcile_candidates_with_rest_books,
)
from quant.maker.trade_evidence import (
    CachedMakerTradeEvidenceClient,
    maker_evidence_request_key,
)
from quant.paper.control_plane_connection import (
    load_paper_control_plane_env,
    paper_control_plane_connection_factory,
)
from quant.paper.live_shadow_store import LiveShadowStore
from quant.paper.paired_probe import OrderFilledEvidenceClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=Decimal, default=Decimal(5))
    parser.add_argument("--max-notional-usd", type=Decimal, default=Decimal(5))
    parser.add_argument("--lookback-seconds", type=int, default=3600)
    parser.add_argument("--discovery-limit", type=int, default=500)
    parser.add_argument("--public-trade-limit", type=int, default=10_000)
    parser.add_argument("--public-trade-lookback-seconds", type=int, default=1800)
    parser.add_argument("--candidates-per-stratum", type=int, default=25)
    parser.add_argument("--recommendations-per-outcome", type=int, default=3)
    parser.add_argument("--sides", default=",".join(CAMPAIGN_SIDES))
    parser.add_argument("--placements", default=",".join(CAMPAIGN_PLACEMENTS))
    parser.add_argument(
        "--horizons-seconds",
        default=",".join(str(value) for value in CAMPAIGN_HORIZONS_SECONDS),
    )
    parser.add_argument(
        "--paper-control-env",
        type=Path,
        default=Path(
            os.environ.get(
                "POLY_QUANT_PAPER_DB_CONTROL_ENV",
                "~/.config/prediction-market-quant/paper-db-control.env",
            )
        ).expanduser(),
    )
    parser.add_argument(
        "--clob-proxy-url",
        default=os.environ.get(
            "POLY_QUANT_MAKER_CLOB_PROXY_URL",
            "http://127.0.0.1:17981",
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/maker/stratified-campaign-current.json"),
    )
    parser.add_argument(
        "--onboard-outcomes",
        default="",
        help=(
            "Optionally add recommendations for these outcomes to the existing "
            "Paper watchlist. This never submits an exchange order."
        ),
    )
    parser.add_argument("--onboard-limit-per-outcome", type=int, default=1)
    parser.add_argument("--onboard-strategy-id", default="maker-calibration")
    args = parser.parse_args(argv)

    control_source = "default_postgres"
    if load_paper_control_plane_env(args.paper_control_env):
        control_source = "paper_control_plane"
    connection_factory = paper_control_plane_connection_factory(
        os.environ,
        fallback_connection_factory=postgres_connection,
    )
    recent_candidates = load_recent_maker_trade_pool(
        lookback_seconds=max(1, args.lookback_seconds),
        limit=max(1, args.discovery_limit),
        connection_factory=connection_factory,
    )
    current_candidates = load_maker_candidate_pool(
        limit=max(1, args.discovery_limit),
        max_age_seconds=300,
        connection_factory=connection_factory,
    )
    discovery_source = "recent_trade_plus_current_redundant_books"
    if len(current_candidates) < max(1, args.discovery_limit):
        registry_candidates = load_maker_discovery_pool(
            limit=max(1, args.discovery_limit),
            connection_factory=connection_factory,
        )
        current_by_asset = {
            str(candidate["asset_id"]): dict(candidate)
            for candidate in registry_candidates
        }
        for candidate in current_candidates:
            asset_id = str(candidate["asset_id"])
            current_by_asset[asset_id] = {
                **current_by_asset.get(asset_id, {}),
                **dict(candidate),
            }
        current_candidates = list(current_by_asset.values())
        discovery_source = (
            "recent_trade_plus_redundant_books_plus_live_registry_"
            "rest_reconciliation"
        )
    candidates_by_asset = {
        str(candidate["asset_id"]): dict(candidate)
        for candidate in current_candidates
    }
    for candidate in recent_candidates:
        asset_id = str(candidate["asset_id"])
        candidates_by_asset[asset_id] = {
            **candidates_by_asset.get(asset_id, {}),
            **dict(candidate),
        }
    candidates = list(candidates_by_asset.values())
    discovered_count = len(candidates)
    observed_at = datetime.now(timezone.utc)
    proxy_url = str(args.clob_proxy_url or "").strip() or None
    gamma_error = None
    try:
        gamma_markets = asyncio.run(
            PolymarketGammaClient(
                proxy_url=proxy_url,
                timeout_seconds=5,
                max_retries=2,
            ).fetch_top_active_markets(max_markets=max(1, args.discovery_limit))
        )
    except Exception as exc:  # noqa: BLE001 - persisted in the plan.
        gamma_markets = []
        gamma_error = f"{exc.__class__.__name__}:{str(exc)[:300]}"
    candidates = enrich_candidates_with_gamma_activity(candidates, gamma_markets)
    public_trade_error = None
    try:
        public_trades = PolymarketDataTradesClient(
            proxy_url=proxy_url,
            timeout_seconds=10,
        ).fetch_recent_taker_trades(limit=max(1, args.public_trade_limit))
    except Exception as exc:  # noqa: BLE001 - persisted in the plan.
        public_trades = ()
        public_trade_error = f"{exc.__class__.__name__}:{str(exc)[:300]}"
    candidates = enrich_candidates_with_recent_public_trades(
        candidates,
        public_trades,
        observed_at=observed_at,
        lookback_seconds=max(1, args.public_trade_lookback_seconds),
    )

    rest_error = None
    try:
        rest_books = asyncio.run(
            PolymarketClobClient(
                proxy_url=proxy_url,
                timeout_seconds=5,
                max_retries=2,
            ).get_books(
                [str(candidate["asset_id"]) for candidate in candidates],
                batch_size=100,
            )
        )
    except Exception as exc:  # noqa: BLE001 - persisted in the plan.
        rest_books = {}
        rest_error = f"{exc.__class__.__name__}:{str(exc)[:300]}"
    candidates = reconcile_candidates_with_rest_books(candidates, rest_books)

    sides = _csv(args.sides)
    placements = _csv(args.placements)
    horizons = tuple(int(value) for value in _csv(args.horizons_seconds))
    evidence_requests: dict[str, dict[str, object]] = {}
    for side in sides:
        for placement in placements:
            preselected, _ = preselect_maker_candidates(
                candidates,
                side=side,
                placement=placement,
                order_size=args.size,
                max_notional_usd=args.max_notional_usd,
                limit=max(1, args.candidates_per_stratum),
            )
            for candidate in preselected:
                asset_id = str(candidate["asset_id"])
                price = Decimal(str(candidate["preselection_limit_price"]))
                request_key = maker_evidence_request_key(
                    asset_id=asset_id,
                    maker_side=side,
                    limit_price=price,
                )
                evidence_requests[request_key] = {
                    "request_key": request_key,
                    "asset_id": asset_id,
                    "maker_side": side,
                    "limit_price": price,
                }
    batch_evidence_error = None
    orderfilled_client = OrderFilledEvidenceClient()
    try:
        orderfilled_watermark = orderfilled_client.coverage_watermark()
    except Exception as exc:  # noqa: BLE001 - persisted in the plan.
        orderfilled_watermark = {"block_number": None, "block_time": None}
        batch_evidence_error = f"{exc.__class__.__name__}:{str(exc)[:500]}"
    watermark_at = orderfilled_watermark.get("block_time")
    evidence_end = (
        min(observed_at, watermark_at)
        if isinstance(watermark_at, datetime)
        else observed_at
    )
    evidence_lag_seconds = max(
        0,
        int((observed_at - evidence_end).total_seconds()),
    )
    try:
        summaries = orderfilled_client.summarize_compatible_maker_volume_batch(
            evidence_requests.values(),
            start=evidence_end
            - timedelta(seconds=max(1, args.lookback_seconds)),
            end=evidence_end,
        )
    except Exception as exc:  # noqa: BLE001 - persisted in the plan.
        summaries = {}
        batch_evidence_error = f"{exc.__class__.__name__}:{str(exc)[:500]}"

    report = plan_stratified_holdout_campaign(
        candidates,
        evidence_client=CachedMakerTradeEvidenceClient(summaries),
        order_size=args.size,
        max_notional_usd=args.max_notional_usd,
        lookback_seconds=max(1, args.lookback_seconds),
        sides=sides,
        placements=placements,
        horizons_seconds=horizons,
        candidates_per_stratum=max(1, args.candidates_per_stratum),
        recommendations_per_outcome=max(1, args.recommendations_per_outcome),
        observed_at=observed_at,
    )
    onboard_outcomes = tuple(value.upper() for value in _csv(args.onboard_outcomes))
    onboard_asset_ids: list[str] = []
    for outcome in onboard_outcomes:
        recommendations = report.get("recommendations", {}).get(outcome, [])
        onboard_asset_ids.extend(
            str(item["asset_id"])
            for item in recommendations[: max(0, args.onboard_limit_per_outcome)]
        )
    onboard_asset_ids = list(dict.fromkeys(onboard_asset_ids))
    onboarded_count = 0
    refresh_requested: list[str] = []
    onboarding_error = None
    if onboard_asset_ids:
        if control_source != "paper_control_plane":
            onboarding_error = "explicit_paper_control_plane_required"
        else:
            try:
                store = LiveShadowStore(connection_factory)
                onboarded_count = store.ensure_calibration_watch_batch(
                    onboard_asset_ids,
                    strategy_id=str(args.onboard_strategy_id),
                    reason="maker_calibration_candidate",
                )
                refresh_requested = store.request_book_refresh_batch(
                    onboard_asset_ids
                )
            except Exception as exc:  # noqa: BLE001 - retained in audit output.
                onboarding_error = f"{exc.__class__.__name__}:{str(exc)[:500]}"
    report.update(
        {
            "control_source": control_source,
            "candidate_source": "existing_paper_live_lob+maker_trade_events+clob_rest",
            "discovery_source": discovery_source,
            "discovered_recent_trade_assets": discovered_count,
            "gamma_activity_rows": len(gamma_markets),
            "gamma_activity_error": gamma_error,
            "recent_public_trade_rows": len(public_trades),
            "recent_public_trade_error": public_trade_error,
            "recent_public_trade_is_submission_truth": False,
            "rest_books_reconciled": sum(
                bool(candidate.get("rest_reconciled")) for candidate in candidates
            ),
            "rest_book_adapter_error": rest_error,
            "batch_trade_evidence_requests": len(evidence_requests),
            "batch_trade_evidence_summaries": len(summaries),
            "batch_trade_evidence_error": batch_evidence_error,
            "orderfilled_watermark": orderfilled_watermark,
            "planning_evidence_as_of": evidence_end,
            "planning_evidence_lag_seconds": evidence_lag_seconds,
            "planning_evidence_current": evidence_lag_seconds <= 120,
            "live_recheck_required_before_submission": True,
            "delayed_evidence_is_submission_truth": False,
            "lob_collector_changed": False,
            "watchlist_changed": bool(onboarded_count or refresh_requested),
            "onboard_requested_outcomes": list(onboard_outcomes),
            "onboard_requested_asset_ids": onboard_asset_ids,
            "onboarded_count": onboarded_count,
            "refresh_requested_asset_ids": refresh_requested,
            "onboarding_error": onboarding_error,
        }
    )
    if report["status"] == "CAMPAIGN_READY" and evidence_lag_seconds > 120:
        report["status_before_freshness_gate"] = report["status"]
        report["status"] = "CANDIDATES_READY_LIVE_RECHECK_REQUIRED"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    compact = {
        key: report[key]
        for key in (
            "status",
            "evidence_status",
            "exchange_submit_called",
            "candidate_source",
            "discovery_source",
            "candidate_count",
            "predicted_outcome_counts",
            "checks",
            "discovered_recent_trade_assets",
            "rest_books_reconciled",
            "gamma_activity_error",
            "rest_book_adapter_error",
        )
    }
    compact["output"] = str(args.output.resolve())
    print(json.dumps(compact, indent=2, sort_keys=True), flush=True)
    return (
        0
        if report["status"]
        in {"CAMPAIGN_READY", "CANDIDATES_READY_LIVE_RECHECK_REQUIRED"}
        else 2
    )


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(value).split(",") if item.strip())


if __name__ == "__main__":
    raise SystemExit(main())
