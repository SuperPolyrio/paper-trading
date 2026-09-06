"""One-order-at-a-time Phase 5C runner with bounded live authorization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping

from .calibration_domain import ProbeState, artifact_bitmap, payload_hash
from .kill_switch import KillSwitch
from .order_rest_reconciler import reconcile_order_lifecycle
from .paired_probe_bridge import sync_calibration_probe_to_paired
from .probe_plan import ProbePlan, validate_probe_plan
from .probe_risk_guard import (
    LIVE_ENABLE_PHRASE,
    _estimated_taker_fee,
    _order_notional,
    evaluate_preflight,
)
from .probe_scheduler import (
    NoSubmitProbeRunner,
    _decorate_candidate,
    _state_events,
    _with_market_parameters,
)
from .real_live_adapter import (
    ClobV2AdapterError,
    OrderSubmissionRejected,
    SubmitOutcomeUnknown,
    VenueMaintenance,
    VenueRestrictedMode,
    validate_exact_market_order_request,
)
from .reconcile import accounting_payload_for_probe, reconcile_probe
from .signed_order_prediction import normalize_prediction_to_signed_order
from quant.paper.paired_probe import RECORD_ONLY
from quant.paper.paper_ledger import PostgresPaperLedgerSink


class LiveProbeRunner(NoSubmitProbeRunner):
    """Prepare and execute exactly one bounded live calibration order."""

    def __init__(
        self,
        plan: ProbePlan,
        *,
        approved_asset_id: str | None = None,
        clean_cohort_id: str | None = None,
        paper_strategy_id: str | None = None,
        clean_cohort_store: Any | None = None,
        lifecycle_timeout_seconds: float = 120.0,
        paper_prediction_max_wall_seconds: float = 15.0,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("target_asset_id", approved_asset_id)
        kwargs.setdefault("candidate_wait_seconds", 90.0)
        kwargs.setdefault("paper_prediction_wait_seconds", 300.0)
        super().__init__(plan, **kwargs)
        self.approved_asset_id = str(approved_asset_id or "").strip()
        self.clean_cohort_id = str(clean_cohort_id or "").strip() or None
        selected_strategy = str(paper_strategy_id or "").strip() or None
        if selected_strategy and not self.clean_cohort_id:
            raise ValueError("paper_strategy_id is only accepted with clean_cohort_id")
        self.paper_strategy_id = selected_strategy or (
            f"post-v2-clean-{self.clean_cohort_id}" if self.clean_cohort_id else None
        )
        if self.clean_cohort_id:
            if clean_cohort_store is None:
                from .clean_v2_cohort import CleanV2CohortStore

                clean_cohort_store = CleanV2CohortStore(
                    self.shadow_store.connection_factory
                )
            self.clean_cohort_store = clean_cohort_store
        else:
            self.clean_cohort_store = None
        self.lifecycle_timeout_seconds = max(1.0, float(lifecycle_timeout_seconds))
        self.paper_prediction_max_wall_seconds = max(
            1.0,
            float(paper_prediction_max_wall_seconds),
        )

    @property
    def live_plan_hash(self) -> str:
        return payload_hash(
            {
                "base_plan_hash": self.plan.plan_hash,
                "approved_asset_id": self.approved_asset_id,
                "clean_cohort_id": self.clean_cohort_id,
                "paper_strategy_id": self.paper_strategy_id,
            },
            prefix="plan-",
        )

    def prepare(self) -> dict[str, Any]:
        self.clean_cohort_id = getattr(self, "clean_cohort_id", None)
        self.paper_strategy_id = getattr(self, "paper_strategy_id", None)
        self.clean_cohort_store = getattr(self, "clean_cohort_store", None)
        started_at = datetime.now(timezone.utc)
        issues = validate_probe_plan(
            self.plan,
            live=True,
            require_credentials=True,
            environ=self.environ,
        )
        if self.plan.execution.count != 1:
            issues.append("phase5c_requires_exactly_one_probe_per_approved_run")
        if set(self.plan.execution.order_types) - {"FAK", "FOK"}:
            issues.append("phase5c_taker_runner_requires_fak_or_fok")
        if len(self.plan.execution.sides) != 1:
            issues.append("phase5c_requires_exactly_one_side")
        side = self.plan.execution.sides[0] if self.plan.execution.sides else ""
        expected_unit = "QUOTE" if side == "BUY" else "SHARES"
        if side not in {"BUY", "SELL"}:
            issues.append("phase5c_side_must_be_buy_or_sell")
        elif self.plan.execution.amount_unit != expected_unit:
            issues.append(
                f"phase5c_{side.lower()}_requires_{expected_unit.lower()}_amount"
            )
        if not self.approved_asset_id:
            issues.append("phase5c_approved_asset_id_missing")
        prepared_candidate: dict[str, Any] | None = None
        if not issues and self.clean_cohort_id:
            try:
                self.clean_cohort_store.ensure_schema()
                self.clean_cohort_store.assert_target_allowed(
                    cohort_id=self.clean_cohort_id,
                    strategy_id=str(self.paper_strategy_id),
                    asset_id=self.approved_asset_id,
                )
            except Exception as exc:
                issues.append(
                    "clean_v2_cohort_target_rejected:"
                    f"{exc.__class__.__name__}:{str(exc)[:240]}"
                )
        if not issues:
            try:
                self.shadow_store.ensure_schema()
                pinned = self.shadow_store.ensure_calibration_watch_batch(
                    [self.approved_asset_id],
                    strategy_id=(
                        str(self.paper_strategy_id)
                        if self.paper_strategy_id
                        else "taker-calibration-live"
                    ),
                    reason="live_probe_candidate",
                )
            except Exception as exc:
                issues.append(
                    "phase5c_candidate_watch_pin_failed:"
                    f"{exc.__class__.__name__}:{str(exc)[:240]}"
                )
            else:
                if pinned < 1:
                    issues.append("phase5c_candidate_watch_pin_failed")
        if not issues:
            candidates = self._wait_for_shadow_fresh_candidates(
                asset_id=self.approved_asset_id
            )
            allowed = set(self.plan.market_policy.allow_market_ids)
            prepared_candidate = next(
                (
                    dict(row)
                    for row in candidates
                    if str(row.get("market_id") or "") in allowed
                    and str(row.get("asset_id") or "") == self.approved_asset_id
                ),
                None,
            )
            if prepared_candidate is None:
                issues.append("phase5c_approved_asset_not_currently_fresh")
            else:
                prepared_market_position = Decimal("0")
                market_snapshot_getter = getattr(
                    getattr(self, "adapter", None),
                    "get_market_snapshot",
                    None,
                )
                if callable(market_snapshot_getter):
                    try:
                        market_snapshot = market_snapshot_getter(
                            asset_id=self.approved_asset_id,
                            condition_id=str(
                                prepared_candidate.get("condition_id") or ""
                            ),
                        )
                        prepared_candidate = _with_market_parameters(
                            prepared_candidate,
                            market_snapshot,
                        )
                    except Exception as exc:
                        issues.append(
                            "phase5c_official_market_snapshot_unavailable:"
                            f"{exc.__class__.__name__}:{str(exc)[:240]}"
                        )
                account_snapshot_getter = getattr(
                    getattr(self, "adapter", None),
                    "get_account_snapshot",
                    None,
                )
                if callable(account_snapshot_getter):
                    try:
                        prepared_account = account_snapshot_getter(
                            asset_id=self.approved_asset_id
                        )
                        prepared_market_position = Decimal(
                            str(prepared_account.conditional.get("balance") or 0)
                        )
                    except Exception as exc:
                        issues.append(
                            "phase5c_official_account_snapshot_unavailable:"
                            f"{exc.__class__.__name__}:{str(exc)[:240]}"
                        )
                tick_size = prepared_candidate.get("tick_size")
                selected_price = prepared_candidate.get(
                    "best_ask" if side == "BUY" else "best_bid"
                )
                if tick_size in (None, ""):
                    issues.append("phase5c_candidate_tick_size_missing")
                else:
                    try:
                        validate_exact_market_order_request(
                            side=side,
                            amount=self.plan.execution.amount,
                            price=selected_price,
                            tick_size=tick_size,
                        )
                    except (ClobV2AdapterError, ValueError) as exc:
                        issues.append(
                            f"phase5c_exact_order_encoding_unavailable:{str(exc)[:240]}"
                        )
                issues.extend(
                    _prepared_candidate_execution_issues(
                        self.plan,
                        prepared_candidate,
                        side=side,
                        market_position=prepared_market_position,
                    )
                )
        phase5b = self._phase5b_gate()
        if phase5b["status"] != "PASS":
            issues.append("formal_phase5b_gate_not_passed")
        if issues:
            return {
                "schema_version": "taker_live_prepare_v1",
                "status": "FAIL",
                "run_id": None,
                "issues": sorted(set(issues)),
                "exchange_order_submitted": False,
            }

        manifest = dict(phase5b["manifest"])
        run_id = payload_hash(
            {
                "mode": "live",
                "manifest_id": manifest["manifest_id"],
                "plan_hash": self.live_plan_hash,
                "started_at": started_at,
            },
            prefix="calrun-",
        )
        self.calibration_store.ensure_schema()
        paper_sell_baseline = None
        if side == "SELL":
            try:
                account_before = self.adapter.get_account_snapshot(
                    asset_id=self.approved_asset_id
                )
                if self.clean_cohort_id:
                    paper_sell_baseline = self._validated_clean_cohort_sell_state(
                        account_before=account_before,
                        asset_id=self.approved_asset_id,
                    )
                else:
                    paper_sell_baseline = self._seed_sell_paper_baseline(
                        run_id=run_id,
                        candidate=prepared_candidate,
                        account_before=account_before,
                    )
            except Exception as exc:
                return {
                    "schema_version": "taker_live_prepare_v1",
                    "status": "FAIL",
                    "run_id": None,
                    "issues": [
                        "phase5c_sell_paper_baseline_unavailable:"
                        f"{exc.__class__.__name__}:{str(exc)[:240]}"
                    ],
                    "exchange_order_submitted": False,
                }
        self.calibration_store.freeze_model(manifest)
        initial_status = (
            "AWAITING_APPROVAL"
            if self.plan.safety.require_manual_run_approval
            else "READY_TO_EXECUTE"
        )
        self.calibration_store.create_run(
            {
                "run_id": run_id,
                "mode": "live",
                "run_status": initial_status,
                "model_version": manifest["paper_execution_model_version"],
                "manifest_id": manifest["manifest_id"],
                "plan_hash": self.live_plan_hash,
                "plan": {
                    **self.plan.redacted_dict(),
                    "approved_asset_id": self.approved_asset_id,
                    "paper_sell_baseline": paper_sell_baseline,
                    "clean_cohort_id": self.clean_cohort_id,
                    "paper_strategy_id": self.paper_strategy_id,
                },
                "expected_probe_count": 1,
                "started_at": started_at,
            }
        )
        planned_gross_notional = (
            self.plan.execution.amount
            if side == "BUY"
            else self.plan.execution.amount
            * Decimal(str(prepared_candidate["best_bid"]))
        )
        if self.clean_cohort_id:
            try:
                self.clean_cohort_store.register_operation(
                    cohort_id=self.clean_cohort_id,
                    run_id=run_id,
                    strategy_id=str(self.paper_strategy_id),
                    asset_id=self.approved_asset_id,
                    market_id=str(prepared_candidate.get("market_id") or ""),
                    condition_id=str(prepared_candidate.get("condition_id") or ""),
                    side=side,
                    order_type=self.plan.execution.order_types[0],
                    amount=self.plan.execution.amount,
                    amount_unit=self.plan.execution.amount_unit,
                    planned_gross_notional=planned_gross_notional,
                )
            except Exception as exc:
                report = {
                    "schema_version": "taker_live_prepare_v1",
                    "status": "FAIL",
                    "run_id": run_id,
                    "issues": [
                        "clean_v2_cohort_operation_registration_failed:"
                        f"{exc.__class__.__name__}:{str(exc)[:240]}"
                    ],
                    "exchange_order_submitted": False,
                }
                self.calibration_store.finish_run(
                    run_id,
                    status="BLOCKED",
                    report=report,
                )
                return report
        return {
            "schema_version": "taker_live_prepare_v1",
            "status": initial_status,
            "run_id": run_id,
            "plan_hash": self.live_plan_hash,
            "market_allowlist": list(self.plan.market_policy.allow_market_ids),
            "approved_asset_id": self.approved_asset_id,
            "clean_cohort_id": self.clean_cohort_id,
            "paper_strategy_id": self.paper_strategy_id,
            "prepared_candidate": {
                key: prepared_candidate.get(key)
                for key in (
                    "market_id",
                    "asset_id",
                    "condition_id",
                    "outcome_name",
                    "best_bid",
                    "best_ask",
                    "tick_size",
                    "min_order_size",
                    "coverage_grade",
                    "shadow_observed_at",
                )
            },
            "planned_order_count": 1,
            "planned_side": side,
            "paper_sell_baseline": paper_sell_baseline,
            "planned_gross_notional_usd": str(planned_gross_notional),
            "max_possible_spend_usd": (
                str(planned_gross_notional) if side == "BUY" else "0"
            ),
            "daily_spend_cap_usd": str(self.plan.limits.max_daily_gross_notional),
            "phase5b_gate": {
                key: value for key, value in phase5b.items() if key != "manifest"
            },
            "authorization": {
                "mode": (
                    "MANUAL_RUN_APPROVAL"
                    if self.plan.safety.require_manual_run_approval
                    else "AUTO_WITHIN_CONFIGURED_LIMITS"
                ),
                "max_order_notional_usd": str(self.plan.limits.max_order_notional),
                "max_daily_gross_notional_usd": str(
                    self.plan.limits.max_daily_gross_notional
                ),
            },
            "approval_environment": (
                {
                    "POLY_QUANT_LIVE_PROBE_ENABLE": "I_UNDERSTAND_REAL_ORDERS",
                    "POLY_QUANT_LIVE_PROBE_APPROVAL": run_id,
                }
                if self.plan.safety.require_manual_run_approval
                else None
            ),
            "exchange_order_submitted": False,
        }

    def execute(self, run_id: str) -> dict[str, Any]:
        self.clean_cohort_id = getattr(self, "clean_cohort_id", None)
        self.paper_strategy_id = getattr(self, "paper_strategy_id", None)
        self.clean_cohort_store = getattr(self, "clean_cohort_store", None)
        issues = validate_probe_plan(
            self.plan,
            live=True,
            require_credentials=True,
            environ=self.environ,
        )
        if issues:
            return self._blocked(run_id, "live plan validation failed", issues)
        run = self.calibration_store.load_run(run_id)
        if run is None:
            return self._blocked(run_id, "prepared run does not exist", ["run_missing"])
        if (
            str(run.get("mode")) != "live"
            or str(run.get("plan_hash")) != self.live_plan_hash
        ):
            return self._blocked(
                run_id,
                "prepared run does not match the frozen live plan",
                ["run_plan_mismatch"],
            )
        if self.plan.safety.require_manual_run_approval:
            if (
                str(self.environ.get("POLY_QUANT_LIVE_PROBE_ENABLE") or "")
                != LIVE_ENABLE_PHRASE
            ):
                return self._blocked(
                    run_id,
                    "explicit live enable phrase is absent",
                    ["live_enable_phrase_missing"],
                )
            if str(self.environ.get("POLY_QUANT_LIVE_PROBE_APPROVAL") or "") != str(
                run_id
            ):
                return self._blocked(
                    run_id,
                    "manual approval does not match this run id",
                    ["manual_run_approval_missing"],
                )
        if KillSwitch(Path(self.plan.safety.kill_switch_file)).active:
            return self._blocked(
                run_id, "live probe kill switch is active", ["kill_switch_active"]
            )
        if self.clean_cohort_id:
            try:
                self.clean_cohort_store.assert_target_allowed(
                    cohort_id=self.clean_cohort_id,
                    strategy_id=str(self.paper_strategy_id),
                    asset_id=self.approved_asset_id,
                )
            except Exception as exc:
                return self._blocked(
                    run_id,
                    "clean V2 cohort target validation failed",
                    [f"{exc.__class__.__name__}:{str(exc)[:240]}"],
                )
        self.calibration_store.transition_run_status(
            run_id,
            expected=(
                "AWAITING_APPROVAL"
                if self.plan.safety.require_manual_run_approval
                else "READY_TO_EXECUTE"
            ),
            target="RUNNING",
        )
        if self.clean_cohort_id:
            self.clean_cohort_store.transition_operation(
                run_id=run_id,
                expected=("PREPARED",),
                target="RUNNING",
            )
        model = self.calibration_store.load_model(str(run["model_version"]))
        manifest = dict((model or {}).get("manifest") or {})
        if not manifest or str(manifest.get("manifest_id")) != str(
            run.get("manifest_id")
        ):
            return self._finish_blocked(
                run_id, "frozen model manifest is unavailable", ["manifest_missing"]
            )

        candidates = self._wait_for_shadow_fresh_candidates(
            asset_id=self.approved_asset_id
        )
        allowed = set(self.plan.market_policy.allow_market_ids)
        candidates = [
            row
            for row in candidates
            if str(row.get("market_id") or "") in allowed
            and str(row.get("asset_id") or "") == self.approved_asset_id
        ]
        if not candidates:
            return self._finish_blocked(
                run_id,
                "no fresh allowlisted A/A_PLUS candidate",
                ["allowlisted_candidate_unavailable"],
            )
        row = self._execute_one(
            run_id=run_id, manifest=manifest, candidate=candidates[0]
        )
        state = str(row.get("probe_state") or "UNKNOWN")
        unsafe = state in {
            ProbeState.SUBMIT_OUTCOME_UNKNOWN.value,
            ProbeState.ACCOUNTING_MISMATCH.value,
            ProbeState.SELF_TRADE_CONTAMINATED.value,
            ProbeState.MANUAL_INTERVENTION.value,
            ProbeState.VENUE_MAINTENANCE.value,
        }
        blocked = state in {
            ProbeState.RISK_ABORTED.value,
            ProbeState.MARKET_DATA_UNSAFE.value,
            ProbeState.HTTP_REJECTED.value,
        }
        status = (
            "STOPPED"
            if unsafe
            else (
                "BLOCKED"
                if blocked
                else (
                    "PROBE_COMPLETE"
                    if state == ProbeState.CALIBRATABLE.value
                    else "PENDING_RECONCILIATION"
                )
            )
        )
        report = {
            "schema_version": "taker_live_run_report_v1",
            "status": status,
            "run_id": run_id,
            "probe_count": 1,
            "submitted_order_count": int(bool(row.get("exchange_submit_called"))),
            "calibratable_probe_count": int(state == ProbeState.CALIBRATABLE.value),
            "probe_state": state,
            "phase5c_complete": False,
            "reason": (
                "unsafe outcome activated the stop boundary"
                if unsafe
                else "single approved probe captured; delayed reconciliation may still be required"
            ),
        }
        self.calibration_store.finish_run(run_id, status=status, report=report)
        if self.clean_cohort_id and not bool(row.get("exchange_submit_called")):
            self._abort_clean_cohort_operation(
                run_id,
                reason=f"live probe ended before submit: {state}",
            )
        return report

    def _execute_one(
        self,
        *,
        run_id: str,
        manifest: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.clean_cohort_id = getattr(self, "clean_cohort_id", None)
        self.paper_strategy_id = getattr(self, "paper_strategy_id", None)
        started = datetime.now(timezone.utc)
        side = self.plan.execution.sides[0]
        order_type = self.plan.execution.order_types[0]
        base = self._base_probe(run_id, manifest, candidate, started, side, order_type)
        states = [ProbeState.PLANNED.value]
        self.calibration_store.upsert_probe(base)
        try:
            user_ws_health = self.user_ws.probe_connection([base["condition_id"]])
            market = self.adapter.get_market_snapshot(
                asset_id=base["asset_id"], condition_id=base["condition_id"]
            )
            refresh_market_gate = getattr(
                getattr(self, "shadow_store", None),
                "refresh_official_probe_market_gate",
                None,
            )
            if callable(refresh_market_gate):
                refresh_market_gate(
                    asset_id=base["asset_id"],
                    condition_id=base["condition_id"],
                    snapshot=market,
                )
            account_before = self.adapter.get_account_snapshot(
                asset_id=base["asset_id"]
            )
            sell_seed = (
                (
                    self._validated_clean_cohort_sell_state(
                        account_before=account_before,
                        asset_id=base["asset_id"],
                    )
                    if self.clean_cohort_id
                    else self._validated_sell_seed_state(
                        account_before=account_before,
                        asset_id=base["asset_id"],
                    )
                )
                if side == "SELL"
                else None
            )
            route_before, route_after = _route_exposure_bounds(
                side=side,
                amount=self.plan.execution.amount,
                amount_unit=self.plan.execution.amount_unit,
                price=Decimal(
                    str(market.get("best_ask") or market.get("best_bid") or 0)
                ),
                position=Decimal(str(account_before.conditional.get("balance") or 0)),
            )
            market["write_route_geoblock"] = self.adapter.require_write_route_allowed(
                side=side,
                asset_id=base["asset_id"],
                exposure_before=route_before,
                exposure_after=route_after,
            )
            refreshed = self._wait_for_shadow_fresh_candidates(
                asset_id=base["asset_id"],
                force_refresh=True,
            )
            if not refreshed:
                raise RuntimeError("fresh shadow head disappeared before prediction")
            candidate = _with_market_parameters(refreshed[0], market)
            paper_execution_strategy_id = self.paper_strategy_id
            staging = None
            if self.clean_cohort_id:
                staging = self.clean_cohort_store.prepare_staging_strategy(
                    run_id=run_id
                )
                paper_execution_strategy_id = str(staging["staging_strategy_id"])
            paper, prediction, candidate = self._freeze_shadow_prediction(
                candidate=candidate,
                run_id=run_id,
                side=side,
                order_type=order_type,
                paper_position_size=(
                    None
                    if self.clean_cohort_id
                    else account_before.conditional.get("balance")
                    if side == "SELL"
                    else None
                ),
                paper_position_cost_basis=(
                    None
                    if self.clean_cohort_id
                    else sell_seed["cost_basis"]
                    if sell_seed is not None
                    else None
                ),
                paired_mode=RECORD_ONLY,
                paper_strategy_id=paper_execution_strategy_id,
            )
            if self.clean_cohort_id:
                self.clean_cohort_store.record_staged_prediction(
                    run_id=run_id,
                    paired_probe_id=str(paper["probe_id"]),
                    paper_intent_id=int(paper["paper_intent_id"]),
                )
            base.update(
                paired_probe_id=paper.get("probe_id"),
                decision_ts=paper.get("decision_ts") or started,
                prediction=prediction,
            )
            if staging is not None:
                base["reconciliation"] = {
                    "clean_cohort_staging": {
                        "status": "PASS",
                        "staging_strategy_id": staging["staging_strategy_id"],
                        "baseline_hash": staging["baseline_hash"],
                    }
                }
            prediction_completed = datetime.now(timezone.utc)
            base["timestamps"]["strategy_decision_ts"] = base["decision_ts"]
            base["timestamps"]["prediction_completed_ts"] = prediction_completed
            base["timestamps"]["prediction_frozen_at"] = prediction_completed
            decision_ts = _timestamp_value(base["decision_ts"])
            prediction_wall_seconds = (
                (prediction_completed - decision_ts).total_seconds()
                if decision_ts is not None
                else float("inf")
            )
            base["timestamps"]["prediction_wall_seconds"] = prediction_wall_seconds
            if prediction_wall_seconds > self.paper_prediction_max_wall_seconds:
                base["probe_state"] = ProbeState.RISK_ABORTED.value
                base["errors"] = [
                    "paper_prediction_wall_age_exceeded:"
                    f"{prediction_wall_seconds:.3f}s>"
                    f"{self.paper_prediction_max_wall_seconds:.3f}s"
                ]
                states.append(base["probe_state"])
                return self._persist_probe(base, states, started)
            day_start = datetime.combine(
                datetime.now(timezone.utc).date(),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            daily_usage = self.calibration_store.recent_usage(since=day_start)
            hourly_usage = self.calibration_store.recent_usage(
                since=datetime.now(timezone.utc) - timedelta(hours=1)
            )
            daily_realized_loss = self.calibration_store.realized_loss(
                since=day_start,
                account_id=str(account_before.funder_address or "").lower() or None,
            )
            # Keep the REST BBO validation immediately adjacent to preflight;
            # database latency must not make a newly validated book look stale.
            market = {
                **market,
                **self.adapter.get_book_snapshot(asset_id=base["asset_id"]),
            }
            base["market_snapshot"] = {**candidate, **market}
            context = self._preflight_context(
                candidate,
                market,
                account_before,
                user_ws_health,
                side=side,
                daily_usage=daily_usage,
                hourly_usage=hourly_usage,
            )
            context["daily_realized_loss"] = daily_realized_loss
            preflight = evaluate_preflight(
                self.plan,
                context,
                live=True,
                run_id=run_id,
                environ=self.environ,
            )
            base["risk_snapshot"] = preflight.as_dict()
            if not preflight.passed:
                base["probe_state"] = ProbeState.RISK_ABORTED.value
                base["errors"] = list(preflight.issues)
                states.append(base["probe_state"])
                return self._persist_probe(base, states, started)
            states.append(ProbeState.PREFLIGHT_OK.value)
            states.append(ProbeState.PREDICTION_FROZEN.value)

            worst_price = str(
                candidate.get("execution_worst_price")
                or (candidate["best_ask"] if side == "BUY" else candidate["best_bid"])
            )
            base["timestamps"]["sign_started_ts"] = datetime.now(timezone.utc)
            prepared = self.adapter.prepare_live_order(
                asset_id=base["asset_id"],
                side=side,
                order_type=order_type,
                amount=str(self.plan.execution.amount),
                amount_unit=self.plan.execution.amount_unit,
                worst_price=worst_price,
                tick_size=str(market["tick_size"]),
                neg_risk=bool(market["neg_risk"]),
                user_usdc_balance=(
                    str(account_before.collateral.get("balance"))
                    if side == "BUY"
                    else None
                ),
            )
            prediction = normalize_prediction_to_signed_order(
                prediction,
                prepared.audit.as_dict(),
                base["market_snapshot"],
            )
            base["prediction"] = prediction
            base["signed_order_audit"] = prepared.audit.as_dict()
            base["timestamps"]["signed_at"] = prepared.audit.signed_at
            base["timestamps"]["sign_completed_ts"] = (
                prepared.audit.signed_at or datetime.now(timezone.utc)
            )
            base["timestamps"]["prediction_normalized_to_signed_order_at"] = (
                datetime.now(timezone.utc)
            )
            if not _prediction_matches_expected(
                prediction,
                self.plan.execution.expected_outcome,
            ):
                base["probe_state"] = ProbeState.RISK_ABORTED.value
                base["errors"] = [
                    "frozen_prediction_does_not_match_expected_outcome:"
                    f"{self.plan.execution.expected_outcome}"
                ]
                states.append(base["probe_state"])
                return self._persist_probe(base, states, started)
            if self.clean_cohort_id:
                frozen = self.clean_cohort_store.freeze_signed_prediction(
                    run_id=run_id,
                    normalized_prediction=prediction,
                    signed_order_audit=prepared.audit.as_dict(),
                )
                prediction = dict(frozen["prediction"])
                base["prediction"] = prediction
                base["reconciliation"] = {
                    **dict(base.get("reconciliation") or {}),
                    "clean_cohort_signed_prediction": {
                        "status": "PASS",
                        "audit_key": frozen["audit_key"],
                        "idempotent": bool(frozen["idempotent"]),
                    },
                }
            base["probe_state"] = ProbeState.SIGNED.value
            states.append(ProbeState.SIGNED.value)
            self.calibration_store.upsert_probe(base)

            account_before_submit = self.adapter.get_account_snapshot(
                asset_id=base["asset_id"]
            )
            submit_before, submit_after = _route_exposure_bounds(
                side=side,
                amount=self.plan.execution.amount,
                amount_unit=self.plan.execution.amount_unit,
                price=Decimal(worst_price),
                position=Decimal(
                    str(account_before_submit.conditional.get("balance") or 0)
                ),
            )
            base["market_snapshot"]["write_route_geoblock_before_submit"] = (
                self.adapter.require_write_route_allowed(
                    side=side,
                    asset_id=base["asset_id"],
                    exposure_before=submit_before,
                    exposure_after=submit_after,
                )
            )
            base["signed_order_audit"] = prepared.audit.as_dict()
            base["probe_state"] = ProbeState.SUBMITTING.value
            base["exchange_submit_called"] = False
            base["lifecycle"] = {
                "state": ProbeState.SUBMITTING.value,
                "http_request_audit": {
                    "request_hash": payload_hash(
                        {
                            "run_id": run_id,
                            "probe_id": base["probe_id"],
                            "order_hash": prepared.audit.order_hash,
                        },
                        prefix="request-",
                    ),
                    "order_hash": prepared.audit.order_hash,
                },
                "account_before": account_before.as_dict(),
            }
            base["timestamps"]["http_send_started_ts"] = datetime.now(timezone.utc)
            states.append(ProbeState.SUBMITTING.value)
            self.calibration_store.upsert_probe(base)
            self.calibration_store.append_events(
                _state_events(base["probe_id"], states, started)
            )

            submission, ws_capture = self.user_ws.submit_while_recording(
                probe_id=base["probe_id"],
                condition_ids=[base["condition_id"]],
                submit=lambda: self._submit_prepared_with_durable_audit(
                    run_id=run_id,
                    prepared=prepared,
                    risk_passed=preflight.passed,
                    probe=base,
                ),
                timeout_seconds=self.lifecycle_timeout_seconds,
            )
            audit, response = submission
            base["signed_order_audit"] = audit.as_dict()
            base["timestamps"]["http_response_completed_ts"] = datetime.now(
                timezone.utc
            )
            order_id = str(response.get("orderID") or response.get("order_id") or "")
            states.append(ProbeState.ACKED.value)
            rest = self.adapter.get_order_reconciliation_snapshot(
                order_id=order_id,
                condition_id=base["condition_id"],
                asset_id=base["asset_id"],
                after=int(started.timestamp()) - 60,
                before=int(datetime.now(timezone.utc).timestamp()) + 60,
            )
            user_events = list(ws_capture.get("events") or ())
            _apply_user_ws_timestamps(base["timestamps"], user_events)
            self.calibration_store.append_events(user_events)
            event_payloads = [dict(event.get("payload") or {}) for event in user_events]
            truth = reconcile_order_lifecycle(
                order_id=order_id,
                user_ws_events=event_payloads,
                rest_order=rest.get("order"),
                rest_trades=rest.get("trades") or (),
            )
            account_after = self.adapter.get_account_snapshot(asset_id=base["asset_id"])
            base["lifecycle"] = {
                **base["lifecycle"],
                "state": str(
                    truth.get("ledger_truth") or truth.get("execution_truth") or "ACKED"
                ),
                "order_id": order_id,
                "http_response": dict(response),
                "user_ws_capture": {
                    key: value for key, value in ws_capture.items() if key != "events"
                },
                "rest_order": rest.get("order") or {},
                "rest_trades": rest.get("trades") or [],
                "open_orders_after": rest.get("open_orders") or [],
                "trade_ids": _identifiers(rest.get("trades") or (), "trade_id", "id"),
                "transaction_hashes": _identifiers(
                    rest.get("trades") or (), "transaction_hash", "tx_hash"
                ),
                "onchain_events": [],
                "account_after": account_after.as_dict(),
                "accounting": accounting_payload_for_probe(
                    side=side,
                    asset_id=base["asset_id"],
                    before=account_before.as_dict(),
                    after=account_after.as_dict(),
                    row=base,
                    truth=truth,
                ),
            }
            base["probe_state"] = _observed_state(truth)
            states.append(base["probe_state"])
            base["artifact_bitmap"] = artifact_bitmap(
                {
                    "INTENT_PRESENT",
                    "PREDICTION_PRESENT",
                    "DECISION_BOOK_PRESENT",
                    "ARRIVAL_BOOK_PRESENT",
                    "MODEL_MANIFEST_PRESENT",
                    "RISK_CONFIG_PRESENT",
                    "MARKET_METADATA_PRESENT",
                    "SIGNED_ORDER_PRESENT",
                    "ORDER_HASH_PRESENT",
                    "HTTP_REQUEST_PRESENT",
                    "HTTP_RESPONSE_PRESENT",
                    "ORDER_ID_PRESENT",
                },
                live=True,
            )
            persisted = self._persist_probe(base, states, started)
            reconciled = reconcile_probe(persisted, user_events)
            clean_cohort_reconciliation = {
                key: value
                for key, value in dict(base.get("reconciliation") or {}).items()
                if key.startswith("clean_cohort_")
            }
            pnl = self.calibration_store.apply_probe_pnl(reconciled)
            reconciled["reconciliation"] = {
                **dict(reconciled.get("reconciliation") or {}),
                **clean_cohort_reconciliation,
                "pnl": pnl,
            }
            persisted = self.calibration_store.upsert_probe(reconciled)
            return self._sync_paired_probe(persisted)
        except VenueMaintenance as exc:
            row = self._submission_failure(
                base, states, started, ProbeState.VENUE_MAINTENANCE, exc
            )
            return self._commit_clean_cohort_prediction_if_submitted(run_id, row)
        except VenueRestrictedMode as exc:
            row = self._submission_failure(
                base, states, started, ProbeState.VENUE_MAINTENANCE, exc
            )
            return self._commit_clean_cohort_prediction_if_submitted(run_id, row)
        except SubmitOutcomeUnknown as exc:
            row = self._submission_failure(
                base, states, started, ProbeState.SUBMIT_OUTCOME_UNKNOWN, exc
            )
            row = self._commit_clean_cohort_prediction_if_submitted(run_id, row)
            self._activate_stop("submit outcome unknown", cancel_all=True)
            return row
        except OrderSubmissionRejected as exc:
            if _is_expected_fok_depth_rejection(
                base,
                exc,
                expected_outcome=self.plan.execution.expected_outcome,
            ):
                row = self._expected_rejection_result(
                    base,
                    states,
                    started,
                    account_before=account_before,
                    exc=exc,
                )
                return self._commit_clean_cohort_prediction_if_submitted(run_id, row)
            row = self._submission_failure(
                base, states, started, ProbeState.HTTP_REJECTED, exc
            )
            return self._commit_clean_cohort_prediction_if_submitted(run_id, row)
        except Exception as exc:
            submitted = bool(base.get("exchange_submit_called"))
            target = (
                ProbeState.MANUAL_INTERVENTION if submitted else ProbeState.RISK_ABORTED
            )
            base["probe_state"] = target.value
            base["errors"] = [f"{exc.__class__.__name__}:{str(exc)[:500]}"]
            states.append(target.value)
            row = self._persist_probe(base, states, started)
            if submitted:
                row = self._commit_clean_cohort_prediction_if_submitted(run_id, row)
                self._activate_stop(
                    "live probe exception after submit boundary", cancel_all=True
                )
            return row

    def _base_probe(
        self,
        run_id: str,
        manifest: Mapping[str, Any],
        candidate: Mapping[str, Any],
        started: datetime,
        side: str,
        order_type: str,
    ) -> dict[str, Any]:
        return {
            "probe_id": payload_hash(
                {"run_id": run_id, "index": 0, "asset_id": candidate.get("asset_id")},
                prefix="calprobe-",
            ),
            "run_id": run_id,
            "clean_cohort_id": getattr(self, "clean_cohort_id", None),
            "paper_strategy_id": getattr(self, "paper_strategy_id", None),
            "paired_probe_id": None,
            "model_version": str(manifest["paper_execution_model_version"]),
            "manifest_id": str(manifest["manifest_id"]),
            "probe_state": ProbeState.PLANNED.value,
            "asset_id": str(candidate.get("asset_id") or ""),
            "market_id": str(candidate.get("market_id") or ""),
            "condition_id": str(candidate.get("condition_id") or ""),
            "side": side,
            "order_type": order_type,
            "amount": self.plan.execution.amount,
            "amount_unit": self.plan.execution.amount_unit,
            "decision_ts": started,
            "artifact_bitmap": artifact_bitmap([], live=True),
            "prediction": {},
            "market_snapshot": _decorate_candidate(candidate),
            "risk_snapshot": {},
            "signed_order_audit": {},
            "lifecycle": {"state": "NOT_SUBMITTED"},
            "reconciliation": {},
            "timestamps": {"planned_at": started},
            "errors": [],
            "exchange_submit_called": False,
        }

    def _submission_failure(
        self,
        base: dict[str, Any],
        states: list[str],
        started: datetime,
        target: ProbeState,
        exc: Exception,
    ) -> dict[str, Any]:
        audit = getattr(exc, "audit", None)
        if audit is not None:
            base["signed_order_audit"] = audit.as_dict()
            base["exchange_submit_called"] = bool(audit.exchange_submit_called)
        base["probe_state"] = target.value
        base["errors"] = [f"{exc.__class__.__name__}:{str(exc)[:500]}"]
        base["lifecycle"] = {**base.get("lifecycle", {}), "state": target.value}
        states.append(target.value)
        return self._persist_probe(base, states, started)

    def _seed_sell_paper_baseline(
        self,
        *,
        run_id: str,
        candidate: Mapping[str, Any],
        account_before: Any,
    ) -> dict[str, str]:
        state = self._validated_sell_seed_state(
            account_before=account_before,
            asset_id=str(candidate["asset_id"]),
        )
        strategy_id = f"taker-calibration-{run_id}"
        PostgresPaperLedgerSink(
            connection_factory=self.shadow_store.connection_factory,
            ensure_schema=False,
        ).seed_calibration_position(
            strategy_id=strategy_id,
            asset_id=str(candidate["asset_id"]),
            market_id=str(candidate["market_id"]),
            condition_id=str(candidate["condition_id"]),
            quantity=state["quantity"],
            cost_basis=state["cost_basis"],
        )
        return {
            "strategy_id": strategy_id,
            "asset_id": str(candidate["asset_id"]),
            "quantity": format(state["quantity"], "f"),
            "cost_basis": format(state["cost_basis"], "f"),
        }

    def _validated_sell_seed_state(
        self,
        *,
        account_before: Any,
        asset_id: str,
    ) -> dict[str, Decimal]:
        account_id = str(account_before.funder_address or "").lower()
        tracked = self.calibration_store.load_pnl_position(
            account_id=account_id,
            asset_id=asset_id,
        )
        if tracked is None:
            raise ValueError("tracked calibration PnL position is missing")
        return _validated_sell_seed(
            observed_quantity=_decimal(account_before.conditional.get("balance")),
            tracked=tracked,
            requested_size=self.plan.execution.amount,
        )

    def _validated_clean_cohort_sell_state(
        self,
        *,
        account_before: Any,
        asset_id: str,
    ) -> dict[str, Decimal]:
        if not self.paper_strategy_id:
            raise ValueError("clean cohort paper strategy is missing")
        with (
            self.shadow_store.connection_factory(readonly=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                """
                SELECT quantity,reserved_quantity,cost_basis
                FROM quant.paper_positions
                WHERE strategy_id=%s AND asset_id=%s
                """,
                (self.paper_strategy_id, str(asset_id)),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError("clean cohort Paper position is missing")
        paper_quantity = _decimal(row.get("quantity")) - _decimal(
            row.get("reserved_quantity")
        )
        real_quantity = _decimal(account_before.conditional.get("balance"))
        if abs(paper_quantity - real_quantity) > Decimal("0.000001"):
            raise ValueError(
                "clean cohort Paper and real position quantities differ: "
                f"paper={paper_quantity},real={real_quantity}"
            )
        if self.plan.execution.amount > paper_quantity:
            raise ValueError(
                "clean cohort SELL exceeds the matched Paper/real position"
            )
        return {
            "quantity": paper_quantity,
            "cost_basis": _decimal(row.get("cost_basis")),
        }

    def _expected_rejection_result(
        self,
        base: dict[str, Any],
        states: list[str],
        started: datetime,
        *,
        account_before: Any,
        exc: OrderSubmissionRejected,
    ) -> dict[str, Any]:
        account_after = self.adapter.get_account_snapshot(asset_id=base["asset_id"])
        before = account_before.as_dict()
        after = account_after.as_dict()
        collateral_unchanged = _decimal(
            (before.get("collateral") or {}).get("balance")
        ) == _decimal((after.get("collateral") or {}).get("balance"))
        position_unchanged = _decimal(
            (before.get("conditional") or {}).get("balance")
        ) == _decimal((after.get("conditional") or {}).get("balance"))
        no_open_orders = not after.get("open_orders")
        accounting_passed = (
            collateral_unchanged and position_unchanged and no_open_orders
        )
        response = dict(getattr(exc, "response", {}) or {})
        base["lifecycle"] = {
            **base.get("lifecycle", {}),
            "state": ProbeState.HTTP_REJECTED.value,
            "http_response": response,
            "http_status_code": getattr(exc, "status_code", None),
            "account_before": before,
            "account_after": after,
            "open_orders_after": after.get("open_orders") or [],
        }
        base["reconciliation"] = {
            "schema_version": "taker_probe_rejection_reconciliation_v1",
            "predicted_class": "REJECT",
            "actual_class": "REJECT",
            "order_type": base.get("order_type"),
            "rejection_reason": _rejection_text(response),
            "accounting": {
                "status": "PASS" if accounting_passed else "FAIL",
                "accounting_reconciled": accounting_passed,
                "collateral_unchanged": collateral_unchanged,
                "position_unchanged": position_unchanged,
                "no_open_orders": no_open_orders,
            },
            "pnl": {
                "status": "PASS" if accounting_passed else "FAIL",
                "pnl_reconciled": accounting_passed,
                "realized_pnl_delta": "0",
                "unrealized_pnl_delta": "0",
            },
        }
        base["artifact_bitmap"] = artifact_bitmap(
            {
                "INTENT_PRESENT",
                "PREDICTION_PRESENT",
                "DECISION_BOOK_PRESENT",
                "ARRIVAL_BOOK_PRESENT",
                "MODEL_MANIFEST_PRESENT",
                "RISK_CONFIG_PRESENT",
                "MARKET_METADATA_PRESENT",
                "SIGNED_ORDER_PRESENT",
                "ORDER_HASH_PRESENT",
                "HTTP_REQUEST_PRESENT",
                "HTTP_RESPONSE_PRESENT",
                "ACCOUNTING_RECONCILED",
            },
            live=True,
            expected_rejection=True,
        )
        base["probe_state"] = (
            ProbeState.CALIBRATABLE.value
            if accounting_passed
            else ProbeState.ACCOUNTING_MISMATCH.value
        )
        base["errors"] = (
            [] if accounting_passed else ["expected_rejection_account_state_changed"]
        )
        states.extend([ProbeState.HTTP_REJECTED.value, base["probe_state"]])
        return self._persist_probe(base, states, started)

    def _persist_probe(
        self,
        base: Mapping[str, Any],
        states: list[str],
        started: datetime,
    ) -> dict[str, Any]:
        row = self.calibration_store.upsert_probe(base)
        self.calibration_store.append_events(
            _state_events(str(base["probe_id"]), states, started)
        )
        return row

    def _sync_paired_probe(self, row: Mapping[str, Any]) -> dict[str, Any]:
        updated = dict(row)
        reconciliation = dict(updated.get("reconciliation") or {})
        try:
            paired = sync_calibration_probe_to_paired(
                updated,
                store=self.shadow_store,
            )
            reconciliation["paired_probe_sync"] = {
                "status": "PASS",
                "paired_probe_id": paired.get("probe_id"),
                "paired_probe_status": paired.get("status"),
            }
        except Exception as exc:
            reconciliation["paired_probe_sync"] = {
                "status": "RETRY_REQUIRED",
                "paired_probe_id": updated.get("paired_probe_id"),
                "error": f"{exc.__class__.__name__}:{str(exc)[:300]}",
            }
        updated["reconciliation"] = reconciliation
        return self.calibration_store.upsert_probe(updated)

    def _commit_clean_cohort_prediction_if_submitted(
        self,
        run_id: str,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        updated = dict(row)
        if not self.clean_cohort_id or not bool(updated.get("exchange_submit_called")):
            return updated
        reconciliation = dict(updated.get("reconciliation") or {})
        try:
            committed = self.clean_cohort_store.commit_staged_prediction(run_id=run_id)
            reconciliation["clean_cohort_paper_commit"] = {
                "status": "PASS",
                "audit_key": committed["audit_key"],
                "strategy_id": committed["strategy_id"],
                "idempotent": bool(committed["idempotent"]),
            }
        except Exception as exc:
            reconciliation["clean_cohort_paper_commit"] = {
                "status": "RETRY_REQUIRED",
                "error": f"{exc.__class__.__name__}:{str(exc)[:300]}",
            }
        updated["reconciliation"] = reconciliation
        return self.calibration_store.upsert_probe(updated)

    def _submit_prepared_with_durable_audit(
        self,
        *,
        run_id: str,
        prepared: Any,
        risk_passed: bool,
        probe: dict[str, Any],
    ) -> Any:
        """Persist the submit fact only after the official adapter returns."""

        submission = self.adapter.submit_prepared_once(
            prepared,
            run_id=run_id,
            risk_passed=risk_passed,
        )
        audit, _response = submission
        probe["signed_order_audit"] = audit.as_dict()
        probe["exchange_submit_called"] = bool(audit.exchange_submit_called)
        self.calibration_store.upsert_probe(probe)
        probe.update(self._commit_clean_cohort_prediction_if_submitted(run_id, probe))
        return submission

    def _activate_stop(self, reason: str, *, cancel_all: bool) -> None:
        KillSwitch(Path(self.plan.safety.kill_switch_file)).activate(reason)
        if cancel_all and self.plan.safety.cancel_all_on_reconciliation_failure:
            try:
                self.adapter.cancel_all()
            except Exception:
                pass

    def _phase5b_gate(self) -> dict[str, Any]:
        output_dir = self.project_root / "runtime_outputs/taker_calibration"
        candidates = sorted(
            output_dir.glob("phase5b-formal*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            passed = (
                report.get("status") == "PASS"
                and int(report.get("complete_probe_count") or 0) >= 50
                and int(report.get("exchange_submit_count") or 0) == 0
                and int(report.get("credential_leak_count") or 0) == 0
                and int(report.get("raw_signature_count") or 0) == 0
                and isinstance(report.get("manifest"), Mapping)
                and bool(report.get("manifest"))
            )
            return {
                "status": "PASS" if passed else "FAIL",
                "report_path": str(path),
                "run_id": report.get("run_id"),
                "complete_probe_count": int(report.get("complete_probe_count") or 0),
                "manifest": dict(report.get("manifest") or {}),
            }
        return {
            "status": "FAIL",
            "report_path": None,
            "complete_probe_count": 0,
            "manifest": {},
        }

    @staticmethod
    def _blocked(run_id: str, reason: str, issues: list[str]) -> dict[str, Any]:
        return {
            "schema_version": "taker_live_run_report_v1",
            "status": "BLOCKED",
            "run_id": run_id,
            "reason": reason,
            "issues": sorted(set(issues)),
            "submitted_order_count": 0,
        }

    def _finish_blocked(
        self, run_id: str, reason: str, issues: list[str]
    ) -> dict[str, Any]:
        report = self._blocked(run_id, reason, issues)
        self.calibration_store.finish_run(run_id, status="BLOCKED", report=report)
        self._abort_clean_cohort_operation(run_id, reason=reason)
        return report

    def _abort_clean_cohort_operation(self, run_id: str, *, reason: str) -> None:
        if not getattr(self, "clean_cohort_id", None):
            return
        try:
            self.clean_cohort_store.transition_operation(
                run_id=run_id,
                expected=("PREPARED", "RUNNING"),
                target="ABORTED_NO_SUBMIT",
                reason=reason,
            )
        except Exception:
            # The live run report remains authoritative. Cohort status repair is
            # explicit so a storage outage cannot be mistaken for a safe retry.
            pass


def _route_exposure_bounds(
    *,
    side: str,
    amount: Decimal,
    amount_unit: str,
    price: Decimal,
    position: Decimal,
) -> tuple[Decimal, Decimal]:
    if str(amount_unit).upper() == "SHARES":
        shares = Decimal(amount)
    elif price > 0:
        shares = Decimal(amount) / Decimal(price)
    else:
        shares = Decimal(0)
    after = position + shares if str(side).upper() == "BUY" else position - shares
    return position, after


def _prepared_candidate_execution_issues(
    plan: ProbePlan,
    candidate: Mapping[str, Any],
    *,
    side: str,
    market_position: Decimal = Decimal("0"),
) -> list[str]:
    """Reject candidates that cannot pass immutable live order limits."""

    issues: list[str] = []
    normalized_side = str(side).upper()
    price_key = "best_ask" if normalized_side == "BUY" else "best_bid"
    price = Decimal(str(candidate.get(price_key) or "0"))
    min_order_size = Decimal(str(candidate.get("min_order_size") or "0"))
    requested_shares = (
        plan.execution.amount
        if plan.execution.amount_unit == "SHARES"
        else (plan.execution.amount / price if price > 0 else Decimal("0"))
    )
    if min_order_size > 0 and requested_shares < min_order_size:
        issues.append("phase5c_order_size_below_market_minimum")
    if plan.market_policy.deny_itode_initially and bool(candidate.get("itode")):
        issues.append("phase5c_itode_market_denied")
    if (
        plan.market_policy.deny_sports_initially
        and str(candidate.get("category") or "").lower() == "sports"
    ):
        issues.append("phase5c_sports_market_denied")
    if str(candidate.get("market_state") or "LIVE") != "LIVE" or not bool(
        candidate.get("execution_eligible", True)
    ):
        issues.append("phase5c_candidate_not_execution_live")

    order_notional = _order_notional(plan, candidate, normalized_side)
    if order_notional <= 0 or order_notional > plan.limits.max_order_notional:
        issues.append("phase5c_order_notional_outside_limit")
    if normalized_side == "BUY":
        projected_market_position = Decimal(market_position) + requested_shares
        if projected_market_position > plan.limits.max_position_per_market:
            issues.append("phase5c_market_position_limit_reached")
        maximum_cash_outflow = order_notional + _estimated_taker_fee(
            plan,
            candidate,
            normalized_side,
        )
        if maximum_cash_outflow > plan.limits.max_order_notional:
            issues.append("phase5c_maximum_cash_outflow_outside_limit")
    return issues


def _validated_sell_seed(
    *,
    observed_quantity: Decimal,
    tracked: Mapping[str, Any],
    requested_size: Decimal,
) -> dict[str, Decimal]:
    real_quantity = _decimal(tracked.get("real_quantity"))
    paper_quantity = _decimal(tracked.get("paper_quantity"))
    real_cost_basis = _decimal(tracked.get("real_cost_basis"))
    paper_cost_basis = _decimal(tracked.get("paper_cost_basis"))
    if observed_quantity <= 0 or requested_size <= 0:
        raise ValueError("SELL requires a positive tracked position and size")
    if requested_size > observed_quantity:
        raise ValueError("SELL size exceeds the observed conditional-token balance")
    if real_quantity != observed_quantity or paper_quantity != observed_quantity:
        raise ValueError("tracked and observed SELL position baselines differ")
    if real_cost_basis != paper_cost_basis:
        raise ValueError("real and paper SELL cost-basis baselines differ")
    if paper_cost_basis < 0:
        raise ValueError("SELL cost-basis baseline cannot be negative")
    return {
        "quantity": paper_quantity,
        "cost_basis": paper_cost_basis,
    }


def _observed_state(truth: Mapping[str, Any]) -> str:
    ledger = str(truth.get("ledger_truth") or "").upper()
    execution = str(truth.get("execution_truth") or "").upper()
    if ledger == "CONFIRMED":
        return ProbeState.CONFIRMED.value
    if ledger == "FAILED":
        return ProbeState.FAILED.value
    if execution == "MATCHED":
        return ProbeState.MATCHED.value
    if execution in {"UNMATCHED", "CANCELED", "CANCELLED"}:
        return ProbeState.UNMATCHED.value
    return ProbeState.ACKED.value


def _apply_user_ws_timestamps(
    timestamps: dict[str, Any],
    events: list[Mapping[str, Any]],
) -> None:
    order_received: list[datetime] = []
    trade_received: list[datetime] = []
    mined: list[datetime] = []
    confirmed: list[datetime] = []
    for event in events:
        payload = (
            event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        )
        event_type = str(
            event.get("event_type") or payload.get("event_type") or ""
        ).upper()
        status = str(payload.get("status") or "").upper().removeprefix("TRADE_STATUS_")
        received_at = _timestamp_value(
            event.get("received_at") or payload.get("_received_at")
        )
        event_ts = _timestamp_value(event.get("event_ts"))
        if event_type == "ORDER" and received_at is not None:
            order_received.append(received_at)
        if event_type == "TRADE" and received_at is not None:
            trade_received.append(received_at)
        if (
            event_type == "TRADE"
            and status in {"MINED", "CONFIRMED"}
            and event_ts is not None
        ):
            mined.append(event_ts)
        if event_type == "TRADE" and status == "CONFIRMED" and event_ts is not None:
            confirmed.append(event_ts)
    if order_received:
        timestamps["user_ws_order_event_receive_ts"] = min(order_received)
    if trade_received:
        timestamps["user_ws_trade_matched_receive_ts"] = min(trade_received)
    if mined:
        timestamps["trade_mined_ts"] = min(mined)
    if confirmed:
        timestamps["trade_confirmed_ts"] = min(confirmed)


def _timestamp_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _identifiers(rows: Any, *keys: str) -> list[str]:
    result: list[str] = []
    for row in rows if isinstance(rows, (list, tuple)) else ():
        if not isinstance(row, Mapping):
            continue
        for key in keys:
            if row.get(key) not in (None, ""):
                result.append(str(row[key]))
                break
    return sorted(set(result))


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def _prediction_matches_expected(
    prediction: Mapping[str, Any],
    expected_outcome: str,
) -> bool:
    expected = str(expected_outcome or "ANY").upper()
    status = str(prediction.get("status") or "").upper()
    if expected == "ANY":
        return True
    if expected == "FILL":
        return status == "FILLED"
    if expected == "PARTIAL":
        return status == "PARTIAL"
    if expected == "REJECT":
        return status in {"REJECTED", "CANCELLED"}
    return False


def _is_expected_fok_depth_rejection(
    base: Mapping[str, Any],
    exc: OrderSubmissionRejected,
    *,
    expected_outcome: str,
) -> bool:
    return (
        str(expected_outcome or "").upper() == "REJECT"
        and str(base.get("order_type") or "").upper() == "FOK"
        and str((base.get("prediction") or {}).get("status") or "").upper()
        == "REJECTED"
        and "order couldn't be fully filled. fok orders are fully filled or killed."
        in _rejection_text(getattr(exc, "response", {})).lower()
    )


def _rejection_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(_rejection_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_rejection_text(item) for item in value)
    return str(value or "")
