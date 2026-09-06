from __future__ import annotations

import inspect
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from quant.paper import db_migration
from quant.paper.tenant_platform import (
    DEFAULT_QUOTAS,
    RLS_TABLES,
    ROLE_PERMISSIONS,
    TENANT_PLATFORM_SCHEMA_STATEMENTS,
    TENANT_PLATFORM_TABLES,
    PaperAuthorizationError,
    PaperPermission,
    PaperRole,
    PostgresTenantPlatformStore,
    QuotaMetric,
    TenantPrincipal,
    TenantScopeError,
    account_snapshot_hash,
    build_tenant_acceptance_report,
    evaluate_quota,
    has_permission,
    quota_bucket_start,
    rls_schema_statements,
)


class ScriptedCursor:
    def __init__(self, rows: list[dict[str, object] | None]) -> None:
        self.rows = list(rows)
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> dict[str, object] | None:
        if not self.rows:
            raise AssertionError("test cursor has no scripted row")
        return self.rows.pop(0)


def test_rbac_matrix_is_fail_closed() -> None:
    assert ROLE_PERMISSIONS[PaperRole.OWNER] == frozenset(PaperPermission)
    assert has_permission(PaperRole.ADMIN, PaperPermission.IMPERSONATE)
    assert not has_permission(PaperRole.ADMIN, PaperPermission.TENANT_ADMIN)
    assert has_permission(PaperRole.TRADER, PaperPermission.ACCOUNT_TRADE)
    assert not has_permission(PaperRole.TRADER, PaperPermission.ACCOUNT_CREATE)
    assert ROLE_PERMISSIONS[PaperRole.VIEWER] == frozenset(
        {PaperPermission.ACCOUNT_READ}
    )


def test_authorization_uses_database_membership_not_caller_supplied_role() -> None:
    principal = TenantPrincipal(tenant_id=uuid4(), actor_user_id=uuid4())
    cursor = ScriptedCursor(
        [
            {"tenant_status": "ACTIVE", "user_status": "ACTIVE"},
            {"role": "VIEWER", "status": "ACTIVE"},
        ]
    )
    store = PostgresTenantPlatformStore(connection_factory=None)

    with pytest.raises(PaperAuthorizationError, match="lacks ACCOUNT_TRADE"):
        store._authorize(cursor, principal, PaperPermission.ACCOUNT_TRADE)

    query, params = cursor.executed[1]
    assert "tenant_id=%s AND user_id=%s" in query
    assert params == (principal.tenant_id, principal.actor_user_id)


def test_impersonation_requires_admin_and_active_same_tenant_target() -> None:
    actor = uuid4()
    target = uuid4()
    principal = TenantPrincipal(
        tenant_id=uuid4(),
        actor_user_id=actor,
        effective_user_id=target,
        impersonation_reason="support case 42",
    )
    store = PostgresTenantPlatformStore(connection_factory=None)
    cursor = ScriptedCursor(
        [
            {"tenant_status": "ACTIVE", "user_status": "ACTIVE"},
            {"role": "ADMIN", "status": "ACTIVE"},
            {"role": "VIEWER", "status": "ACTIVE"},
        ]
    )
    assert (
        store._authorize(cursor, principal, PaperPermission.ACCOUNT_READ)
        is PaperRole.VIEWER
    )

    missing_target = ScriptedCursor(
        [
            {"tenant_status": "ACTIVE", "user_status": "ACTIVE"},
            {"role": "ADMIN", "status": "ACTIVE"},
            None,
        ]
    )
    with pytest.raises(TenantScopeError, match="target"):
        store._authorize(missing_target, principal, PaperPermission.ACCOUNT_READ)


def test_all_tenant_owned_tables_force_rls_and_scope_both_read_and_write() -> None:
    statements = rls_schema_statements()
    for table in RLS_TABLES:
        assert f"ALTER TABLE quant.{table} ENABLE ROW LEVEL SECURITY" in statements
        assert f"ALTER TABLE quant.{table} FORCE ROW LEVEL SECURITY" in statements
        policy = next(
            statement
            for statement in statements
            if f"CREATE POLICY paper_tenant_isolation ON quant.{table}" in statement
        )
        assert "current_setting('app.current_tenant_id', true)" in policy
        assert "USING" in policy
        assert "WITH CHECK" in policy


def test_product_views_do_not_duplicate_orders_across_account_strategies() -> None:
    schema = "\n".join(TENANT_PLATFORM_SCHEMA_STATEMENTS)
    orders_view = schema.split("CREATE OR REPLACE VIEW quant.paper_tenant_orders_v", 1)[
        1
    ]
    orders_view = orders_view.split(
        "CREATE OR REPLACE VIEW quant.paper_tenant_fills_v", 1
    )[0]
    assert "paper_account_registry a" in orders_view
    assert "paper_intent_ownership o" in orders_view
    assert "FROM quant.paper_strategies s" not in orders_view


def test_intent_ownership_has_full_tenant_account_strategy_deployment_chain() -> None:
    schema = "\n".join(TENANT_PLATFORM_SCHEMA_STATEMENTS)
    ownership = schema.split(
        "CREATE TABLE IF NOT EXISTS quant.paper_intent_ownership", 1
    )[1]
    ownership = ownership.split("CREATE TABLE IF NOT EXISTS quant.paper_quotas", 1)[0]
    assert "REFERENCES quant.paper_account_registry" in ownership
    assert "REFERENCES quant.paper_strategies" in ownership
    assert "REFERENCES quant.paper_strategy_deployments" in ownership
    assert "REFERENCES quant.paper_live_order_intents" in ownership


def test_quota_windows_limits_and_defaults() -> None:
    observed = datetime(2026, 8, 7, 3, 4, 59, 999, tzinfo=timezone.utc)
    assert quota_bucket_start(observed, 60) == datetime(
        2026, 8, 7, 3, 4, tzinfo=timezone.utc
    )
    assert quota_bucket_start(observed, 0) == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert evaluate_quota(
        hard_limit=Decimal(10),
        used_before=Decimal(9),
        requested=Decimal(1),
    ) == (True, Decimal(10))
    assert evaluate_quota(
        hard_limit=Decimal(10),
        used_before=Decimal(10),
        requested=Decimal("0.1"),
    ) == (False, Decimal("10.1"))
    assert {quota.metric for quota in DEFAULT_QUOTAS} == set(QuotaMetric)


def test_quota_rejects_naive_time_and_negative_values() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        quota_bucket_start(datetime(2026, 8, 7), 60)  # noqa: DTZ001
    with pytest.raises(ValueError, match="non-negative"):
        evaluate_quota(
            hard_limit=Decimal(1),
            used_before=Decimal(0),
            requested=Decimal(-1),
        )


def test_account_snapshot_hash_is_deterministic() -> None:
    left = {"cash": Decimal("1.00"), "positions": [{"asset": "a", "size": 2}]}
    right = {"positions": [{"size": 2, "asset": "a"}], "cash": Decimal("1.00")}
    assert account_snapshot_hash(left) == account_snapshot_hash(right)
    assert len(account_snapshot_hash(left)) == 64


def test_fork_is_quiescent_and_never_copies_reservations_or_orders() -> None:
    source = inspect.getsource(PostgresTenantPlatformStore.fork_account)
    assert "status IN ('QUEUED','PROCESSING','WORKING')" in source
    assert "status='ACTIVE'" in source
    assert "reserved_quantity,cost_basis" in source
    assert "reserved_quantity,cost_basis" in source and ",0,%s,%s,%s" in source
    assert (
        "paper_live_order_intents"
        not in source.split("for position in positions:", 1)[1]
    )


def test_migration_includes_tenant_authority_tables() -> None:
    assert set(TENANT_PLATFORM_TABLES) <= set(db_migration.EXECUTION_TABLES)
    assert set(TENANT_PLATFORM_TABLES) <= set(db_migration.SNAPSHOT_TABLES)
    assert db_migration.MIGRATION_VERSION.startswith("0029-")
    assert "paper_fee_schedules" in db_migration.CORE_PARITY_TABLES
    assert "paper_fill_fee_charges" in db_migration.CORE_PARITY_TABLES


def test_acceptance_report_is_honest_about_local_evidence_boundary() -> None:
    report = build_tenant_acceptance_report()
    assert report["status"] == "PASS"
    assert report["evidence_class"] == "DETERMINISTIC_LOCAL_NO_DATABASE"
    assert report["production_rls_applied"] is False
    assert report["production_database_mutated"] is False
    assert report["live_orders_submitted"] is False
    assert report["checks"]["public_api_credentials"]["status"] == "PASS"
    assert report["checks"]["public_api_credentials"]["public_self_issuance"] is False


def test_tenant_module_has_no_live_order_submission_dependency() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "quant" / "paper" / "tenant_platform.py").read_text(
        encoding="utf-8"
    )
    assert "py_clob_client" not in source
    assert "POLYMARKET_PRIVATE_KEY" not in source
    assert "post_order" not in source


def test_operator_cli_and_rls_probe_refuse_unguarded_mutation() -> None:
    root = Path(__file__).resolve().parents[2]
    for script, message in (
        (root / "scripts" / "manage_paper_tenants.py", "--apply is required"),
        (
            root / "scripts" / "run_paper_tenant_rls_probe.py",
            "--confirm-test-database is required",
        ),
        (
            root / "scripts" / "run_paper_tenant_postgres_acceptance.py",
            "--confirm-disposable-database is required",
        ),
    ):
        result = subprocess.run(
            [str(script), "init-schema"]
            if script.name == "manage_paper_tenants.py"
            else [str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert message in result.stderr


def test_tenant_runtime_role_is_no_login_rls_only_and_denies_legacy_tables() -> None:
    root = Path(__file__).resolve().parents[2]
    sql = (root / "deploy" / "postgres" / "paper_tenant_runtime_role.sql").read_text(
        encoding="utf-8"
    )
    assert "NOLOGIN NOSUPERUSER" in sql
    assert "NOBYPASSRLS" in sql
    assert "GRANT SELECT ON" in sql
    assert "REVOKE ALL ON" in sql
    assert "quant.paper_accounts" in sql
    assert "quant.paper_live_order_intents" in sql
    assert "GRANT SELECT, INSERT" not in sql
