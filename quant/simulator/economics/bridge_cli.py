"""CLI argument contract for official Bridge commands.

Runtime construction is intentionally injectable so credentials, DB settings and the
Unified Admission service remain outside this transport-facing parser.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from typing import Any

from quant.core.db import postgres_connection
from quant.paper.authority import ControlPlanePostgresConnectionFactory
from quant.simulator.admission import (
    PolymarketGeoblockClient,
    PostgresAdmissionStore,
    UnifiedAdmissionService,
)

from .bridge_client import BridgeCommandService, PolymarketBridgeClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paper-bridge")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--strategy-id")
    parser.add_argument("--proxy-url")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("supported-assets")

    quote = subparsers.add_parser("quote")
    quote.add_argument("--direction", choices=("DEPOSIT", "WITHDRAWAL"), required=True)
    quote.add_argument("--from-amount-base-unit", required=True)
    quote.add_argument("--from-chain-id", required=True)
    quote.add_argument("--from-token-address", required=True)
    quote.add_argument("--recipient-address", required=True)
    quote.add_argument("--to-chain-id", required=True)
    quote.add_argument("--to-token-address", required=True)

    deposit = subparsers.add_parser("deposit-address")
    deposit.add_argument("--wallet-address", required=True)
    deposit.add_argument("--execute", action="store_true")

    withdrawal = subparsers.add_parser("withdrawal-address")
    withdrawal.add_argument("--wallet-address", required=True)
    withdrawal.add_argument("--to-chain-id", required=True)
    withdrawal.add_argument("--to-token-address", required=True)
    withdrawal.add_argument("--recipient-address", required=True)
    withdrawal.add_argument("--execute", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("--bridge-address", required=True)

    recovery = subparsers.add_parser("recovery")
    recovery.add_argument("--bridge-address", required=True)
    recovery.add_argument("--transaction-hash", required=True)
    return parser


def run(service: BridgeCommandService, argv: Sequence[str] | None = None) -> Any:
    args = build_parser().parse_args(argv)
    return _dispatch(service, args)


def _dispatch(service: BridgeCommandService, args: argparse.Namespace) -> Any:
    common = {"account_id": args.account_id, "strategy_id": args.strategy_id}
    if args.command == "supported-assets":
        return service.supported_assets(**common)
    if args.command == "quote":
        return service.quote(
            **common,
            direction=args.direction,
            request={
                "fromAmountBaseUnit": args.from_amount_base_unit,
                "fromChainId": args.from_chain_id,
                "fromTokenAddress": args.from_token_address,
                "recipientAddress": args.recipient_address,
                "toChainId": args.to_chain_id,
                "toTokenAddress": args.to_token_address,
            },
        )
    if args.command == "deposit-address":
        return service.deposit_address(
            **common, wallet_address=args.wallet_address, execute=args.execute
        )
    if args.command == "withdrawal-address":
        return service.withdrawal_address(
            **common,
            wallet_address=args.wallet_address,
            to_chain_id=args.to_chain_id,
            to_token_address=args.to_token_address,
            recipient_address=args.recipient_address,
            execute=args.execute,
        )
    if args.command == "status":
        return service.status(**common, bridge_address=args.bridge_address)
    return service.recovery(
        **common,
        bridge_address=args.bridge_address,
        transaction_hash=args.transaction_hash,
    )


def render(result: Any) -> str:
    return json.dumps(result, indent=2, sort_keys=True, default=str)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    proxy_url = args.proxy_url or os.environ.get("PAPER_BRIDGE_PROXY_URL") or None
    connection_factory = ControlPlanePostgresConnectionFactory(postgres_connection)
    admission_store = PostgresAdmissionStore(connection_factory)
    admission_store.ensure_schema()
    service = BridgeCommandService(
        client=PolymarketBridgeClient(
            proxy_url=proxy_url,
            builder_code=os.environ.get("POLYMARKET_BUILDER_CODE") or None,
        ),
        admission=UnifiedAdmissionService(
            store=admission_store,
            geoblock_provider=PolymarketGeoblockClient(proxy_url=proxy_url),
        ),
    )
    print(render(_dispatch(service, args)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
