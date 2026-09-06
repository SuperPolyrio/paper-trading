from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from quant.adapters.polymarket_paper_clob_client import PolymarketPaperClobClient
from quant.paper import live_shadow_service
from quant.paper.security import (
    HashChainAuditLog,
    PaperSecurityViolation,
    audit_paper_dependency_boundary,
    build_credential_manifest,
    enforce_paper_security_boundary,
    inspect_environment_file,
    scan_paths_for_secret_material,
    verify_credential_rotation,
    verify_hash_chain,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_paper_dependency_boundary_has_no_live_submit_path() -> None:
    assert audit_paper_dependency_boundary(PROJECT_ROOT) == []


def test_paper_security_rejects_live_key_without_leaking_value(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    sentinel = "private-value-that-must-not-leak"
    with pytest.raises(PaperSecurityViolation) as exc_info:
        enforce_paper_security_boundary(
            environ={"POLY_QUANT_PROBE_PRIVATE_KEY": sentinel},
            audit_log=audit_path,
            source_root=PROJECT_ROOT,
        )
    assert sentinel not in str(exc_info.value)
    assert sentinel not in audit_path.read_text(encoding="utf-8")


def test_worker_cli_rejects_live_key_before_database_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "worker-cli-secret-must-not-leak"
    database_touched = False

    def forbidden_database_factory():
        nonlocal database_touched
        database_touched = True
        raise AssertionError("database must not be initialized")

    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", sentinel)
    monkeypatch.setattr(
        live_shadow_service,
        "postgres_connection",
        forbidden_database_factory,
    )
    audit_path = tmp_path / "worker-audit.jsonl"
    with pytest.raises(PaperSecurityViolation) as exc_info:
        live_shadow_service.main(
            [
                "run",
                "--paper-security-enforce",
                "--paper-security-audit-log",
                str(audit_path),
            ]
        )
    assert database_touched is False
    assert sentinel not in str(exc_info.value)
    assert sentinel not in audit_path.read_text(encoding="utf-8")


def test_environment_file_reports_names_only(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.env"
    path.write_text(
        "POLYMARKET_PRIVATE_KEY=do-not-report\nPOLYDATA_POSTGRES_PASSWORD=also-private\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    report = inspect_environment_file(path)
    assert report["forbidden_live_keys"] == ["POLYMARKET_PRIVATE_KEY"]
    assert report["plaintext_secret_keys"] == ["POLYDATA_POSTGRES_PASSWORD"]
    assert "do-not-report" not in str(report)
    assert "also-private" not in str(report)


def test_secret_scan_reports_location_without_secret_value(tmp_path: Path) -> None:
    path = tmp_path / "bad.py"
    path.write_text(
        "credential = '-----BEGIN PRIVATE KEY-----'\n",
        encoding="utf-8",
    )
    issues = scan_paths_for_secret_material([path])
    assert issues == [{"path": str(path), "line": 1, "rule": "private_key_pem"}]
    assert "BEGIN PRIVATE KEY" not in str(issues)


def test_paper_clob_client_exposes_no_order_methods() -> None:
    forbidden = {"create_order", "post_order", "submit_order", "cancel_order"}
    assert all(not hasattr(PolymarketPaperClobClient, method) for method in forbidden)
    assert PolymarketPaperClobClient.capabilities == {
        "GET_BOOK",
        "GET_BOOKS",
        "GET_MARKET",
        "GET_CLOB_MARKET_INFO",
        "GET_FEE_RATE",
    }


def test_credential_rotation_and_hash_chain(tmp_path: Path) -> None:
    credential = tmp_path / "paper-db-password"
    credential.write_text("v1\n", encoding="utf-8")
    os.chmod(credential, 0o600)
    before = build_credential_manifest({"paper-db-password": credential})
    credential.write_text("v2\n", encoding="utf-8")
    os.chmod(credential, 0o600)
    after = build_credential_manifest({"paper-db-password": credential})
    rotation = verify_credential_rotation(
        before,
        after,
        required_names=["paper-db-password"],
    )
    assert rotation["status"] == "PASS"

    audit_path = tmp_path / "audit.jsonl"
    log = HashChainAuditLog(audit_path)
    log.append(event_type="START", status="PASS")
    log.append(event_type="ROTATE", status="PASS")
    assert verify_hash_chain(audit_path)["status"] == "PASS"
    audit_path.write_text(
        audit_path.read_text(encoding="utf-8").replace("START", "TAMPERED", 1),
        encoding="utf-8",
    )
    assert verify_hash_chain(audit_path)["status"] == "FAIL"


def test_all_paper_units_exclude_generic_and_target_environment_files() -> None:
    unit_root = PROJECT_ROOT / "deploy" / "systemd"
    paper_units = list(unit_root.glob("*paper*.service"))
    paper_units.extend(unit_root.glob("*professional-simulator*.service"))
    for path in paper_units:
        text = path.read_text(encoding="utf-8")
        assert ".config/polydata/polydata.env" not in text, path.name
        assert "paper-db-target.env" not in text, path.name

    for name in (
        "poly-quant-gcp-paper-live-shadow.service",
        "poly-quant-gcp-paper-health.service",
    ):
        text = (unit_root / name).read_text(encoding="utf-8")
        assert "paper-runtime.env" in text
        assert "LoadCredential=paper-db-password:" in text

    for name in (
        "poly-quant-gcp-paper-live-shadow.service",
        "poly-quant-gcp-paper-health.service",
        "poly-quant-gcp-paper-soak-6h.service",
        "poly-quant-gcp-paper-soak-24h.service",
        "poly-quant-gcp-paper-soak-7d.service",
    ):
        text = (unit_root / name).read_text(encoding="utf-8")
        assert "paper-db-active.env" in text

    for name in (
        "poly-quant-gcp-paper-soak-6h.service",
        "poly-quant-gcp-paper-soak-24h.service",
        "poly-quant-gcp-paper-soak-7d.service",
    ):
        text = (unit_root / name).read_text(encoding="utf-8")
        assert "WorkingDirectory=/opt/paper-trading" in text
        assert "Environment=PYTHONPATH=/opt/paper-trading" in text
        assert (
            "Environment=PREDICTION_MARKET_QUANT_PYTHON_BIN="
            "/opt/polyData/.venv/bin/python"
        ) in text
        expected_runner = (
            "run_gcp_paper_final_soak.sh"
            if name == "poly-quant-gcp-paper-soak-24h.service"
            else "run_paper_live_shadow_acceptance_soak.sh"
        )
        assert (
            "ExecStart=/bin/bash /opt/paper-trading/scripts/"
            + expected_runner
        ) in text


def test_gcp_identity_dry_run_never_grants_paper_access_to_live_secrets() -> None:
    script = PROJECT_ROOT / "deploy" / "gcp" / "provision_paper_live_isolation.sh"
    result = subprocess.run(
        [str(script), "--project", "acceptance-project", "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )
    output = result.stdout
    paper_member = "serviceAccount:poly-quant-paper-worker@acceptance-project.iam.gserviceaccount.com"
    live_lines = [line for line in output.splitlines() if "poly-quant-live-" in line]
    assert live_lines
    assert all(paper_member not in line for line in live_lines)
    assert "poly-quant-paper-db-password" in output


def test_gcp_installer_honors_active_db_route_and_retries_canary() -> None:
    script = PROJECT_ROOT / "scripts" / "install_gcp_paper_worker.sh"
    text = script.read_text(encoding="utf-8")
    assert text.count("paper-db-active.env") >= 4
    assert text.count("poly-quant-gcp-paper-soak-6h.service") >= 2
    assert text.count("poly-quant-gcp-paper-soak-24h.service") >= 2
    assert text.count("poly-quant-gcp-paper-soak-7d.service") >= 2
    assert "env_file_args+=(--env-file" in text
    assert "canary_ok=false" in text
    assert "if [[ \"${canary_ok}\" != \"true\" ]]" in text
    stop_watchdog = text.index(
        "stop poly-quant-gcp-paper-live-shadow-watchdog.timer"
    )
    stop_worker = text.index(
        "stop poly-quant-gcp-paper-live-shadow.service", stop_watchdog
    )
    start_worker = text.index(
        "start poly-quant-gcp-paper-live-shadow.service", stop_worker
    )
    start_watchdog = text.index(
        "enable --now poly-quant-gcp-paper-live-shadow-watchdog.timer",
        start_worker,
    )
    assert stop_watchdog < stop_worker < start_worker < start_watchdog
    assert "continuing with durable startup recovery" in text
    assert "PAPER_WORKER_POST_DEPLOY_DRAIN_SECONDS" in text
    assert 'rm -f "${health_dir}/status.json"' in text
    assert text.count("poly-quant-gcp-paper-operations-snapshot.timer") >= 6
    assert "run_paper_operations_snapshot.sh" in text


def test_gcp_installer_rollback_requires_valid_backup_before_delete() -> None:
    script = PROJECT_ROOT / "scripts" / "install_gcp_paper_worker.sh"
    text = script.read_text(encoding="utf-8")

    guard = text.index('if [[ ! -s "${backup}" ]] || ! tar -tzf')
    skip = text.index("paper worker rollback skipped", guard)
    delete = text.index('rm -rf "${root:?}/${relative}"', skip)
    restore = text.index('tar -xzf "${backup}"', delete)

    assert guard < skip < delete < restore
    assert text.count('exit "${status}"') == 1


def test_gcp_installer_stopped_worker_preflight_skips_only_event_socket() -> None:
    script = PROJECT_ROOT / "scripts" / "install_gcp_paper_worker.sh"
    text = script.read_text(encoding="utf-8")

    first_preflight = text.index("preflight --skip-db --skip-event-socket")
    stop_worker = text.index(
        "stop poly-quant-gcp-paper-live-shadow.service", first_preflight
    )
    stopped_preflight = text.index("preflight --skip-event-socket", stop_worker)

    assert first_preflight < stop_worker < stopped_preflight
    assert "preflight --skip-db\n" not in text[stop_worker:]


def test_gcp_paper_launcher_enforces_operations_but_not_route_geoblock() -> None:
    script = PROJECT_ROOT / "scripts" / "run_gcp_paper_live_shadow.sh"
    text = script.read_text(encoding="utf-8")

    assert (
        "PAPER_OPERATIONS_ADMISSION_FLAG:---operations-admission-enforce"
        in text
    )
    assert "PAPER_UNIFIED_ADMISSION_FLAG:---unified-admission-shadow" in text
    assert "--operations-status-path" in text
    assert "--operations-status-max-age-seconds" in text
    assert "--operations-yellow-max-notional" in text
    assert "--build-manifest" in text


def test_operations_snapshot_publishes_status_and_metrics_atomically() -> None:
    script = PROJECT_ROOT / "scripts" / "run_paper_operations_snapshot.sh"
    text = script.read_text(encoding="utf-8")

    assert 'local temporary="${target_path}.tmp.$$"' in text
    assert 'mv -f "$temporary" "$target_path"' in text
    assert 'publish_artifact "${output_dir}/status.json"' in text
    assert 'publish_artifact "${output_dir}/metrics.prom"' in text

    unit = (
        PROJECT_ROOT
        / "deploy"
        / "systemd"
        / "poly-quant-gcp-paper-operations-snapshot.service"
    ).read_text(encoding="utf-8")
    assert "PAPER_OPERATIONS_STATUS_PATH=/mnt/l2-archive/.health/" in unit
    assert "PAPER_OPERATIONS_METRICS_PATH=/mnt/l2-archive/.health/" in unit


def test_gcp_paper_launcher_always_enforces_durable_authority() -> None:
    script = PROJECT_ROOT / "scripts" / "run_gcp_paper_live_shadow.sh"
    text = script.read_text(encoding="utf-8")

    assert "--authority-enforce" in text
    assert "PAPER_LIVE_AUTHORITY_LEASE_SECONDS:-60" in text
    assert "PAPER_LIVE_AUTHORITY_HEARTBEAT_SECONDS:-10" in text


def test_gcp_egress_dry_run_targets_only_paper_identity_and_denies_remainder() -> None:
    script = PROJECT_ROOT / "deploy" / "gcp" / "provision_paper_egress_policy.sh"
    result = subprocess.run(
        [
            str(script),
            "--project",
            "acceptance-project",
            "--network",
            "paper-network",
            "--db-cidr",
            "10.20.0.0/24",
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    output = result.stdout
    assert (
        "poly-quant-paper-worker@acceptance-project.iam.gserviceaccount.com" in output
    )
    assert "poly-quant-live-calibration" not in output
    assert "--action DENY --rules all --destination-ranges 0.0.0.0/0" in output


def test_live_calibration_units_use_separate_runtime_and_minimal_credentials() -> None:
    unit_root = PROJECT_ROOT / "deploy" / "systemd"
    units = {
        path.name: path.read_text(encoding="utf-8")
        for path in unit_root.glob("*calibration*.service")
    }
    assert units
    for name, text in units.items():
        assert ".config/polydata/polydata.env" not in text, name
        assert "calibration.env" not in text, name
        assert "live-calibration-runtime.env" in text, name
        assert "LoadCredential=live-db-password:" in text, name

    assert (
        "LoadCredential=live-private-key:"
        not in units["poly-quant-calibration-drift-monitor.service"]
    )
    assert (
        "LoadCredential=live-private-key:"
        not in units["poly-quant-calibration-portfolio-monitor.service"]
    )
    assert (
        "LoadCredential=live-private-key:"
        in units["poly-quant-calibration-settlement-watcher.service"]
    )
    assert (
        "LoadCredential=live-private-key:"
        in units["poly-quant-maker-calibration-collector.service"]
    )


def test_maker_collector_loads_full_identity_for_authenticated_recovery() -> None:
    script = (
        PROJECT_ROOT / "scripts" / "run_maker_calibration_collector_secure.sh"
    ).read_text(encoding="utf-8")

    assert "live_load_trading_credentials" in script
    assert "live_load_api_credentials\n" not in script


def test_live_calibration_secret_installer_is_allowlisted_and_owner_only(
    tmp_path: Path,
) -> None:
    script = PROJECT_ROOT / "scripts/install_live_calibration_secret_from_stdin.sh"
    target = tmp_path / "live-secrets/api-key"
    result = subprocess.run(
        [str(script), "live-api-key", str(target)],
        input="fixture-secret-value\n",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert target.read_text(encoding="utf-8") == "fixture-secret-value\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700

    rejected = subprocess.run(
        [str(script), "unknown-secret", str(tmp_path / "rejected")],
        input="must-not-install\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 64
    assert not (tmp_path / "rejected").exists()


def test_production_manifest_tracks_maker_calibration_runtime() -> None:
    from quant.paper.production_runtime import MANIFEST_FILES

    assert "scripts/run_maker_calibration_collector.py" in MANIFEST_FILES
    assert "scripts/run_maker_calibration_collector_secure.sh" in MANIFEST_FILES
    assert "deploy/systemd/poly-quant-maker-calibration-collector.service" in MANIFEST_FILES
