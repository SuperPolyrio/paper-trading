"""Promotion boundary for immutable calibrated execution models."""

from __future__ import annotations

from typing import Any, Mapping

from .calibration_report import assert_promotion_report
from .store import CalibrationStore


class ExecutionModelRegistry:
    def __init__(self, store: CalibrationStore | None = None) -> None:
        self.store = store or CalibrationStore()

    def promote(
        self,
        *,
        run_id: str,
        model_version: str,
        report: Mapping[str, Any],
    ) -> dict[str, Any]:
        assert_promotion_report(report)
        return self.store.promote_model(
            run_id=run_id,
            model_version=model_version,
            report=report,
        )
