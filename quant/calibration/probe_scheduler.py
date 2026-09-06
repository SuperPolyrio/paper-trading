"""Phase 5B no-submit orchestration over the live paper shadow."""

from __future__ import annotations

from datetime import datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import os
import time
from typing import Any, Callable, Mapping, Sequence

from quant.core.db import postgres_connection
from quant.paper.authority import ControlPlanePostgresConnectionFactory
from quant.paper.control_plane_connection import (
    paper_control_plane_connection_factory,
)
from quant.paper.live_shadow_store import LiveShadowStore
from quant.paper.paired_probe import (
    NO_SUBMIT,
    RECORD_ONLY,
    load_live_probe_candidates,
    run_paired_probe,
)
from quant.paper.taker_execution import TakerExecutionConfig

from .calibration_domain import ModelState, ProbeState, artifact_bitmap, canonical_json, payload_hash
from .experiment_manifest import build_experiment_manifest
from .market_taxonomy import normalize_market_domain
from .probe_plan import ProbePlan, validate_probe_plan
from .probe_risk_guard import PreflightResult, evaluate_preflight
from .real_live_adapter import AccountSnapshot, PolymarketV2LiveAdapter
from .signed_order_prediction import normalize_prediction_to_signed_order
from .store import CalibrationStore
from .user_ws_recorder import UserWsRecorder


class _RetryableCandidate(RuntimeError):
    pass


def _paper_shadow_connection_factory(
    environ: Mapping[str, str],
) -> ControlPlanePostgresConnectionFactory:
    """Use the colocated paper authority DB without moving calibration state."""
    return paper_control_plane_connection_factory(
        environ,
        fallback_connection_factory=postgres_connection,
    )


_EVENT_DRIVEN_CHECKPOINT_MAX_AGE_SECONDS = 60.0
_TARGETED_QUIET_CHECKPOINT_MAX_AGE_SECONDS = 300.0
# GCP persists the cross-host health projection about every 20-35 seconds.
# Per-route message freshness remains capped separately at 30 seconds.
_SHADOW_HEALTH_MAX_AGE_SECONDS = 45.0
_ROUTE_MESSAGE_MAX_AGE_SECONDS = 30.0


_RETRYABLE_PREFLIGHT_ISSUES = frozenset(
    {
        "candidate_book_stale",
        "candidate_rest_book_mismatch",
        "market_position_limit_reached",
        "sports_market_denied",
        "itode_market_denied",
        "market_too_close_to_scheduled_close",
        "scheduled_close_missing",
    }
)


class NoSubmitProbeRunner:
    def __init__(
        self,
        plan: ProbePlan,
        *,
        calibration_store: CalibrationStore | None = None,
        shadow_store: LiveShadowStore | None = None,
        adapter: PolymarketV2LiveAdapter | None = None,
        user_ws: UserWsRecorder | None = None,
        environ: Mapping[str, str] | None = None,
        project_root: Path | None = None,
        candidate_wait_seconds: float = 30.0,
        candidate_poll_seconds: float = 0.1,
        paper_prediction_wait_seconds: float = 30.0,
        candidate_loader: Callable[..., list[dict[str, Any]]] = load_live_probe_candidates,
        frozen_manifest: Mapping[str, Any] | None = None,
        target_asset_id: str | None = None,
    ) -> None:
        self.plan = plan
        self.environ = os.environ if environ is None else environ
        self.calibration_store = calibration_store or CalibrationStore()
        self.shadow_store = shadow_store or LiveShadowStore(
            _paper_shadow_connection_factory(self.environ)
        )
        self.adapter = adapter or PolymarketV2LiveAdapter(plan, environ=self.environ)
        self.user_ws = user_ws or UserWsRecorder(plan, environ=self.environ)
        self.project_root = project_root or Path(__file__).resolve().parents[2]
        self.shadow_status_path = self.project_root / "runtime_outputs/paper_live_shadow/status.json"
        self.candidate_wait_seconds = max(0.0, float(candidate_wait_seconds))
        self.candidate_poll_seconds = max(0.01, float(candidate_poll_seconds))
        self.paper_prediction_wait_seconds = max(
            0.1, float(paper_prediction_wait_seconds)
        )
        self.candidate_loader = candidate_loader
        self.frozen_manifest = dict(frozen_manifest or {})
        self.target_asset_id = str(target_asset_id or "").strip() or None

    def run(self) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        issues = validate_probe_plan(
            self.plan,
            live=False,
            require_credentials=True,
            environ=self.environ,
        )
        if issues:
            return _startup_failure(self.plan, issues, started_at)
        execution_config = TakerExecutionConfig(max_book_age_ms=self.plan.market_policy.max_book_age_ms)
        if self.frozen_manifest:
            manifest = dict(self.frozen_manifest)
        else:
            manifest = build_experiment_manifest(
                project_root=self.project_root,
                execution_config=execution_config,
                configuration={
                    "execution_config_hash": execution_config.config_hash,
                    "clob_host": self.plan.network.clob_host,
                    "chain_id": self.plan.network.chain_id,
                    "proxy_url": self.plan.network.proxy_url,
                    "signature_type": self.plan.account.expected_signature_type,
                },
                model_state=ModelState.CALIBRATING,
                now=started_at,
            ).as_dict()
        required_manifest_keys = {
            "manifest_id",
            "paper_execution_model_version",
            "execution_config_hash",
            "model_state",
        }
        missing_manifest_keys = required_manifest_keys - set(manifest)
        if missing_manifest_keys:
            return _startup_failure(
                self.plan,
                ["frozen_manifest_missing:" + ",".join(sorted(missing_manifest_keys))],
                started_at,
            )
        run_id = payload_hash(
            {"manifest_id": manifest["manifest_id"], "plan_hash": self.plan.plan_hash, "started_at": started_at},
            prefix="calrun-",
        )
        self.calibration_store.ensure_schema()
        self.shadow_store.ensure_schema()
        self.calibration_store.freeze_model(manifest)
        self.calibration_store.create_run(
            {
                "run_id": run_id,
                "mode": "no-submit",
                "run_status": "RUNNING",
                "model_version": manifest["paper_execution_model_version"],
                "manifest_id": manifest["manifest_id"],
                "plan_hash": self.plan.plan_hash,
                "plan": self.plan.redacted_dict(),
                "expected_probe_count": self.plan.execution.count,
                "started_at": started_at,
            }
        )
        seed_candidates = self._wait_for_shadow_fresh_candidates()
        if not seed_candidates:
            report = _empty_report(run_id, manifest, self.plan, "no fresh A/A_PLUS candidates")
            self.calibration_store.finish_run(run_id, status="FAIL", report=report)
            return report

        first = _decorate_candidate(seed_candidates[0])
        market_snapshot: dict[str, Any] = {}
        try:
            user_ws = self.user_ws.probe_connection([str(first["condition_id"])])
            preflight = None
            last_retry = "no candidate evaluated"
            deadline = time.monotonic() + max(60.0, self.candidate_wait_seconds * 4)
            while preflight is None and time.monotonic() < deadline:
                candidates = self._wait_for_shadow_fresh_candidates()
                if not candidates:
                    last_retry = "no fresh shadow head during shared preflight"
                    continue
                for raw_candidate in candidates:
                    current = _decorate_candidate(raw_candidate)
                    try:
                        current_market = self.adapter.get_market_snapshot(
                            asset_id=str(current["asset_id"]),
                            condition_id=str(current["condition_id"]),
                        )
                        current_account = self.adapter.get_account_snapshot(
                            asset_id=str(current["asset_id"])
                        )
                        refreshed = self._wait_for_shadow_fresh_candidates(
                            asset_id=str(current["asset_id"])
                        )
                        if not refreshed:
                            raise _RetryableCandidate(
                                "shadow_head_not_fresh_after_shared_preflight_checks"
                            )
                        current = _decorate_candidate(refreshed[0])
                        current_market = {
                            **current_market,
                            **self.adapter.get_book_snapshot(asset_id=str(current["asset_id"])),
                        }
                        current_preflight = evaluate_preflight(
                            self.plan,
                            self._preflight_context(
                                current,
                                current_market,
                                current_account,
                                user_ws,
                            ),
                            live=False,
                            run_id=run_id,
                            environ=self.environ,
                        )
                    except _RetryableCandidate as exc:
                        last_retry = str(exc)
                        continue
                    except Exception as exc:
                        if _is_retryable_adapter_error(exc):
                            last_retry = f"adapter_transient:{exc.__class__.__name__}"
                            continue
                        raise
                    if (
                        not current_preflight.passed
                        and set(current_preflight.issues)
                        and set(current_preflight.issues) <= _RETRYABLE_PREFLIGHT_ISSUES
                    ):
                        last_retry = "transient_preflight:" + ",".join(current_preflight.issues)
                        continue
                    first = current
                    market_snapshot = current_market
                    preflight = current_preflight
                    break
            if preflight is None:
                raise _RetryableCandidate(f"shared_preflight_timeout:{last_retry}")
        except Exception as exc:
            preflight = PreflightResult(
                passed=False,
                state=ProbeState.RISK_ABORTED.value,
                checked_at=datetime.now(timezone.utc),
                issues=(f"preflight_adapter_error:{exc.__class__.__name__}:{str(exc)[:300]}",),
                checks={"exchange_submit_allowed": False},
                snapshot_hash=payload_hash(str(exc), prefix="risk-"),
            )
        if not preflight.passed:
            report = self._persist_preflight_failure(
                run_id=run_id,
                manifest=manifest,
                candidate=first,
                market_snapshot=market_snapshot,
                preflight=preflight,
            )
            self.calibration_store.finish_run(run_id, status="FAIL", report=report)
            return report

        rows: list[dict[str, Any]] = []
        run_issues: list[str] = []
        candidate_skip_reasons: list[str] = []
        for index in range(self.plan.execution.count):
            probe_deadline = time.monotonic() + max(60.0, self.candidate_wait_seconds * 4)
            row: dict[str, Any] | None = None
            while time.monotonic() < probe_deadline:
                fresh_candidates = self._wait_for_shadow_fresh_candidates()
                if not fresh_candidates:
                    candidate_skip_reasons.append(f"shadow_fresh_timeout_at_probe:{index}")
                    continue
                candidate = _decorate_candidate(fresh_candidates[index % len(fresh_candidates)])
                try:
                    row = self._run_one(
                        index=index,
                        run_id=run_id,
                        manifest=manifest,
                        candidate=candidate,
                        shared_preflight=preflight,
                    )
                except _RetryableCandidate as exc:
                    candidate_skip_reasons.append(str(exc)[:300])
                    continue
                break
            if row is None:
                run_issues.append(f"no_shadow_ready_candidate_at_probe:{index}")
                break
            rows.append(row)
            if index + 1 < self.plan.execution.count and self.plan.execution.interval_seconds > 0:
                time.sleep(float(self.plan.execution.interval_seconds))
        report = build_no_submit_report(
            run_id=run_id,
            manifest=manifest,
            plan=self.plan,
            probes=rows,
            credential_values=[
                str(self.environ.get(name) or "")
                for name in (
                    self.plan.account.private_key_env,
                    self.plan.account.api_key_env,
                    self.plan.account.api_secret_env,
                    self.plan.account.api_passphrase_env,
                )
            ],
        )
        if run_issues:
            report["status"] = "FAIL"
            report["reason"] = "fresh market data became unavailable during the no-submit run"
            report["issues"] = run_issues
        report["candidate_skip_count"] = len(candidate_skip_reasons)
        report["candidate_skip_reasons"] = candidate_skip_reasons[-100:]
        self.calibration_store.finish_run(run_id, status=report["status"], report=report)
        return report

    def _wait_for_fresh_candidates(
        self,
        *,
        asset_id: str | None = None,
        after_receive_ts: Any | None = None,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + self.candidate_wait_seconds
        while True:
            loader_args: dict[str, Any] = {
                "limit": max(20, min(500, self.plan.execution.count * 2)),
                "max_age_seconds": self.plan.market_policy.max_book_age_ms / 1000,
            }
            if asset_id:
                loader_args["asset_id"] = str(asset_id)
            candidates = self.candidate_loader(self.shadow_store, **loader_args)
            if after_receive_ts is not None:
                baseline = str(after_receive_ts)
                candidates = [
                    row
                    for row in candidates
                    if str(row.get("last_receive_ts") or "") not in {"", baseline}
                ]
            if candidates:
                return candidates
            if time.monotonic() >= deadline:
                return []
            time.sleep(min(self.candidate_poll_seconds, max(0.0, deadline - time.monotonic())))

    def _wait_for_shadow_fresh_candidates(
        self,
        *,
        asset_id: str | None = None,
        max_age_seconds: float | None = None,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        requested_asset_id = str(asset_id or self.target_asset_id or "").strip() or None
        deadline = time.monotonic() + self.candidate_wait_seconds
        refresh_requested = False
        refresh_requested_at: datetime | None = None
        if requested_asset_id and force_refresh:
            refresh = getattr(self.shadow_store, "request_book_refresh", None)
            if callable(refresh):
                refresh_requested_at = datetime.now(timezone.utc)
                refresh_requested = bool(refresh(requested_asset_id))
        accepted_head_age_seconds = (
            _EVENT_DRIVEN_CHECKPOINT_MAX_AGE_SECONDS
            if max_age_seconds is None
            else max(0.0, float(max_age_seconds))
        )
        accepted_quiet_age_seconds = (
            _TARGETED_QUIET_CHECKPOINT_MAX_AGE_SECONDS
            if max_age_seconds is None
            else accepted_head_age_seconds
        )
        while True:
            payload = self._load_shadow_status()
            route_states = payload.get("route_states") if isinstance(payload.get("route_states"), Mapping) else {}
            route_last_message_at = (
                payload.get("route_last_message_at")
                if isinstance(payload.get("route_last_message_at"), Mapping)
                else {}
            )
            observed_now = datetime.now(timezone.utc)
            status_updated_at = _parse_utc(payload.get("updated_at"))
            route_message_times = [_parse_utc(route_last_message_at.get(route)) for route in route_states]
            healthy_transport = (
                payload.get("transport_state") == "REDUNDANT"
                and route_states
                and all(state == "CONNECTED" for state in route_states.values())
                and status_updated_at is not None
                and 0 <= (observed_now - status_updated_at).total_seconds() <= _SHADOW_HEALTH_MAX_AGE_SECONDS
                and len(route_message_times) == len(route_states)
                and all(
                    observed_at is not None
                    and 0 <= (observed_now - observed_at).total_seconds() <= _ROUTE_MESSAGE_MAX_AGE_SECONDS
                    for observed_at in route_message_times
                )
            )
            heads = (
                payload.get("fresh_book_sample")
                if isinstance(payload.get("fresh_book_sample"), list)
                else []
            )
            candidates: list[dict[str, Any]] = []
            if healthy_transport:
                trusted_heads: dict[str, Mapping[str, Any]] = {}
                for head in heads:
                    if not isinstance(head, Mapping):
                        continue
                    head_asset_id = str(head.get("asset_id") or "")
                    observed_at = _parse_utc(head.get("observed_at"))
                    age_seconds = (
                        (observed_now - observed_at).total_seconds()
                        if observed_at is not None
                        else _EVENT_DRIVEN_CHECKPOINT_MAX_AGE_SECONDS + 1
                    )
                    if (
                        not head_asset_id
                        or (
                            requested_asset_id
                            and head_asset_id != requested_asset_id
                        )
                        or str(head.get("market_state") or "") != "LIVE"
                        or str(head.get("book_status") or "").upper() != "READY"
                        or str(head.get("coverage_grade") or "") not in {"A_PLUS", "A"}
                        or bool(head.get("has_gap"))
                        or age_seconds < 0
                        or age_seconds > accepted_head_age_seconds
                        or (
                            refresh_requested_at is not None
                            and (
                                observed_at is None
                                or observed_at < refresh_requested_at
                            )
                        )
                    ):
                        continue
                    trusted_heads[head_asset_id] = head
                rows = self.candidate_loader(
                    self.shadow_store,
                    limit=1 if requested_asset_id else 500,
                    max_age_seconds=300,
                    **(
                        {"asset_id": requested_asset_id}
                        if requested_asset_id
                        else {}
                    ),
                )
                for row in rows:
                    candidate = dict(row)
                    bootstrap_candidate = (
                        requested_asset_id is not None
                        and str(candidate.get("watch_reason") or "")
                        == "live_probe_candidate"
                    )
                    head = trusted_heads.get(str(candidate.get("asset_id") or ""))
                    if head is None and requested_asset_id:
                        row_observed_at = _parse_utc(candidate.get("last_receive_ts"))
                        row_age_seconds = (
                            (observed_now - row_observed_at).total_seconds()
                            if row_observed_at is not None
                            else accepted_quiet_age_seconds + 1
                        )
                        post_refresh_head = (
                            refresh_requested_at is not None
                            and row_observed_at is not None
                            and row_observed_at >= refresh_requested_at
                        )
                        if (
                            (not force_refresh or post_refresh_head)
                            and
                            (
                                bootstrap_candidate
                                or (
                                    str(candidate.get("market_state") or "") == "LIVE"
                                    and bool(candidate.get("execution_eligible"))
                                )
                            )
                            and str(candidate.get("coverage_grade") or "") in {"A_PLUS", "A"}
                            and not bool(candidate.get("has_gap"))
                            and bool(candidate.get("redundant_feed_match"))
                            and 0 <= row_age_seconds <= accepted_quiet_age_seconds
                        ):
                            head = {
                                "asset_id": candidate.get("asset_id"),
                                "checkpoint_id": candidate.get("connection_id"),
                                "generation": None,
                                "observed_at": row_observed_at.isoformat(),
                                "best_bid": candidate.get("best_bid"),
                                "best_ask": candidate.get("best_ask"),
                                "coverage_grade": candidate.get("coverage_grade"),
                                "has_gap": candidate.get("has_gap"),
                            }
                    if head is None:
                        continue
                    observed_at = _parse_utc(head.get("observed_at"))
                    age_ms = max(0, int((observed_now - observed_at).total_seconds() * 1000))
                    candidate.update(
                        {
                            "best_bid": head.get("best_bid"),
                            "best_ask": head.get("best_ask"),
                            "book_age_ms": age_ms,
                            "coverage_grade": head.get("coverage_grade"),
                            "has_gap": bool(head.get("has_gap")),
                            "shadow_checkpoint_id": head.get("checkpoint_id"),
                            "shadow_generation": head.get("generation"),
                            "shadow_observed_at": head.get("observed_at"),
                            "shadow_transport_state": payload.get("transport_state"),
                            "shadow_route_states": dict(route_states),
                        }
                    )
                    candidates.append(candidate)
                    if len(candidates) >= (1 if requested_asset_id else 10):
                        break
                if candidates:
                    return candidates
                if requested_asset_id and not refresh_requested:
                    refresh = getattr(self.shadow_store, "request_book_refresh", None)
                    if callable(refresh):
                        refresh_requested = bool(refresh(requested_asset_id))
            if time.monotonic() >= deadline:
                return []
            time.sleep(min(self.candidate_poll_seconds, max(0.0, deadline - time.monotonic())))

    def _load_shadow_status(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.shadow_status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        updated_at = _parse_utc(payload.get("updated_at"))
        now = datetime.now(timezone.utc)
        status_age = (now - updated_at).total_seconds() if updated_at is not None else None
        if (
            status_age is not None
            and -1 <= status_age <= _SHADOW_HEALTH_MAX_AGE_SECONDS
        ):
            return payload

        status_reader = getattr(self.shadow_store, "status", None)
        if not callable(status_reader):
            return payload
        status = status_reader()
        health = status.get("health") if isinstance(status, Mapping) else None
        if not isinstance(health, Mapping):
            return payload
        route_states = (
            dict(health.get("route_states"))
            if isinstance(health.get("route_states"), Mapping)
            else {}
        )
        route_messages = (
            dict(health.get("route_messages"))
            if isinstance(health.get("route_messages"), Mapping)
            else {}
        )
        last_message_at = health.get("last_message_at")
        return {
            **dict(health),
            "updated_at": _iso_utc(health.get("updated_at")),
            "last_message_at": _iso_utc(last_message_at),
            "route_states": route_states,
            "route_last_message_at": {
                route: _iso_utc(last_message_at)
                for route, state in route_states.items()
                if state == "CONNECTED" and int(route_messages.get(route) or 0) > 0
            },
            "fresh_book_sample": [],
        }

    def _wait_for_recurring_candidates(self) -> list[dict[str, Any]]:
        deadline = time.monotonic() + self.candidate_wait_seconds
        receive_markers: dict[str, set[str]] = {}
        latest: dict[str, dict[str, Any]] = {}
        while True:
            candidates = self.candidate_loader(
                self.shadow_store,
                limit=max(20, min(500, self.plan.execution.count * 2)),
                max_age_seconds=self.plan.market_policy.max_book_age_ms / 1000,
            )
            for row in candidates:
                asset_id = str(row.get("asset_id") or "")
                receive_ts = str(row.get("last_receive_ts") or "")
                if not asset_id or not receive_ts:
                    continue
                receive_markers.setdefault(asset_id, set()).add(receive_ts)
                latest[asset_id] = dict(row)
            recurring = [
                latest[str(row.get("asset_id"))]
                for row in candidates
                if len(receive_markers.get(str(row.get("asset_id")), set())) >= 2
            ]
            if recurring:
                return recurring
            if time.monotonic() >= deadline:
                return []
            time.sleep(min(self.candidate_poll_seconds, max(0.0, deadline - time.monotonic())))

    def _run_one(
        self,
        *,
        index: int,
        run_id: str,
        manifest: Mapping[str, Any],
        candidate: Mapping[str, Any],
        shared_preflight: PreflightResult,
    ) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        side = self.plan.execution.sides[index % len(self.plan.execution.sides)]
        order_type = self.plan.execution.order_types[index % len(self.plan.execution.order_types)]
        base = {
            "probe_id": payload_hash(
                {"run_id": run_id, "index": index, "asset_id": candidate.get("asset_id")},
                prefix="calprobe-",
            ),
            "run_id": run_id,
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
            "artifact_bitmap": artifact_bitmap([], live=False),
            "prediction": {},
            "market_snapshot": dict(candidate),
            "risk_snapshot": shared_preflight.as_dict(),
            "signed_order_audit": {},
            "lifecycle": {"state": "NOT_SUBMITTED"},
            "reconciliation": {},
            "timestamps": {"planned_at": started},
            "errors": [],
            "exchange_submit_called": False,
        }
        event_states = [ProbeState.PLANNED.value]
        try:
            market_snapshot = self.adapter.get_market_snapshot(
                asset_id=base["asset_id"], condition_id=base["condition_id"]
            )
            account = self.adapter.get_account_snapshot(asset_id=base["asset_id"])
            refreshed = self._wait_for_shadow_fresh_candidates(asset_id=base["asset_id"])
            if not refreshed:
                raise _RetryableCandidate("shadow_head_not_fresh_after_adapter_checks")
            candidate = _with_market_parameters(refreshed[0], market_snapshot)
            paper, prediction, candidate = self._freeze_shadow_prediction(
                candidate=candidate,
                run_id=run_id,
                side=side,
                order_type=order_type,
                paper_position_size=(
                    account.conditional.get("balance") if side == "SELL" else None
                ),
            )
            base["paired_probe_id"] = paper.get("probe_id")
            base["decision_ts"] = paper.get("decision_ts") or started
            base["prediction"] = prediction
            base["timestamps"]["prediction_frozen_at"] = datetime.now(timezone.utc)
            event_states.append(ProbeState.PREDICTION_FROZEN.value)
            market_snapshot = {
                **market_snapshot,
                **self.adapter.get_book_snapshot(asset_id=base["asset_id"]),
            }
            base["market_snapshot"] = {**candidate, **market_snapshot}
            context = self._preflight_context(
                candidate,
                market_snapshot,
                account,
                {"connected": True},
                side=side,
            )
            preflight = evaluate_preflight(
                self.plan,
                context,
                live=False,
                run_id=run_id,
                environ=self.environ,
            )
            base["risk_snapshot"] = preflight.as_dict()
            if not preflight.passed:
                if (
                    set(preflight.issues)
                    and set(preflight.issues) <= _RETRYABLE_PREFLIGHT_ISSUES
                ):
                    raise _RetryableCandidate(
                        "transient_preflight:" + ",".join(sorted(preflight.issues))
                    )
                base["probe_state"] = ProbeState.RISK_ABORTED.value
                base["errors"] = list(preflight.issues)
                event_states.append(ProbeState.RISK_ABORTED.value)
                persisted = self.calibration_store.upsert_probe(base)
                self.calibration_store.append_events(
                    _state_events(base["probe_id"], event_states, started)
                )
                return persisted
            event_states.append(ProbeState.PREFLIGHT_OK.value)
            worst_price = str(
                candidate.get("execution_worst_price")
                or (candidate["best_ask"] if side == "BUY" else candidate["best_bid"])
            )
            signed = self.adapter.build_and_sign_no_submit(
                asset_id=base["asset_id"],
                side=side,
                order_type=order_type,
                amount=str(self.plan.execution.amount),
                amount_unit=self.plan.execution.amount_unit,
                worst_price=worst_price,
                tick_size=str(market_snapshot["tick_size"]),
                neg_risk=bool(market_snapshot["neg_risk"]),
                user_usdc_balance=(
                    str(account.collateral.get("balance")) if side == "BUY" else None
                ),
            )
            prediction = normalize_prediction_to_signed_order(
                prediction,
                signed.as_dict(),
                base["market_snapshot"],
            )
            base["prediction"] = prediction
            base["signed_order_audit"] = signed.as_dict()
            base["timestamps"]["signed_at"] = signed.signed_at
            base["timestamps"]["prediction_normalized_to_signed_order_at"] = (
                datetime.now(timezone.utc)
            )
            present = {
                "INTENT_PRESENT",
                "PREDICTION_PRESENT",
                "MODEL_MANIFEST_PRESENT",
                "RISK_CONFIG_PRESENT",
                "MARKET_METADATA_PRESENT",
                "SIGNED_ORDER_PRESENT",
                "ORDER_HASH_PRESENT",
            }
            if prediction.get("decision_checkpoint_id"):
                present.add("DECISION_BOOK_PRESENT")
            if prediction.get("arrival_checkpoint_id"):
                present.add("ARRIVAL_BOOK_PRESENT")
            base["artifact_bitmap"] = artifact_bitmap(present, live=False)
            base["probe_state"] = (
                ProbeState.SIGNED.value
                if base["artifact_bitmap"]["complete"]
                else ProbeState.DATA_INCOMPLETE.value
            )
            event_states.append(base["probe_state"])
        except _RetryableCandidate:
            raise
        except Exception as exc:
            if _is_retryable_adapter_error(exc):
                raise _RetryableCandidate(f"adapter_transient:{exc.__class__.__name__}") from exc
            base["probe_state"] = ProbeState.DATA_INCOMPLETE.value
            base["errors"] = [f"{exc.__class__.__name__}:{str(exc)[:500]}"]
            event_states.append(ProbeState.DATA_INCOMPLETE.value)
        persisted = self.calibration_store.upsert_probe(base)
        self.calibration_store.append_events(_state_events(base["probe_id"], event_states, started))
        return persisted

    def _freeze_shadow_prediction(
        self,
        *,
        candidate: Mapping[str, Any],
        run_id: str,
        side: str,
        order_type: str,
        paper_position_size: Any | None = None,
        paper_position_cost_basis: Any | None = None,
        paired_mode: str = NO_SUBMIT,
        paper_strategy_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        decorated = _decorate_candidate(candidate)
        paper = run_paired_probe(
            store=self.shadow_store,
            mode=paired_mode,
            strategy_id=(
                str(paper_strategy_id).strip()
                if paper_strategy_id
                else f"taker-calibration-{run_id}"
            ),
            candidate=decorated,
            side=side,
            amount=self.plan.execution.amount,
            amount_unit=self.plan.execution.amount_unit,
            order_type=order_type,
            fee_rate=decorated.get("fee_rate"),
            fee_exponent=decorated.get("fee_exponent"),
            fee_taker_only=bool(decorated.get("fee_taker_only", True)),
            paper_position_seed=paper_position_size,
            paper_position_cost_basis=paper_position_cost_basis,
            limit_price=(
                (
                    decorated.get("best_ask")
                    if side == "BUY"
                    else decorated.get("best_bid")
                )
                if self.plan.execution.price_mode == "BBO_ONLY"
                else None
            ),
            wait_seconds=self.paper_prediction_wait_seconds,
            evidence_client=None,
        )
        prediction = dict(paper.get("paper_prediction") or {})
        expected_status = "PENDING_LIVE" if paired_mode == RECORD_ONLY else "SHADOW_READY"
        if paper.get("status") != expected_status:
            raise _RetryableCandidate(
                f"shadow_not_ready:{paper.get('status')}:{prediction.get('reason')}"
            )
        arrival_checkpoint_id = prediction.get("arrival_checkpoint_id")
        if not arrival_checkpoint_id:
            raise _RetryableCandidate(
                "shadow_arrival_checkpoint_unavailable:"
                f"{prediction.get('status') or 'UNKNOWN'}:"
                f"{prediction.get('reason') or 'no_reason'}"
            )
        checkpoint = self.shadow_store.load_book_checkpoint(arrival_checkpoint_id)
        if not checkpoint:
            raise _RetryableCandidate(
                f"shadow_arrival_checkpoint_missing:{arrival_checkpoint_id}"
            )
        return paper, prediction, _candidate_from_checkpoint(decorated, checkpoint, prediction)

    def _preflight_context(
        self,
        candidate: Mapping[str, Any],
        market_snapshot: Mapping[str, Any],
        account: AccountSnapshot | None,
        user_ws: Mapping[str, Any],
        *,
        side: str | None = None,
        daily_usage: Mapping[str, Any] | None = None,
        hourly_usage: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        collateral = dict(account.collateral) if account else {}
        conditional = dict(account.conditional) if account else {}
        allowance_resolver = getattr(
            self.adapter, "required_order_allowance", None
        )
        if account is not None and callable(allowance_resolver):
            neg_risk = bool(market_snapshot.get("neg_risk"))
            collateral_allowance = allowance_resolver(
                account,
                asset_type="COLLATERAL",
                neg_risk=neg_risk,
            )
            conditional_allowance = allowance_resolver(
                account,
                asset_type="CONDITIONAL",
                neg_risk=neg_risk,
            )
        else:
            collateral_allowance = {
                "allowance": str(_allowance(collateral)),
                "spender": None,
                "source": "legacy_minimum_allowance_fallback",
            }
            conditional_allowance = {
                "allowance": str(_allowance(conditional)),
                "spender": None,
                "source": "legacy_minimum_allowance_fallback",
            }
        usage = dict(daily_usage) if daily_usage is not None else self.calibration_store.recent_usage(
            since=datetime.combine(datetime.now(timezone.utc).date(), datetime_time.min, tzinfo=timezone.utc)
        )
        hourly = dict(hourly_usage) if hourly_usage is not None else self.calibration_store.recent_usage(
            since=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        validated_candidate = _rest_validated_candidate(
            candidate,
            market_snapshot,
            side=side or self.plan.execution.sides[0],
        )
        return {
            "model_state": ModelState.CALIBRATING.value,
            "sdk_version": self.adapter.sdk_version,
            "observed_funder_address": account.funder_address if account else None,
            "observed_signature_type": account.signature_type if account else None,
            "server_clock_offset_ms": market_snapshot.get("server_clock_offset_ms"),
            "user_ws_connected": bool(user_ws.get("connected")),
            "matching_engine_mode": (
                account.matching_engine_mode
                if account and bool(market_snapshot.get("matching_engine_public_ok"))
                else "UNKNOWN"
            ),
            "write_route_geoblock": market_snapshot.get("write_route_geoblock") or {},
            "candidate": {
                **validated_candidate,
                "tick_size": market_snapshot.get("tick_size") or validated_candidate.get("tick_size"),
                "fee_rate_bps": market_snapshot.get("fee_rate_bps"),
                "fee_rate": market_snapshot.get("fee_rate"),
                "fee_exponent": market_snapshot.get("fee_exponent"),
                "fee_taker_only": market_snapshot.get("fee_taker_only"),
                "min_order_size": (
                    market_snapshot.get("min_order_size") or validated_candidate.get("min_order_size")
                ),
                "itode": bool(market_snapshot.get("itode") or validated_candidate.get("itode")),
                "neg_risk": market_snapshot.get("neg_risk"),
            },
            "side": side or self.plan.execution.sides[0],
            "daily_gross_notional": usage.get("gross_quote_amount") or 0,
            "daily_realized_loss": None,
            "hourly_submitted_orders": hourly.get("submitted_orders") or 0,
            "market_position": _number(conditional.get("balance"), default=-1),
            "open_orders_count": len(account.open_orders) if account else 999999,
            "collateral_balance": _number(collateral.get("balance"), default=-1),
            "collateral_allowance": collateral_allowance["allowance"],
            "collateral_allowance_spender": collateral_allowance.get("spender"),
            "collateral_allowance_source": collateral_allowance.get("source"),
            "conditional_token_balance": _number(conditional.get("balance"), default=-1),
            "conditional_token_allowance": conditional_allowance["allowance"],
            "conditional_token_allowance_spender": conditional_allowance.get(
                "spender"
            ),
            "conditional_token_allowance_source": conditional_allowance.get(
                "source"
            ),
        }

    def _persist_preflight_failure(
        self,
        *,
        run_id: str,
        manifest: Mapping[str, Any],
        candidate: Mapping[str, Any],
        market_snapshot: Mapping[str, Any],
        preflight: PreflightResult,
    ) -> dict[str, Any]:
        probe_id = payload_hash({"run_id": run_id, "preflight": True}, prefix="calprobe-")
        row = self.calibration_store.upsert_probe(
            {
                "probe_id": probe_id,
                "run_id": run_id,
                "paired_probe_id": None,
                "model_version": manifest["paper_execution_model_version"],
                "manifest_id": manifest["manifest_id"],
                "probe_state": ProbeState.RISK_ABORTED.value,
                "asset_id": str(candidate.get("asset_id") or ""),
                "market_id": str(candidate.get("market_id") or ""),
                "condition_id": str(candidate.get("condition_id") or ""),
                "side": self.plan.execution.sides[0],
                "order_type": self.plan.execution.order_types[0],
                "amount": self.plan.execution.amount,
                "amount_unit": self.plan.execution.amount_unit,
                "decision_ts": preflight.checked_at,
                "artifact_bitmap": artifact_bitmap(
                    {"MODEL_MANIFEST_PRESENT", "RISK_CONFIG_PRESENT"}, live=False
                ),
                "prediction": {},
                "market_snapshot": {**candidate, **market_snapshot},
                "risk_snapshot": preflight.as_dict(),
                "signed_order_audit": {},
                "lifecycle": {"state": "NOT_SUBMITTED"},
                "reconciliation": {},
                "timestamps": {"preflight_checked_at": preflight.checked_at},
                "errors": list(preflight.issues),
                "exchange_submit_called": False,
            }
        )
        self.calibration_store.append_events(
            _state_events(
                probe_id,
                [ProbeState.PLANNED.value, ProbeState.RISK_ABORTED.value],
                preflight.checked_at,
            )
        )
        return build_no_submit_report(
            run_id=run_id,
            manifest=manifest,
            plan=self.plan,
            probes=[row],
            credential_values=[],
        )


def build_no_submit_report(
    *,
    run_id: str,
    manifest: Mapping[str, Any],
    plan: ProbePlan,
    probes: Sequence[Mapping[str, Any]],
    credential_values: Sequence[str],
) -> dict[str, Any]:
    rows = [dict(row) for row in probes]
    complete = [
        row
        for row in rows
        if bool((row.get("artifact_bitmap") or {}).get("complete"))
        and str(row.get("probe_state")) == ProbeState.SIGNED.value
        and bool((row.get("signed_order_audit") or {}).get("order_hash"))
        and not bool(row.get("exchange_submit_called"))
    ]
    submitted = [row for row in rows if bool(row.get("exchange_submit_called"))]
    signed = [row for row in rows if bool((row.get("signed_order_audit") or {}).get("order_hash"))]
    raw_signatures = [
        row
        for row in rows
        if bool((row.get("signed_order_audit") or {}).get("raw_signature_persisted"))
        or bool((row.get("signed_order_audit") or {}).get("signature"))
    ]
    expected_model = str(manifest.get("paper_execution_model_version") or "")
    model_mismatches = [
        row
        for row in rows
        if expected_model
        and (row.get("prediction") or {}).get("model_version")
        and str((row.get("prediction") or {}).get("model_version")) != expected_model
    ]
    payload_text = canonical_json(rows)
    leaked = [index for index, value in enumerate(credential_values) if value and value in payload_text]
    formal_sample_size = plan.execution.count >= 50
    all_complete = len(complete) == plan.execution.count == len(rows)
    if submitted or leaked or raw_signatures:
        status = "FAIL"
        reason = "no-submit safety invariant was violated"
    elif model_mismatches:
        status = "FAIL"
        reason = "one or more predictions do not match the frozen model version"
    elif all_complete and formal_sample_size:
        status = "PASS"
        reason = "Phase 5B no-submit artifact gate passed"
    elif all_complete:
        status = "SMOKE_PASS"
        reason = "no-submit mechanics passed, but fewer than 50 probes cannot close Phase 5B"
    else:
        status = "FAIL"
        reason = "one or more no-submit probes are incomplete"
    return {
        "schema_version": "taker_no_submit_report_v1",
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reason": reason,
        "manifest": dict(manifest),
        "plan_hash": plan.plan_hash,
        "expected_probe_count": plan.execution.count,
        "probe_count": len(rows),
        "complete_probe_count": len(complete),
        "signed_order_count": len(signed),
        "exchange_submit_count": len(submitted),
        "credential_leak_count": len(leaked),
        "raw_signature_count": len(raw_signatures),
        "model_version_mismatch_count": len(model_mismatches),
        "formal_minimum_probe_count": 50,
        "formal_sample_size_met": formal_sample_size,
        "artifact_complete_rate_pct": (
            format(Decimal(len(complete)) * 100 / Decimal(len(rows)), ".2f") if rows else "0.00"
        ),
        "state_counts": _counts(str(row.get("probe_state") or "UNKNOWN") for row in rows),
        "probes": rows,
    }


def _startup_failure(plan: ProbePlan, issues: Sequence[str], started_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": "taker_no_submit_report_v1",
        "run_id": None,
        "generated_at": started_at.isoformat(),
        "status": "FAIL",
        "reason": "probe plan or required credentials failed startup validation",
        "plan_hash": plan.plan_hash,
        "issues": list(issues),
        "exchange_submit_count": 0,
        "probe_count": 0,
    }


def _empty_report(
    run_id: str,
    manifest: Mapping[str, Any],
    plan: ProbePlan,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": "taker_no_submit_report_v1",
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "FAIL",
        "reason": reason,
        "manifest": dict(manifest),
        "plan_hash": plan.plan_hash,
        "probe_count": 0,
        "exchange_submit_count": 0,
    }


def _decorate_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    candidate = dict(row)
    raw = candidate.get("raw_metadata") if isinstance(candidate.get("raw_metadata"), Mapping) else {}
    clob = raw.get("clobMarket") if isinstance(raw.get("clobMarket"), Mapping) else {}
    fee = clob.get("fd") if isinstance(clob.get("fd"), Mapping) else {}
    source_category = (
        candidate.get("source_category")
        or candidate.get("category")
        or raw.get("category")
        or "unknown"
    )
    candidate["source_category"] = source_category
    candidate["category"] = normalize_market_domain(
        source_category,
        market_title=candidate.get("market_title"),
        event_title=candidate.get("event_title"),
        market_slug=candidate.get("market_slug"),
    )
    candidate["itode"] = bool(candidate.get("itode") or raw.get("itode") or clob.get("itode"))
    if candidate.get("fee_rate") in (None, "") and fee.get("r") not in (None, ""):
        candidate["fee_rate"] = fee.get("r")
    if candidate.get("fee_exponent") in (None, "") and fee.get("e") not in (None, ""):
        candidate["fee_exponent"] = fee.get("e")
    if not isinstance(candidate.get("fee_taker_only"), bool) and isinstance(fee.get("to"), bool):
        candidate["fee_taker_only"] = fee.get("to")
    if not isinstance(candidate.get("neg_risk"), bool) and isinstance(clob.get("nr"), bool):
        candidate["neg_risk"] = clob.get("nr")
    candidate["end_date"] = (
        candidate.get("end_date") or raw.get("endDate") or raw.get("end_date")
    )
    candidate["market_state"] = candidate.get("market_state") or "LIVE"
    candidate["execution_eligible"] = bool(candidate.get("execution_eligible", True))
    candidate["has_gap"] = bool(candidate.get("has_gap", False))
    candidate["rest_book_match"] = bool(candidate.get("rest_book_match"))
    return candidate


def _with_market_parameters(
    candidate: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(candidate)
    for field in (
        "tick_size",
        "min_order_size",
        "fee_rate_bps",
        "fee_rate",
        "fee_exponent",
        "fee_taker_only",
        "itode",
        "neg_risk",
        "end_date",
        "market_state",
        "execution_eligible",
        "market_title",
        "market_slug",
        "source_category",
        "category",
        "event_title",
    ):
        if market_snapshot.get(field) not in (None, ""):
            merged[field] = market_snapshot[field]
    return _decorate_candidate(merged)


def _candidate_from_checkpoint(
    candidate: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    prediction: Mapping[str, Any],
) -> dict[str, Any]:
    refreshed = dict(candidate)
    bids = checkpoint.get("bids") if isinstance(checkpoint.get("bids"), list) else []
    asks = checkpoint.get("asks") if isinstance(checkpoint.get("asks"), list) else []
    refreshed["bids"] = bids
    refreshed["asks"] = asks
    if bids and isinstance(bids[0], (list, tuple)) and len(bids[0]) >= 2:
        refreshed["best_bid"] = bids[0][0]
    if asks and isinstance(asks[0], (list, tuple)) and len(asks[0]) >= 2:
        refreshed["best_ask"] = asks[0][0]
    fills = prediction.get("fills") if isinstance(prediction.get("fills"), list) else []
    fill_prices = [
        str(row.get("price"))
        for row in fills
        if isinstance(row, Mapping) and row.get("price") not in (None, "")
    ]
    if fill_prices:
        refreshed["execution_worst_price"] = (
            str(max(Decimal(price) for price in fill_prices))
            if str(prediction.get("side") or "").upper() == "BUY"
            else str(min(Decimal(price) for price in fill_prices))
        )
    elif prediction.get("avg_fill_price") not in (None, ""):
        refreshed["execution_worst_price"] = str(prediction["avg_fill_price"])
    try:
        refreshed["book_age_ms"] = int(prediction.get("book_age_ms") or 0)
    except (TypeError, ValueError):
        refreshed["book_age_ms"] = 0
    refreshed.update(
        {
            "coverage_grade": checkpoint.get("coverage_grade") or refreshed.get("coverage_grade"),
            "market_state": checkpoint.get("market_state") or refreshed.get("market_state"),
            "has_gap": bool(checkpoint.get("has_gap")),
            "shadow_checkpoint_id": checkpoint.get("checkpoint_id"),
            "shadow_generation": checkpoint.get("generation"),
            "shadow_observed_at": _iso_utc(checkpoint.get("observed_at")),
            "shadow_book_fingerprint": checkpoint.get("book_fingerprint"),
            "shadow_source_connection_id": checkpoint.get("source_connection_id"),
            "shadow_source_message_seq": checkpoint.get("source_message_seq"),
        }
    )
    return refreshed


def _parse_utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso_utc(value: Any) -> str | None:
    parsed = _parse_utc(value)
    return parsed.isoformat() if parsed is not None else None


def _is_retryable_adapter_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    for _ in range(5):
        if current is None:
            return False
        if isinstance(current, (TimeoutError, ConnectionError, OSError)):
            return True
        text = f"{current.__class__.__name__}:{current}".lower()
        if any(
            marker in text
            for marker in (
                "timeout",
                "connecterror",
                "connection reset",
                "network is unreachable",
                "remoteprotocol",
                "proxyerror",
                "ssl eof",
                "server disconnected",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _state_events(probe_id: str, states: Sequence[str], started: datetime) -> list[dict[str, Any]]:
    rows = []
    for index, state in enumerate(states):
        event_ts = datetime.now(timezone.utc)
        identity = {"probe_id": probe_id, "state": state, "index": index, "started": started}
        rows.append(
            {
                "event_key": payload_hash(identity, prefix="calstate-"),
                "probe_id": probe_id,
                "event_type": "PROBE_STATE",
                "source": "taker-calibration-runner",
                "event_ts": event_ts,
                "payload": {"state": state, "sequence": index},
            }
        )
    return rows


def _number(value: Any, *, default: float) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(str(default))


def _allowance(payload: Mapping[str, Any]) -> Decimal:
    direct = payload.get("allowance")
    if direct not in (None, ""):
        return _number(direct, default=-1)
    values = payload.get("allowances")
    if isinstance(values, Mapping):
        parsed = [_number(value, default=-1) for value in values.values()]
        valid = [value for value in parsed if value >= 0]
        return min(valid) if valid else Decimal("-1")
    return Decimal("-1")


def _rest_book_matches(
    candidate: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    *,
    side: str | None = None,
    tolerance: Decimal = Decimal("0.0001"),
) -> bool:
    local_bid = _number(candidate.get("best_bid"), default=-1)
    local_ask = _number(candidate.get("best_ask"), default=-1)
    rest_bid = _number(market_snapshot.get("rest_best_bid"), default=-1)
    rest_ask = _number(market_snapshot.get("rest_best_ask"), default=-1)
    normalized_side = str(side or "").upper()
    if normalized_side == "BUY":
        return min(local_ask, rest_ask) >= 0 and abs(local_ask - rest_ask) <= tolerance
    if normalized_side == "SELL":
        return min(local_bid, rest_bid) >= 0 and abs(local_bid - rest_bid) <= tolerance
    return (
        min(local_bid, local_ask, rest_bid, rest_ask) >= 0
        and abs(local_bid - rest_bid) <= tolerance
        and abs(local_ask - rest_ask) <= tolerance
    )


def _rest_validated_candidate(
    candidate: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    *,
    side: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    validated = dict(candidate)
    matched = _rest_book_matches(candidate, market_snapshot, side=side)
    validated["rest_book_match"] = matched
    if not matched:
        return validated
    observed_at = _parse_utc(market_snapshot.get("rest_book_observed_at"))
    observed_now = now or datetime.now(timezone.utc)
    validation_age_ms = (
        max(0, int((observed_now - observed_at).total_seconds() * 1000))
        if observed_at is not None
        else 999999999
    )
    validated.update(
        {
            "checkpoint_book_age_ms": candidate.get("book_age_ms"),
            "book_age_ms": validation_age_ms,
            "book_validation_mode": "REST_BBO_MATCH",
            "rest_book_observed_at": market_snapshot.get("rest_book_observed_at"),
            "rest_book_hash": market_snapshot.get("rest_book_hash"),
        }
    )
    return validated


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))
