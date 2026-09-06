from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "manage_gcp_paper_db_cutover.sh"


def test_cutover_requires_explicit_approval(tmp_path: Path) -> None:
    result = subprocess.run(
        ("bash", str(SCRIPT), "cutover"),
        cwd=ROOT,
        env={
            **os.environ,
            "PAPER_DB_ARTIFACT_DIR": str(tmp_path / "artifacts"),
            "PAPER_DB_ACTIVE_ENV": str(tmp_path / "active.env"),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "PAPER_DB_CUTOVER_APPROVE=YES" in result.stderr


def test_rollback_restores_previous_route(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "systemctl-calls.txt"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {calls}\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    active = tmp_path / "active.env"
    rollback = tmp_path / "active.env.rollback"
    runtime_password = tmp_path / "paper-db-password"
    rollback_password = tmp_path / "paper-db-password.rollback"
    active.write_text("POLYDATA_POSTGRES_HOST=10.0.0.9\n", encoding="utf-8")
    rollback.write_text("POLYDATA_POSTGRES_HOST=127.0.0.1\n", encoding="utf-8")
    runtime_password.write_text("target-secret\n", encoding="utf-8")
    rollback_password.write_text("source-secret\n", encoding="utf-8")
    runtime_password.chmod(0o600)
    rollback_password.chmod(0o600)

    subprocess.run(
        ("bash", str(SCRIPT), "rollback"),
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PAPER_DB_ROLLBACK_APPROVE": "YES",
            "PAPER_DB_ARTIFACT_DIR": str(tmp_path / "artifacts"),
            "PAPER_DB_ACTIVE_ENV": str(active),
            "PAPER_DB_RUNTIME_PASSWORD_FILE": str(runtime_password),
        },
        check=True,
        capture_output=True,
        text=True,
    )

    assert active.read_text(encoding="utf-8") == "POLYDATA_POSTGRES_HOST=127.0.0.1\n"
    assert not rollback.exists()
    assert runtime_password.read_text(encoding="utf-8") == "source-secret\n"
    assert runtime_password.stat().st_mode & 0o777 == 0o600
    assert not rollback_password.exists()
    systemctl_calls = calls.read_text(encoding="utf-8")
    assert "stop poly-quant-gcp-paper-health.service" in systemctl_calls
    assert "start poly-quant-gcp-paper-live-shadow.service" in systemctl_calls


def test_rollback_fails_closed_without_previous_credential(tmp_path: Path) -> None:
    active = tmp_path / "active.env"
    rollback = tmp_path / "active.env.rollback"
    active.write_text("POLYDATA_POSTGRES_HOST=10.0.0.9\n", encoding="utf-8")
    rollback.write_text("POLYDATA_POSTGRES_HOST=127.0.0.1\n", encoding="utf-8")

    result = subprocess.run(
        ("bash", str(SCRIPT), "rollback"),
        cwd=ROOT,
        env={
            **os.environ,
            "PAPER_DB_ROLLBACK_APPROVE": "YES",
            "PAPER_DB_ARTIFACT_DIR": str(tmp_path / "artifacts"),
            "PAPER_DB_ACTIVE_ENV": str(active),
            "PAPER_DB_RUNTIME_PASSWORD_FILE": str(tmp_path / "paper-db-password"),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 78
    assert "rollback DB credential is missing" in result.stderr
    assert active.read_text(encoding="utf-8") == "POLYDATA_POSTGRES_HOST=10.0.0.9\n"
    assert rollback.exists()


def test_target_password_in_environment_file_is_rejected(tmp_path: Path) -> None:
    target_env = tmp_path / "target.env"
    target_env.write_text(
        "PAPER_TARGET_POSTGRES_HOST=10.0.0.8\n"
        "PAPER_TARGET_POSTGRES_PASSWORD=must-not-be-loaded\n",
        encoding="utf-8",
    )
    target_password = tmp_path / "target-password"
    target_password.write_text("credential-file-value\n", encoding="utf-8")
    target_password.chmod(0o600)

    result = subprocess.run(
        ("bash", str(SCRIPT), "prepare"),
        cwd=ROOT,
        env={
            **os.environ,
            "PAPER_DB_ARTIFACT_DIR": str(tmp_path / "artifacts"),
            "PAPER_DB_TARGET_ENV": str(target_env),
            "PAPER_DB_TARGET_PASSWORD_FILE": str(target_password),
            "PAPER_DB_ACTIVE_ENV": str(tmp_path / "active.env"),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 78
    assert "target DB password must use PAPER_DB_TARGET_PASSWORD_FILE" in result.stderr
    assert "must-not-be-loaded" not in result.stderr
    assert "credential-file-value" not in result.stderr


def test_cutover_switches_route_and_credentials_atomically(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl_calls = tmp_path / "systemctl-calls.txt"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {systemctl_calls}\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    sleep = fake_bin / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    python_calls = tmp_path / "python-calls.txt"
    fake_python = fake_bin / "python"
    fake_python.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {python_calls}\nexit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    source_env = tmp_path / "source.env"
    source_env.write_text("POLYDATA_POSTGRES_HOST=127.0.0.1\n", encoding="utf-8")
    target_env = tmp_path / "target.env"
    target_env.write_text(
        "PAPER_TARGET_POSTGRES_HOST=10.148.0.5\n"
        "PAPER_TARGET_POSTGRES_PORT=5432\n"
        "PAPER_TARGET_POSTGRES_USER=paper_authority\n"
        "PAPER_TARGET_POSTGRES_DATABASE=paper_authority\n"
        "PAPER_TARGET_POSTGRES_SEARCH_PATH=quant,core,public\n",
        encoding="utf-8",
    )
    target_password = tmp_path / "paper-db-target-password"
    target_password.write_text("target-secret\n", encoding="utf-8")
    target_password.chmod(0o600)
    runtime_password = tmp_path / "paper-db-password"
    runtime_password.write_text("source-secret\n", encoding="utf-8")
    runtime_password.chmod(0o600)
    active = tmp_path / "active.env"
    active.write_text("POLYDATA_POSTGRES_HOST=127.0.0.1\n", encoding="utf-8")
    active.chmod(0o600)

    subprocess.run(
        ("bash", str(SCRIPT), "cutover"),
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PAPER_DB_CUTOVER_APPROVE": "YES",
            "PAPER_DB_PYTHON": str(fake_python),
            "PAPER_DB_SOURCE_ENV": str(source_env),
            "PAPER_DB_TARGET_ENV": str(target_env),
            "PAPER_DB_ACTIVE_ENV": str(active),
            "PAPER_DB_TARGET_PASSWORD_FILE": str(target_password),
            "PAPER_DB_RUNTIME_PASSWORD_FILE": str(runtime_password),
            "PAPER_DB_ARTIFACT_DIR": str(tmp_path / "artifacts"),
            "PAPER_LIVE_STATUS_PATH": str(tmp_path / "status.json"),
            "POLY_QUANT_GCP_ROOT": str(ROOT),
        },
        check=True,
        capture_output=True,
        text=True,
    )

    active_text = active.read_text(encoding="utf-8")
    assert "POLYDATA_POSTGRES_HOST=10.148.0.5" in active_text
    assert "POLYDATA_POSTGRES_USER=paper_authority" in active_text
    assert "PASSWORD" not in active_text
    assert active.stat().st_mode & 0o777 == 0o600
    assert runtime_password.read_text(encoding="utf-8") == "target-secret\n"
    assert runtime_password.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "active.env.rollback").read_text(encoding="utf-8") == (
        "POLYDATA_POSTGRES_HOST=127.0.0.1\n"
    )
    assert (tmp_path / "paper-db-password.rollback").read_text(
        encoding="utf-8"
    ) == "source-secret\n"
    assert "PASSWORD" not in target_env.read_text(encoding="utf-8")
    assert "preflight --require-private" in python_calls.read_text(encoding="utf-8")
    calls = systemctl_calls.read_text(encoding="utf-8")
    assert "stop poly-quant-gcp-paper-health.service" in calls
    assert "start poly-quant-gcp-paper-live-shadow.service" in calls
