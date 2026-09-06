#!/usr/bin/env python3
"""Operator CLI for the paper tenant control plane; mutations require --apply."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.core.db import PostgresSettings
from quant.paper.db_migration import apply_schema
from quant.paper.public_api import (
    ALLOWED_API_SCOPES,
    PostgresPaperApiBackend,
)
from quant.paper.tenant_platform import (
    PaperRole,
    PostgresTenantPlatformStore,
    QuotaMetric,
    TenantPrincipal,
)


def _principal(args: argparse.Namespace) -> TenantPrincipal:
    return TenantPrincipal(
        tenant_id=args.tenant_id,
        actor_user_id=args.actor_user_id,
    )


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="required for every database mutation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-schema")

    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--tenant-name", required=True)
    bootstrap.add_argument("--owner-email", required=True)
    bootstrap.add_argument("--owner-display-name", required=True)
    bootstrap.add_argument("--idempotency-key", required=True)

    for name in (
        "add-user",
        "create-account",
        "fork-account",
        "set-quota",
        "create-api-key",
        "revoke-api-key",
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--tenant-id", type=UUID, required=True)
        command.add_argument("--actor-user-id", type=UUID, required=True)
        if name == "add-user":
            command.add_argument("--email", required=True)
            command.add_argument("--display-name", required=True)
            command.add_argument(
                "--role", choices=[role.value for role in PaperRole], required=True
            )
        elif name == "create-account":
            command.add_argument("--name", required=True)
            command.add_argument("--idempotency-key", required=True)
            command.add_argument("--initial-cash", type=Decimal, default=Decimal(10000))
        elif name == "fork-account":
            command.add_argument("--parent-account-id", type=UUID, required=True)
            command.add_argument("--name", required=True)
            command.add_argument("--idempotency-key", required=True)
        elif name == "set-quota":
            command.add_argument(
                "--metric", choices=[item.value for item in QuotaMetric], required=True
            )
            command.add_argument(
                "--subject-type",
                choices=("TENANT", "USER", "ACCOUNT", "STRATEGY"),
                required=True,
            )
            command.add_argument("--subject-id")
            command.add_argument("--hard-limit", type=Decimal, required=True)
            command.add_argument("--window-seconds", type=int, required=True)
        elif name == "create-api-key":
            command.add_argument("--name", required=True)
            command.add_argument(
                "--scope",
                dest="scopes",
                action="append",
                choices=sorted(ALLOWED_API_SCOPES),
                required=True,
            )
            command.add_argument(
                "--expires-at",
                type=datetime.fromisoformat,
                help="optional ISO-8601 timestamp; include a timezone",
            )
        else:
            command.add_argument("--api-key-id", type=UUID, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.apply:
        parser.error("--apply is required; no database changes were made")
    if args.command == "init-schema":
        print(_json(apply_schema(PostgresSettings())))
        return 0
    store = PostgresTenantPlatformStore()
    if args.command == "bootstrap":
        principal = store.bootstrap_tenant(
            tenant_name=args.tenant_name,
            owner_email=args.owner_email,
            owner_display_name=args.owner_display_name,
            idempotency_key=args.idempotency_key,
        )
        result: Any = {
            "tenant_id": principal.tenant_id,
            "owner_user_id": principal.actor_user_id,
        }
    elif args.command == "add-user":
        result = store.add_user(
            _principal(args),
            email=args.email,
            display_name=args.display_name,
            role=PaperRole(args.role),
        )
    elif args.command == "create-account":
        result = store.create_account(
            _principal(args),
            name=args.name,
            idempotency_key=args.idempotency_key,
            initial_cash=args.initial_cash,
        )
    elif args.command == "fork-account":
        result = store.fork_account(
            _principal(args),
            parent_account_id=args.parent_account_id,
            name=args.name,
            idempotency_key=args.idempotency_key,
        )
    elif args.command == "set-quota":
        result = {
            "quota_key": store.set_quota(
                _principal(args),
                metric=QuotaMetric(args.metric),
                subject_type=args.subject_type,
                subject_id=args.subject_id,
                hard_limit=args.hard_limit,
                window_seconds=args.window_seconds,
            )
        }
    elif args.command == "create-api-key":
        if args.expires_at is not None and args.expires_at.tzinfo is None:
            parser.error("--expires-at must include a timezone")
        result = PostgresPaperApiBackend(tenant_store=store).create_api_key(
            _principal(args),
            name=args.name,
            scopes=args.scopes,
            expires_at=args.expires_at,
        )
        result["warning"] = "token is shown once; store it in a secret manager"
    else:
        result = {
            "api_key_id": args.api_key_id,
            "revoked": PostgresPaperApiBackend(
                tenant_store=store
            ).revoke_api_key_for_principal(_principal(args), args.api_key_id),
        }
    print(_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
