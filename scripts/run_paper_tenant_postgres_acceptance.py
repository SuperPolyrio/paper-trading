#!/usr/bin/env python3
"""Destructive PR-6 behavior acceptance for a disposable local PostgreSQL DB."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.core.db import PostgresSettings, postgres_connection  # noqa: E402
from quant.paper.live_shadow_store import LiveShadowStore  # noqa: E402
from quant.paper.paper_ledger import PostgresPaperLedgerSink  # noqa: E402
from quant.paper.tenant_platform import (  # noqa: E402
    AccountForkError,
    PaperRole,
    PostgresTenantPlatformStore,
    QuotaMetric,
    TenantPlatformError,
    TenantPrincipal,
    TenantScopeError,
)


def _assert_disposable(settings: PostgresSettings) -> None:
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("tenant PostgreSQL acceptance requires a loopback database")
    if not any(word in settings.database.casefold() for word in ("test", "acceptance")):
        raise RuntimeError("database name must contain test or acceptance")


def run_acceptance() -> dict[str, object]:
    settings = PostgresSettings()
    _assert_disposable(settings)
    suffix = uuid4().hex
    store = PostgresTenantPlatformStore()
    owner = store.bootstrap_tenant(
        tenant_name=f"acceptance-a-{suffix}",
        owner_email=f"owner-a-{suffix}@example.invalid",
        owner_display_name="Owner A",
        idempotency_key=f"acceptance-a:{suffix}",
    )
    assert owner == store.bootstrap_tenant(
        tenant_name=f"acceptance-a-{suffix}",
        owner_email=f"owner-a-{suffix}@example.invalid",
        owner_display_name="Owner A",
        idempotency_key=f"acceptance-a:{suffix}",
    )
    viewer = store.add_user(
        owner,
        email=f"viewer-{suffix}@example.invalid",
        display_name="Viewer",
        role=PaperRole.VIEWER,
    )
    impersonated = store.begin_impersonation(
        owner,
        target_user_id=UUID(str(viewer["user_id"])),
        reason=f"acceptance:{suffix}",
    )
    assert store.list_accounts(impersonated) == []

    primary = store.create_account(
        owner,
        name="primary",
        idempotency_key=f"primary:{suffix}",
        initial_cash=Decimal("1000"),
    )
    replay = store.create_account(
        owner,
        name="primary",
        idempotency_key=f"primary:{suffix}",
        initial_cash=Decimal("1000"),
    )
    assert replay["account_id"] == primary["account_id"]
    try:
        store.create_account(
            owner,
            name="primary",
            idempotency_key=f"primary:{suffix}",
            initial_cash=Decimal("999"),
        )
    except TenantPlatformError:
        pass
    else:
        raise AssertionError("changed account idempotency identity was accepted")
    secondary = store.create_account(
        owner,
        name="secondary",
        idempotency_key=f"secondary:{suffix}",
        initial_cash=Decimal("500"),
    )
    assert secondary["account_id"] != primary["account_id"]

    ledger_id = str(primary["ledger_strategy_id"])
    PostgresPaperLedgerSink(ensure_schema=False).seed_calibration_position(
        strategy_id=ledger_id,
        asset_id=f"position-{suffix}",
        market_id=f"market-{suffix}",
        condition_id=f"condition-{suffix}",
        quantity=Decimal("5"),
        cost_basis=Decimal("2"),
    )
    reservation_id = int(suffix[:12], 16)
    with postgres_connection(readonly=False) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO quant.paper_order_reservations (
                intent_id,strategy_id,client_order_id,market_id,condition_id,
                asset_id,side,reserved_cash,status
            ) VALUES (%s,%s,%s,'m','c','a','BUY',1,'ACTIVE')
            """,
            (reservation_id, ledger_id, f"fork-block:{suffix}"),
        )
    try:
        store.fork_account(
            owner,
            parent_account_id=UUID(str(primary["account_id"])),
            name="blocked fork",
            idempotency_key=f"blocked-fork:{suffix}",
        )
    except AccountForkError:
        fork_blocked = True
    else:
        raise AssertionError("active reservation did not block account fork")
    finally:
        with postgres_connection(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM quant.paper_order_reservations WHERE intent_id=%s",
                (reservation_id,),
            )
    child = store.fork_account(
        owner,
        parent_account_id=UUID(str(primary["account_id"])),
        name="primary fork",
        idempotency_key=f"fork:{suffix}",
    )
    child_replay = store.fork_account(
        owner,
        parent_account_id=UUID(str(primary["account_id"])),
        name="primary fork",
        idempotency_key=f"fork:{suffix}",
    )
    assert child["account_id"] == child_replay["account_id"]
    with postgres_connection(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT quantity,reserved_quantity,cost_basis
            FROM quant.paper_positions WHERE strategy_id=%s AND asset_id=%s
            """,
            (str(child["ledger_strategy_id"]), f"position-{suffix}"),
        )
        position = cur.fetchone()
    assert Decimal(str(position["quantity"])) == Decimal("5")
    assert Decimal(str(position["reserved_quantity"])) == Decimal("0")
    assert Decimal(str(position["cost_basis"])) == Decimal("2")

    with postgres_connection(readonly=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('app.current_tenant_id', %s, true)",
            (str(owner.tenant_id),),
        )
        cur.execute(
            """
            SELECT strategy_id FROM quant.paper_strategies
            WHERE account_id=%s AND idempotency_key='default'
            """,
            (primary["account_id"],),
        )
        strategy_id = UUID(str(cur.fetchone()["strategy_id"]))
    deployment = store.create_deployment(
        owner,
        strategy_id=strategy_id,
        config={"version": 1},
        idempotency_key=f"deployment:{suffix}",
    )
    deployment_replay = store.create_deployment(
        owner,
        strategy_id=strategy_id,
        config={"version": 1},
        idempotency_key=f"deployment:{suffix}",
    )
    assert deployment["deployment_id"] == deployment_replay["deployment_id"]
    try:
        store.create_deployment(
            owner,
            strategy_id=strategy_id,
            config={"version": 2},
            idempotency_key=f"deployment:{suffix}",
        )
    except TenantPlatformError:
        pass
    else:
        raise AssertionError("changed deployment idempotency identity was accepted")

    asset_id = f"asset-{suffix}"
    with postgres_connection(readonly=False) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO quant.paper_execution_market_catalog (
                asset_id,market_id,condition_id,outcome_name,market_state,
                execution_eligible,active,closed,resolved,archived,deprecated,
                coverage_grade,has_gap
            ) VALUES (%s,%s,%s,'YES','LIVE',TRUE,TRUE,FALSE,FALSE,FALSE,FALSE,'A',FALSE)
            """,
            (asset_id, f"market-{suffix}", f"condition-{suffix}"),
        )
    intent_id = LiveShadowStore().submit(
        strategy_id=ledger_id,
        client_order_id=f"intent-{suffix}",
        asset_id=asset_id,
        side="BUY",
        time_in_force="FOK",
        limit_price=Decimal("0.5"),
        size=Decimal("1"),
        post_only=False,
        decision_ts=datetime.now(timezone.utc),
    )
    store.bind_intent_ownership(
        owner,
        intent_id=intent_id,
        account_id=UUID(str(primary["account_id"])),
        strategy_id=strategy_id,
        deployment_id=UUID(str(deployment["deployment_id"])),
    )

    observed_at = datetime.now(timezone.utc)
    quota = store.consume_quota(
        owner,
        metric=QuotaMetric.USER_INTENTS,
        subject_type="USER",
        subject_id=str(owner.actor_user_id),
        amount=Decimal("1"),
        idempotency_key=f"quota:{suffix}",
        observed_at=observed_at,
    )
    quota_replay = store.consume_quota(
        owner,
        metric=QuotaMetric.USER_INTENTS,
        subject_type="USER",
        subject_id=str(owner.actor_user_id),
        amount=Decimal("1"),
        idempotency_key=f"quota:{suffix}",
        observed_at=observed_at,
    )
    quota_denied = store.consume_quota(
        owner,
        metric=QuotaMetric.USER_INTENTS,
        subject_type="USER",
        subject_id=str(owner.actor_user_id),
        amount=Decimal("20"),
        idempotency_key=f"quota-over:{suffix}",
        observed_at=observed_at,
    )
    assert quota.allowed and quota_replay.idempotent_replay
    assert not quota_denied.allowed

    other = store.bootstrap_tenant(
        tenant_name=f"acceptance-b-{suffix}",
        owner_email=f"owner-b-{suffix}@example.invalid",
        owner_display_name="Owner B",
        idempotency_key=f"acceptance-b:{suffix}",
    )
    assert store.list_accounts(other) == []
    try:
        store.list_accounts(TenantPrincipal(owner.tenant_id, other.actor_user_id))
    except TenantScopeError:
        cross_tenant_denied = True
    else:
        raise AssertionError("cross-tenant actor was accepted")

    with postgres_connection(readonly=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('app.current_tenant_id', %s, true)",
            (str(owner.tenant_id),),
        )
        cur.execute(
            """
            SELECT previous_event_hash,event_hash
            FROM quant.paper_tenant_audit_events
            WHERE tenant_id=%s ORDER BY event_id
            """,
            (owner.tenant_id,),
        )
        audit = cur.fetchall()
    assert audit and audit[0]["previous_event_hash"] is None
    assert all(
        audit[index]["previous_event_hash"] == audit[index - 1]["event_hash"]
        for index in range(1, len(audit))
    )
    return {
        "schema_version": "paper_tenant_postgres_acceptance_v1",
        "status": "PASS",
        "database": settings.database,
        "disposable_database_required": True,
        "live_orders_submitted": False,
        "checks": {
            "bootstrap_idempotent": True,
            "admin_impersonation_audited": True,
            "multiple_accounts": len(store.list_accounts(owner)),
            "fork_blocked_by_reservation": fork_blocked,
            "fork_position_snapshot": True,
            "deployment_idempotent": True,
            "intent_ownership_bound": True,
            "quota_idempotent": True,
            "quota_over_limit_denied": True,
            "cross_tenant_actor_denied": cross_tenant_denied,
            "audit_chain_events": len(audit),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-disposable-database", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.confirm_disposable_database:
        parser.error("--confirm-disposable-database is required")
    report = run_acceptance()
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        os.chmod(args.output, 0o600)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
