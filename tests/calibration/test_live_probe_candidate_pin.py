from dataclasses import replace
from types import SimpleNamespace

import pytest

from quant.calibration.live_probe_runner import (
    LiveProbeRunner,
    _prepared_candidate_execution_issues,
)
from quant.calibration.probe_plan import load_probe_plan
from quant.calibration.probe_scheduler import _with_market_parameters


class _CalibrationStore:
    def ensure_schema(self) -> None:
        pass

    def freeze_model(self, _manifest) -> None:
        pass

    def create_run(self, payload) -> None:
        self.created = payload


class _ShadowStore:
    def __init__(self) -> None:
        self.pin = None

    def ensure_schema(self) -> None:
        pass

    def ensure_calibration_watch_batch(
        self,
        asset_ids,
        *,
        strategy_id: str,
        reason: str,
    ) -> int:
        self.pin = (list(asset_ids), strategy_id, reason)
        return 1


class _CleanCohortStore:
    def __init__(self) -> None:
        self.target = None
        self.operation = None

    def ensure_schema(self) -> None:
        pass

    def assert_target_allowed(self, **kwargs):
        self.target = kwargs
        return {"status": "READY"}

    def register_operation(self, **kwargs):
        self.operation = kwargs
        return {"status": "PREPARED"}


def test_prepare_pins_candidate_until_live_execution(monkeypatch) -> None:
    plan = load_probe_plan("config/calibration/taker_account_truth_micro_live.yaml")
    plan = replace(
        plan,
        market_policy=replace(plan.market_policy, allow_market_ids=("7891729",)),
    )
    calibration = _CalibrationStore()
    shadow = _ShadowStore()
    runner = LiveProbeRunner(
        plan,
        approved_asset_id="asset-1",
        calibration_store=calibration,
        shadow_store=shadow,
        adapter=object(),
        user_ws=object(),
    )
    monkeypatch.setattr(
        "quant.calibration.live_probe_runner.validate_probe_plan",
        lambda *_args, **_kwargs: [],
    )
    runner._wait_for_shadow_fresh_candidates = lambda **_kwargs: [
        {
            "asset_id": "asset-1",
            "market_id": "7891729",
            "condition_id": "condition-1",
            "outcome_name": "YES",
            "best_bid": "0.09",
            "best_ask": "0.16",
            "tick_size": "0.001",
            "min_order_size": "1",
            "coverage_grade": "A",
            "shadow_observed_at": "2026-09-01T00:00:00+00:00",
        }
    ]
    runner._phase5b_gate = lambda: {
        "status": "PASS",
        "complete_probe_count": 50,
        "report_path": "phase5b.json",
        "run_id": "phase5b-run",
        "manifest": {
            "manifest_id": "manifest-1",
            "paper_execution_model_version": "model-1",
        },
    }

    result = runner.prepare()

    assert result["status"] == "READY_TO_EXECUTE"
    assert shadow.pin is not None
    assert shadow.pin[0] == ["asset-1"]
    assert shadow.pin[1] == "taker-calibration-live"
    assert shadow.pin[2] == "live_probe_candidate"


def test_prepare_binds_clean_cohort_to_shared_paper_strategy(monkeypatch) -> None:
    plan = load_probe_plan("config/calibration/taker_account_truth_micro_live.yaml")
    plan = replace(
        plan,
        market_policy=replace(plan.market_policy, allow_market_ids=("7891729",)),
    )
    calibration = _CalibrationStore()
    calibration.finish_run = lambda *_args, **_kwargs: None
    shadow = _ShadowStore()
    cohort = _CleanCohortStore()
    runner = LiveProbeRunner(
        plan,
        approved_asset_id="asset-1",
        clean_cohort_id="cohort-1",
        calibration_store=calibration,
        shadow_store=shadow,
        clean_cohort_store=cohort,
        adapter=object(),
        user_ws=object(),
    )
    monkeypatch.setattr(
        "quant.calibration.live_probe_runner.validate_probe_plan",
        lambda *_args, **_kwargs: [],
    )
    runner._wait_for_shadow_fresh_candidates = lambda **_kwargs: [
        {
            "asset_id": "asset-1",
            "market_id": "7891729",
            "condition_id": "condition-1",
            "outcome_name": "YES",
            "best_bid": "0.09",
            "best_ask": "0.16",
            "tick_size": "0.001",
            "min_order_size": "1",
            "coverage_grade": "A",
            "shadow_observed_at": "2026-09-01T00:00:00+00:00",
        }
    ]
    runner._phase5b_gate = lambda: {
        "status": "PASS",
        "complete_probe_count": 50,
        "report_path": "phase5b.json",
        "run_id": "phase5b-run",
        "manifest": {
            "manifest_id": "manifest-1",
            "paper_execution_model_version": "model-1",
        },
    }

    result = runner.prepare()

    assert result["status"] == "READY_TO_EXECUTE"
    assert result["paper_strategy_id"] == "post-v2-clean-cohort-1"
    assert shadow.pin[1] == "post-v2-clean-cohort-1"
    assert cohort.target["asset_id"] == "asset-1"
    assert cohort.operation["run_id"] == result["run_id"]


def test_submit_fact_is_not_persisted_when_adapter_never_returns() -> None:
    class Adapter:
        def submit_prepared_once(self, *_args, **_kwargs):
            raise RuntimeError("failed before HTTP outcome")

    writes: list[dict] = []
    commits: list[dict] = []
    runner = LiveProbeRunner.__new__(LiveProbeRunner)
    runner.adapter = Adapter()
    runner.calibration_store = SimpleNamespace(
        upsert_probe=lambda row: writes.append(dict(row)) or dict(row)
    )
    runner._commit_clean_cohort_prediction_if_submitted = (
        lambda _run_id, row: commits.append(dict(row)) or dict(row)
    )
    probe = {"exchange_submit_called": False, "signed_order_audit": {}}

    with pytest.raises(RuntimeError, match="before HTTP outcome"):
        runner._submit_prepared_with_durable_audit(
            run_id="run-1",
            prepared=object(),
            risk_passed=True,
            probe=probe,
        )

    assert probe["exchange_submit_called"] is False
    assert writes == []
    assert commits == []


def test_submit_fact_and_clean_cohort_commit_follow_adapter_result() -> None:
    audit = SimpleNamespace(
        exchange_submit_called=True,
        as_dict=lambda: {"exchange_submit_called": True, "order_hash": "hash-1"},
    )
    writes: list[dict] = []
    commits: list[dict] = []
    runner = LiveProbeRunner.__new__(LiveProbeRunner)
    runner.adapter = SimpleNamespace(
        submit_prepared_once=lambda *_args, **_kwargs: (
            audit,
            {"orderID": "order-1"},
        )
    )
    runner.calibration_store = SimpleNamespace(
        upsert_probe=lambda row: writes.append(dict(row)) or dict(row)
    )

    def commit(_run_id: str, row: dict) -> dict:
        commits.append(dict(row))
        return {**row, "cohort_committed": True}

    runner._commit_clean_cohort_prediction_if_submitted = commit
    probe = {"exchange_submit_called": False, "signed_order_audit": {}}

    submission = runner._submit_prepared_with_durable_audit(
        run_id="run-1",
        prepared=object(),
        risk_passed=True,
        probe=probe,
    )

    assert submission[1]["orderID"] == "order-1"
    assert probe["exchange_submit_called"] is True
    assert probe["cohort_committed"] is True
    assert len(writes) == 1
    assert len(commits) == 1


def test_prepare_candidate_uses_official_category_and_rejects_sports() -> None:
    plan = load_probe_plan("config/calibration/taker_clean_v2_cohort_live.yaml")
    candidate = _with_market_parameters(
        {
            "asset_id": "asset-1",
            "best_bid": "0.40",
            "best_ask": "0.50",
            "min_order_size": "1",
            "market_state": "LIVE",
            "execution_eligible": True,
        },
        {
            "category": "sports",
            "source_category": "sports",
            "event_title": "Team A vs Team B",
            "market_title": "Will Team A win?",
            "tick_size": "0.01",
        },
    )

    issues = _prepared_candidate_execution_issues(plan, candidate, side="BUY")

    assert candidate["category"] == "sports"
    assert candidate["event_title"] == "Team A vs Team B"
    assert "phase5c_sports_market_denied" in issues


def test_prepare_candidate_rejects_projected_position_before_run_creation() -> None:
    plan = load_probe_plan("config/calibration/taker_clean_v2_cohort_live.yaml")
    candidate = {
        "best_bid": "0.002",
        "best_ask": "0.028",
        "min_order_size": "5",
        "market_state": "LIVE",
        "execution_eligible": True,
    }

    issues = _prepared_candidate_execution_issues(
        plan,
        candidate,
        side="BUY",
        market_position=0,
    )

    assert "phase5c_market_position_limit_reached" in issues
