"""Controlled one-order post-only maker holdout runner."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from datetime import time as datetime_time
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant.adapters.polymarket_data_trades_client import PolymarketDataTradesClient
from quant.calibration.calibration_domain import ModelState, payload_hash
from quant.calibration.kill_switch import KillSwitch
from quant.calibration.order_rest_reconciler import reconcile_order_lifecycle
from quant.calibration.probe_plan import ProbePlan, validate_probe_plan
from quant.calibration.probe_risk_guard import evaluate_preflight
from quant.calibration.probe_scheduler import (
    NoSubmitProbeRunner,
    _decorate_candidate,
    _with_market_parameters,
)
from quant.calibration.real_live_adapter import (
    OrderSubmissionRejected,
    SubmitOutcomeUnknown,
)
from quant.calibration.user_ws_recorder import DurableUserWsEventJournal
from quant.execution.models.maker_probability_calibration import (
    MakerProbabilityCalibrationArtifact,
)
from quant.execution.models.maker_queue import (
    MakerQueueEngine,
    MakerQueueState,
    QueueModel,
    poisson_arrival_probability,
)
from quant.maker.hot_preflight import acquire_hot_preflight_candidate
from quant.maker.own_order_truth import (
    OwnOrderFilledTruthReconciler,
    trade_transaction_hashes,
)
from quant.maker.probe_sizing import (
    plan_maker_probe_size,
    validate_maker_probe_size,
)
from quant.maker.trade_evidence import (
    BestAvailableMakerTradeEvidenceClient,
    PersistedMakerTradeEvidenceClient,
)

PLACEMENTS = {
    "AT_BEST",
    "ONE_TICK_BEHIND",
    "ONE_TICK_INSIDE_SPREAD",
    "NEAR_OPPOSITE",
    "ADAPTIVE_FRONT",
}
MAKER_FORECAST_LOOKBACK_SECONDS = 3600


class MakerLiveProbeRunner(NoSubmitProbeRunner):
    """Submit at most one post-only order, then cancel and reconcile it."""

    def __init__(
        self,
        plan: ProbePlan,
        *,
        asset_id: str,
        market_id: str,
        placement: str = "ONE_TICK_BEHIND",
        resting_seconds: float = 10.0,
        post_cancel_seconds: float = 30.0,
        output_dir: Path | None = None,
        holdout_path: Path | None = None,
        evidence_client: Any | None = None,
        onchain_truth_reconciler: OwnOrderFilledTruthReconciler | None = None,
        required_predicted_outcome: str = "ANY",
        cancel_on_first_fill: bool = False,
        probe_target: str = "GENERAL",
        probability_calibration_artifact: (
            MakerProbabilityCalibrationArtifact | None
        ) = None,
        probability_calibration_path: Path | None = None,
        targeted_hot_preflight: bool = False,
        hot_preflight_proxy_url: str | None = None,
        hot_preflight_timeout_seconds: float = 10.0,
        hot_preflight_activity_seconds: float = 0.0,
        hot_preflight_capture: Any | None = None,
        public_trade_client: Any | None = None,
        public_trade_lookback_seconds: int = 900,
        allow_incomplete_trade_evidence_for_calibration: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(plan, target_asset_id=asset_id, **kwargs)
        self.asset_id = str(asset_id)
        self.market_id = str(market_id)
        self.placement = str(placement).upper()
        self.resting_seconds = max(1.0, float(resting_seconds))
        self.post_cancel_seconds = max(1.0, float(post_cancel_seconds))
        self.output_dir = output_dir or (
            self.project_root / "runtime_outputs/maker_calibration/probes"
        )
        self.holdout_path = holdout_path or (
            self.project_root / "runtime_outputs/maker_calibration/holdout.jsonl"
        )
        self.evidence_client = evidence_client or BestAvailableMakerTradeEvidenceClient(
            live_client=PersistedMakerTradeEvidenceClient(
                self.shadow_store.connection_factory
            )
        )
        self.onchain_truth_reconciler = (
            onchain_truth_reconciler or OwnOrderFilledTruthReconciler()
        )
        self.required_predicted_outcome = str(required_predicted_outcome).upper()
        self.probe_target = str(probe_target).upper()
        if self.probe_target not in {"GENERAL", "FULL", "PARTIAL"}:
            raise ValueError("probe_target must be GENERAL, FULL or PARTIAL")
        self.cancel_on_first_fill = bool(
            cancel_on_first_fill or self.probe_target == "PARTIAL"
        )
        self.targeted_hot_preflight = bool(targeted_hot_preflight)
        self.hot_preflight_proxy_url = hot_preflight_proxy_url
        self.hot_preflight_timeout_seconds = max(
            0.1, float(hot_preflight_timeout_seconds)
        )
        self.hot_preflight_activity_seconds = max(
            0.0, float(hot_preflight_activity_seconds)
        )
        self.hot_preflight_capture = hot_preflight_capture
        self.public_trade_client = public_trade_client or PolymarketDataTradesClient(
            proxy_url=hot_preflight_proxy_url,
            timeout_seconds=min(10.0, self.hot_preflight_timeout_seconds),
        )
        self.public_trade_lookback_seconds = max(1, int(public_trade_lookback_seconds))
        self.allow_incomplete_trade_evidence_for_calibration = bool(
            allow_incomplete_trade_evidence_for_calibration
        )
        calibration_path = probability_calibration_path or Path(
            os.environ.get(
                "PAPER_MAKER_PROBABILITY_CALIBRATION_ARTIFACT",
                str(
                    self.project_root
                    / "runtime_outputs/maker_calibration/probability-current.json"
                ),
            )
        )
        self.probability_calibration_artifact = probability_calibration_artifact
        if self.probability_calibration_artifact is None and calibration_path.is_file():
            self.probability_calibration_artifact = (
                MakerProbabilityCalibrationArtifact.load(calibration_path)
            )

    def run_probe(self, *, live: bool) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        run_id = payload_hash(
            {
                "mode": "live" if live else "no-submit",
                "plan_hash": self.plan.plan_hash,
                "asset_id": self.asset_id,
                "market_id": self.market_id,
                "placement": self.placement,
                "required_predicted_outcome": self.required_predicted_outcome,
                "probe_target": self.probe_target,
                "started": started,
            },
            prefix="maker-calrun-",
        )
        payload: dict[str, Any] = {
            "schema_version": "maker_post_only_live_probe_v1",
            "run_id": run_id,
            "mode": "LIVE" if live else "NO_SUBMIT",
            "status": "STARTED",
            "started_at": started.isoformat(),
            "asset_id": self.asset_id,
            "market_id": self.market_id,
            "placement": self.placement,
            "required_predicted_outcome": self.required_predicted_outcome,
            "cancel_on_first_fill": self.cancel_on_first_fill,
            "probe_target": self.probe_target,
            "resting_seconds": self.resting_seconds,
            "post_cancel_seconds": self.post_cancel_seconds,
            "targeted_hot_preflight": self.targeted_hot_preflight,
            "hot_preflight_activity_seconds": self.hot_preflight_activity_seconds,
            "allow_incomplete_trade_evidence_for_calibration": (
                self.allow_incomplete_trade_evidence_for_calibration
            ),
            "exchange_submit_called": False,
            "exact_cancel_called": False,
            "emergency_cancel_all_called": False,
            "errors": [],
        }
        try:
            issues = self._validate(live=live)
            if issues:
                payload.update(status="BLOCKED", errors=issues)
                return self._persist(payload)

            self.calibration_store.ensure_schema()
            self.shadow_store.ensure_schema()
            candidate = self._candidate()
            user_ws = self.user_ws.probe_connection([str(candidate["condition_id"])])
            market = self.adapter.get_market_snapshot(
                asset_id=self.asset_id,
                condition_id=str(candidate["condition_id"]),
            )
            account_before = self.adapter.get_account_snapshot(asset_id=self.asset_id)
            alignment_deadline = time.monotonic() + 5.0
            while True:
                refreshed = self._candidate()
                market = {
                    **market,
                    **self.adapter.get_book_snapshot(asset_id=self.asset_id),
                }
                candidate = _with_market_parameters(refreshed, market)
                if rest_bbo_matches(
                    candidate,
                    market,
                    side=self.plan.execution.sides[0],
                ):
                    if candidate.get("hot_preflight"):
                        hot_evidence = dict(candidate["hot_preflight"])
                        hot_evidence["rest_bbo_match"] = True
                        hot_evidence["rest_book_observed_at"] = market.get(
                            "rest_book_observed_at"
                        )
                        hot_evidence["rest_book_hash"] = market.get("rest_book_hash")
                        candidate.update(
                            {
                                "rest_book_match": True,
                                "redundant_feed_match": True,
                                "book_validation_mode": (
                                    "GCP_REGISTRY_PLUS_TARGETED_NATIVE_WS_PLUS_REST"
                                ),
                                "hot_preflight": hot_evidence,
                            }
                        )
                    break
                if time.monotonic() >= alignment_deadline:
                    raise RuntimeError(
                        "local and REST BBO did not align within five seconds"
                    )
                time.sleep(0.25)
            rest_minimum = Decimal(str(market.get("min_order_size") or 0))
            if self.plan.execution.amount < rest_minimum:
                raise RuntimeError(
                    "maker size is below the current REST book minimum "
                    f"({self.plan.execution.amount} < {rest_minimum})"
                )
            payload["candidate"] = _json_value(_decorate_candidate(candidate))
            payload["market_snapshot"] = _json_value(market)
            payload["account_before"] = account_before.as_dict()
            price = maker_limit_price(
                candidate,
                side=self.plan.execution.sides[0],
                placement=self.placement,
                tick_size=Decimal(str(market["tick_size"])),
            )
            payload["intent"] = {
                "side": self.plan.execution.sides[0],
                "order_type": self.plan.execution.order_types[0],
                "post_only": True,
                "size": str(self.plan.execution.amount),
                "amount_unit": "SHARES",
                "limit_price": format(price, "f"),
                "gross_notional_usd": format(self.plan.execution.amount * price, "f"),
                "resolved_placement": resolved_maker_placement(
                    candidate,
                    side=self.plan.execution.sides[0],
                    placement=self.placement,
                    tick_size=Decimal(str(market["tick_size"])),
                ),
            }
            payload["maker_trade_forecast"] = maker_trade_forecast(
                self.evidence_client,
                asset_id=self.asset_id,
                side=self.plan.execution.sides[0],
                price=price,
                horizon_seconds=Decimal(str(self.resting_seconds)),
            )
            forecast = payload["maker_trade_forecast"]
            try:
                public_activity = (
                    self.public_trade_client.summarize_compatible_maker_activity(
                        condition_id=str(candidate["condition_id"]),
                        asset_id=self.asset_id,
                        maker_side=self.plan.execution.sides[0],
                        limit_price=price,
                        lookback_seconds=self.public_trade_lookback_seconds,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - preserved in probe audit.
                public_activity = {
                    "schema_version": "maker_recent_public_trade_activity_v1",
                    "status": "UNAVAILABLE",
                    "source": "official_data_api_taker_trades",
                    "source_ready": False,
                    "compatible_trade_count": 0,
                    "compatible_trade_volume": "0",
                    "prediction_truth_claimed": False,
                    "own_order_execution_truth_claimed": False,
                    "error": f"{exc.__class__.__name__}:{str(exc)[:300]}",
                }
            payload["recent_public_trade_activity"] = public_activity
            if self.probe_target in {"FULL", "PARTIAL"}:
                sizing = plan_maker_probe_size(
                    target_outcome=self.probe_target,
                    limit_price=price,
                    min_order_size=rest_minimum,
                    median_trade_size=Decimal(
                        str(forecast.get("median_trade_size") or 0)
                    ),
                    max_gross_notional=self.plan.limits.max_order_notional,
                )
                payload["probe_sizing"] = sizing
                payload["probe_sizing"]["requested_size"] = format(
                    self.plan.execution.amount, "f"
                )
                payload["probe_sizing"]["requested_gross_notional_usd"] = format(
                    self.plan.execution.amount * price, "f"
                )
                sizing_issues = validate_maker_probe_size(
                    requested_size=self.plan.execution.amount,
                    sizing_plan=sizing,
                )
                if sizing_issues:
                    payload.update(status="BLOCKED", errors=list(sizing_issues))
                    return self._persist(payload)
            resolved_placement = str(payload["intent"]["resolved_placement"])
            payload["model_predictions"] = maker_predictions(
                candidate,
                asset_id=self.asset_id,
                side=self.plan.execution.sides[0],
                price=price,
                size=self.plan.execution.amount,
                horizon_seconds=Decimal(str(self.resting_seconds)),
                run_id=run_id,
                forecast_trade_volume=Decimal(
                    str(payload["maker_trade_forecast"]["forecast_trade_volume"])
                ),
                aggressor_arrival_probability=Decimal(
                    str(
                        payload["maker_trade_forecast"].get(
                            "aggressor_arrival_probability"
                        )
                        or 0
                    )
                ),
                category=str(
                    candidate.get("source_category")
                    or candidate.get("category")
                    or "unknown"
                ),
                quote_position=resolved_placement,
                observed_trade_count=int(forecast.get("trade_count") or 0),
                observed_trade_volume=Decimal(
                    str(forecast.get("compatible_trade_volume") or 0)
                ),
                lookback_seconds=Decimal(str(forecast.get("lookback_seconds") or 1)),
                probability_calibration=self.probability_calibration_artifact,
                trade_evidence_ready=(forecast.get("status") == "READY"),
            )
            payload["maker_probability_calibration"] = (
                self.probability_calibration_artifact.reference_dict()
                if self.probability_calibration_artifact is not None
                else {"status": "UNAVAILABLE_STRICT_AND_UNCALIBRATED_RESEARCH_ONLY"}
            )
            prediction_snapshot = {
                "schema_version": "maker_prediction_snapshot_v1",
                "run_id": run_id,
                "frozen_at": datetime.now(timezone.utc).isoformat(),
                "asset_id": self.asset_id,
                "market_id": self.market_id,
                "condition_id": candidate.get("condition_id"),
                "side": self.plan.execution.sides[0],
                "size": format(self.plan.execution.amount, "f"),
                "limit_price": format(price, "f"),
                "placement": self.placement,
                "resting_seconds": format(Decimal(str(self.resting_seconds)), "f"),
                "book_checkpoint_id": candidate.get("shadow_checkpoint_id"),
                "book_generation": candidate.get("shadow_generation"),
                "book_observed_at": candidate.get("shadow_observed_at"),
                "coverage_grade": candidate.get("coverage_grade"),
                "book_validation_mode": candidate.get("book_validation_mode"),
                "hot_preflight": candidate.get("hot_preflight"),
                "maker_trade_forecast": payload["maker_trade_forecast"],
                "model_predictions": payload["model_predictions"],
                "maker_probability_calibration": payload[
                    "maker_probability_calibration"
                ],
            }
            payload["prediction_snapshot"] = prediction_snapshot
            payload["prediction_snapshot_hash"] = payload_hash(
                prediction_snapshot,
                prefix="maker-prediction-",
            )
            evidence_admission = maker_trade_evidence_admission(
                payload["maker_trade_forecast"],
                targeted_hot_preflight=self.targeted_hot_preflight,
                probe_target=self.probe_target,
                allow_incomplete=(self.allow_incomplete_trade_evidence_for_calibration),
                placement=resolved_placement,
                hot_preflight=candidate.get("hot_preflight"),
                market_snapshot=market,
                recent_public_trade_activity=public_activity,
            )
            payload["maker_trade_evidence_admission"] = evidence_admission
            if not evidence_admission["allowed"]:
                payload.update(
                    status="BLOCKED",
                    errors=["maker_trade_evidence_not_ready"],
                )
                return self._persist(payload)
            strict_prediction = payload["model_predictions"]["STRICT_TRADE_EVIDENCE"]
            predicted_outcome = actual_outcome(
                Decimal(str(strict_prediction.get("expected_filled_size") or 0)),
                self.plan.execution.amount,
            )
            payload["strict_predicted_outcome"] = predicted_outcome
            if not predicted_outcome_matches(
                predicted_outcome,
                self.required_predicted_outcome,
            ):
                payload.update(
                    status="BLOCKED",
                    errors=[
                        (
                            "maker_prediction_does_not_match_required_outcome:"
                            f"{self.required_predicted_outcome}:{predicted_outcome}"
                        )
                    ],
                )
                return self._persist(payload)

            daily_usage = self.calibration_store.recent_usage(
                since=datetime.combine(
                    started.date(), datetime_time.min, tzinfo=timezone.utc
                )
            )
            hourly_usage = self.calibration_store.recent_usage(
                since=started - timedelta(hours=1)
            )
            maker_usage = self._maker_usage(started)
            context = self._preflight_context(
                candidate,
                market,
                account_before,
                user_ws,
                side=self.plan.execution.sides[0],
                daily_usage={
                    **daily_usage,
                    "gross_quote_amount": (
                        Decimal(str(daily_usage.get("gross_quote_amount") or 0))
                        + maker_usage["daily_gross"]
                    ),
                },
                hourly_usage={
                    **hourly_usage,
                    "submitted_orders": (
                        int(hourly_usage.get("submitted_orders") or 0)
                        + maker_usage["hourly_submitted"]
                    ),
                },
            )
            context["daily_realized_loss"] = self.calibration_store.realized_loss(
                since=datetime.combine(
                    started.date(), datetime_time.min, tzinfo=timezone.utc
                ),
                account_id=str(account_before.funder_address).lower(),
            )
            context["model_state"] = ModelState.CALIBRATING.value
            context["candidate"] = {
                **dict(context["candidate"]),
                "rest_book_match": True,
                "checkpoint_book_age_ms": candidate.get("book_age_ms"),
                "book_age_ms": 0,
                "book_validation_mode": (
                    "GCP_REGISTRY_PLUS_TARGETED_NATIVE_WS_PLUS_REST"
                    if candidate.get("hot_preflight")
                    else "MAKER_SIDE_REST_BBO_MATCH"
                ),
                "rest_book_observed_at": market.get("rest_book_observed_at"),
                "rest_book_hash": market.get("rest_book_hash"),
            }
            context["write_route_geoblock"] = (
                self.adapter.require_write_route_allowed() if live else {}
            )
            context["post_only"] = True
            preflight = evaluate_preflight(
                self.plan,
                context,
                live=live,
                run_id=run_id,
                environ=self.environ,
            )
            payload["preflight"] = preflight.as_dict()
            if not preflight.passed:
                payload.update(status="BLOCKED", errors=list(preflight.issues))
                return self._persist(payload)

            expiration = (
                int(time.time() + self.resting_seconds + self.post_cancel_seconds + 120)
                if self.plan.execution.order_types[0] == "GTD"
                else 0
            )
            prepared = self.adapter.prepare_limit_order(
                asset_id=self.asset_id,
                side=self.plan.execution.sides[0],
                order_type=self.plan.execution.order_types[0],
                size=str(self.plan.execution.amount),
                price=format(price, "f"),
                tick_size=str(market["tick_size"]),
                neg_risk=bool(market["neg_risk"]),
                expiration=expiration,
                post_only=True,
                user_usdc_balance=(
                    str(account_before.collateral.get("balance"))
                    if self.plan.execution.sides[0] == "BUY"
                    else None
                ),
            )
            payload["signed_order_audit"] = prepared.audit.as_dict()
            if not live:
                payload.update(
                    status="NO_SUBMIT_READY",
                    artifact_complete=True,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                return self._persist(payload)

            self.adapter.require_write_route_allowed()
            payload["exchange_submit_called"] = True
            user_ws_journal = DurableUserWsEventJournal(
                self.output_dir / f"{run_id}.user-ws.jsonl"
            )

            def persist_submission(
                submission_value: Any,
                submitted_order_id: str,
                submitted_at: datetime,
            ) -> None:
                submission_audit, submission_response = submission_value
                self._persist_submission_checkpoint(
                    {
                        "schema_version": "maker_submission_checkpoint_v2",
                        "run_id": run_id,
                        "order_id": submitted_order_id,
                        "asset_id": self.asset_id,
                        "market_id": self.market_id,
                        "condition_id": candidate.get("condition_id"),
                        "side": self.plan.execution.sides[0],
                        "size": str(self.plan.execution.amount),
                        "submitted_at": submitted_at.isoformat(),
                        "account_before": account_before.as_dict(),
                        "signed_order_audit": submission_audit.as_dict(),
                        "submission": dict(submission_response),
                        "placement": self.placement,
                        "resting_seconds": self.resting_seconds,
                        "post_cancel_seconds": self.post_cancel_seconds,
                        "cancel_on_first_fill": self.cancel_on_first_fill,
                        "probe_target": self.probe_target,
                        "probe_sizing": payload.get("probe_sizing"),
                        "candidate": payload.get("candidate"),
                        "market_snapshot": payload.get("market_snapshot"),
                        "intent": payload.get("intent"),
                        "maker_trade_forecast": payload.get("maker_trade_forecast"),
                        "model_predictions": payload.get("model_predictions"),
                        "maker_probability_calibration": payload.get(
                            "maker_probability_calibration"
                        ),
                        "prediction_snapshot": payload.get("prediction_snapshot"),
                        "prediction_snapshot_hash": payload.get(
                            "prediction_snapshot_hash"
                        ),
                    }
                )

            submission, cancellation, capture = self.user_ws.submit_watch_cancel(
                probe_id=run_id,
                condition_ids=[str(candidate["condition_id"])],
                submit=lambda: self.adapter.submit_prepared_once(
                    prepared,
                    run_id=run_id,
                    risk_passed=True,
                ),
                cancel=self.adapter.cancel_order,
                resting_seconds=self.resting_seconds,
                post_cancel_seconds=self.post_cancel_seconds,
                event_sink=user_ws_journal.append,
                submission_sink=persist_submission,
                cancel_on_first_fill=self.cancel_on_first_fill,
                original_size=self.plan.execution.amount,
            )
            audit, response = submission
            order_id = str(response.get("orderID") or response.get("order_id") or "")
            payload["signed_order_audit"] = audit.as_dict()
            payload["submission"] = dict(response)
            payload["order_id"] = order_id
            payload["exact_cancel_called"] = bool(capture.get("cancel_attempted"))
            payload["cancellation"] = _json_value(cancellation)
            payload["user_ws_capture"] = _json_value(capture)
            payload["user_ws_capture"].update(
                {
                    "journal_path": str(user_ws_journal.path),
                    "journal_sha256": user_ws_journal.sha256(),
                    "journal_event_count": len(user_ws_journal.load_events()),
                    "restart_recovery_checkpoint": str(
                        self.output_dir / f"{run_id}.submission.json"
                    ),
                }
            )
            user_events = list(capture.get("events") or ())
            user_event_payloads = [
                dict(event.get("payload") or {}) for event in user_events
            ]
            rest = self.adapter.get_order_reconciliation_snapshot(
                order_id=order_id,
                condition_id=str(candidate["condition_id"]),
                asset_id=self.asset_id,
                after=int(started.timestamp()) - 60,
                before=int(datetime.now(timezone.utc).timestamp()) + 60,
            )
            truth = reconcile_order_lifecycle(
                order_id=order_id,
                user_ws_events=user_event_payloads,
                rest_order=rest.get("order"),
                rest_trades=rest.get("trades") or (),
            )
            matched_size = Decimal(str(truth.get("actual_matched_size") or 0))
            try:
                onchain_orderfilled = self.onchain_truth_reconciler.reconcile(
                    order_id=order_id,
                    asset_id=self.asset_id,
                    expected_matched_size=matched_size,
                    transaction_hashes=trade_transaction_hashes(
                        [*(rest.get("trades") or ()), *user_event_payloads],
                        order_id=order_id,
                    ),
                    window_start=started - timedelta(minutes=2),
                    window_end=datetime.now(timezone.utc) + timedelta(minutes=2),
                )
            except Exception as exc:  # noqa: BLE001
                onchain_orderfilled = {
                    "schema_version": "maker_own_orderfilled_truth_v1",
                    "status": "SOURCE_UNAVAILABLE",
                    "order_id": order_id,
                    "asset_id": self.asset_id,
                    "expected_matched_size": format(matched_size, "f"),
                    "source": "canonical_clickhouse_orderfilled",
                    "error": f"{exc.__class__.__name__}:{str(exc)[:500]}",
                    "used_for_actual_live_outcome": True,
                }
            account_after = self.adapter.get_account_snapshot(asset_id=self.asset_id)
            order_still_open = any(
                str(row.get("id") or row.get("orderID") or "") == order_id
                for row in rest.get("open_orders") or ()
            )
            payload["rest_reconciliation"] = _json_value(rest)
            payload["truth"] = _json_value(truth)
            payload["onchain_orderfilled"] = _json_value(onchain_orderfilled)
            payload["account_after"] = account_after.as_dict()
            payload["account_delta"] = account_delta(
                account_before.as_dict(), account_after.as_dict()
            )
            payload["account_delta_reconciled"] = maker_account_delta_matches_truth(
                side=str(payload["intent"]["side"]),
                matched_size=matched_size,
                quote_amount=Decimal(str(truth.get("actual_quote_amount") or 0)),
                fee=Decimal(str(truth.get("actual_fee") or 0)),
                delta=payload["account_delta"],
            )
            payload["order_still_open"] = order_still_open
            payload["actual_outcome"] = actual_outcome(
                Decimal(str(truth.get("actual_matched_size") or 0)),
                self.plan.execution.amount,
            )
            payload["outcome_observation"] = maker_outcome_observation(
                outcome=str(payload["actual_outcome"]),
                matched_size=matched_size,
                requested_size=self.plan.execution.amount,
                resting_seconds=Decimal(str(self.resting_seconds)),
                capture=capture,
            )
            payload["finality_label"] = maker_finality_label(
                matched_size=matched_size,
                orderfilled=onchain_orderfilled,
            )
            cancel_acknowledged = bool(cancellation) and not capture.get("cancel_error")
            payload["cancel_acknowledged"] = cancel_acknowledged
            payload["artifact_complete"] = maker_probe_artifact_complete(
                order_terminal_evidence=bool(rest.get("order") or cancel_acknowledged),
                order_still_open=order_still_open,
                rest_order_reconciled=bool(truth.get("rest_order_reconciled")),
                order_not_found=(
                    rest.get("order_lookup_error") == "HTTP_404_ORDER_NOT_FOUND"
                ),
                matched_size=matched_size,
                ledger_truth=str(truth.get("ledger_truth") or ""),
                orderfilled_status=str(onchain_orderfilled.get("status") or ""),
                account_delta_reconciled=bool(payload["account_delta_reconciled"]),
            )
            if order_still_open:
                self._emergency_stop(
                    payload, "maker order remained open after exact cancel"
                )
                payload["status"] = "STOPPED_OPEN_ORDER"
            elif capture.get("cancel_error") and not rest.get("order"):
                self._emergency_stop(
                    payload, "cancel outcome unknown and REST order unavailable"
                )
                payload["status"] = "STOPPED_CANCEL_UNKNOWN"
            else:
                payload["status"] = (
                    "CALIBRATABLE"
                    if payload["artifact_complete"]
                    else "PENDING_RECONCILIATION"
                )
            payload["completed_at"] = datetime.now(timezone.utc).isoformat()
            self._append_holdout(payload)
            return self._persist(payload)
        except OrderSubmissionRejected as exc:
            payload["errors"].append(f"{exc.__class__.__name__}:{str(exc)[:500]}")
            if exc.audit is not None:
                payload["signed_order_audit"] = exc.audit.as_dict()
                payload["order_id"] = exc.audit.order_hash
            payload["submission_rejection"] = {
                "status_code": exc.status_code,
                "response": dict(exc.response),
            }
            payload["status"] = "HTTP_REJECTED"
            payload["artifact_complete"] = True
            return self._persist(payload)
        except SubmitOutcomeUnknown as exc:
            payload["errors"].append(f"{exc.__class__.__name__}:{str(exc)[:500]}")
            if exc.audit is not None:
                payload["signed_order_audit"] = exc.audit.as_dict()
                payload["order_id"] = exc.audit.order_hash
            payload["unknown_submit_recovery"] = self._recover_unknown_submit(
                payload,
                order_id=str(payload.get("order_id") or ""),
            )
            self._emergency_stop(payload, "maker submit outcome unknown")
            recovery = payload["unknown_submit_recovery"]
            payload["status"] = (
                "RECOVERED_NO_EXPOSURE"
                if recovery.get("no_exposure_confirmed")
                else "STOPPED_SUBMIT_UNKNOWN"
            )
            return self._persist(payload)
        except Exception as exc:
            payload["errors"].append(f"{exc.__class__.__name__}:{str(exc)[:500]}")
            if payload["exchange_submit_called"]:
                self._emergency_stop(payload, "maker probe exception after submit")
                payload["status"] = "STOPPED_EXCEPTION"
            else:
                payload["status"] = "BLOCKED"
            return self._persist(payload)

    def _validate(self, *, live: bool) -> list[str]:
        issues = validate_probe_plan(
            self.plan,
            live=live,
            require_credentials=True,
            environ=self.environ,
        )
        if self.placement not in PLACEMENTS:
            issues.append("maker_placement_invalid")
        if self.required_predicted_outcome not in {
            "ANY",
            "NO_FILL",
            "PARTIAL",
            "FULL",
            "PARTIAL_OR_FULL",
        }:
            issues.append("maker_required_predicted_outcome_invalid")
        if self.plan.execution.count != 1:
            issues.append("maker_probe_count_must_equal_one")
        if len(self.plan.execution.sides) != 1:
            issues.append("maker_probe_requires_one_side")
        if len(self.plan.execution.order_types) != 1 or set(
            self.plan.execution.order_types
        ) - {"GTC", "GTD"}:
            issues.append("maker_probe_requires_gtc_or_gtd")
        if self.plan.execution.amount_unit != "SHARES":
            issues.append("maker_probe_amount_unit_must_be_shares")
        if len(self.plan.market_policy.allow_market_ids) != 1:
            issues.append("maker_probe_requires_one_allowlisted_market")
        elif self.market_id not in self.plan.market_policy.allow_market_ids:
            issues.append("maker_market_not_allowlisted")
        return sorted(set(issues))

    def _candidate(self) -> dict[str, Any]:
        if self.targeted_hot_preflight:
            kwargs: dict[str, Any] = {}
            if self.hot_preflight_capture is not None:
                kwargs["capture"] = self.hot_preflight_capture
            row = acquire_hot_preflight_candidate(
                self.shadow_store,
                asset_id=self.asset_id,
                market_id=self.market_id,
                proxy_url=self.hot_preflight_proxy_url,
                timeout_seconds=self.hot_preflight_timeout_seconds,
                max_book_age_seconds=max(
                    86_400.0,
                    self.plan.market_policy.max_book_age_ms / 1000,
                ),
                activity_observation_seconds=self.hot_preflight_activity_seconds,
                **kwargs,
            )
            if self.plan.execution.amount < Decimal(
                str(row.get("min_order_size") or 0)
            ):
                raise RuntimeError("maker size is below the market minimum")
            return row
        rows = self._wait_for_shadow_fresh_candidates(
            asset_id=self.asset_id,
            max_age_seconds=max(
                300.0,
                self.plan.market_policy.max_book_age_ms / 1000,
            ),
        )
        row = next(
            (
                dict(item)
                for item in rows
                if str(item.get("market_id") or "") == self.market_id
            ),
            None,
        )
        if row is None:
            raise RuntimeError("approved maker asset is not currently shadow-ready")
        if self.plan.execution.amount < Decimal(str(row.get("min_order_size") or 0)):
            raise RuntimeError("maker size is below the market minimum")
        return row

    def _maker_usage(self, now: datetime) -> dict[str, Any]:
        daily_gross = Decimal(0)
        hourly_submitted = 0
        if not self.holdout_path.exists():
            return {
                "daily_gross": daily_gross,
                "hourly_submitted": hourly_submitted,
            }
        for line in self.holdout_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                observed = datetime.fromisoformat(
                    str(row.get("started_at") or "").replace("Z", "+00:00")
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            if observed.date() == now.date():
                daily_gross += Decimal(
                    str((row.get("intent") or {}).get("gross_notional_usd") or 0)
                )
            if observed >= now - timedelta(hours=1):
                hourly_submitted += 1
        return {
            "daily_gross": daily_gross,
            "hourly_submitted": hourly_submitted,
        }

    def _emergency_stop(self, payload: dict[str, Any], reason: str) -> None:
        try:
            KillSwitch(Path(self.plan.safety.kill_switch_file)).activate(reason)
        except Exception as exc:
            payload["errors"].append(
                f"kill_switch_activate:{exc.__class__.__name__}:{str(exc)[:300]}"
            )
        if self.plan.safety.cancel_all_on_reconciliation_failure:
            payload["emergency_cancel_all_called"] = True
            try:
                payload["emergency_cancel_all"] = self.adapter.cancel_all()
            except Exception as exc:
                payload["errors"].append(
                    f"emergency_cancel_all:{exc.__class__.__name__}:{str(exc)[:300]}"
                )

    def _recover_unknown_submit(
        self,
        payload: Mapping[str, Any],
        *,
        order_id: str,
    ) -> dict[str, Any]:
        candidate = (
            payload.get("candidate")
            if isinstance(payload.get("candidate"), Mapping)
            else {}
        )
        condition_id = str(candidate.get("condition_id") or "")
        if not order_id or not condition_id:
            return {
                "status": "INCOMPLETE",
                "no_exposure_confirmed": False,
                "reason": "order hash or condition id missing",
            }
        try:
            rest = self.adapter.get_order_reconciliation_snapshot(
                order_id=order_id,
                condition_id=condition_id,
                asset_id=self.asset_id,
                after=int(
                    datetime.fromisoformat(
                        str(payload["started_at"]).replace("Z", "+00:00")
                    ).timestamp()
                )
                - 60,
                before=int(datetime.now(timezone.utc).timestamp()) + 60,
            )
            account = self.adapter.get_account_snapshot(asset_id=self.asset_id)
        except Exception as exc:
            return {
                "status": "RECOVERY_FAILED",
                "no_exposure_confirmed": False,
                "reason": f"{exc.__class__.__name__}:{str(exc)[:300]}",
            }
        open_ids = {
            str(row.get("id") or row.get("orderID") or "")
            for row in rest.get("open_orders") or ()
            if isinstance(row, Mapping)
        }
        no_exposure = bool(
            order_id not in open_ids
            and not rest.get("order")
            and not rest.get("trades")
            and not account.open_orders
        )
        return {
            "status": "NO_EXPOSURE_CONFIRMED" if no_exposure else "EVIDENCE_PRESENT",
            "no_exposure_confirmed": no_exposure,
            "order": rest.get("order") or {},
            "trade_count": len(rest.get("trades") or ()),
            "open_order_ids": sorted(item for item in open_ids if item),
            "account_open_order_count": len(account.open_orders),
            "conditional_balance": account.conditional.get("balance"),
            "collateral_balance": account.collateral.get("balance"),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _append_holdout(self, payload: Mapping[str, Any]) -> None:
        row = maker_holdout_row(payload)
        self.holdout_path.parent.mkdir(parents=True, exist_ok=True)
        with self.holdout_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
                + "\n"
            )

    def _persist(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload.setdefault("completed_at", datetime.now(timezone.utc).isoformat())
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{payload['run_id']}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        latest = self.output_dir.parent / "latest.json"
        latest_tmp = latest.with_suffix(".json.tmp")
        latest_tmp.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        latest_tmp.replace(latest)
        payload["artifact_path"] = str(path)
        return payload

    def _persist_submission_checkpoint(self, payload: Mapping[str, Any]) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{payload['run_id']}.submission.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return path


def maker_holdout_row(payload: Mapping[str, Any]) -> dict[str, Any]:
    prediction = (
        payload.get("model_predictions", {}).get("PROBABILISTIC_QUEUE", {})
        if isinstance(payload.get("model_predictions"), Mapping)
        else {}
    )
    truth = payload.get("truth") if isinstance(payload.get("truth"), Mapping) else {}
    intent = payload.get("intent") if isinstance(payload.get("intent"), Mapping) else {}
    outcome = str(payload.get("actual_outcome") or "").upper()
    prediction_snapshot = (
        payload.get("prediction_snapshot")
        if isinstance(payload.get("prediction_snapshot"), Mapping)
        else {}
    )
    first_fill_seconds = _elapsed_seconds(
        payload.get("started_at"), truth.get("first_match_at")
    )
    full_fill_seconds = (
        _elapsed_seconds(payload.get("started_at"), truth.get("last_match_at"))
        if outcome == "FULL"
        else None
    )
    order_size = Decimal(str(intent.get("size") or 0))
    queue_ahead = Decimal(str(prediction.get("queue_ahead_estimate") or 0))
    return {
        "schema_version": "maker_live_holdout_row_v1",
        "run_id": payload.get("run_id"),
        "event_id": (payload.get("candidate") or {}).get("condition_id"),
        "utc_day": str(payload.get("started_at") or "")[:10],
        "artifact_complete": bool(payload.get("artifact_complete")),
        "p_no_fill": prediction.get("p_no_fill"),
        "p_partial": prediction.get("p_partial"),
        "p_full": prediction.get("p_full"),
        "order_size": intent.get("size"),
        "expected_filled_size": prediction.get("expected_filled_size"),
        "actual_outcome": payload.get("actual_outcome"),
        "actual_filled_size": truth.get("actual_matched_size"),
        "expected_time_to_first_fill_seconds": prediction.get(
            "expected_time_to_first_fill_seconds"
        ),
        "actual_time_to_first_fill_seconds": first_fill_seconds,
        "expected_time_to_full_fill_seconds": prediction.get(
            "expected_time_to_full_fill_seconds"
        ),
        "actual_time_to_full_fill_seconds": full_fill_seconds,
        "order_id": payload.get("order_id"),
        "asset_id": payload.get("asset_id"),
        "market_id": payload.get("market_id"),
        "placement": payload.get("placement"),
        "quote_position": payload.get("placement"),
        "resting_seconds": payload.get("resting_seconds"),
        "queue_ahead_estimate": str(queue_ahead),
        "queue_bucket": _maker_queue_ratio_bucket(queue_ahead, order_size),
        "maker_trade_forecast": payload.get("maker_trade_forecast"),
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at"),
        "prediction_frozen_at": prediction_snapshot.get("frozen_at"),
        "prediction_snapshot_hash": payload.get("prediction_snapshot_hash"),
        "outcome_observation": payload.get("outcome_observation"),
        "finality_label": payload.get("finality_label"),
        "truth_reconciled_at": truth.get("reconciled_at"),
    }


def _maker_queue_ratio_bucket(queue_ahead: Decimal, order_size: Decimal) -> str:
    if queue_ahead <= 0:
        return "Q0_FRONT"
    ratio = queue_ahead / max(Decimal("0.000001"), order_size)
    if ratio <= 1:
        return "Q1_LE_1X"
    if ratio <= 5:
        return "Q2_1_TO_5X"
    if ratio <= 20:
        return "Q3_5_TO_20X"
    return "Q4_GT_20X"


def _elapsed_seconds(start: Any, end: Any) -> str | None:
    if start in (None, "") or end in (None, ""):
        return None
    try:
        started = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        ended = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    elapsed = max(0.0, (ended - started).total_seconds())
    return format(Decimal(str(elapsed)), "f")


def maker_limit_price(
    candidate: Mapping[str, Any],
    *,
    side: str,
    placement: str,
    tick_size: Decimal,
) -> Decimal:
    normalized_side = str(side).upper()
    normalized_placement = resolved_maker_placement(
        candidate,
        side=side,
        placement=placement,
        tick_size=tick_size,
    )
    if normalized_side == "BUY":
        price = Decimal(str(candidate["best_bid"]))
        if normalized_placement == "ONE_TICK_BEHIND":
            price -= tick_size
        elif normalized_placement == "ONE_TICK_INSIDE_SPREAD":
            price += tick_size
        elif normalized_placement == "NEAR_OPPOSITE":
            price = Decimal(str(candidate["best_ask"])) - tick_size
        price = max(tick_size, price)
        if price >= Decimal(str(candidate["best_ask"])):
            raise ValueError("post-only BUY would cross the current ask")
    elif normalized_side == "SELL":
        price = Decimal(str(candidate["best_ask"]))
        if normalized_placement == "ONE_TICK_BEHIND":
            price += tick_size
        elif normalized_placement == "ONE_TICK_INSIDE_SPREAD":
            price -= tick_size
        elif normalized_placement == "NEAR_OPPOSITE":
            price = Decimal(str(candidate["best_bid"])) + tick_size
        price = min(Decimal(1) - tick_size, price)
        if price <= Decimal(str(candidate["best_bid"])):
            raise ValueError("post-only SELL would cross the current bid")
    else:
        raise ValueError("maker side must be BUY or SELL")
    if (price / tick_size) != (price / tick_size).to_integral_value():
        raise ValueError("computed maker price is not tick aligned")
    return price


def resolved_maker_placement(
    candidate: Mapping[str, Any],
    *,
    side: str,
    placement: str,
    tick_size: Decimal,
) -> str:
    """Resolve adaptive placement without pretending a one-tick spread has room."""

    normalized = str(placement).upper()
    if normalized != "ADAPTIVE_FRONT":
        return normalized
    best_bid = Decimal(str(candidate["best_bid"]))
    best_ask = Decimal(str(candidate["best_ask"]))
    spread = best_ask - best_bid
    return "ONE_TICK_INSIDE_SPREAD" if spread >= tick_size * 2 else "AT_BEST"


def maker_predictions(
    candidate: Mapping[str, Any],
    *,
    asset_id: str,
    side: str,
    price: Decimal,
    size: Decimal,
    horizon_seconds: Decimal,
    run_id: str,
    forecast_trade_volume: Decimal = Decimal(0),
    aggressor_arrival_probability: Decimal | None = None,
    category: str = "unknown",
    quote_position: str = "AT_BEST",
    observed_trade_count: int = 0,
    observed_trade_volume: Decimal = Decimal(0),
    lookback_seconds: Decimal = Decimal(1),
    probability_calibration: MakerProbabilityCalibrationArtifact | None = None,
    trade_evidence_ready: bool = True,
) -> dict[str, Any]:
    levels = (
        candidate.get("bids") if str(side).upper() == "BUY" else candidate.get("asks")
    )
    displayed = Decimal(0)
    for level in levels if isinstance(levels, list) else ():
        if isinstance(level, Mapping):
            level_price = level.get("price")
            level_size = level.get("size")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            level_price, level_size = level[:2]
        else:
            continue
        if Decimal(str(level_price or 0)) == price:
            displayed = Decimal(str(level_size or 0))
            break
    state = MakerQueueState(
        paper_order_id=run_id,
        asset_id=asset_id,
        side=side,
        price_tick=price,
        queue_model_version="maker_queue_v1_candidate",
        displayed_size_at_accept=displayed,
        own_orders_ahead=Decimal(0),
        estimated_external_queue_ahead=displayed,
        order_size=size,
    )
    rows: dict[str, Any] = {}
    for model in QueueModel:
        model_forecast_volume = max(Decimal(0), forecast_trade_volume)
        model_arrival_probability = aggressor_arrival_probability
        if model == QueueModel.PROBABILISTIC_QUEUE and probability_calibration:
            model_forecast_volume, model_arrival_probability, _ = (
                probability_calibration.activity_forecast(
                    category=category,
                    side=side,
                    quote_position=quote_position,
                    horizon_seconds=horizon_seconds,
                    observed_trade_count=observed_trade_count,
                    observed_trade_volume=observed_trade_volume,
                    lookback_seconds=lookback_seconds,
                )
            )
        prediction = MakerQueueEngine(model).predict(
            state,
            forecast_trade_volume=model_forecast_volume,
            horizon_seconds=horizon_seconds,
            aggressor_arrival_probability=model_arrival_probability,
        )
        if model == QueueModel.PROBABILISTIC_QUEUE and probability_calibration:
            if trade_evidence_ready:
                prediction = probability_calibration.calibrate_prediction(
                    prediction,
                    order_size=size,
                    horizon_seconds=horizon_seconds,
                )
            else:
                prediction = replace(
                    prediction,
                    p_no_fill=Decimal(1),
                    p_partial=Decimal(0),
                    p_full=Decimal(0),
                    expected_filled_size=Decimal(0),
                    filled_size_p10=Decimal(0),
                    filled_size_p50=Decimal(0),
                    filled_size_p90=Decimal(0),
                    expected_time_to_first_fill_seconds=None,
                    expected_time_to_full_fill_seconds=None,
                    confidence=Decimal(0),
                    domain_status="RESEARCH_ABSTAIN_TRADE_EVIDENCE_NOT_READY",
                    fill_probability=Decimal(0),
                    expected_time_to_fill_seconds=None,
                    time_to_fill_p90_seconds=None,
                    model_confidence=Decimal(0),
                    calibration_domain=probability_calibration.calibration_domain,
                    aggressor_arrival_probability=Decimal(0),
                )
        rows[model.value] = _json_value(asdict(prediction))
    return rows


def maker_trade_forecast(
    evidence_client: Any,
    *,
    asset_id: str,
    side: str,
    price: Decimal,
    horizon_seconds: Decimal,
    observed_at: datetime | None = None,
    lookback_seconds: int = MAKER_FORECAST_LOOKBACK_SECONDS,
) -> dict[str, Any]:
    """Project a trailing, strictly pre-decision compatible trade rate."""

    cutoff = observed_at or datetime.now(timezone.utc)
    window_seconds = max(1, int(lookback_seconds))
    window_start = cutoff - timedelta(seconds=window_seconds)
    base = {
        "model_version": "maker_trade_trailing_rate_v3_arrival_conditioned",
        "source": getattr(
            evidence_client,
            "source_name",
            "clickhouse_orderfilled_delayed",
        ),
        "window_start": window_start.isoformat(),
        "window_end": cutoff.isoformat(),
        "lookback_seconds": window_seconds,
        "horizon_seconds": format(max(Decimal(0), horizon_seconds), "f"),
        "maker_side": str(side).upper(),
        "limit_price": format(price, "f"),
    }
    try:
        summary = evidence_client.summarize_compatible_maker_volume(
            asset_id=asset_id,
            maker_side=side,
            limit_price=price,
            start=window_start,
            end=cutoff,
        )
        observed_volume = max(
            Decimal(0),
            Decimal(str(summary.get("compatible_trade_volume") or 0)),
        )
        projected = (
            observed_volume * max(Decimal(0), horizon_seconds) / Decimal(window_seconds)
        )
        source_ready = bool(summary.get("source_ready", True))
        trade_count = int(summary.get("trade_count") or 0)
        arrival_probability = poisson_arrival_probability(
            observed_count=trade_count,
            lookback_seconds=window_seconds,
            horizon_seconds=horizon_seconds,
        )
        return {
            **base,
            "status": "READY" if source_ready else "EVIDENCE_NOT_READY",
            "source": summary.get("source") or base["source"],
            "source_ready": source_ready,
            "trade_count": trade_count,
            "compatible_trade_volume": format(observed_volume, "f"),
            "median_trade_size": str(summary.get("median_trade_size") or "0"),
            "p75_trade_size": str(summary.get("p75_trade_size") or "0"),
            "forecast_trade_volume": format(projected, "f"),
            "aggressor_arrival_probability": format(arrival_probability, "f"),
            "last_trade_at": summary.get("last_trade_at") or None,
            "coverage_reason": summary.get("coverage_reason")
            or summary.get("live_coverage_reason"),
            "coverage_first_at": summary.get("coverage_first_at"),
            "coverage_last_at": summary.get("coverage_last_at"),
            "coverage_max_gap_seconds": summary.get("coverage_max_gap_seconds"),
            "coverage_watermark_at": summary.get("coverage_watermark_at"),
            "evidence_window_start": summary.get("evidence_window_start"),
            "evidence_window_end": summary.get("evidence_window_end"),
            "live_source_error": summary.get("live_source_error"),
        }
    except Exception as exc:
        return {
            **base,
            "status": "UNAVAILABLE_CONSERVATIVE_ZERO",
            "source_ready": False,
            "trade_count": 0,
            "compatible_trade_volume": "0",
            "median_trade_size": "0",
            "p75_trade_size": "0",
            "forecast_trade_volume": "0",
            "aggressor_arrival_probability": "0",
            "last_trade_at": None,
            "error": f"{exc.__class__.__name__}:{str(exc)[:300]}",
        }


def actual_outcome(actual_size: Decimal, requested_size: Decimal) -> str:
    tolerance = max(Decimal("0.000001"), requested_size * Decimal("0.000001"))
    if actual_size <= tolerance:
        return "NO_FILL"
    if actual_size + tolerance >= requested_size:
        return "FULL"
    return "PARTIAL"


def maker_outcome_observation(
    *,
    outcome: str,
    matched_size: Decimal,
    requested_size: Decimal,
    resting_seconds: Decimal,
    capture: Mapping[str, Any],
) -> dict[str, Any]:
    """Describe what was observed without treating a timed cancel as infinity."""

    normalized = str(outcome).upper()
    trigger = str(capture.get("cancel_trigger") or "HORIZON_EXPIRED")
    horizon = max(Decimal("0"), Decimal(resting_seconds))
    label_horizon = format(horizon.normalize(), "f")
    if normalized == "NO_FILL":
        status = "RIGHT_CENSORED_FIRST_FILL"
        label = f"CENSORED_AT_{label_horizon}S"
        first_fill_censored = True
        full_fill_censored = True
    elif normalized == "PARTIAL":
        status = "PARTIAL_OBSERVED_REMAINDER_CENSORED"
        label = (
            "PARTIAL_CANCEL_ON_FIRST_FILL"
            if trigger == "FIRST_PARTIAL_FILL"
            else "PARTIAL_AT_TERMINAL"
        )
        first_fill_censored = False
        full_fill_censored = True
    else:
        status = "FULLY_OBSERVED"
        label = (
            "FULL_BEFORE_CANCEL"
            if trigger == "FULL_BEFORE_CANCEL"
            else "FULL_AT_TERMINAL"
        )
        first_fill_censored = False
        full_fill_censored = False
    return {
        "schema_version": "maker_outcome_observation_v1",
        "status": status,
        "label": label,
        "execution_outcome": normalized,
        "matched_size": format(max(Decimal("0"), matched_size), "f"),
        "requested_size": format(max(Decimal("0"), requested_size), "f"),
        "planned_horizon_seconds": format(horizon, "f"),
        "cancel_trigger": trigger,
        "cancel_requested_at": capture.get("cancel_requested_at"),
        "first_positive_match_at": capture.get("first_positive_match_at"),
        "first_fill_right_censored": first_fill_censored,
        "full_fill_right_censored": full_fill_censored,
        "permanent_no_fill_claimed": False,
    }


def maker_finality_label(
    *, matched_size: Decimal, orderfilled: Mapping[str, Any]
) -> dict[str, Any]:
    """Keep venue execution and chain settlement as separate labels."""

    source_status = str(orderfilled.get("status") or "UNKNOWN").upper()
    if matched_size <= 0:
        label = "NOT_REQUIRED_NO_FILL"
        confirmed = True
    elif source_status == "CONFIRMED_MATCH":
        label = "CONFIRMED"
        confirmed = True
    elif source_status == "RECEIPT_FAILED":
        label = "FAILED"
        confirmed = False
    else:
        label = "MATCHED_PROVISIONAL"
        confirmed = False
    return {
        "schema_version": "maker_execution_finality_v1",
        "execution_matched_size": format(max(Decimal("0"), matched_size), "f"),
        "label": label,
        "confirmed": confirmed,
        "orderfilled_status": source_status,
        "receipt_status": (
            (orderfilled.get("receipt_truth") or {}).get("status")
            if isinstance(orderfilled.get("receipt_truth"), Mapping)
            else None
        ),
    }


def maker_probe_artifact_complete(
    *,
    order_terminal_evidence: bool,
    order_still_open: bool,
    rest_order_reconciled: bool,
    order_not_found: bool,
    matched_size: Decimal,
    ledger_truth: str,
    orderfilled_status: str,
    account_delta_reconciled: bool,
) -> bool:
    """Require both venue finality and chain truth before calibration."""

    terminal = bool(
        order_terminal_evidence
        and not order_still_open
        and (rest_order_reconciled or order_not_found)
    )
    if not terminal:
        return False
    if not account_delta_reconciled:
        return False
    if matched_size <= 0:
        return True
    return (
        str(ledger_truth).upper() == "CONFIRMED"
        and str(orderfilled_status).upper() == "CONFIRMED_MATCH"
    )


def maker_account_delta_matches_truth(
    *,
    side: str,
    matched_size: Decimal,
    quote_amount: Decimal,
    fee: Decimal,
    delta: Mapping[str, Any],
) -> bool:
    """Prove that the balance window contains only this Maker outcome."""

    actual_cash = Decimal(str(delta.get("collateral") or 0))
    actual_tokens = Decimal(str(delta.get("conditional") or 0))
    size = max(Decimal("0"), Decimal(matched_size))
    quote = max(Decimal("0"), Decimal(quote_amount))
    charged_fee = max(Decimal("0"), Decimal(fee))
    if size <= 0:
        expected_cash = Decimal("0")
        expected_tokens = Decimal("0")
    elif str(side).upper() == "BUY":
        expected_cash = -quote - charged_fee
        expected_tokens = size
    elif str(side).upper() == "SELL":
        expected_cash = quote - charged_fee
        expected_tokens = -size
    else:
        return False
    tolerance = Decimal("0.000001")
    return (
        abs(actual_cash - expected_cash) <= tolerance
        and abs(actual_tokens - expected_tokens) <= tolerance
    )


def predicted_outcome_matches(actual: str, required: str) -> bool:
    normalized = str(required or "ANY").upper()
    outcome = str(actual or "").upper()
    if normalized == "ANY":
        return True
    if normalized == "PARTIAL_OR_FULL":
        return outcome in {"PARTIAL", "FULL"}
    return outcome == normalized


def maker_trade_evidence_admission(
    forecast: Mapping[str, Any],
    *,
    targeted_hot_preflight: bool,
    probe_target: str,
    allow_incomplete: bool,
    placement: str = "AT_BEST",
    hot_preflight: Mapping[str, Any] | None = None,
    market_snapshot: Mapping[str, Any] | None = None,
    recent_public_trade_activity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep strategy evidence strict while permitting bounded label collection."""

    if str(forecast.get("status") or "") == "READY":
        return {
            "allowed": True,
            "mode": "COMPLETE_PREDECISION_EVIDENCE",
            "calibrated_prediction_claimed": True,
        }
    controlled_probe = str(probe_target).upper() in {"FULL", "PARTIAL"}
    observed_trade_count = int(forecast.get("trade_count") or 0)
    allowed = bool(
        allow_incomplete
        and targeted_hot_preflight
        and controlled_probe
        and observed_trade_count > 0
    )
    if allowed:
        return {
            "allowed": True,
            "mode": "INCOMPLETE_WINDOW_LABEL_COLLECTION_ONLY",
            "calibrated_prediction_claimed": False,
            "observed_trade_count": observed_trade_count,
            "coverage_reason": forecast.get("coverage_reason"),
            "authoritative_outcome_source": (
                "OWN_USER_WS_REST_ORDER_STATUS_AND_ORDERFILLED"
            ),
        }

    hot = hot_preflight if isinstance(hot_preflight, Mapping) else {}
    ws = (
        hot.get("targeted_ws_book")
        if isinstance(hot.get("targeted_ws_book"), Mapping)
        else {}
    )
    counts = (
        ws.get("activity_counts")
        if isinstance(ws.get("activity_counts"), Mapping)
        else {}
    )
    market = market_snapshot if isinstance(market_snapshot, Mapping) else {}
    clob = (
        market.get("clob_market_info")
        if isinstance(market.get("clob_market_info"), Mapping)
        else {}
    )
    activity_count = sum(
        max(0, int(counts.get(name) or 0))
        for name in ("price_change", "best_bid_ask", "last_trade_price")
    )
    public_activity = (
        recent_public_trade_activity
        if isinstance(recent_public_trade_activity, Mapping)
        else {}
    )
    public_trade_count = max(0, int(public_activity.get("compatible_trade_count") or 0))
    public_trade_ready = bool(
        public_activity.get("status") == "READY"
        and public_activity.get("source_ready") is True
        and public_trade_count > 0
        and public_activity.get("prediction_truth_claimed") is False
        and public_activity.get("own_order_execution_truth_claimed") is False
    )
    prospective = bool(
        allow_incomplete
        and targeted_hot_preflight
        and controlled_probe
        and str(placement).upper() == "NEAR_OPPOSITE"
        and (activity_count > 0 or public_trade_ready)
        and clob.get("ao") is True
    )
    if prospective:
        return {
            "allowed": True,
            "mode": "PROSPECTIVE_OWN_ORDER_LABEL_COLLECTION_ONLY",
            "calibrated_prediction_claimed": False,
            "observed_trade_count": observed_trade_count,
            "activity_event_count": activity_count,
            "recent_public_compatible_trade_count": public_trade_count,
            "recent_public_trade_payload_sha256": public_activity.get("payload_sha256"),
            "coverage_reason": forecast.get("coverage_reason"),
            "historical_trade_window_claimed_complete": False,
            "authoritative_outcome_source": (
                "OWN_USER_WS_REST_ORDER_STATUS_AND_ORDERFILLED"
            ),
        }
    return {
        "allowed": False,
        "mode": "REJECT_INCOMPLETE_EVIDENCE",
        "calibrated_prediction_claimed": False,
        "observed_trade_count": observed_trade_count,
        "coverage_reason": forecast.get("coverage_reason"),
        "authoritative_outcome_source": (
            "OWN_USER_WS_REST_ORDER_STATUS_AND_ORDERFILLED" if allowed else None
        ),
    }


def rest_bbo_matches(
    candidate: Mapping[str, Any],
    market: Mapping[str, Any],
    *,
    side: str,
    tolerance: Decimal = Decimal("0.0001"),
) -> bool:
    try:
        local_bid = Decimal(str(candidate["best_bid"]))
        local_ask = Decimal(str(candidate["best_ask"]))
        rest_bid = Decimal(str(market["rest_best_bid"]))
        rest_ask = Decimal(str(market["rest_best_ask"]))
    except Exception:
        return False
    if str(side).upper() == "BUY":
        return abs(local_bid - rest_bid) <= tolerance and local_bid < rest_ask
    if str(side).upper() == "SELL":
        return abs(local_ask - rest_ask) <= tolerance and local_ask > rest_bid
    return False


def account_delta(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, str]:
    return {
        "collateral": format(
            Decimal(str((after.get("collateral") or {}).get("balance") or 0))
            - Decimal(str((before.get("collateral") or {}).get("balance") or 0)),
            "f",
        ),
        "conditional": format(
            Decimal(str((after.get("conditional") or {}).get("balance") or 0))
            - Decimal(str((before.get("conditional") or {}).get("balance") or 0)),
            "f",
        ),
    }


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))
