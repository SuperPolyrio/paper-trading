"""Operate an immutable post-CLOB-V2 Paper/live validation cohort."""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from quant.core.db import postgres_connection
from quant.paper.authority import ControlPlanePostgresConnectionFactory
from quant.paper.control_plane_connection import (
    paper_control_plane_connection_factory,
)
from quant.paper.live_shadow_store import LiveShadowStore
from quant.simulator.account_truth import (
    AccountTruthReconciler,
    OfficialAccountClient,
    OfficialAccountTruthService,
    PaperAccountSnapshotLoader,
    PolygonReceiptArchive,
    PostgresAccountTruthStore,
)

from .clean_v2_cohort import CleanV2CohortService, CleanV2CohortStore
from .probe_plan import load_probe_plan
from .real_live_adapter import PolymarketV2LiveAdapter
from .store import CalibrationStore


DEFAULT_RPC_URLS = (
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "init",
            "status",
            "sync-run",
            "checkpoint",
            "abandon-run",
            "invalidate",
        ),
    )
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--account-address", default="")
    parser.add_argument("--max-gross-notional", type=Decimal, default=Decimal("10"))
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/calibration/taker_account_truth_micro_live.yaml"),
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("runtime_outputs/account_truth/raw"),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("runtime_outputs/clean_v2_cohort"),
    )
    parser.add_argument(
        "--receipt-root",
        type=Path,
        default=Path("runtime_outputs/account_truth/chain_receipts"),
    )
    parser.add_argument("--rpc-url", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--quantity-tolerance", default="0.000001")
    parser.add_argument("--money-tolerance", default="0.00001")
    parser.add_argument("--price-tolerance", default="0.000001")
    parser.add_argument("--reason", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = _parser().parse_args(argv)
    try:
        account = _account_address(args.account_address)
        service = _build_service(
            args,
            account=account,
            require_live_adapter=args.command == "sync-run",
        )
        if args.command == "init":
            payload = service.initialize(
                cohort_id=args.cohort_id,
                account_address=account,
                max_gross_notional=args.max_gross_notional,
            )
        elif args.command == "status":
            cohort = service.store.load_cohort(args.cohort_id)
            operations = service.store.list_operations(args.cohort_id)
            payload = {
                "schema_version": "post-clob-v2-clean-cohort-status-v1",
                "status": str(cohort["status"]),
                "cohort_id": str(cohort["cohort_id"]),
                "paper_strategy_id": str(cohort["paper_strategy_id"]),
                "baseline_id": str(cohort["baseline_id"]),
                "baseline_as_of": cohort["baseline_as_of"],
                "venue_regime_id": str(cohort["venue_regime_id"]),
                "operation_count": len(operations),
                "committed_gross_notional": cohort["committed_gross_notional"],
                "max_gross_notional": cohort["max_gross_notional"],
                "last_checkpoint_status": cohort["last_checkpoint_status"],
                "operations": [_public_operation(row) for row in operations],
                "live_submission_performed": False,
            }
        elif args.command == "sync-run":
            if not args.run_id:
                raise ValueError("sync-run requires --run-id")
            row = service.sync_run_evidence(args.run_id)
            payload = {
                "schema_version": "post-clob-v2-clean-cohort-sync-v1",
                "status": str(row["status"]),
                "cohort_id": str(row["cohort_id"]),
                "run_id": str(row["run_id"]),
                "operation": _public_operation(row),
                "live_submission_performed": False,
            }
        elif args.command == "checkpoint":
            payload = service.checkpoint(args.cohort_id)
            payload["live_submission_performed"] = False
        elif args.command == "abandon-run":
            if not args.run_id:
                raise ValueError("abandon-run requires --run-id")
            row = service.store.transition_operation(
                run_id=args.run_id,
                expected=("PREPARED", "RUNNING"),
                target="ABORTED_NO_SUBMIT",
                reason="explicit operator abandonment before exchange submission",
            )
            payload = {
                "schema_version": "post-clob-v2-clean-cohort-abandon-v1",
                "status": str(row["status"]),
                "cohort_id": str(row["cohort_id"]),
                "run_id": str(row["run_id"]),
                "live_submission_performed": False,
            }
        else:
            row = service.store.invalidate_cohort(
                args.cohort_id,
                reason=args.reason,
            )
            payload = {
                "schema_version": "post-clob-v2-clean-cohort-invalidate-v1",
                "status": str(row["status"]),
                "cohort_id": str(row["cohort_id"]),
                "invalidated_at": row["invalidated_at"],
                "invalidated_reason": row["invalidated_reason"],
                "live_submission_performed": False,
            }
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0 if str(payload.get("status")) not in {"FAIL", "ERROR"} else 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": "post-clob-v2-clean-cohort-error-v1",
                    "status": "FAIL",
                    "error": f"{exc.__class__.__name__}:{str(exc)}",
                    "live_submission_performed": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2


def _build_service(
    args: argparse.Namespace,
    *,
    account: str,
    require_live_adapter: bool,
) -> CleanV2CohortService:
    paper_factory = paper_control_plane_connection_factory(
        os.environ,
        fallback_connection_factory=postgres_connection,
    )
    account_factory = ControlPlanePostgresConnectionFactory(postgres_connection)
    account_store = PostgresAccountTruthStore(
        account_factory,
        artifact_root=args.artifact_root,
    )
    client = OfficialAccountClient(
        base_url=(
            os.environ.get("POLY_QUANT_ACCOUNT_TRUTH_DATA_API_URL", "").strip()
            or os.environ.get("POLYDATA_POLYMARKET_DATA_API_BASE", "").strip()
            or "https://data-api.polymarket.com"
        ),
        proxy_url=(
            os.environ.get("POLY_QUANT_ACCOUNT_TRUTH_PROXY_URL", "").strip()
            or os.environ.get("POLY_QUANT_REWARD_PROXY_URL", "").strip()
            or None
        ),
        timeout_seconds=args.timeout_seconds,
    )
    account_truth = OfficialAccountTruthService(
        client=client,
        store=account_store,
        snapshot_loader=PaperAccountSnapshotLoader(paper_factory),
        reconciler=AccountTruthReconciler(
            quantity_tolerance=Decimal(args.quantity_tolerance),
            money_tolerance=Decimal(args.money_tolerance),
            price_tolerance=Decimal(args.price_tolerance),
        ),
    )
    plan = load_probe_plan(args.config)
    receipt_source = PolygonReceiptArchive(
        rpc_urls=tuple(args.rpc_url) or _environment_rpc_urls() or DEFAULT_RPC_URLS,
        artifact_root=args.receipt_root / account,
        timeout_seconds=args.timeout_seconds,
    )
    shadow_store = LiveShadowStore(paper_factory)
    return CleanV2CohortService(
        store=CleanV2CohortStore(paper_factory),
        account_truth=account_truth,
        calibration_store=CalibrationStore(),
        shadow_store=shadow_store,
        live_adapter=(
            PolymarketV2LiveAdapter(plan, environ=os.environ)
            if require_live_adapter
            else None
        ),
        receipt_source=receipt_source,
        output_root=args.output_root,
    )


def _public_operation(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    return {
        key: row.get(key)
        for key in (
            "operation_id",
            "sequence",
            "run_id",
            "paired_probe_id",
            "paper_intent_id",
            "asset_id",
            "market_id",
            "condition_id",
            "side",
            "order_type",
            "amount",
            "amount_unit",
            "planned_gross_notional",
            "status",
            "order_id",
            "trade_ids",
            "transaction_hashes",
            "paper_staging_strategy_id",
            "staged_paper_audit_key",
            "signed_paper_audit_key",
            "signed_prediction_frozen_at",
            "committed_paper_audit_key",
            "paper_committed_at",
            "submitted_at",
            "terminal_at",
        )
    } | {
        "evidence_sha256": row.get("evidence_sha256"),
        "artifact_path": evidence.get("artifact_path"),
        "artifact_sha256": evidence.get("artifact_sha256"),
    }


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


def _account_address(explicit: str) -> str:
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
                raise ValueError("account address must be a 20-byte EVM address")
            return address
    raise ValueError("account address is not configured")


def _environment_rpc_urls() -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in os.environ.get("POLY_QUANT_ACCOUNT_MIRROR_RPC_URLS", "").split(",")
        if item.strip()
    )


if __name__ == "__main__":
    raise SystemExit(main())
