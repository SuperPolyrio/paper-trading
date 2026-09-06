"""Run a read-only official-wallet replay from Polygon transaction receipts."""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from quant.core.db import postgres_connection
from quant.paper.authority import ControlPlanePostgresConnectionFactory

from .chain_activity_mirror import (
    ChainActivityMirrorService,
    PolygonReceiptArchive,
    write_chain_activity_mirror_report,
)
from .reconciler import AccountTruthReconciler
from .report import write_account_truth_report
from .store import PostgresAccountTruthStore

DEFAULT_RPC_URLS = (
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-address", default="")
    parser.add_argument("--rpc-url", action="append", default=[])
    parser.add_argument("--receipt-workers", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument(
        "--receipt-root",
        default="runtime_outputs/account_truth/chain_receipts",
    )
    parser.add_argument(
        "--artifact-root",
        default="runtime_outputs/account_truth/chain_mirror",
    )
    parser.add_argument("--quantity-tolerance", default="0.0001")
    parser.add_argument("--money-tolerance", default="0.0001")
    parser.add_argument("--price-tolerance", default="0.0001")
    parser.add_argument("--dust-threshold", default="0.01")
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = _parser().parse_args(argv)
    account = _required_address(args.account_address)
    connection_factory = ControlPlanePostgresConnectionFactory(postgres_connection)
    store = PostgresAccountTruthStore(connection_factory)
    official = store.load_latest_bundle(account_address=account)
    rpc_urls = tuple(args.rpc_url) or _environment_rpc_urls() or DEFAULT_RPC_URLS
    receipt_source = PolygonReceiptArchive(
        rpc_urls=rpc_urls,
        artifact_root=Path(args.receipt_root) / account,
        timeout_seconds=float(args.timeout_seconds),
    )
    service = ChainActivityMirrorService(
        connection_factory=connection_factory,
        receipt_source=receipt_source,
        receipt_workers=args.receipt_workers,
        official_quantity_tolerance=Decimal(args.quantity_tolerance),
        official_dust_threshold=Decimal(args.dust_threshold),
    )
    mirror = service.run(official=official)
    reconciler = AccountTruthReconciler(
        quantity_tolerance=Decimal(args.quantity_tolerance),
        money_tolerance=Decimal(args.money_tolerance),
        price_tolerance=Decimal(args.price_tolerance),
    )
    account_truth = reconciler.reconcile(
        official=official,
        paper=mirror.snapshot,
        comparison_scope="WHOLE_ACCOUNT",
    )
    store.persist_reconciliation(account_truth)
    account_truth_paths = write_account_truth_report(
        output_root=Path(args.artifact_root) / "account_truth",
        official=official,
        report=account_truth,
        execution_gate_paths=(),
    )
    paths = write_chain_activity_mirror_report(
        output_root=args.artifact_root,
        result=mirror,
        official=official,
        account_truth=account_truth,
        account_truth_paths=account_truth_paths,
    )
    summary = json.loads(Path(paths["summary"]).read_text(encoding="utf-8"))
    payload = {
        "schema_version": "chain-activity-account-mirror-cli-v1",
        "status": summary["overall_status"],
        "asset_replay_gate": summary["asset_replay_gate"],
        "economic_replay_gate": summary["economic_replay_gate"],
        "account_truth_gate": summary["account_truth_gate"],
        "pnl_truth_contract": summary["pnl_truth_contract"],
        "official_run_id": official.run_id,
        "mirror_run_id": mirror.mirror_run_id,
        "paths": paths,
        "live_submission_performed": False,
        "ledger_overwritten": False,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "PASS" else 2


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


def _environment_rpc_urls() -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in os.environ.get("POLY_QUANT_ACCOUNT_MIRROR_RPC_URLS", "").split(",")
        if item.strip()
    )


if __name__ == "__main__":
    raise SystemExit(main())
