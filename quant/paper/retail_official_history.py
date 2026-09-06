"""Durable official-wallet shadow import jobs for the retail Paper product."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


JobRunner = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class RetailOfficialHistoryWorkerConfig:
    artifact_root: Path = Path("runtime_outputs/retail_official_history")
    lease_seconds: int = 3600
    command_timeout_seconds: int = 3600

    def __post_init__(self) -> None:
        if self.lease_seconds < 60:
            raise ValueError("official history lease must be at least 60 seconds")
        if self.command_timeout_seconds < 60:
            raise ValueError("official history timeout must be at least 60 seconds")


class RetailOfficialHistoryJobStore:
    """Claim and fence jobs without granting workers tenant API privileges."""

    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory

    def claim_next(
        self, *, runner_id: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.paper_official_history_sync_requests
                WHERE status='PENDING'
                   OR (status='RUNNING' AND lease_expires_at<clock_timestamp())
                ORDER BY requested_at,request_id
                FOR UPDATE SKIP LOCKED LIMIT 1
                """
            )
            selected = cur.fetchone()
            if selected is None:
                conn.commit()
                return None
            request_id = selected["request_id"]
            cur.execute(
                """
                UPDATE quant.paper_official_history_sync_requests SET
                    status='RUNNING',runner_id=%s,
                    attempt_count=attempt_count+1,
                    started_at=COALESCE(started_at,clock_timestamp()),
                    lease_expires_at=clock_timestamp()+(%s*interval '1 second'),
                    error_code=NULL,updated_at=clock_timestamp()
                WHERE request_id=%s
                RETURNING *
                """,
                (runner_id, int(lease_seconds), request_id),
            )
            row = dict(cur.fetchone())
            conn.commit()
        return row

    def finish(
        self,
        *,
        request_id: UUID,
        runner_id: str,
        result: Mapping[str, Any],
    ) -> bool:
        status = str(result.get("status") or "FAILED").upper()
        if status not in {"PASS", "DEGRADED", "FAILED"}:
            raise ValueError(f"invalid official history result status: {status}")
        canonical = json.dumps(result, sort_keys=True, default=str).encode()
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_official_history_sync_requests SET
                    status=%s,result_summary=%s::jsonb,result_sha256=%s,
                    error_code=%s,completed_at=clock_timestamp(),
                    lease_expires_at=NULL,updated_at=clock_timestamp()
                WHERE request_id=%s AND status='RUNNING' AND runner_id=%s
                RETURNING request_id
                """,
                (
                    status,
                    canonical.decode(),
                    hashlib.sha256(canonical).hexdigest(),
                    result.get("error_code"),
                    request_id,
                    runner_id,
                ),
            )
            changed = cur.fetchone() is not None
            conn.commit()
        return changed


class RetailOfficialHistoryWorker:
    def __init__(
        self,
        *,
        store: RetailOfficialHistoryJobStore,
        config: RetailOfficialHistoryWorkerConfig | None = None,
        runner: JobRunner | None = None,
        runner_id: str | None = None,
    ) -> None:
        self.store = store
        self.config = config or RetailOfficialHistoryWorkerConfig()
        self.runner_id = runner_id or f"retail-official-history-{uuid4()}"
        self.runner = runner or self._run_official_commands

    def run_once(self) -> dict[str, Any]:
        job = self.store.claim_next(
            runner_id=self.runner_id, lease_seconds=self.config.lease_seconds
        )
        if job is None:
            return {"status": "IDLE", "runner_id": self.runner_id}
        try:
            result = dict(self.runner(job))
        except Exception as exc:  # noqa: BLE001 - durable job records any runner failure
            result = {
                "status": "FAILED",
                "error_code": type(exc).__name__,
                "error": str(exc),
                "ledger_overwritten": False,
            }
        finished = self.store.finish(
            request_id=UUID(str(job["request_id"])),
            runner_id=self.runner_id,
            result=result,
        )
        return {
            "status": str(result.get("status") or "FAILED"),
            "request_id": str(job["request_id"]),
            "runner_id": self.runner_id,
            "fenced_write_applied": finished,
            "result": result,
        }

    def _run_official_commands(self, job: Mapping[str, Any]) -> dict[str, Any]:
        request_id = str(job["request_id"])
        root = self.config.artifact_root / request_id
        economics_root = root / "economics"
        truth_raw = root / "account_truth" / "raw"
        truth_report = root / "account_truth" / "report"
        for path in (economics_root, truth_raw, truth_report):
            path.mkdir(parents=True, exist_ok=True)

        account = str(job["account_address"])
        start = _iso(job["window_start"])
        end = _iso(job["window_end"])
        sync_command = [
            sys.executable,
            "-m",
            "quant.simulator.rewards.official_sync_cli",
            "backfill",
            "--account-address",
            account,
            "--strategy-id",
            str(job["shadow_strategy_id"]),
            "--account-id",
            f"official-shadow:{request_id}",
            "--start",
            start,
            "--end",
            end,
            "--output-dir",
            str(economics_root),
            "--no-sdk",
        ]
        comparison_scope = str(job["comparison_scope"])
        truth_command = [
            sys.executable,
            "-m",
            "quant.simulator.account_truth.cli",
            "reconcile" if comparison_scope == "WHOLE_ACCOUNT" else "capture",
            "--account-address",
            account,
            "--artifact-root",
            str(truth_raw),
            "--output-root",
            str(truth_report),
        ]
        if comparison_scope == "WHOLE_ACCOUNT":
            truth_command.extend(
                ("--strategy-id", str(job["paper_strategy_id"]))
            )
        commands = [
            _run_command(sync_command, timeout=self.config.command_timeout_seconds),
            _run_command(truth_command, timeout=self.config.command_timeout_seconds),
        ]
        if comparison_scope == "WHOLE_ACCOUNT":
            mirror_command = [
                sys.executable,
                "-m",
                "quant.simulator.account_truth.chain_mirror_cli",
                "--account-address",
                account,
                "--artifact-root",
                str(root / "chain_mirror"),
            ]
            commands.append(
                _run_command(
                    mirror_command,
                    timeout=self.config.command_timeout_seconds,
                )
            )
        sync_ok = commands[0]["returncode"] == 0
        truth_ok = commands[1]["returncode"] == 0
        mirror_payload = commands[2].get("stdout") if len(commands) > 2 else None
        mirror_asset_ok = bool(
            isinstance(mirror_payload, Mapping)
            and mirror_payload.get("asset_replay_gate") == "PASS"
        )
        mirror_economics_ok = bool(
            isinstance(mirror_payload, Mapping)
            and mirror_payload.get("economic_replay_gate") == "PASS"
        )
        mirror_ok = len(commands) == 2 or (
            mirror_asset_ok and mirror_economics_ok
        )
        status = (
            "PASS"
            if sync_ok and truth_ok and mirror_ok
            else "DEGRADED"
            if sync_ok and mirror_ok
            else "FAILED"
        )
        return {
            "schema_version": "retail_official_history_result_v1",
            "status": status,
            "account_address": account,
            "window_start": start,
            "window_end": end,
            "comparison_scope": comparison_scope,
            "shadow_strategy_id": str(job["shadow_strategy_id"]),
            "paper_strategy_id": str(job["paper_strategy_id"]),
            "commands": tuple(commands),
            "artifact_root": str(root),
            "ledger_overwritten": False,
            "official_events_enter_paper_ledger": False,
            "chain_reference": {
                "requested": comparison_scope == "WHOLE_ACCOUNT",
                "asset_replay_gate": (
                    mirror_payload.get("asset_replay_gate")
                    if isinstance(mirror_payload, Mapping)
                    else None
                ),
                "economic_replay_gate": (
                    mirror_payload.get("economic_replay_gate")
                    if isinstance(mirror_payload, Mapping)
                    else None
                ),
                "official_account_truth_gate": (
                    mirror_payload.get("account_truth_gate")
                    if isinstance(mirror_payload, Mapping)
                    else None
                ),
            },
            "error_code": (
                None
                if status == "PASS"
                else "OFFICIAL_SOURCE_OR_RECONCILIATION_INCOMPLETE"
            ),
        }


def _run_command(command: list[str], *, timeout: int) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    finished_at = datetime.now(timezone.utc)
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    return {
        "module": command[2] if len(command) > 2 and command[1] == "-m" else command[0],
        "returncode": int(completed.returncode),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
        "stdout": _parse_json_or_tail(stdout),
        "stderr_tail": stderr[-4000:],
    }


def _parse_json_or_tail(value: str) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"tail": value[-16000:]}


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        selected = value
    else:
        selected = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if selected.tzinfo is None:
        raise ValueError("official history timestamps must be timezone-aware")
    return selected.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "RetailOfficialHistoryJobStore",
    "RetailOfficialHistoryWorker",
    "RetailOfficialHistoryWorkerConfig",
]
