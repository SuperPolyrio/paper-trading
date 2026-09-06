"""Separate Maker calibration implementation acceptance from live promotion."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


def evaluate_calibration_acquisition(
    *,
    authenticated_evaluation: Mapping[str, Any],
    historical_recovery: Mapping[str, Any],
    collector_status: Mapping[str, Any],
    live_preflight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate evidence acquisition without promoting missing live outcomes."""

    evaluated = int(authenticated_evaluation.get("evaluated_count") or 0)
    evidence = _mapping(authenticated_evaluation.get("evidence"))
    outcome_counts = {
        name: int(_mapping(authenticated_evaluation.get("outcome_counts")).get(name) or 0)
        for name in ("NO_FILL", "PARTIAL", "FULL")
    }
    implementation_checks = {
        "historical_recovery_read_only": (
            historical_recovery.get("status") == "PASS_READ_ONLY"
            and historical_recovery.get("exchange_submit_called") is False
            and historical_recovery.get("exchange_cancel_called") is False
        ),
        "collector_running_without_order_mutation": (
            collector_status.get("status") in {"PASS", "DEGRADED"}
            and collector_status.get("exchange_submit_called") is False
            and collector_status.get("exact_cancel_called") is False
            and collector_status.get("resubmit_forbidden") is True
        ),
        "authenticated_evidence_validated": (
            evaluated > 0
            and int(evidence.get("authenticated_own_order_count") or 0) == evaluated
        ),
        "prediction_frozen_before_submission": (
            evaluated > 0
            and int(evidence.get("prediction_before_submission_count") or 0)
            == evaluated
        ),
        "right_censoring_metadata_complete": (
            evaluated > 0
            and int(evidence.get("censoring_metadata_count") or 0) == evaluated
            and not any(
                "PERMANENT" in str(label).upper()
                for label in _mapping(evidence.get("observation_label_counts"))
            )
        ),
    }
    if live_preflight is not None:
        implementation_checks["live_preflight_fail_closed_without_submit"] = (
            live_preflight.get("exchange_submit_called") is False
            and live_preflight.get("status")
            in {
                "NO_SUBMIT_READY",
                "BLOCKED",
                "NO_CANDIDATES",
                "WARMUP_REQUIRED",
            }
        )
    implementation_passed = all(implementation_checks.values())
    live_checks = {
        "authenticated_partial_observed": outcome_counts["PARTIAL"] > 0,
        "authenticated_full_observed": outcome_counts["FULL"] > 0,
        "holdout_promotion_gate_passed": (
            authenticated_evaluation.get("status") == "PASS"
            and authenticated_evaluation.get("promotion_allowed") is True
        ),
    }
    live_passed = all(live_checks.values())
    if implementation_passed and live_passed:
        status = "PASS_LIVE_MAKER_CALIBRATED"
    elif implementation_passed:
        status = "PASS_IMPLEMENTATION_LIVE_EVIDENCE_PENDING"
    else:
        status = "BLOCKED_IMPLEMENTATION"
    return {
        "schema_version": "maker_calibration_acquisition_acceptance_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "implementation_status": "PASS" if implementation_passed else "BLOCKED",
        "live_evidence_status": "PASS" if live_passed else "BLOCKED",
        "implementation_checks": implementation_checks,
        "live_evidence_checks": live_checks,
        "authenticated_outcome_counts": outcome_counts,
        "claims": {
            "controlled_full_partial_probe_implemented": implementation_passed,
            "historical_wallet_recovery_implemented": implementation_passed,
            "persistent_user_ws_rest_chain_recovery_implemented": implementation_passed,
            "permanent_no_fill_claimed": False,
            "exact_fifo_claimed": False,
            "live_maker_calibrated": live_passed,
        },
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
