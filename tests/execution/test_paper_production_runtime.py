from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path

import pytest

from quant.paper import production_runtime


def test_manifest_covers_soak_runtime_and_gcp_units() -> None:
    required = {
        "quant/paper/canary.py",
        "quant/paper/cash_reconciliation.py",
        "quant/paper/paired_probe.py",
        "quant/calibration/clean_v2_cohort.py",
        "quant/calibration/order_rest_reconciler.py",
        "quant/calibration/signed_order_prediction.py",
        "quant/maker/own_order_truth.py",
        "quant/backtest/order_state.py",
        "quant/backtest/shadow_live_validation.py",
        "quant/simulator/oms/oms_store.py",
        "quant/simulator/oms/paper_adapter.py",
        "scripts/run_paper_live_shadow_acceptance_soak.sh",
        "scripts/run_gcp_paper_final_soak.sh",
        "scripts/start_gcp_paper_soak_24h_after_pass.sh",
        "scripts/start_gcp_paper_soak_7d_after_pass.sh",
        "scripts/run_paper_operations_snapshot.sh",
        "scripts/run_paper_db_control_tunnel.sh",
        "scripts/run_paper_paired_probe.py",
        "scripts/run_paper_paired_reconciler.sh",
        "deploy/systemd/poly-quant-paper-db-control-tunnel.service",
        "deploy/systemd/poly-quant-paper-paired-reconciler.service",
        "deploy/systemd/poly-quant-paper-paired-reconciler.timer",
        "deploy/systemd/poly-quant-gcp-paper-soak-6h.service",
        "deploy/systemd/poly-quant-gcp-paper-soak-24h.service",
        "deploy/systemd/poly-quant-gcp-paper-soak-7d.service",
        "deploy/systemd/poly-quant-gcp-paper-operations-snapshot.service",
        "deploy/systemd/poly-quant-gcp-paper-operations-snapshot.timer",
    }

    assert required <= set(production_runtime.MANIFEST_FILES)


def test_production_runtime_import_closure_is_loadable() -> None:
    for module_name in production_runtime.PRODUCTION_IMPORT_MODULES:
        assert importlib.import_module(module_name) is not None


def test_preflight_requires_persistent_kernel_queue_counters() -> None:
    required = {
        "accepted_event_count",
        "failed_event_count",
        "queue_counts_initialized",
    }

    assert required <= production_runtime.REQUIRED_COLUMNS[
        "paper_global_event_kernel_state"
    ]


def test_final_gcp_soak_chain_uses_one_isolated_v3_evidence_window() -> None:
    unit_root = Path("deploy/systemd")
    soak_24h = (unit_root / "poly-quant-gcp-paper-soak-24h.service").read_text()
    soak_7d = (unit_root / "poly-quant-gcp-paper-soak-7d.service").read_text()
    promotion = Path("scripts/start_gcp_paper_soak_7d_after_pass.sh").read_text()

    assert "gcp_soak_24h_accuracy_final_v3" in soak_24h
    assert "gcp_soak_7d_accuracy_final_v3" in soak_7d
    assert "gcp_soak_24h_accuracy_final_v3/latest.json" in promotion
    assert "accuracy_final_v1" not in soak_24h + soak_7d + promotion
    assert "accuracy_final_v2" not in soak_24h + soak_7d + promotion
    assert "run_gcp_paper_final_soak.sh" in soak_24h


def test_stopped_worker_preflight_can_skip_only_owned_event_socket() -> None:
    args = production_runtime._parser().parse_args(
        ["preflight", "--skip-event-socket"]
    )

    assert args.skip_event_socket is True
    assert args.skip_db is False


def _write_status(path: Path, **overrides: object) -> None:
    payload: dict[str, object] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "transport_state": "REDUNDANT",
        "worker_id": "worker-1",
        "ready_books": 10,
        "watched_assets": 10,
        "queued_intents": 0,
        "processing_intents": 0,
        "backpressure_status": "ACCEPT",
        "last_error": None,
        "watch_refresh_consecutive_failures": 0,
        "authority_mode": "FENCED",
        "authority_state": "HELD",
        "authority_partition_key": "paper-global",
        "authority_owner_instance_id": "worker-1",
        "authority_lease_epoch": 1,
        "authority_lease_until": (
            datetime.now(timezone.utc) + timedelta(seconds=30)
        ).isoformat(),
        "authority_heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "authority_fencing_enforced": True,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_manifest_detects_changed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "worker.py"
    artifact.write_text("one\n", encoding="utf-8")
    monkeypatch.setattr(production_runtime, "MANIFEST_FILES", ("worker.py",))

    manifest = production_runtime.build_manifest(
        tmp_path,
        source_git_sha="abc123",
        source_dirty=False,
    )
    assert production_runtime.verify_manifest(tmp_path, manifest) == []

    artifact.write_text("two\n", encoding="utf-8")
    assert production_runtime.verify_manifest(tmp_path, manifest) == [
        "hash mismatch: worker.py"
    ]


def test_ready_health_rejects_worker_error(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    _write_status(status_path, last_error="database timeout")

    code, payload = production_runtime.evaluate_ready(
        status_path,
        max_age_seconds=30,
    )

    assert code == HTTPStatus.SERVICE_UNAVAILABLE
    assert payload["reasons"] == ["worker_last_error_present"]


def test_authority_health_requires_live_enforced_epoch(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    _write_status(
        status_path,
        authority_mode="FENCED",
        authority_state="HELD",
        authority_partition_key="paper-global",
        authority_owner_instance_id="worker-1",
        authority_lease_epoch=7,
        authority_lease_until=(
            datetime.now(timezone.utc) + timedelta(seconds=15)
        ).isoformat(),
        authority_heartbeat_at=datetime.now(timezone.utc).isoformat(),
        authority_fencing_enforced=True,
    )

    code, payload = production_runtime.evaluate_authority(
        status_path,
        max_age_seconds=30,
    )

    assert code == HTTPStatus.OK
    assert payload["status"] == "PASS"
    assert payload["lease_epoch"] == 7


def test_authority_health_rejects_expired_unenforced_lease(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    _write_status(
        status_path,
        authority_mode="FENCED",
        authority_state="HELD",
        authority_partition_key="paper-global",
        authority_owner_instance_id="worker-1",
        authority_lease_epoch=2,
        authority_lease_until=(
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(),
        authority_fencing_enforced=False,
    )

    code, payload = production_runtime.evaluate_authority(
        status_path,
        max_age_seconds=30,
    )

    assert code == HTTPStatus.SERVICE_UNAVAILABLE
    assert payload["reasons"] == [
        "database_fencing_not_enforced",
        "authority_lease_expired",
    ]


def test_operations_status_endpoint_distinguishes_status_from_slo(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "operations.json"
    status_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "FAIL",
                "operational_level": "ORANGE",
            }
        ),
        encoding="utf-8",
    )

    status_code, status = production_runtime.evaluate_operations_status(
        status_path,
        max_age_seconds=120,
        require_slo_pass=False,
    )
    slo_code, slo = production_runtime.evaluate_operations_status(
        status_path,
        max_age_seconds=120,
        require_slo_pass=True,
    )

    assert status_code == HTTPStatus.OK
    assert status["endpoint_status"] == "PASS"
    assert slo_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert slo["endpoint_reasons"] == ["slo_not_passing"]


def test_state_verification_allows_new_rows_but_not_lost_rows() -> None:
    before = {
        "counts": {"intent_ids": 2},
        "collections": {"intent_ids": ["a", "b"]},
    }
    after = {
        "counts": {"intent_ids": 3},
        "collections": {"intent_ids": ["a", "b", "c"]},
    }
    assert production_runtime.verify_state(before, after)["status"] == "PASS"

    missing = production_runtime.verify_state(
        before,
        {"counts": {"intent_ids": 1}, "collections": {"intent_ids": ["a"]}},
    )
    assert missing["status"] == "FAIL"
    assert missing["missing"] == {"intent_ids": ["b"]}


def test_canary_reports_expected_authority_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch(url: str, _timeout: float) -> tuple[int, dict[str, object]]:
        if url.endswith("/health/authority"):
            return HTTPStatus.SERVICE_UNAVAILABLE, {"status": "FAIL"}
        if url.endswith("/version"):
            return HTTPStatus.OK, {"build_id": "build-1"}
        return HTTPStatus.OK, {"status": "PASS"}

    monkeypatch.setattr(production_runtime, "_fetch_json", fake_fetch)

    result = production_runtime.run_canary(
        "http://127.0.0.1:18700",
        timeout=1,
        expected_build_id="build-1",
        require_ready=False,
        require_authority=False,
    )

    assert result["status"] == "PASS_WITH_BLOCKERS"
    assert result["failures"] == []
    assert result["production_blockers"] == ["authority"]


def test_canary_can_require_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch(url: str, _timeout: float) -> tuple[int, dict[str, object]]:
        if url.endswith("/health/authority"):
            return HTTPStatus.SERVICE_UNAVAILABLE, {"status": "FAIL"}
        if url.endswith("/version"):
            return HTTPStatus.OK, {"build_id": "build-1"}
        return HTTPStatus.OK, {"status": "PASS"}

    monkeypatch.setattr(production_runtime, "_fetch_json", fake_fetch)
    result = production_runtime.run_canary(
        "http://127.0.0.1:18700",
        timeout=1,
        expected_build_id="build-1",
        require_ready=False,
        require_authority=True,
    )

    assert result["status"] == "FAIL"
    assert result["failures"] == ["authority"]
