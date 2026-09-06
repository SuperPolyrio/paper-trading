"""Same-region PostgreSQL migration and parity checks for the paper worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg import OperationalError, sql

from quant.calibration.store import CalibrationStore
from quant.core.db import PostgresSettings, postgres_connection
from quant.settlement.dispute_policy import PostgresDisputePolicyStore
from quant.settlement.resolution_store import PostgresResolutionLifecycleStore
from quant.simulator.admission import PostgresAdmissionStore
from quant.simulator.combo.store import PostgresComboStore
from quant.simulator.economics import (
    PostgresAccountCashflowStore,
    PostgresAccountProgramStore,
    PostgresAccountReturnStore,
)
from quant.simulator.finality import PostgresFillFinalityStore
from quant.simulator.integrity import PostgresIntegrityCaseStore
from quant.simulator.liquidity import PostgresLiquidityOverlayStore
from quant.simulator.oms import PostgresOwnOrderStore
from quant.simulator.operations import PostgresAugmentedNegRiskStore
from quant.simulator.operations.operation_store import PostgresPositionOperationStore
from quant.simulator.regime.store import PostgresVenueRegimeStore
from quant.simulator.regime.venue_migration import PostgresVenueMigrationReplayStore
from quant.simulator.rewards import (
    PostgresHoldingRewardStore,
    PostgresLiquidityRewardStore,
    PostgresMakerRebateStore,
    PostgresOfficialAccountSyncStore,
    PostgresReferralRewardStore,
    PostgresRewardLedgerStore,
    PostgresTakerRebateStore,
)
from quant.simulator.run_artifact_store import PostgresSimulatorArtifactStore

from .authority import (
    AUTHORITY_TABLES,
    AuthorityLeaseStore,
    ControlPlanePostgresConnectionFactory,
)
from .live_shadow_store import LiveShadowStore
from .paper_audit import PostgresPaperAuditSink
from .paper_ledger import PostgresPaperLedgerSink
from .tenant_platform import TENANT_PLATFORM_TABLES, PostgresTenantPlatformStore

MIGRATION_VERSION = "0029-maker-research-queue-v1"

TARGET_CATALOG_PROTECTED_ASSETS_QUERY = """
    SELECT DISTINCT asset_id
    FROM (
        SELECT asset_id FROM quant.paper_live_watchlist WHERE enabled=TRUE
        UNION ALL SELECT asset_id FROM quant.paper_live_order_intents
                  WHERE status IN ('QUEUED','PROCESSING','WORKING')
        UNION ALL SELECT asset_id FROM quant.paper_positions WHERE quantity <> 0
        UNION ALL SELECT asset_id FROM quant.paper_calibration_pnl_positions
                  WHERE real_quantity <> 0 OR paper_quantity <> 0
    ) protected
    ORDER BY asset_id
"""

CATALOG_DELETE_UNREFERENCED_QUERY = """
    DELETE FROM quant.paper_execution_market_catalog catalog
    WHERE NOT EXISTS (
        SELECT 1 FROM paper_catalog_sync_stage synced
        WHERE synced.asset_id = catalog.asset_id
    )
      AND NOT EXISTS (
          SELECT 1 FROM quant.paper_live_watchlist protected
          WHERE protected.asset_id=catalog.asset_id
            AND protected.enabled=TRUE
      )
      AND NOT EXISTS (
          SELECT 1 FROM quant.paper_live_order_intents protected
          WHERE protected.asset_id=catalog.asset_id
            AND protected.status IN ('QUEUED','PROCESSING','WORKING')
      )
      AND NOT EXISTS (
          SELECT 1 FROM quant.paper_positions protected
          WHERE protected.asset_id=catalog.asset_id
            AND protected.quantity <> 0
      )
      AND NOT EXISTS (
          SELECT 1 FROM quant.paper_calibration_pnl_positions protected
          WHERE protected.asset_id=catalog.asset_id
            AND (
                protected.real_quantity <> 0
                OR protected.paper_quantity <> 0
            )
      )
"""

CATALOG_SOURCE_QUERY_MAX_ATTEMPTS = 3
CATALOG_SOURCE_QUERY_RETRY_SECONDS = 1.0

# This list deliberately excludes registry history, outbox, lifecycle events,
# token-universe snapshots, and L2 archive metadata. Those belong to the data
# plane, not the execution authority database.
EXECUTION_TABLES = (
    "paper_execution_market_catalog",
    *AUTHORITY_TABLES,
    "venue_regimes",
    "execution_model_versions",
    "venue_regime_snapshots",
    "paper_live_watchlist",
    "paper_accounts",
    "paper_strategy_risk_controls",
    "paper_positions",
    "paper_position_marks",
    "paper_order_reservations",
    "paper_reservation_release_events",
    "paper_daily_accounting_snapshots",
    "paper_portfolio_nav_current",
    "paper_portfolio_nav_snapshots",
    "paper_live_order_intents",
    "paper_tenants",
    "paper_users",
    "paper_memberships",
    "paper_account_registry",
    "paper_account_generations",
    "paper_strategies",
    "paper_strategy_deployments",
    "paper_intent_ownership",
    "paper_quotas",
    "paper_usage_meter",
    "paper_quota_consumptions",
    "paper_api_keys",
    "paper_api_idempotency",
    "paper_api_request_log",
    "paper_tenant_audit_events",
    "paper_tenant_deletion_requests",
    "paper_replay_sessions",
    "paper_replay_events",
    "paper_scenario_runs",
    "paper_conditional_orders",
    "paper_conditional_order_events",
    "paper_resource_freezes",
    "paper_admin_jobs",
    "paper_dlq_events",
    "paper_maintenance_notices",
    "paper_incidents",
    "paper_incident_notes",
    "paper_retention_policies",
    "paper_evidence_bundles",
    "paper_order_events",
    "paper_market_terms",
    "paper_fee_schedules",
    "paper_risk_decisions",
    "paper_execution_profile_decisions",
    "paper_paired_probes",
    "paper_live_book_checkpoints",
    "paper_live_current_books",
    "paper_live_maker_trade_events",
    "paper_taker_order_audits",
    "paper_fills",
    "paper_fill_fee_charges",
    "paper_reward_schedules",
    "paper_reward_accruals",
    "paper_reward_payouts",
    "paper_reward_reconciliations",
    "paper_reward_clawbacks",
    "paper_account_economic_events",
    "paper_account_cashflow_operations",
    "paper_bridge_transfer_states",
    "paper_sponsor_commitments",
    "paper_sponsor_distributions",
    "paper_dispute_bonds",
    "paper_official_account_activities",
    "paper_official_reward_rules",
    "paper_official_bridge_transactions",
    "paper_official_account_sync_runs",
    "paper_official_account_sync_checkpoints",
    "paper_official_reward_source_checks",
    "paper_reward_aggregate_reconciliations",
    "paper_reward_calibration_reports",
    "paper_maker_rebate_fill_equivalents",
    "paper_maker_rebate_daily_estimates",
    "paper_taker_weighted_volume_events",
    "paper_taker_tier_snapshots",
    "paper_taker_rebate_daily_estimates",
    "paper_liquidity_reward_order_samples",
    "paper_liquidity_reward_sample_scores",
    "paper_liquidity_reward_epoch_estimates",
    "paper_holding_reward_position_samples",
    "paper_holding_reward_daily_estimates",
    "paper_referral_fee_evidence",
    "paper_referral_reward_daily_estimates",
    "paper_account_return_reports",
    "paper_fill_ctf_settlement_audits",
    "paper_ledger_entries",
    "paper_journal_lines",
    "paper_portfolio_applied_results",
    "paper_settlements",
    "paper_settlement_receivables",
    "paper_complete_set_merges",
    "simulator_complete_set_lots",
    "simulator_complete_set_lot_legs",
    "simulator_complete_set_consumptions",
    "simulator_complete_set_consumption_legs",
    "paper_execution_finality",
    "paper_execution_finality_events",
    "paper_rebates",
    "paper_calibration_models",
    "paper_calibration_runs",
    "paper_calibration_probes",
    "paper_calibration_events",
    "paper_calibration_pnl_positions",
    "paper_calibration_pnl_entries",
    "paper_calibration_pnl_settlements",
    "maker_queue_states",
    "maker_research_queue_states",
    "maker_research_queue_events",
    "market_settlement_payouts",
    "market_resolution_states",
    "market_resolution_events",
    "paper_sim_events",
    "paper_global_event_kernel_state",
    "paper_global_event_kernel_events",
    "paper_inflight_commands",
    "paper_venue_shadow_runs",
    "paper_order_batches",
    "paper_order_batch_children",
    "simulator_liquidity_levels",
    "simulator_liquidity_allocations",
    "simulator_oms_orders",
    "simulator_oms_admissions",
    "simulator_oms_events",
    "simulator_oms_position_assignments",
    "simulator_oms_strategy_attribution",
    "simulator_oms_attributed_fills",
    "simulator_fill_finality_trades",
    "simulator_fill_finality_events",
    "simulator_position_operations",
    "simulator_position_operation_events",
    "simulator_position_operation_nonces",
    "simulator_position_operation_reservations",
    "simulator_position_operation_token_reservations",
    "paper_position_operation_applications",
    "simulator_augmented_neg_risk_events",
    "simulator_augmented_neg_risk_outcomes",
    "simulator_augmented_conversion_matrices",
    "simulator_augmented_conversion_reconciliations",
    "simulator_admission_policies",
    "simulator_geoblock_snapshots",
    "simulator_admission_decisions",
    "simulator_combo_market_catalog",
    "simulator_official_combo_rfqs",
    "simulator_combo_command_attempts",
    "simulator_official_combo_rfq_events",
    "simulator_quoter_ws_events",
    "simulator_combo_positions",
    "simulator_combo_accounting_events",
    "simulator_combo_collateral_plans",
    "simulator_dispute_rule_snapshots",
    "simulator_dispute_cases",
    "simulator_dispute_case_events",
    "simulator_venue_migration_replays",
    "simulator_venue_migration_events",
    "simulator_integrity_cases",
    "simulator_integrity_evidence",
    "simulator_integrity_case_actions",
    "execution_tca",
)

# The execution catalog is derived from registry state and has no source-side
# counterpart. It is populated by sync_market_catalog, not table snapshots.
DERIVED_OR_LOCAL_TABLES = {
    "paper_execution_market_catalog",
    *AUTHORITY_TABLES,
}
SNAPSHOT_TABLES = tuple(
    table for table in EXECUTION_TABLES if table not in DERIVED_OR_LOCAL_TABLES
)

# These rows determine cash, positions, fills, terminal order state and
# idempotency. They are the strict shadow verifier set while the source worker
# is still running. Volatile health/current-book read models are checked only
# during the paused final sync.
CORE_PARITY_TABLES = (
    "paper_accounts",
    "paper_positions",
    "paper_order_reservations",
    "paper_reservation_release_events",
    "paper_live_order_intents",
    *TENANT_PLATFORM_TABLES,
    "paper_order_events",
    "paper_fee_schedules",
    "paper_risk_decisions",
    "paper_taker_order_audits",
    "paper_fills",
    "paper_fill_fee_charges",
    "paper_reward_schedules",
    "paper_reward_accruals",
    "paper_reward_payouts",
    "paper_reward_reconciliations",
    "paper_reward_clawbacks",
    "paper_account_economic_events",
    "paper_account_cashflow_operations",
    "paper_bridge_transfer_states",
    "paper_sponsor_commitments",
    "paper_sponsor_distributions",
    "paper_dispute_bonds",
    "paper_official_account_activities",
    "paper_official_reward_rules",
    "paper_official_bridge_transactions",
    "paper_official_account_sync_runs",
    "paper_official_account_sync_checkpoints",
    "paper_official_reward_source_checks",
    "paper_reward_aggregate_reconciliations",
    "paper_reward_calibration_reports",
    "paper_maker_rebate_fill_equivalents",
    "paper_maker_rebate_daily_estimates",
    "paper_taker_weighted_volume_events",
    "paper_taker_tier_snapshots",
    "paper_taker_rebate_daily_estimates",
    "paper_liquidity_reward_order_samples",
    "paper_liquidity_reward_sample_scores",
    "paper_liquidity_reward_epoch_estimates",
    "paper_holding_reward_position_samples",
    "paper_holding_reward_daily_estimates",
    "paper_referral_fee_evidence",
    "paper_referral_reward_daily_estimates",
    "paper_account_return_reports",
    "paper_fill_ctf_settlement_audits",
    "paper_ledger_entries",
    "paper_journal_lines",
    "paper_portfolio_applied_results",
    "paper_settlements",
    "paper_settlement_receivables",
    "simulator_complete_set_lots",
    "simulator_complete_set_lot_legs",
    "simulator_complete_set_consumptions",
    "simulator_complete_set_consumption_legs",
    "paper_execution_finality",
    "paper_execution_finality_events",
    "paper_calibration_pnl_positions",
    "paper_calibration_pnl_entries",
    "paper_calibration_pnl_settlements",
    "paper_inflight_commands",
    "simulator_liquidity_levels",
    "simulator_liquidity_allocations",
    "simulator_oms_orders",
    "simulator_oms_events",
    "simulator_fill_finality_trades",
    "simulator_fill_finality_events",
    "simulator_position_operations",
    "simulator_position_operation_events",
    "simulator_position_operation_nonces",
    "simulator_position_operation_reservations",
    "simulator_position_operation_token_reservations",
    "paper_position_operation_applications",
    "simulator_augmented_neg_risk_events",
    "simulator_augmented_neg_risk_outcomes",
    "simulator_augmented_conversion_matrices",
    "simulator_augmented_conversion_reconciliations",
    "simulator_admission_policies",
    "simulator_geoblock_snapshots",
    "simulator_admission_decisions",
    "simulator_official_combo_rfqs",
    "simulator_combo_command_attempts",
    "simulator_official_combo_rfq_events",
    "simulator_quoter_ws_events",
    "simulator_combo_positions",
    "simulator_combo_accounting_events",
    "simulator_combo_collateral_plans",
    "simulator_dispute_cases",
    "simulator_dispute_case_events",
    "simulator_venue_migration_replays",
    "simulator_venue_migration_events",
    "simulator_integrity_cases",
    "simulator_integrity_evidence",
    "simulator_integrity_case_actions",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: Path | None, payload: Mapping[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_env_files(paths: Sequence[Path]) -> None:
    if not paths:
        return
    from dotenv import load_dotenv

    for path in paths:
        load_dotenv(path, override=True)


def settings_from_prefix(
    prefix: str,
    *,
    host_override: str | None = None,
    port_override: int | None = None,
) -> PostgresSettings:
    normalized = prefix.rstrip("_").upper()

    def required(name: str) -> str:
        value = os.environ.get(f"{normalized}_{name}", "").strip()
        if not value:
            raise ValueError(f"missing {normalized}_{name}")
        return value

    return PostgresSettings(
        host=str(host_override).strip() if host_override else required("HOST"),
        port=int(port_override) if port_override is not None else int(required("PORT")),
        user=required("USER"),
        password=required("PASSWORD"),
        database=required("DATABASE"),
        search_path=os.environ.get(
            f"{normalized}_SEARCH_PATH", "quant,core,oracle,ops,public"
        ),
        connect_timeout_seconds=int(
            os.environ.get(f"{normalized}_CONNECT_TIMEOUT_SECONDS", "10")
        ),
        statement_timeout_ms=int(
            os.environ.get(f"{normalized}_STATEMENT_TIMEOUT_MS", "0")
        ),
        lock_timeout_ms=int(os.environ.get(f"{normalized}_LOCK_TIMEOUT_MS", "0")),
    )


def _factory(settings: PostgresSettings) -> Any:
    @contextmanager
    def connect(*, readonly: bool = False) -> Iterator[Any]:
        with postgres_connection(settings, readonly=readonly) as conn:
            yield conn

    return connect


def _migration_checksum() -> str:
    payload = json.dumps(
        {
            "version": MIGRATION_VERSION,
            "tables": EXECUTION_TABLES,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply_schema(settings: PostgresSettings) -> dict[str, Any]:
    factory = ControlPlanePostgresConnectionFactory(_factory(settings))
    with factory(readonly=False) as conn, conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS quant")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS quant.paper_schema_migrations (
                version TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            )
            """
        )
        conn.commit()

    LiveShadowStore(factory).ensure_schema()
    PostgresPaperLedgerSink(factory)
    PostgresPaperAuditSink(factory)
    PostgresSimulatorArtifactStore(factory).ensure_schema()
    PostgresLiquidityOverlayStore(factory).ensure_schema()
    PostgresOwnOrderStore(factory).ensure_schema()
    PostgresFillFinalityStore(factory).ensure_schema()
    PostgresPositionOperationStore(factory).ensure_schema()
    PostgresAugmentedNegRiskStore(factory).ensure_schema()
    PostgresAdmissionStore(factory).ensure_schema()
    PostgresComboStore(factory).ensure_schema()
    PostgresVenueRegimeStore(factory).ensure_schema()
    PostgresVenueMigrationReplayStore(factory).ensure_schema()
    PostgresIntegrityCaseStore(factory).ensure_schema()
    PostgresRewardLedgerStore(factory).ensure_schema()
    PostgresAccountCashflowStore(factory).ensure_schema()
    PostgresAccountProgramStore(factory).ensure_schema()
    PostgresOfficialAccountSyncStore(factory).ensure_schema()
    PostgresMakerRebateStore(factory).ensure_schema()
    PostgresTakerRebateStore(factory).ensure_schema()
    PostgresLiquidityRewardStore(factory).ensure_schema()
    PostgresHoldingRewardStore(factory).ensure_schema()
    PostgresReferralRewardStore(factory).ensure_schema()
    PostgresAccountReturnStore(factory).ensure_schema()
    PostgresResolutionLifecycleStore(factory).ensure_schema()
    PostgresDisputePolicyStore(factory).ensure_schema()
    CalibrationStore(factory).ensure_schema()
    AuthorityLeaseStore(factory).ensure_schema()
    PostgresTenantPlatformStore(factory).ensure_schema()

    checksum = _migration_checksum()
    with factory(readonly=False) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT checksum FROM quant.paper_schema_migrations WHERE version=%s",
            (MIGRATION_VERSION,),
        )
        row = cur.fetchone()
        if row is not None and str(row["checksum"]) != checksum:
            raise RuntimeError("migration checksum drift detected")
        cur.execute(
            """
            INSERT INTO quant.paper_schema_migrations (version, checksum)
            VALUES (%s, %s)
            ON CONFLICT (version) DO NOTHING
            """,
            (MIGRATION_VERSION, checksum),
        )
        conn.commit()
    return {
        "schema_version": "paper_db_schema_apply_v1",
        "generated_at": _now().isoformat(),
        "status": "PASS",
        "migration_version": MIGRATION_VERSION,
        "migration_checksum": checksum,
        "database": settings.database,
    }


def _relation_exists(conn: Any, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL AS present", (f"quant.{table}",)
        )
        return bool(cur.fetchone()["present"])


def _table_shape(conn: Any, table: str) -> tuple[list[str], dict[str, str], list[str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema='quant' AND table_name=%s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        rows = cur.fetchall()
        columns = [str(row["column_name"]) for row in rows]
        types = {str(row["column_name"]): str(row["data_type"]) for row in rows}
        cur.execute(
            """
            SELECT a.attname AS column_name
            FROM pg_index i
            JOIN pg_class c ON c.oid=i.indrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN unnest(i.indkey) WITH ORDINALITY AS keys(attnum, ordinality) ON TRUE
            JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=keys.attnum
            WHERE n.nspname='quant' AND c.relname=%s AND i.indisprimary
            ORDER BY keys.ordinality
            """,
            (table,),
        )
        primary_key = [str(row["column_name"]) for row in cur.fetchall()]
    return columns, types, primary_key


def _copy_value(value: Any, data_type: str) -> Any:
    if value is not None and data_type in {"json", "jsonb"}:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return value


def _selected_tables(
    source: Any,
    target: Any,
    requested: Sequence[str],
) -> list[str]:
    missing: list[str] = []
    selected: list[str] = []
    for table in requested:
        source_exists = _relation_exists(source, table)
        target_exists = _relation_exists(target, table)
        if source_exists and target_exists:
            selected.append(table)
        elif source_exists != target_exists:
            missing.append(f"{table}:source={source_exists},target={target_exists}")
    if missing:
        raise RuntimeError("schema mismatch: " + "; ".join(missing))
    return selected


def _require_tenant_migration_privilege(
    conn: Any,
    tables: Sequence[str],
    *,
    endpoint: str,
) -> None:
    tenant_tables = sorted(set(tables) & set(TENANT_PLATFORM_TABLES))
    if not tenant_tables:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rolsuper OR rolbypassrls AS allowed
            FROM pg_roles WHERE rolname=current_user
            """
        )
        row = cur.fetchone()
    if row is None or not bool(row["allowed"]):
        raise RuntimeError(
            f"{endpoint} migration identity must be SUPERUSER or BYPASSRLS "
            f"when copying tenant tables: {tenant_tables}"
        )


def sync_tables(
    source_settings: PostgresSettings,
    target_settings: PostgresSettings,
    *,
    tables: Sequence[str] = SNAPSHOT_TABLES,
) -> dict[str, Any]:
    started = time.perf_counter()
    copied: dict[str, int] = {}
    with (
        postgres_connection(source_settings, readonly=True) as source,
        postgres_connection(target_settings, readonly=False) as target,
    ):
        with source.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        selected = _selected_tables(source, target, tables)
        _require_tenant_migration_privilege(source, selected, endpoint="source")
        _require_tenant_migration_privilege(target, selected, endpoint="target")
        if selected:
            identifiers = [sql.Identifier("quant", table) for table in selected]
            with target.cursor() as cur:
                cur.execute(
                    sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                        sql.SQL(", ").join(identifiers)
                    )
                )
        for table in selected:
            source_columns, source_types, primary_key = _table_shape(source, table)
            target_columns, _, _ = _table_shape(target, table)
            missing_columns = sorted(set(source_columns) - set(target_columns))
            if missing_columns:
                raise RuntimeError(
                    f"target table quant.{table} is missing columns: {missing_columns}"
                )
            columns = sorted(source_columns)
            if not columns:
                continue
            order_by = primary_key or columns
            select_query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.Identifier("quant", table),
                sql.SQL(", ").join(map(sql.Identifier, order_by)),
            )
            copy_query = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier("quant", table),
                sql.SQL(", ").join(map(sql.Identifier, columns)),
            )
            count = 0
            with source.cursor(name=f"paper_migrate_{table}") as source_cur:
                source_cur.itersize = 1000
                source_cur.execute(select_query)
                with target.cursor().copy(copy_query) as copy:
                    for row in source_cur:
                        copy.write_row(
                            tuple(
                                _copy_value(row[column], source_types[column])
                                for column in columns
                            )
                        )
                        count += 1
            copied[table] = count
            if len(primary_key) == 1:
                with target.cursor() as cur:
                    cur.execute(
                        "SELECT pg_get_serial_sequence(%s, %s) AS sequence_name",
                        (f"quant.{table}", primary_key[0]),
                    )
                    sequence_name = cur.fetchone()["sequence_name"]
                    if sequence_name:
                        cur.execute(
                            sql.SQL(
                                "SELECT setval(%s, COALESCE(MAX({}), 1), COUNT(*) > 0) FROM {}"
                            ).format(
                                sql.Identifier(primary_key[0]),
                                sql.Identifier("quant", table),
                            ),
                            (sequence_name,),
                        )
        target.commit()
    return {
        "schema_version": "paper_db_snapshot_sync_v1",
        "generated_at": _now().isoformat(),
        "status": "PASS",
        "tables": copied,
        "total_rows": sum(copied.values()),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        normalized = (
            value.astimezone(timezone.utc)
            if value.tzinfo
            else value.replace(tzinfo=timezone.utc)
        )
        return normalized.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, UUID):
        return str(value)
    return value


def table_fingerprint(
    conn: Any,
    table: str,
    *,
    columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    available_columns, _, primary_key = _table_shape(conn, table)
    selected_columns = sorted(columns or available_columns)
    missing_columns = sorted(set(selected_columns) - set(available_columns))
    if missing_columns:
        raise RuntimeError(f"quant.{table} is missing columns: {missing_columns}")
    order_by = primary_key or selected_columns
    query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
        sql.SQL(", ").join(map(sql.Identifier, selected_columns)),
        sql.Identifier("quant", table),
        sql.SQL(", ").join(map(sql.Identifier, order_by)),
    )
    digest = hashlib.sha256()
    count = 0
    with conn.cursor(name=f"paper_hash_{table}") as cur:
        cur.itersize = 1000
        cur.execute(query)
        for row in cur:
            payload = json.dumps(
                _canonical(row),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            digest.update(payload.encode("utf-8"))
            digest.update(b"\n")
            count += 1
    return {
        "count": count,
        "sha256": digest.hexdigest(),
        "primary_key": primary_key,
        "columns": selected_columns,
    }


def verify_parity(
    source_settings: PostgresSettings,
    target_settings: PostgresSettings,
    *,
    tables: Sequence[str] = SNAPSHOT_TABLES,
) -> dict[str, Any]:
    started = time.perf_counter()
    results: dict[str, Any] = {}
    with (
        postgres_connection(source_settings, readonly=True) as source,
        postgres_connection(target_settings, readonly=True) as target,
    ):
        with source.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        selected = _selected_tables(source, target, tables)
        _require_tenant_migration_privilege(source, selected, endpoint="source")
        _require_tenant_migration_privilege(target, selected, endpoint="target")
        for table in selected:
            source_columns, _, _ = _table_shape(source, table)
            target_columns, _, _ = _table_shape(target, table)
            missing_columns = sorted(set(source_columns) - set(target_columns))
            if missing_columns:
                results[table] = {
                    "status": "FAIL",
                    "missing_target_columns": missing_columns,
                }
                continue
            columns = sorted(source_columns)
            source_fingerprint = table_fingerprint(source, table, columns=columns)
            target_fingerprint = table_fingerprint(target, table, columns=columns)
            results[table] = {
                "status": "PASS"
                if source_fingerprint == target_fingerprint
                else "FAIL",
                "source": source_fingerprint,
                "target": target_fingerprint,
                "target_extra_columns": sorted(
                    set(target_columns) - set(source_columns)
                ),
            }
    failures = [
        table for table, result in results.items() if result["status"] != "PASS"
    ]
    return {
        "schema_version": "paper_db_parity_v1",
        "generated_at": _now().isoformat(),
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "tables": results,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def sync_market_catalog(
    source_settings: PostgresSettings,
    target_settings: PostgresSettings,
) -> dict[str, Any]:
    started = time.perf_counter()
    seed_query = """
        SELECT DISTINCT asset_id
        FROM (
            SELECT asset_id FROM quant.paper_market_registry_tokens
            WHERE execution_eligible=TRUE
            UNION ALL SELECT asset_id FROM quant.paper_live_watchlist WHERE enabled=TRUE
            UNION ALL SELECT asset_id FROM quant.paper_live_order_intents
                      WHERE status IN ('QUEUED','PROCESSING','WORKING')
            UNION ALL SELECT asset_id FROM quant.paper_positions WHERE quantity <> 0
            UNION ALL SELECT asset_id FROM quant.paper_calibration_pnl_positions
                      WHERE real_quantity <> 0 OR paper_quantity <> 0
        ) seeds
        ORDER BY asset_id
    """
    scope_query = """
        SELECT DISTINCT r.condition_id,
               COALESCE(m.event_id, r.condition_id) AS event_id,
               COALESCE(m.enable_neg_risk, FALSE) AS enable_neg_risk
        FROM quant.paper_market_registry_tokens r
        LEFT JOIN core.markets m ON m.id=r.market_id
        WHERE r.asset_id=ANY(%s::text[])
    """
    needed_query = """
        SELECT DISTINCT asset_id
        FROM (
            SELECT r.asset_id
            FROM quant.paper_market_registry_tokens r
            WHERE r.asset_id=ANY(%s::text[])
            UNION ALL
            SELECT r.asset_id
            FROM quant.paper_market_registry_tokens r
            WHERE r.condition_id=ANY(%s::text[])
            UNION ALL
            SELECT r.asset_id
            FROM quant.paper_market_registry_tokens r
            JOIN core.markets m ON m.id=r.market_id
            WHERE m.enable_neg_risk=TRUE
              AND m.event_id=ANY(%s::text[])
        ) expanded
        ORDER BY asset_id
    """
    base_query = """
        SELECT r.asset_id,
               COALESCE(r.market_id::text, r.gamma_market_id, r.condition_id) AS market_id,
               COALESCE(r.condition_id, '') AS condition_id,
               r.market_slug, r.market_title,
               COALESCE(r.outcome_name, 'UNKNOWN') AS outcome_name,
               COALESCE(r.outcome_index, 0) AS outcome_index,
               COALESCE(m.event_id, r.condition_id) AS event_id,
               COALESCE(NULLIF(m.category, ''), 'unknown') AS category,
               COALESCE(m.enable_neg_risk, FALSE) AS enable_neg_risk,
               COALESCE(r.market_state, 'DISCOVERED') AS market_state,
               COALESCE(r.execution_eligible, FALSE) AS execution_eligible,
               COALESCE(r.active, FALSE) AS active,
               COALESCE(r.closed, FALSE) AS closed,
               COALESCE(r.resolved, FALSE) AS resolved,
               COALESCE(r.archived, FALSE) AS archived,
               COALESCE(r.deprecated, FALSE) AS deprecated,
               COALESCE(c.coverage_grade, 'D') AS coverage_grade,
               COALESCE(c.has_gap, TRUE) AS has_gap,
               c.last_receive_ts, r.current_tick_size, r.min_order_size,
               token_metadata.end_date,
               '{}'::jsonb::text AS raw_metadata,
               NULL::text AS event_title,
               NULL::numeric AS event_volume,
               NULL::text AS winning_asset_id,
               NULL::text AS resolution_status,
               NULL::text AS resolution_source,
               NULL::timestamptz AS resolved_time,
               r.updated_at AS source_updated_at
        FROM quant.paper_market_registry_tokens r
        LEFT JOIN core.markets m ON m.id=r.market_id
        LEFT JOIN quant.market_token_metadata token_metadata
          ON token_metadata.token_id=r.asset_id
        LEFT JOIN quant.clob_l2_current_coverage c USING (asset_id)
        WHERE r.asset_id=ANY(%s::text[])
        ORDER BY r.asset_id
    """
    event_query = """
        SELECT DISTINCT ON (r.asset_id)
               r.asset_id, metadata.event_title, metadata.volume AS event_volume
        FROM quant.paper_market_registry_tokens r
        JOIN quant.market_event_members member ON member.market_id=r.market_id
        JOIN quant.market_event_metadata metadata
          ON metadata.event_slug=member.event_slug
        WHERE r.asset_id=ANY(%s::text[])
        ORDER BY r.asset_id, metadata.updated_at DESC, metadata.event_slug
    """
    resolution_query = """
        SELECT DISTINCT ON (r.asset_id)
               r.asset_id, aggregate.winning_asset_id,
               aggregate.resolution_status, aggregate.resolution_source,
               aggregate.resolved_time
        FROM quant.paper_market_registry_tokens r
        JOIN quant.paper_market_registry_markets aggregate
          ON aggregate.condition_id=r.condition_id
        WHERE r.asset_id=ANY(%s::text[])
        ORDER BY r.asset_id, aggregate.updated_at DESC, aggregate.market_key
    """
    columns = (
        "asset_id",
        "market_id",
        "condition_id",
        "market_slug",
        "market_title",
        "outcome_name",
        "outcome_index",
        "event_id",
        "category",
        "enable_neg_risk",
        "market_state",
        "execution_eligible",
        "active",
        "closed",
        "resolved",
        "archived",
        "deprecated",
        "coverage_grade",
        "has_gap",
        "last_receive_ts",
        "current_tick_size",
        "min_order_size",
        "end_date",
        "raw_metadata",
        "event_title",
        "event_volume",
        "winning_asset_id",
        "resolution_status",
        "resolution_source",
        "resolved_time",
        "source_updated_at",
    )
    rows: list[dict[str, Any]] = []
    phase_seconds: dict[str, float] = {}
    with (
        postgres_connection(target_settings, readonly=True) as target,
        target.cursor() as cur,
    ):
        phase_started = time.perf_counter()
        cur.execute(TARGET_CATALOG_PROTECTED_ASSETS_QUERY)
        target_seed_assets = [str(row["asset_id"]) for row in cur.fetchall()]
        phase_seconds["target_seed"] = round(
            time.perf_counter() - phase_started,
            3,
        )
    phase_attempts: dict[str, int] = {}
    phase_started = time.perf_counter()
    seed_rows, phase_attempts["seed"] = _fetch_catalog_source_rows(
        source_settings,
        seed_query,
    )
    seed_assets = sorted(
        {
            *(str(row["asset_id"]) for row in seed_rows),
            *target_seed_assets,
        }
    )
    phase_seconds["seed"] = round(time.perf_counter() - phase_started, 3)

    phase_started = time.perf_counter()
    scope, scope_attempts = _fetch_catalog_source_rows(
        source_settings,
        scope_query,
        (seed_assets,),
    )
    condition_ids = sorted({str(row["condition_id"]) for row in scope})
    neg_risk_event_ids = sorted(
        {str(row["event_id"]) for row in scope if row["enable_neg_risk"]}
    )
    needed_rows, needed_attempts = _fetch_catalog_source_rows(
        source_settings,
        needed_query,
        (seed_assets, condition_ids, neg_risk_event_ids),
    )
    needed_assets = [str(row["asset_id"]) for row in needed_rows]
    phase_attempts["scope"] = scope_attempts + needed_attempts
    phase_seconds["scope"] = round(time.perf_counter() - phase_started, 3)

    phase_started = time.perf_counter()
    rows, phase_attempts["base"] = _fetch_catalog_source_rows(
        source_settings,
        base_query,
        (needed_assets,),
    )
    phase_seconds["base"] = round(time.perf_counter() - phase_started, 3)

    by_asset = {str(row["asset_id"]): row for row in rows}
    phase_started = time.perf_counter()
    event_rows, phase_attempts["event"] = _fetch_catalog_source_rows(
        source_settings,
        event_query,
        (needed_assets,),
    )
    for event in event_rows:
        row = by_asset.get(str(event["asset_id"]))
        if row is not None:
            row["event_title"] = event["event_title"]
            row["event_volume"] = event["event_volume"]
    phase_seconds["event"] = round(time.perf_counter() - phase_started, 3)

    phase_started = time.perf_counter()
    resolution_rows, phase_attempts["resolution"] = _fetch_catalog_source_rows(
        source_settings,
        resolution_query,
        (needed_assets,),
    )
    for resolution in resolution_rows:
        row = by_asset.get(str(resolution["asset_id"]))
        if row is not None:
            for key in (
                "winning_asset_id",
                "resolution_status",
                "resolution_source",
                "resolved_time",
            ):
                row[key] = resolution[key]
    phase_seconds["resolution"] = round(time.perf_counter() - phase_started, 3)
    target_factory = ControlPlanePostgresConnectionFactory(_factory(target_settings))
    with target_factory(readonly=False) as target, target.cursor() as cur:
        cur.execute(
            """
            CREATE TEMP TABLE paper_catalog_sync_stage
            (LIKE quant.paper_execution_market_catalog INCLUDING DEFAULTS)
            ON COMMIT DROP
            """
        )
        copy_query = sql.SQL("COPY paper_catalog_sync_stage ({}) FROM STDIN").format(
            sql.SQL(", ").join(map(sql.Identifier, columns))
        )
        with cur.copy(copy_query) as copy:
            for row in rows:
                copy.write_row(tuple(row[column] for column in columns))
        statement = sql.SQL(
            "INSERT INTO quant.paper_execution_market_catalog ({columns}) "
            "SELECT {columns} FROM paper_catalog_sync_stage "
            "ON CONFLICT (asset_id) DO UPDATE SET {updates}, "
            "synced_at=clock_timestamp()"
        ).format(
            columns=sql.SQL(", ").join(map(sql.Identifier, columns)),
            updates=sql.SQL(", ").join(
                sql.SQL("{}=EXCLUDED.{}").format(
                    sql.Identifier(column), sql.Identifier(column)
                )
                for column in columns
                if column != "asset_id"
            ),
        )
        cur.execute(statement)
        cur.execute(CATALOG_DELETE_UNREFERENCED_QUERY)
        deleted = int(cur.rowcount or 0)
        target.commit()
    return {
        "schema_version": "paper_execution_market_catalog_sync_v1",
        "generated_at": _now().isoformat(),
        "status": "PASS",
        "rows": len(rows),
        "deleted": deleted,
        "seed_assets": len(seed_assets),
        "target_protected_assets": len(target_seed_assets),
        "needed_assets": len(needed_assets),
        "phase_attempts": phase_attempts,
        "phase_seconds": phase_seconds,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def _fetch_catalog_source_rows(
    source_settings: PostgresSettings,
    query: str,
    params: Sequence[Any] = (),
    *,
    max_attempts: int = CATALOG_SOURCE_QUERY_MAX_ATTEMPTS,
) -> tuple[list[dict[str, Any]], int]:
    """Run one catalog phase on a fresh source connection.

    The source may be reached through a reverse SSH tunnel. Keeping each heavy
    read on its own connection avoids coupling the whole refresh to one
    long-lived tunnel session. Only transport failures are retried; SQL and
    schema errors remain fail-closed.
    """
    if max_attempts < 1:
        raise ValueError("catalog source query requires at least one attempt")
    for attempt in range(1, max_attempts + 1):
        try:
            with (
                postgres_connection(source_settings, readonly=True) as source,
                source.cursor() as cur,
            ):
                cur.execute(query, params)
                return [dict(row) for row in cur.fetchall()], attempt
        except OperationalError:
            if attempt >= max_attempts:
                raise
            time.sleep(CATALOG_SOURCE_QUERY_RETRY_SECONDS * attempt)
    raise AssertionError("unreachable catalog source retry state")


def latency_report(settings: PostgresSettings, *, samples: int) -> dict[str, Any]:
    values: list[float] = []
    errors: list[str] = []
    for _ in range(max(1, samples)):
        started = time.perf_counter()
        try:
            with (
                postgres_connection(settings, readonly=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute("SELECT 1 AS ok")
                cur.fetchone()
            values.append((time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - report deployment failures
            errors.append(f"{type(exc).__name__}: {exc}")
    ordered = sorted(values)

    def percentile(fraction: float) -> float | None:
        if not ordered:
            return None
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]

    return {
        "schema_version": "paper_db_latency_v1",
        "generated_at": _now().isoformat(),
        "status": "PASS" if values and not errors else "FAIL",
        "host": settings.host,
        "port": settings.port,
        "samples": len(values),
        "errors": errors,
        "min_ms": round(min(values), 3) if values else None,
        "p50_ms": round(percentile(0.50), 3) if values else None,
        "p95_ms": round(percentile(0.95), 3) if values else None,
        "max_ms": round(max(values), 3) if values else None,
    }


def _private_route(host: str) -> tuple[bool, list[str]]:
    try:
        addresses = sorted(
            {
                str(item[4][0])
                for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            }
        )
    except socket.gaierror:
        return False, []
    valid = bool(addresses) and all(
        ip_address(address).is_private
        and not ip_address(address).is_loopback
        and not ip_address(address).is_link_local
        for address in addresses
    )
    return valid, addresses


def preflight(
    settings: PostgresSettings,
    *,
    require_private: bool,
    require_migration: bool = True,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    route_private, resolved_addresses = _private_route(settings.host)
    checks.append(
        {
            "name": "private_route",
            "status": "PASS"
            if route_private
            else "FAIL"
            if require_private
            else "WARN",
            "detail": {
                "host": settings.host,
                "port": settings.port,
                "resolved_addresses": resolved_addresses,
            },
        }
    )
    try:
        with postgres_connection(settings, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT current_database() AS database,
                       current_setting('server_version') AS server_version,
                       inet_server_addr()::text AS server_address,
                       pg_is_in_recovery() AS in_recovery
                """
            )
            server = dict(cur.fetchone())
            cur.execute(
                "SELECT to_regclass('quant.paper_schema_migrations') AS relation"
            )
            relation = cur.fetchone()["relation"]
            migration = None
            if relation is not None:
                cur.execute(
                    "SELECT checksum FROM quant.paper_schema_migrations WHERE version=%s",
                    (MIGRATION_VERSION,),
                )
                migration = cur.fetchone()
        checks.append({"name": "database", "status": "PASS", "detail": server})
        checks.append(
            {
                "name": "migration",
                "status": (
                    "PASS"
                    if migration and migration["checksum"] == _migration_checksum()
                    else "FAIL"
                    if require_migration
                    else "WARN"
                ),
                "detail": dict(migration) if migration else None,
            }
        )
    except Exception as exc:  # noqa: BLE001 - preflight must return connection failures
        checks.append(
            {
                "name": "database",
                "status": "FAIL",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )
    failures = [check for check in checks if check["status"] == "FAIL"]
    warnings = [check for check in checks if check["status"] == "WARN"]
    return {
        "schema_version": "paper_db_preflight_v1",
        "generated_at": _now().isoformat(),
        "status": "FAIL" if failures else "PASS_WITH_WARNINGS" if warnings else "PASS",
        "checks": checks,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", action="append", type=Path, default=[])
    parser.add_argument("--source-prefix", default="PAPER_SOURCE_POSTGRES")
    parser.add_argument("--target-prefix", default="PAPER_TARGET_POSTGRES")
    parser.add_argument("--source-host-override")
    parser.add_argument("--source-port-override", type=int)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in (
        "apply-schema",
        "sync-catalog",
        "sync",
        "verify",
        "latency",
        "preflight",
    ):
        command = sub.add_parser(name)
        command.add_argument("--output", type=Path)
    sub.choices["sync"].add_argument("--core-only", action="store_true")
    sub.choices["verify"].add_argument("--core-only", action="store_true")
    sub.choices["latency"].add_argument("--samples", type=int, default=10)
    sub.choices["preflight"].add_argument("--require-private", action="store_true")
    sub.choices["preflight"].add_argument(
        "--allow-missing-migration", action="store_true"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _load_env_files(args.env_file)
    target = settings_from_prefix(args.target_prefix)
    if args.command == "apply-schema":
        payload = apply_schema(target)
    elif args.command == "sync-catalog":
        source = settings_from_prefix(
            args.source_prefix,
            host_override=args.source_host_override,
            port_override=args.source_port_override,
        )
        payload = sync_market_catalog(source, target)
    elif args.command == "sync":
        source = settings_from_prefix(
            args.source_prefix,
            host_override=args.source_host_override,
            port_override=args.source_port_override,
        )
        payload = sync_tables(
            source,
            target,
            tables=CORE_PARITY_TABLES if args.core_only else SNAPSHOT_TABLES,
        )
    elif args.command == "verify":
        source = settings_from_prefix(
            args.source_prefix,
            host_override=args.source_host_override,
            port_override=args.source_port_override,
        )
        payload = verify_parity(
            source,
            target,
            tables=CORE_PARITY_TABLES if args.core_only else SNAPSHOT_TABLES,
        )
    elif args.command == "latency":
        payload = latency_report(target, samples=args.samples)
    elif args.command == "preflight":
        payload = preflight(
            target,
            require_private=args.require_private,
            require_migration=not args.allow_missing_migration,
        )
    else:  # pragma: no cover
        raise AssertionError(args.command)
    _write_json(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 1 if payload["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
