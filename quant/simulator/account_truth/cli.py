"""CLI for read-only official account truth capture and reconciliation."""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from quant.core.db import postgres_connection
from quant.paper.authority import ControlPlanePostgresConnectionFactory
from quant.paper.control_plane_connection import (
    paper_control_plane_connection_factory,
)

from .official_account_client import OfficialAccountClient, OfficialAccountFetchError
from .reconciler import AccountTruthReconciler, PaperAccountSnapshotLoader
from .report import write_account_truth_report
from .service import OfficialAccountTruthService
from .store import PostgresAccountTruthStore

DEFAULT_EXECUTION_GATE_PATHS = (
    Path("reports/calibration/taker-campaign-current/promotion_decision.json"),
    Path("reports/maker/holdout-current/evaluation.json"),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "capture",
            "reconcile",
            "baseline",
            "delta-reconcile",
            "daemon",
            "status",
        ),
    )
    parser.add_argument("--account-address", default="")
    parser.add_argument(
        "--strategy-id",
        action="append",
        default=_environment_list("POLY_QUANT_ACCOUNT_TRUTH_STRATEGY_IDS"),
    )
    parser.add_argument(
        "--scope-id",
        default=os.environ.get("POLY_QUANT_ACCOUNT_TRUTH_SCOPE_ID", ""),
    )
    parser.add_argument(
        "--comparison-scope",
        choices=("WHOLE_ACCOUNT", "CALIBRATION_DELTA", "OFFICIAL_ONLY"),
        default="WHOLE_ACCOUNT",
    )
    parser.add_argument("--data-api-url", default="")
    parser.add_argument("--proxy-url", default="")
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--interval-seconds", type=float, default=900.0)
    parser.add_argument("--artifact-root", default="runtime_outputs/account_truth/raw")
    parser.add_argument("--output-root", default="runtime_outputs/account_truth")
    parser.add_argument("--execution-gate", action="append", default=[])
    parser.add_argument("--skip-execution-gates", action="store_true")
    parser.add_argument("--quantity-tolerance", default="0.000001")
    parser.add_argument("--money-tolerance", default="0.00001")
    parser.add_argument("--price-tolerance", default="0.000001")
    parser.add_argument(
        "--convergence-window-seconds",
        type=float,
        default=float(
            os.environ.get("POLY_QUANT_ACCOUNT_TRUTH_CONVERGENCE_SECONDS", "300")
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = _parser().parse_args(argv)
    account = _required_address(args.account_address)
    connection_factory = ControlPlanePostgresConnectionFactory(postgres_connection)
    store = PostgresAccountTruthStore(
        connection_factory,
        artifact_root=Path(args.artifact_root),
    )
    if args.command == "status":
        print(json.dumps(_status(store, account), indent=2, default=str))
        return 0
    if args.command == "reconcile" and not args.strategy_id:
        raise ValueError("reconcile requires at least one --strategy-id")
    if args.command == "reconcile" and args.comparison_scope == "CALIBRATION_DELTA":
        raise ValueError("use delta-reconcile for CALIBRATION_DELTA")
    if args.command in {"baseline", "delta-reconcile"} and not args.scope_id.strip():
        raise ValueError(f"{args.command} requires --scope-id")
    if args.command == "delta-reconcile" and not args.strategy_id:
        raise ValueError("delta-reconcile requires at least one --strategy-id")
    if args.command == "daemon" and args.scope_id and not args.strategy_id:
        raise ValueError("daemon with --scope-id requires at least one --strategy-id")
    client = OfficialAccountClient(
        base_url=(
            str(args.data_api_url).strip()
            or os.environ.get("POLY_QUANT_ACCOUNT_TRUTH_DATA_API_URL", "").strip()
            or os.environ.get("POLYDATA_POLYMARKET_DATA_API_BASE", "").strip()
            or "https://data-api.polymarket.com"
        ),
        proxy_url=(
            str(args.proxy_url).strip()
            or os.environ.get("POLY_QUANT_ACCOUNT_TRUTH_PROXY_URL", "").strip()
            or os.environ.get("POLY_QUANT_REWARD_PROXY_URL", "").strip()
            or None
        ),
        timeout_seconds=float(args.timeout_seconds),
        min_request_interval_seconds=float(
            os.environ.get("POLY_QUANT_ACCOUNT_TRUTH_MIN_REQUEST_INTERVAL", "0")
        ),
    )
    reconciler = AccountTruthReconciler(
        quantity_tolerance=Decimal(args.quantity_tolerance),
        money_tolerance=Decimal(args.money_tolerance),
        price_tolerance=Decimal(args.price_tolerance),
    )
    service = OfficialAccountTruthService(
        client=client,
        store=store,
        snapshot_loader=PaperAccountSnapshotLoader(
            paper_control_plane_connection_factory(
                os.environ,
                fallback_connection_factory=postgres_connection,
            )
        ),
        reconciler=reconciler,
        convergence_window_seconds=args.convergence_window_seconds,
    )
    execution_paths = (
        ()
        if args.skip_execution_gates
        else tuple(Path(path) for path in args.execution_gate)
        or DEFAULT_EXECUTION_GATE_PATHS
    )

    def run_once() -> tuple[dict[str, Any], int]:
        if args.command == "baseline":
            official, baseline = service.create_baseline(
                account_address=account,
                scope_id=args.scope_id,
                strategy_ids=tuple(args.strategy_id),
            )
            return {
                "schema_version": "official-account-truth-cli-v1",
                "status": "BASELINE_READY",
                "baseline_id": baseline["baseline_id"],
                "scope_id": baseline["scope_id"],
                "official_run_id": baseline["official_run_id"],
                "source_as_of": baseline["source_as_of"].isoformat(),
                "strategy_ids": list(baseline["strategy_ids"]),
                "captured_official_run_id": official.run_id,
            }, 0
        use_delta = args.command == "delta-reconcile" or (
            args.command == "daemon" and bool(args.scope_id)
        )
        use_paper = args.command == "reconcile" or (
            args.command == "daemon" and bool(args.strategy_id)
        )
        if use_delta:
            official, report = service.capture_and_reconcile_delta(
                account_address=account,
                scope_id=args.scope_id,
                strategy_ids=tuple(args.strategy_id),
            )
        else:
            official, report = service.capture_and_reconcile(
                account_address=account,
                strategy_ids=(tuple(args.strategy_id) if use_paper else ()),
                comparison_scope=(
                    args.comparison_scope if use_paper else "OFFICIAL_ONLY"
                ),
            )
        paths = write_account_truth_report(
            output_root=Path(args.output_root),
            official=official,
            report=report,
            execution_gate_paths=execution_paths,
        )
        payload = {
            "schema_version": "official-account-truth-cli-v1",
            "official_run_id": official.run_id,
            "reconciliation_id": report.reconciliation_id,
            "account_truth_gate": report.status.value,
            "official_source_status": report.official_source_status,
            "comparison_scope": report.comparison_scope,
            "mismatch_count": len(report.mismatches),
            "paths": paths,
        }
        if report.official_source_status == "SOURCE_CONFLICT":
            return payload, 2
        if (use_delta or use_paper) and report.status.value.startswith("FAIL"):
            return payload, 2
        return payload, 0

    if args.command in {"capture", "reconcile", "baseline", "delta-reconcile"}:
        try:
            payload, code = run_once()
        except OfficialAccountFetchError as exc:
            result = exc.result
            payload = {
                "schema_version": "official-account-truth-cli-v1",
                "status": "FETCH_FAILED",
                "endpoint": result.endpoint,
                "http_status": result.http_status,
                "error_code": result.error_code,
                "observed_at": result.observed_at.isoformat(),
                "payload_hash": result.payload_hash,
                "failure_evidence_persisted": True,
            }
            code = 2
        print(json.dumps(payload, indent=2, sort_keys=True))
        return code

    stopped = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_code = 0
    while not stopped:
        try:
            payload, last_code = run_once()
        except Exception as exc:  # noqa: BLE001 - daemon records and retries any source failure
            payload = {
                "schema_version": "official-account-truth-cli-v1",
                "status": "ERROR",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            last_code = 2
        print(json.dumps(payload, sort_keys=True), flush=True)
        deadline = time.monotonic() + max(1.0, float(args.interval_seconds))
        while not stopped and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    return last_code


def _load_env() -> None:
    for path in (
        Path(".env"),
        Path(".env.local"),
        Path.home() / ".config/prediction-market-quant/official-account-sync.env",
        Path.home() / ".config/prediction-market-quant/calibration.env",
        Path.home() / ".config/prediction-market-quant/paper-db-control.env",
    ):
        if path.exists():
            load_dotenv(path, override=False)


def _environment_list(name: str) -> list[str]:
    return [
        item.strip() for item in os.environ.get(name, "").split(",") if item.strip()
    ]


def _required_address(explicit: str) -> str:
    for value in (
        explicit,
        os.environ.get("POLY_QUANT_ACCOUNT_TRUTH_WALLET_ADDRESS", ""),
        os.environ.get("POLY_QUANT_REWARD_WALLET_ADDRESS", ""),
        os.environ.get("POLYMARKET_FUNDER_ADDRESS", ""),
        os.environ.get("POLYMARKET_USER_ADDRESS", ""),
    ):
        address = str(value).strip().lower()
        if address:
            if not address.startswith("0x") or len(address) != 42:
                raise ValueError("account truth wallet must be a 20-byte EVM address")
            return address
    raise ValueError("account truth wallet address is not configured")


def _status(store: PostgresAccountTruthStore, account: str) -> dict[str, Any]:
    with store.connection_factory(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM quant.paper_official_account_snapshot_runs
            WHERE account_address=%s ORDER BY source_as_of DESC,observed_at DESC LIMIT 1
            """,
            (account,),
        )
        snapshot = cur.fetchone()
        cur.execute(
            """
            SELECT * FROM quant.paper_account_truth_reconciliation_runs
            WHERE account_address=%s ORDER BY generated_at DESC LIMIT 1
            """,
            (account,),
        )
        reconciliation = cur.fetchone()
        cur.execute(
            """
            SELECT baseline_id,scope_id,official_run_id,strategy_ids,
                   source_as_of,status,baseline_hash,created_at
            FROM quant.paper_account_truth_baselines
            WHERE account_address=%s ORDER BY source_as_of DESC LIMIT 1
            """,
            (account,),
        )
        baseline = cur.fetchone()
    return {
        "schema_version": "official-account-truth-status-v1",
        "account_address": account,
        "latest_snapshot": dict(snapshot) if snapshot else None,
        "latest_reconciliation": dict(reconciliation) if reconciliation else None,
        "latest_baseline": dict(baseline) if baseline else None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
