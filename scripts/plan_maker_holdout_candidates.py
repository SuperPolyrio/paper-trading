#!/usr/bin/env python3
"""Build a read-only maker holdout candidate plan without submitting orders."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.adapters.polymarket_clob_client import PolymarketClobClient
from quant.adapters.polymarket_data_trades_client import PolymarketDataTradesClient
from quant.adapters.polymarket_gamma_client import PolymarketGammaClient
from quant.core.db import postgres_connection
from quant.maker.candidate_planner import (
    enrich_candidates_with_gamma_activity,
    enrich_candidates_with_recent_public_trades,
    load_maker_candidate_pool,
    load_maker_discovery_pool,
    load_recent_maker_trade_pool,
    preselect_maker_candidates,
    rank_maker_candidates,
    reconcile_candidates_with_rest_books,
)
from quant.maker.trade_evidence import (
    CachedMakerTradeEvidenceClient,
    PersistedMakerTradeEvidenceClient,
)
from quant.paper.control_plane_connection import (
    load_paper_control_plane_env,
    paper_control_plane_connection_factory,
)
from quant.paper.live_shadow_store import LiveShadowStore
from quant.paper.paired_probe import OrderFilledEvidenceClient


DEFAULT_MAKER_API_PROXY_URL = "http://127.0.0.1:18080"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--side", choices=("BUY", "SELL"), default="BUY")
    parser.add_argument(
        "--placement",
        choices=(
            "AT_BEST",
            "ONE_TICK_BEHIND",
            "ONE_TICK_INSIDE_SPREAD",
            "NEAR_OPPOSITE",
            "ADAPTIVE_FRONT",
        ),
        default="ADAPTIVE_FRONT",
    )
    parser.add_argument("--size", type=Decimal, default=Decimal(5))
    parser.add_argument("--resting-seconds", type=Decimal, default=Decimal(60))
    parser.add_argument("--lookback-seconds", type=int, default=3600)
    parser.add_argument("--candidate-limit", type=int, default=200)
    parser.add_argument("--discovery-limit", type=int, default=1_000)
    parser.add_argument(
        "--gamma-discovery-limit",
        type=int,
        default=500,
        help="Official Gamma activity rows used only to prioritize discovery",
    )
    parser.add_argument("--public-trade-limit", type=int, default=10_000)
    parser.add_argument("--public-trade-lookback-seconds", type=int, default=1800)
    parser.add_argument(
        "--targeted-public-trade-lookups",
        type=int,
        default=24,
        help=(
            "Bounded per-condition official trade lookups after global discovery; "
            "used only to select prospective own-order label probes"
        ),
    )
    parser.add_argument(
        "--onboard-top",
        type=int,
        default=0,
        help=(
            "Idempotently add the top REST-qualified candidates to the paper "
            "real-time watchlist; this never submits an exchange order"
        ),
    )
    parser.add_argument(
        "--onboard-strategy-id",
        default="maker-calibration",
        help="Paper watchlist owner used by --onboard-top",
    )
    parser.add_argument(
        "--replace-onboard-set",
        action="store_true",
        help=(
            "Replace only the ephemeral rows owned by --onboard-strategy-id; "
            "active intents, positions, probes and other strategies are preserved"
        ),
    )
    parser.add_argument(
        "--active-probe-dir",
        type=Path,
        default=Path("runtime_outputs/maker_calibration/probes"),
        help="Preserve assets belonging to submitted probes while rotating candidates",
    )
    parser.add_argument(
        "--active-probe-grace-seconds",
        type=int,
        default=3600,
        help="Keep completed probe assets warm for this reconciliation grace period",
    )
    parser.add_argument(
        "--discovery-scope",
        choices=("ALL_LIVE", "WATCHLIST_STRICT", "HOT_WS_TRADES"),
        default="ALL_LIVE",
        help="Search all live execution tokens before requiring paper watch readiness",
    )
    parser.add_argument("--max-book-age-seconds", type=float, default=60.0)
    parser.add_argument(
        "--max-notional-usd",
        type=Decimal,
        default=Decimal(5),
        help=(
            "Candidate-study ceiling only; no order is submitted. Five dollars "
            "covers the venue's five-share minimum across ordinary prices."
        ),
    )
    parser.add_argument(
        "--required-outcome",
        choices=("ANY", "NO_FILL", "PARTIAL", "FULL", "PARTIAL_OR_FULL"),
        default="ANY",
        help="Fail the selection gate when the requested calibration outcome is absent",
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
        help="Explicit Paper authority connection env; falls back to the default DB",
    )
    parser.add_argument(
        "--clob-proxy-url",
        default=os.environ.get(
            "POLY_QUANT_MAKER_CLOB_PROXY_URL",
            DEFAULT_MAKER_API_PROXY_URL,
        ),
        help="Isolated proxy used by the read-only CLOB, Gamma, and Data adapters",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/maker/candidate-plan-current.json"),
    )
    args = parser.parse_args(argv)
    control_source = "default_postgres"
    if load_paper_control_plane_env(args.paper_control_env):
        control_source = "paper_control_plane"
    connection_factory = paper_control_plane_connection_factory(
        os.environ,
        fallback_connection_factory=postgres_connection,
    )
    if args.discovery_scope == "ALL_LIVE":
        candidates = load_maker_discovery_pool(
            limit=max(1, args.discovery_limit),
            connection_factory=connection_factory,
        )
    elif args.discovery_scope == "WATCHLIST_STRICT":
        candidates = load_maker_candidate_pool(
            limit=max(1, args.discovery_limit),
            max_age_seconds=max(0.1, args.max_book_age_seconds),
            connection_factory=connection_factory,
        )
    else:
        candidates = load_recent_maker_trade_pool(
            lookback_seconds=max(1, args.lookback_seconds),
            limit=max(1, args.discovery_limit),
            connection_factory=connection_factory,
        )
    discovered_count = len(candidates)
    observed_at = datetime.now(timezone.utc)
    gamma_error = None
    try:
        gamma_markets = asyncio.run(
            PolymarketGammaClient(
                proxy_url=str(args.clob_proxy_url or "").strip() or None,
                timeout_seconds=5,
                max_retries=2,
            ).fetch_top_active_markets(
                max_markets=max(1, int(args.gamma_discovery_limit)),
            )
        )
    except Exception as exc:  # noqa: BLE001 - persisted in the audit report.
        gamma_error = f"{exc.__class__.__name__}:{str(exc)[:300]}"
        gamma_markets = []
    candidates = enrich_candidates_with_gamma_activity(candidates, gamma_markets)
    public_trade_error = None
    public_trade_client = PolymarketDataTradesClient(
        proxy_url=str(args.clob_proxy_url or "").strip() or None,
        timeout_seconds=10,
    )
    try:
        public_trades = public_trade_client.fetch_recent_taker_trades(
            limit=max(1, args.public_trade_limit)
        )
    except Exception as exc:  # noqa: BLE001 - persisted in the audit report.
        public_trade_error = f"{exc.__class__.__name__}:{str(exc)[:300]}"
        public_trades = ()
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
                proxy_url=str(args.clob_proxy_url or "").strip() or None,
                timeout_seconds=5,
                max_retries=2,
            ).get_books(
                [str(candidate["asset_id"]) for candidate in candidates],
                batch_size=100,
            )
        )
    except Exception as exc:  # noqa: BLE001 - report a bounded adapter failure.
        rest_error = f"{exc.__class__.__name__}:{str(exc)[:300]}"
        rest_books = {}
    candidates = reconcile_candidates_with_rest_books(candidates, rest_books)
    rest_reconciled_count = sum(
        bool(candidate.get("rest_reconciled")) for candidate in candidates
    )
    candidates, preselection_rejected = preselect_maker_candidates(
        candidates,
        side=args.side,
        placement=args.placement,
        order_size=args.size,
        max_notional_usd=args.max_notional_usd,
        limit=max(1, args.candidate_limit),
    )
    candidates, targeted_public_trade_report = (
        _refresh_candidates_with_targeted_public_trades(
            candidates,
            client=public_trade_client,
            max_lookups=max(0, int(args.targeted_public_trade_lookups)),
            observed_at=observed_at,
            lookback_seconds=max(1, args.public_trade_lookback_seconds),
        )
    )
    candidates, targeted_preselection_rejected = preselect_maker_candidates(
        candidates,
        side=args.side,
        placement=args.placement,
        order_size=args.size,
        max_notional_usd=args.max_notional_usd,
        limit=max(1, args.candidate_limit),
    )
    preselection_rejected.extend(targeted_preselection_rejected)
    warmup_candidates = candidates[: max(0, int(args.onboard_top))]
    protected_probe_assets = _load_protected_probe_assets(
        args.active_probe_dir,
        grace_seconds=max(0, int(args.active_probe_grace_seconds)),
        observed_at=observed_at,
    )
    onboarded_count = 0
    disabled_count = 0
    refresh_requested: list[str] = []
    onboarding_error = None
    if warmup_candidates:
        try:
            store = LiveShadowStore(connection_factory)
            warmup_asset_ids = list(
                dict.fromkeys(
                    [str(candidate["asset_id"]) for candidate in warmup_candidates]
                    + sorted(protected_probe_assets)
                )
            )
            if args.replace_onboard_set:
                synchronized = store.synchronize_calibration_watch_batch(
                    warmup_asset_ids,
                    strategy_id=str(args.onboard_strategy_id),
                    reason="maker_calibration_candidate",
                )
                onboarded_count = int(synchronized["upserted"])
                disabled_count = int(synchronized["disabled"])
            else:
                onboarded_count = store.ensure_calibration_watch_batch(
                    warmup_asset_ids,
                    strategy_id=str(args.onboard_strategy_id),
                    reason="maker_calibration_candidate",
                )
            refresh_requested = store.request_book_refresh_batch(warmup_asset_ids)
        except Exception as exc:  # noqa: BLE001 - persisted in the audit report.
            onboarding_error = f"{exc.__class__.__name__}:{str(exc)[:500]}"
    batch_evidence_error = None
    if args.discovery_scope == "ALL_LIVE":
        requests = [
            {
                "asset_id": candidate["asset_id"],
                "maker_side": args.side,
                "limit_price": candidate["preselection_limit_price"],
            }
            for candidate in candidates
        ]
        try:
            summaries = (
                OrderFilledEvidenceClient().summarize_compatible_maker_volume_batch(
                    requests,
                    start=observed_at
                    - timedelta(seconds=max(1, args.lookback_seconds)),
                    end=observed_at,
                )
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the immutable report.
            batch_evidence_error = f"{exc.__class__.__name__}:{str(exc)[:500]}"
            summaries = {}
        evidence_client = CachedMakerTradeEvidenceClient(summaries)
    else:
        evidence_client = PersistedMakerTradeEvidenceClient(connection_factory)
    payload = rank_maker_candidates(
        candidates,
        evidence_client=evidence_client,
        side=args.side,
        placement=args.placement,
        order_size=args.size,
        resting_seconds=args.resting_seconds,
        lookback_seconds=max(1, args.lookback_seconds),
        max_notional_usd=args.max_notional_usd,
        observed_at=observed_at,
    )
    payload["candidate_source"] = (
        f"{control_source}+{args.discovery_scope.lower()}+clob_rest"
    )
    payload["control_env_loaded"] = args.paper_control_env.is_file()
    payload["discovered_live_tokens"] = discovered_count
    payload["gamma_activity_rows"] = len(gamma_markets)
    payload["gamma_activity_error"] = gamma_error
    payload["recent_public_trade_rows"] = len(public_trades)
    payload["recent_public_trade_error"] = public_trade_error
    payload["recent_public_trade_is_submission_truth"] = False
    payload["targeted_public_trade_refresh"] = targeted_public_trade_report
    payload["gamma_activity_matched_candidates"] = sum(
        bool(candidate.get("gamma_activity_matched")) for candidate in candidates
    )
    payload["rest_books_requested"] = discovered_count
    payload["rest_books_reconciled"] = rest_reconciled_count
    payload["evidence_candidates_selected"] = len(candidates)
    prospective_candidates = [
        _prospective_label_candidate(
            candidate,
            side=args.side,
            order_size=args.size,
        )
        for candidate in candidates
        if int(candidate.get("recent_public_compatible_trade_count") or 0) > 0
    ]
    payload["prospective_label_candidate_count"] = len(prospective_candidates)
    payload["prospective_label_candidates"] = prospective_candidates
    payload["preselection_rejected_count"] = len(preselection_rejected)
    payload["preselection_rejected"] = preselection_rejected
    payload["rest_book_adapter_error"] = rest_error
    payload["batch_trade_evidence_error"] = batch_evidence_error
    payload["warmup"] = {
        "requested": bool(warmup_candidates),
        "strategy_id": str(args.onboard_strategy_id),
        "candidate_count": len(warmup_candidates),
        "watch_rows_upserted": onboarded_count,
        "watch_rows_disabled": disabled_count,
        "replace_onboard_set": bool(args.replace_onboard_set),
        "refresh_requested_count": len(refresh_requested),
        "error": onboarding_error,
        "exchange_submit_called": False,
        "candidates": [
            {
                "asset_id": str(candidate["asset_id"]),
                "market_id": str(candidate.get("market_id") or ""),
                "market_title": candidate.get("market_title"),
                "market_slug": candidate.get("market_slug"),
                "outcome_name": candidate.get("outcome_name"),
                "limit_price": str(candidate.get("preselection_limit_price") or ""),
                "resolved_placement": candidate.get("resolved_placement"),
            }
            for candidate in warmup_candidates
        ],
    }
    payload["active_probe_lease"] = {
        "checkpoint_dir": str(args.active_probe_dir),
        "grace_seconds": max(0, int(args.active_probe_grace_seconds)),
        "protected_asset_count": len(protected_probe_assets),
        "protected_asset_ids": sorted(protected_probe_assets),
        "exchange_submit_called": False,
    }
    _apply_evidence_readiness_status(payload, warmup_candidates)
    _apply_required_outcome_gate(payload, args.required_outcome)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    return 0 if payload["status"] == "FORECAST_READY" else 2


def _refresh_candidates_with_targeted_public_trades(
    candidates: list[dict[str, Any]],
    *,
    client: PolymarketDataTradesClient,
    max_lookups: int,
    observed_at: datetime,
    lookback_seconds: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recover per-condition tape hidden by the bounded global trade page."""

    if max_lookups <= 0:
        return candidates, {
            "status": "DISABLED",
            "max_lookups": 0,
            "condition_lookups": 0,
            "assets_refreshed": 0,
            "errors": [],
            "prediction_truth_claimed": False,
            "exchange_submit_called": False,
        }

    by_condition: dict[str, tuple[dict[str, Any], ...]] = {}
    errors: list[dict[str, str]] = []
    refreshed: list[dict[str, Any]] = []
    assets_refreshed = 0
    for candidate in candidates:
        if int(candidate.get("recent_public_compatible_trade_count") or 0) > 0:
            refreshed.append(candidate)
            continue
        condition_id = str(candidate.get("condition_id") or "").strip()
        if not condition_id:
            refreshed.append(candidate)
            continue
        if condition_id not in by_condition:
            if len(by_condition) >= max_lookups:
                refreshed.append(candidate)
                continue
            try:
                by_condition[condition_id] = client.fetch_recent_taker_trades(
                    condition_id=condition_id,
                    limit=500,
                )
            except Exception as exc:  # noqa: BLE001 - retained in the audit report.
                by_condition[condition_id] = ()
                errors.append(
                    {
                        "condition_id": condition_id,
                        "error": f"{exc.__class__.__name__}:{str(exc)[:300]}",
                    }
                )
        candidate = enrich_candidates_with_recent_public_trades(
            [candidate],
            by_condition[condition_id],
            observed_at=observed_at,
            lookback_seconds=lookback_seconds,
        )[0]
        refreshed.append(candidate)
        assets_refreshed += 1

    return refreshed, {
        "status": "PASS_WITH_ERRORS" if errors else "PASS",
        "source": "official_data_api_condition_trades",
        "max_lookups": max_lookups,
        "condition_lookups": len(by_condition),
        "assets_refreshed": assets_refreshed,
        "errors": errors[:20],
        "prediction_truth_claimed": False,
        "own_order_execution_truth_claimed": False,
        "exchange_submit_called": False,
    }


def _prospective_label_candidate(
    candidate: dict[str, object], *, side: str, order_size: Decimal
) -> dict[str, object]:
    """Keep current-tape candidates visible when historical truth is delayed."""

    price = Decimal(str(candidate["preselection_limit_price"]))
    size = max(
        Decimal(str(order_size)),
        Decimal(str(candidate.get("min_order_size") or 0)),
    )
    return {
        "asset_id": str(candidate["asset_id"]),
        "market_id": str(candidate.get("market_id") or ""),
        "condition_id": str(candidate.get("condition_id") or ""),
        "market_title": candidate.get("market_title"),
        "market_slug": candidate.get("market_slug"),
        "outcome_name": candidate.get("outcome_name"),
        "category": candidate.get("category"),
        "side": str(side).upper(),
        "resolved_placement": candidate.get("resolved_placement"),
        "limit_price": format(price, "f"),
        "order_size": format(size, "f"),
        "gross_notional_usd": format(price * size, "f"),
        "queue_ahead": format(
            Decimal(str(candidate.get("preselection_queue_ahead") or 0)), "f"
        ),
        "recent_public_compatible_trade_count": int(
            candidate.get("recent_public_compatible_trade_count") or 0
        ),
        "recent_public_compatible_trade_volume": format(
            Decimal(str(candidate.get("recent_public_compatible_trade_volume") or 0)),
            "f",
        ),
        "recent_public_compatible_last_at": candidate.get(
            "recent_public_compatible_last_at"
        ),
        "evidence_class": "PROSPECTIVE_CURRENT_TAPE_ONLY",
        "prediction_truth_claimed": False,
        "requires_targeted_hot_preflight": True,
        "requires_authenticated_own_order_truth": True,
        "exchange_submit_called": False,
    }


def _load_protected_probe_assets(
    checkpoint_dir: Path,
    *,
    grace_seconds: int,
    observed_at: datetime,
) -> set[str]:
    """Protect accepted live probes from periodic watchlist candidate rotation."""

    protected: set[str] = set()
    if not checkpoint_dir.is_dir():
        return protected
    for submission_path in checkpoint_dir.glob("*.submission.json"):
        try:
            submission = json.loads(submission_path.read_text(encoding="utf-8"))
            if not isinstance(submission, dict):
                continue
            asset_id = str(submission.get("asset_id") or "").strip()
            run_id = str(submission.get("run_id") or "").strip()
            if not asset_id or not run_id:
                continue
            recovery_path = submission_path.with_name(f"{run_id}.recovery.json")
            if not recovery_path.is_file():
                protected.add(asset_id)
                continue
            recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
            if not isinstance(recovery, dict):
                protected.add(asset_id)
                continue
            if bool(recovery.get("order_still_open")):
                protected.add(asset_id)
                continue
            completed = bool(
                recovery.get("status") == "CALIBRATABLE"
                and recovery.get("artifact_complete")
            )
            if not completed:
                protected.add(asset_id)
                continue
            completed_at = datetime.fromisoformat(str(recovery["completed_at"]))
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)
            if (
                observed_at - completed_at.astimezone(timezone.utc)
            ).total_seconds() <= max(0, int(grace_seconds)):
                protected.add(asset_id)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return protected


def _apply_evidence_readiness_status(
    payload: dict[str, object], warmup_candidates: list[dict[str, object]]
) -> None:
    if payload.get("status") != "NO_CANDIDATES":
        return
    rejected = payload.get("rejected")
    rows = rejected if isinstance(rejected, list) else []
    reasons = sorted(
        {
            str(row.get("coverage_reason"))
            for row in rows
            if isinstance(row, dict) and row.get("coverage_reason")
        }
    )
    evidence_only = bool(rows) and all(
        isinstance(row, dict) and row.get("reason") == "maker_trade_evidence_not_ready"
        for row in rows
    )
    if evidence_only:
        payload["status"] = (
            "WARMUP_REQUIRED" if warmup_candidates else "EVIDENCE_NOT_READY"
        )
        payload["evidence_status"] = "CURRENT_TRADE_TRUTH_NOT_READY"
        payload["evidence_block_reasons"] = reasons


def _apply_required_outcome_gate(
    payload: dict[str, object], required_outcome: str
) -> None:
    normalized = str(required_outcome).upper()
    required = (
        {"PARTIAL", "FULL"}
        if normalized == "PARTIAL_OR_FULL"
        else set()
        if normalized == "ANY"
        else {normalized}
    )
    counts = payload.get("predicted_outcome_counts")
    target_counts = counts if isinstance(counts, dict) else {}
    available = sorted(
        outcome for outcome in required if int(target_counts.get(outcome) or 0) > 0
    )
    pool_status = str(payload.get("status") or "NO_CANDIDATES")
    passed = pool_status == "FORECAST_READY" and (not required or bool(available))
    payload["candidate_pool_status"] = pool_status
    payload["outcome_selection_gate"] = {
        "required_outcome": normalized,
        "required_targets": sorted(required),
        "available_required_targets": available,
        "status": "PREDICTED_TARGET_AVAILABLE" if passed else "NO_TARGET_CANDIDATES",
    }
    if pool_status == "FORECAST_READY" and not passed:
        payload["status"] = "NO_TARGET_CANDIDATES"


if __name__ == "__main__":
    raise SystemExit(main())
