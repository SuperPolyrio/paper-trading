"""Strict holdout gate for calibrated taker model promotion."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping


def build_calibration_report(fit: Mapping[str, Any]) -> dict[str, Any]:
    holdout = fit.get("holdout_metrics") if isinstance(fit.get("holdout_metrics"), Mapping) else {}
    sample_count = int(fit.get("sample_count") or 0)
    total_probe_count = int(fit.get("total_probe_count") or 0)
    state_counts = fit.get("probe_state_counts") if isinstance(fit.get("probe_state_counts"), Mapping) else {}
    checks = {
        "sample_count_100_to_300": 100 <= sample_count <= 300,
        "all_probes_calibratable": total_probe_count == sample_count,
        "all_calibratable_artifacts_complete": int(fit.get("complete_artifact_count") or 0) == sample_count,
        "all_calibratable_pnl_reconciled": int(fit.get("pnl_reconciled_count") or 0) == sample_count,
        "pnl_total_error_max_le_rounding_unit": _optional_le(
            fit.get("pnl_total_error_max"), Decimal("0.00001")
        ),
        "unknown_outcome_zero": int(state_counts.get("SUBMIT_OUTCOME_UNKNOWN") or 0) == 0,
        "accounting_mismatch_zero": int(state_counts.get("ACCOUNTING_MISMATCH") or 0) == 0,
        "self_trade_contamination_zero": int(state_counts.get("SELF_TRADE_CONTAMINATED") or 0) == 0,
        "holdout_present": int(fit.get("holdout_count") or 0) > 0,
        "fok_false_positive_full_fill_zero": int(holdout.get("fok_false_positive_full_fill_count") or 0) == 0,
        "holdout_fill_class_accuracy_ge_97": _decimal(holdout.get("fill_class_accuracy_pct")) >= Decimal("97"),
        "vwap_error_median_le_0_25_tick": _optional_le(holdout.get("vwap_error_median_ticks"), Decimal("0.25")),
        "vwap_error_p95_le_1_tick": _optional_le(holdout.get("vwap_error_p95_ticks"), Decimal("1")),
        "filled_size_relative_error_p95_le_1pct": _optional_le(
            holdout.get("filled_size_relative_error_p95"), Decimal("0.01")
        ),
        "fee_error_max_le_rounding_unit": _optional_le(
            holdout.get("fee_error_max"), Decimal("0.00001")
        ),
    }
    passed = all(checks.values())
    return {
        "schema_version": "taker_calibration_report_v1",
        "status": "PASS" if passed else "PENDING" if sample_count == 0 else "FAIL",
        "promotion_allowed": passed,
        "checks": checks,
        "fit": dict(fit),
        "pnl_grade": "TAKER_CALIBRATED_IN_DOMAIN" if passed else "SHADOW_UNCALIBRATED",
    }


def assert_promotion_report(report: Mapping[str, Any]) -> None:
    checks = report.get("checks") if isinstance(report.get("checks"), Mapping) else {}
    fit = report.get("fit") if isinstance(report.get("fit"), Mapping) else {}
    if str(report.get("status")) != "PASS" or not bool(report.get("promotion_allowed")):
        raise RuntimeError("calibration report does not satisfy promotion gates")
    if not checks or not all(value is True for value in checks.values()):
        raise RuntimeError("calibration report contains a failed or missing gate")
    if int(fit.get("sample_count") or 0) < 100 or int(fit.get("holdout_count") or 0) <= 0:
        raise RuntimeError("calibration report has insufficient live holdout evidence")


def _optional_le(value: Any, maximum: Decimal) -> bool:
    return value not in (None, "") and _decimal(value) <= maximum


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("-Infinity")
