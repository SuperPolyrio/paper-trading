"""Retail product layer around the tenant-scoped Paper trading core."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from quant.core.db import postgres_connection
from quant.risk.event_risk import evaluate_event_scenarios, load_event_risk_input

from .prediction_quality import PredictionObservation, build_prediction_quality_report
from .tenant_platform import PostgresTenantPlatformStore, TenantPrincipal


RETAIL_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_virtual_wallets (
        virtual_wallet_id TEXT PRIMARY KEY,
        tenant_id UUID NOT NULL REFERENCES quant.paper_tenants(tenant_id),
        owner_user_id UUID NOT NULL,
        account_id UUID NOT NULL,
        display_name TEXT NOT NULL,
        base_currency TEXT NOT NULL DEFAULT 'pUSD',
        initial_balance NUMERIC NOT NULL,
        wallet_generation INTEGER NOT NULL DEFAULT 0 CHECK (wallet_generation >= 0),
        status TEXT NOT NULL DEFAULT 'ACTIVE'
            CHECK (status IN ('ACTIVE','FROZEN','CLOSED')),
        is_default BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (tenant_id,virtual_wallet_id),
        UNIQUE (tenant_id,account_id),
        FOREIGN KEY (tenant_id,owner_user_id)
            REFERENCES quant.paper_users(tenant_id,user_id),
        FOREIGN KEY (tenant_id,account_id)
            REFERENCES quant.paper_account_registry(tenant_id,account_id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS paper_virtual_wallet_default_uq
    ON quant.paper_virtual_wallets(tenant_id,owner_user_id)
    WHERE is_default
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_identity_bindings (
        binding_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL REFERENCES quant.paper_tenants(tenant_id),
        user_id UUID NOT NULL,
        provider TEXT NOT NULL CHECK (provider IN ('GUEST','EVM')),
        provider_subject TEXT NOT NULL,
        wallet_address TEXT,
        verified_at TIMESTAMPTZ,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (provider,provider_subject),
        FOREIGN KEY (tenant_id,user_id)
            REFERENCES quant.paper_users(tenant_id,user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_wallet_login_challenges (
        challenge_id UUID PRIMARY KEY,
        wallet_address TEXT NOT NULL,
        nonce_hash TEXT NOT NULL CHECK (length(nonce_hash)=64),
        message TEXT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        consumed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_wallet_login_challenges_lookup_idx
    ON quant.paper_wallet_login_challenges(wallet_address,expires_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_user_watchlist (
        tenant_id UUID NOT NULL,
        user_id UUID NOT NULL,
        market_slug TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (tenant_id,user_id,market_slug),
        FOREIGN KEY (tenant_id,user_id)
            REFERENCES quant.paper_users(tenant_id,user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_market_recent_views (
        tenant_id UUID NOT NULL,
        user_id UUID NOT NULL,
        market_slug TEXT NOT NULL,
        viewed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        view_count INTEGER NOT NULL DEFAULT 1 CHECK (view_count > 0),
        PRIMARY KEY (tenant_id,user_id,market_slug),
        FOREIGN KEY (tenant_id,user_id)
            REFERENCES quant.paper_users(tenant_id,user_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_market_recent_views_owner_idx
    ON quant.paper_market_recent_views(tenant_id,user_id,viewed_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_user_preferences (
        tenant_id UUID NOT NULL,
        user_id UUID NOT NULL,
        locale TEXT NOT NULL DEFAULT 'zh-CN'
            CHECK (locale IN ('zh-CN','en-US')),
        timezone_name TEXT NOT NULL DEFAULT 'Asia/Shanghai',
        reduce_motion BOOLEAN NOT NULL DEFAULT FALSE,
        high_contrast BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (tenant_id,user_id),
        FOREIGN KEY (tenant_id,user_id)
            REFERENCES quant.paper_users(tenant_id,user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_prediction_journal (
        prediction_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        user_id UUID NOT NULL,
        virtual_wallet_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        market_id TEXT NOT NULL,
        market_slug TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'other',
        outcome_name TEXT NOT NULL,
        subjective_probability NUMERIC NOT NULL
            CHECK (subjective_probability >= 0 AND subjective_probability <= 1),
        confidence NUMERIC CHECK (confidence >= 0 AND confidence <= 1),
        thesis TEXT NOT NULL DEFAULT '',
        evidence_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
        invalidation_condition TEXT NOT NULL DEFAULT '',
        exit_plan TEXT NOT NULL DEFAULT '',
        decision_market_price NUMERIC NOT NULL,
        capital_at_risk NUMERIC NOT NULL DEFAULT 0 CHECK (capital_at_risk >= 0),
        decision_ts TIMESTAMPTZ NOT NULL,
        resolution_ts TIMESTAMPTZ,
        resolved_outcome NUMERIC CHECK (resolved_outcome IN (0,0.5,1)),
        final_pre_resolution_price NUMERIC,
        visibility TEXT NOT NULL DEFAULT 'PRIVATE'
            CHECK (visibility IN ('PRIVATE','DELAYED','PUBLIC')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (tenant_id,user_id)
            REFERENCES quant.paper_users(tenant_id,user_id),
        FOREIGN KEY (tenant_id,virtual_wallet_id)
            REFERENCES quant.paper_virtual_wallets(tenant_id,virtual_wallet_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_prediction_journal_owner_idx
    ON quant.paper_prediction_journal(tenant_id,user_id,decision_ts DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_event_relationships (
        relationship_id UUID PRIMARY KEY,
        event_id TEXT NOT NULL,
        source_node_id TEXT NOT NULL,
        target_node_id TEXT NOT NULL,
        relationship_type TEXT NOT NULL CHECK (
            relationship_type IN (
                'MUTUALLY_EXCLUSIVE','COLLECTIVELY_EXHAUSTIVE','IMPLIES',
                'OPPOSITE','SHARED_ORACLE','TEMPORALLY_NESTED'
            )
        ),
        source_kind TEXT NOT NULL CHECK (
            source_kind IN ('OFFICIAL','CONTRACT','CURATED','DERIVED_BINARY')
        ),
        source_hash TEXT NOT NULL CHECK (length(source_hash)=64),
        confidence NUMERIC NOT NULL DEFAULT 1
            CHECK (confidence >= 0 AND confidence <= 1),
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        observed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        valid_until TIMESTAMPTZ,
        UNIQUE (
            event_id,source_node_id,target_node_id,relationship_type,source_hash
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_event_relationships_event_idx
    ON quant.paper_event_relationships(event_id,relationship_type,observed_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_user_risk_profiles (
        tenant_id UUID NOT NULL,
        user_id UUID NOT NULL,
        virtual_wallet_id TEXT NOT NULL,
        max_order_notional NUMERIC,
        max_event_exposure NUMERIC,
        max_category_exposure NUMERIC,
        max_daily_loss NUMERIC,
        max_drawdown_pct NUMERIC,
        max_book_participation NUMERIC,
        max_orders_per_hour INTEGER,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (tenant_id,user_id,virtual_wallet_id),
        FOREIGN KEY (tenant_id,user_id)
            REFERENCES quant.paper_users(tenant_id,user_id),
        FOREIGN KEY (tenant_id,virtual_wallet_id)
            REFERENCES quant.paper_virtual_wallets(tenant_id,virtual_wallet_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_retail_order_admissions (
        admission_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        user_id UUID NOT NULL,
        virtual_wallet_id TEXT NOT NULL,
        request_key TEXT NOT NULL,
        intent_id BIGINT,
        asset_id TEXT NOT NULL,
        side TEXT NOT NULL,
        allowed BOOLEAN NOT NULL,
        reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
        observed JSONB NOT NULL DEFAULT '{}'::jsonb,
        profile_updated_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (tenant_id,request_key),
        FOREIGN KEY (tenant_id,user_id)
            REFERENCES quant.paper_users(tenant_id,user_id),
        FOREIGN KEY (tenant_id,virtual_wallet_id)
            REFERENCES quant.paper_virtual_wallets(tenant_id,virtual_wallet_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_retail_order_admissions_owner_idx
    ON quant.paper_retail_order_admissions(tenant_id,user_id,created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_user_notifications (
        notification_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        user_id UUID NOT NULL,
        notification_type TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'INFO'
            CHECK (severity IN ('INFO','WARNING','CRITICAL')),
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        resource_type TEXT,
        resource_id TEXT,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        read_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (tenant_id,user_id)
            REFERENCES quant.paper_users(tenant_id,user_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_user_notifications_owner_idx
    ON quant.paper_user_notifications(tenant_id,user_id,created_at DESC)
    """,
    "ALTER TABLE quant.paper_user_notifications ADD COLUMN IF NOT EXISTS source_key TEXT",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS paper_user_notifications_source_uq
    ON quant.paper_user_notifications(tenant_id,user_id,source_key)
    WHERE source_key IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_public_portfolios (
        virtual_wallet_id TEXT PRIMARY KEY,
        tenant_id UUID NOT NULL,
        user_id UUID NOT NULL,
        display_name TEXT NOT NULL,
        visibility TEXT NOT NULL DEFAULT 'PRIVATE'
            CHECK (visibility IN ('PRIVATE','DELAYED','PUBLIC')),
        disclosure_delay_minutes INTEGER NOT NULL DEFAULT 60
            CHECK (disclosure_delay_minutes >= 0),
        public_bio TEXT NOT NULL DEFAULT '',
        last_public_nav NUMERIC,
        last_public_return NUMERIC,
        last_public_drawdown_pct NUMERIC,
        last_brier NUMERIC,
        last_execution_score NUMERIC,
        metrics_as_of TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (tenant_id,user_id)
            REFERENCES quant.paper_users(tenant_id,user_id),
        FOREIGN KEY (tenant_id,virtual_wallet_id)
            REFERENCES quant.paper_virtual_wallets(tenant_id,virtual_wallet_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_portfolio_follows (
        follower_wallet_id TEXT NOT NULL,
        followed_wallet_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (follower_wallet_id,followed_wallet_id),
        CHECK (follower_wallet_id <> followed_wallet_id),
        FOREIGN KEY (follower_wallet_id)
            REFERENCES quant.paper_public_portfolios(virtual_wallet_id),
        FOREIGN KEY (followed_wallet_id)
            REFERENCES quant.paper_public_portfolios(virtual_wallet_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_competitions (
        competition_id UUID PRIMARY KEY,
        owner_tenant_id UUID NOT NULL,
        owner_user_id UUID NOT NULL,
        name TEXT NOT NULL,
        starts_at TIMESTAMPTZ NOT NULL,
        ends_at TIMESTAMPTZ NOT NULL,
        initial_balance NUMERIC NOT NULL CHECK (initial_balance > 0),
        market_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'DRAFT'
            CHECK (status IN ('DRAFT','OPEN','RUNNING','ENDED','CANCELLED')),
        rules_hash TEXT NOT NULL CHECK (length(rules_hash)=64),
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        CHECK (ends_at > starts_at),
        FOREIGN KEY (owner_tenant_id,owner_user_id)
            REFERENCES quant.paper_users(tenant_id,user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_competition_memberships (
        competition_id UUID NOT NULL REFERENCES quant.paper_competitions(competition_id),
        virtual_wallet_id TEXT NOT NULL REFERENCES quant.paper_virtual_wallets(virtual_wallet_id),
        joined_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        starting_nav NUMERIC NOT NULL,
        server_score JSONB NOT NULL DEFAULT '{}'::jsonb,
        score_as_of TIMESTAMPTZ,
        status TEXT NOT NULL DEFAULT 'ACTIVE'
            CHECK (status IN ('ACTIVE','DISQUALIFIED','WITHDRAWN')),
        PRIMARY KEY (competition_id,virtual_wallet_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_data_subject_requests (
        request_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        user_id UUID NOT NULL,
        request_type TEXT NOT NULL CHECK (request_type IN ('EXPORT','DELETE')),
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING','PROCESSING','COMPLETED','REJECTED')),
        reason TEXT NOT NULL DEFAULT '',
        artifact_path TEXT,
        artifact_sha256 TEXT,
        artifact_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
        runner_id TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        lease_expires_at TIMESTAMPTZ,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        completed_at TIMESTAMPTZ,
        FOREIGN KEY (tenant_id,user_id)
            REFERENCES quant.paper_users(tenant_id,user_id)
    )
    """,
    """
    ALTER TABLE quant.paper_data_subject_requests
    ADD COLUMN IF NOT EXISTS artifact_manifest JSONB NOT NULL DEFAULT '{}'::jsonb
    """,
    """
    ALTER TABLE quant.paper_data_subject_requests
    ADD COLUMN IF NOT EXISTS runner_id TEXT
    """,
    """
    ALTER TABLE quant.paper_data_subject_requests
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE quant.paper_data_subject_requests
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_history_sync_requests (
        request_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        user_id UUID NOT NULL,
        virtual_wallet_id TEXT NOT NULL,
        account_address TEXT NOT NULL,
        paper_strategy_id TEXT NOT NULL,
        shadow_strategy_id TEXT NOT NULL,
        window_start TIMESTAMPTZ NOT NULL,
        window_end TIMESTAMPTZ NOT NULL,
        comparison_scope TEXT NOT NULL DEFAULT 'OFFICIAL_ONLY'
            CHECK (comparison_scope IN ('OFFICIAL_ONLY','WHOLE_ACCOUNT')),
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING','RUNNING','PASS','DEGRADED','FAILED')),
        request_key TEXT NOT NULL,
        runner_id TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        lease_expires_at TIMESTAMPTZ,
        result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
        result_sha256 TEXT,
        error_code TEXT,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (tenant_id,user_id,request_key),
        CHECK (window_end > window_start),
        FOREIGN KEY (tenant_id,user_id)
            REFERENCES quant.paper_users(tenant_id,user_id),
        FOREIGN KEY (tenant_id,virtual_wallet_id)
            REFERENCES quant.paper_virtual_wallets(tenant_id,virtual_wallet_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_official_history_sync_queue_idx
    ON quant.paper_official_history_sync_requests(status,requested_at,request_id)
    """,
    """
    ALTER TABLE quant.paper_official_history_sync_requests
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE quant.paper_official_history_sync_requests
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ
    """,
)


TENANT_SCOPED_RETAIL_TABLES = (
    "paper_virtual_wallets",
    "paper_identity_bindings",
    "paper_user_watchlist",
    "paper_market_recent_views",
    "paper_prediction_journal",
    "paper_user_risk_profiles",
    "paper_retail_order_admissions",
    "paper_user_notifications",
    "paper_data_subject_requests",
    "paper_official_history_sync_requests",
)


def _decimal(value: Any, *, minimum: Decimal | None = None) -> Decimal | None:
    if value in (None, ""):
        return None
    selected = Decimal(str(value))
    if minimum is not None and selected < minimum:
        raise ValueError(f"value must be >= {minimum}")
    return selected


def _as_utc_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        selected = value
    else:
        raw = str(value or "").strip().replace("Z", "+00:00")
        if not raw:
            raise ValueError(f"{field} is required")
        try:
            selected = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 datetime") from exc
    if selected.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return selected.astimezone(timezone.utc)


def _stable_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def normalize_evm_address(value: str) -> str:
    address = str(value or "").strip().lower()
    if len(address) != 42 or not address.startswith("0x"):
        raise ValueError("wallet address must be a 20-byte EVM address")
    try:
        int(address[2:], 16)
    except ValueError as exc:
        raise ValueError("wallet address must be hexadecimal") from exc
    return address


class PostgresRetailPaperService:
    def __init__(
        self,
        connection_factory: Any = postgres_connection,
        *,
        tenant_store: PostgresTenantPlatformStore | None = None,
    ) -> None:
        self.connection_factory = connection_factory
        self.tenant_store = tenant_store or PostgresTenantPlatformStore(connection_factory)

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for statement in RETAIL_SCHEMA_STATEMENTS:
                cur.execute(statement)
            for table in TENANT_SCOPED_RETAIL_TABLES:
                cur.execute(f"ALTER TABLE quant.{table} ENABLE ROW LEVEL SECURITY")
                cur.execute(
                    f"DROP POLICY IF EXISTS paper_retail_tenant_isolation ON quant.{table}"
                )
                cur.execute(
                    f"""
                    CREATE POLICY paper_retail_tenant_isolation ON quant.{table}
                    USING (
                      tenant_id = NULLIF(
                        current_setting('app.current_tenant_id', true),''
                      )::uuid
                    )
                    WITH CHECK (
                      tenant_id = NULLIF(
                        current_setting('app.current_tenant_id', true),''
                      )::uuid
                    )
                    """
                )
            conn.commit()

    def provision_default_wallet(
        self,
        principal: TenantPrincipal,
        *,
        display_name: str = "My Paper Wallet",
        initial_balance: Decimal = Decimal("10000"),
        provider: str,
        provider_subject: str,
        wallet_address: str | None = None,
    ) -> dict[str, Any]:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT wallet.*,strategy.strategy_id
                FROM quant.paper_virtual_wallets wallet
                JOIN quant.paper_strategies strategy
                  ON strategy.tenant_id=wallet.tenant_id
                 AND strategy.account_id=wallet.account_id
                 AND strategy.idempotency_key='default'
                WHERE wallet.tenant_id=%s AND wallet.owner_user_id=%s
                  AND wallet.is_default
                """,
                (principal.tenant_id, principal.subject_user_id),
            )
            existing = cur.fetchone()
            conn.commit()
        if existing is not None:
            return dict(existing)

        account = self.tenant_store.create_account(
            principal,
            name=str(display_name).strip() or "My Paper Wallet",
            idempotency_key="retail-default-wallet-v1",
            initial_cash=initial_balance,
        )
        wallet_uuid = uuid5(
            principal.tenant_id, f"paper-virtual-wallet:{principal.subject_user_id}:default"
        )
        virtual_wallet_id = f"pwallet_{wallet_uuid.hex[:26]}"
        selected_provider = str(provider).upper()
        if selected_provider not in {"GUEST", "EVM"}:
            raise ValueError("provider must be GUEST or EVM")
        normalized_address = (
            normalize_evm_address(wallet_address) if wallet_address else None
        )
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                INSERT INTO quant.paper_virtual_wallets (
                    virtual_wallet_id,tenant_id,owner_user_id,account_id,
                    display_name,initial_balance
                ) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id,account_id) DO UPDATE SET
                    display_name=EXCLUDED.display_name,
                    updated_at=clock_timestamp()
                RETURNING *
                """,
                (
                    virtual_wallet_id,
                    principal.tenant_id,
                    principal.subject_user_id,
                    account["account_id"],
                    str(display_name).strip() or "My Paper Wallet",
                    initial_balance,
                ),
            )
            wallet = dict(cur.fetchone())
            cur.execute(
                """
                INSERT INTO quant.paper_identity_bindings (
                    binding_id,tenant_id,user_id,provider,provider_subject,
                    wallet_address,verified_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (provider,provider_subject) DO UPDATE SET
                    wallet_address=EXCLUDED.wallet_address,
                    verified_at=EXCLUDED.verified_at,
                    updated_at=clock_timestamp()
                """,
                (
                    uuid4(),
                    principal.tenant_id,
                    principal.subject_user_id,
                    selected_provider,
                    str(provider_subject),
                    normalized_address,
                    datetime.now(timezone.utc) if selected_provider == "EVM" else None,
                ),
            )
            cur.execute(
                """
                SELECT strategy_id FROM quant.paper_strategies
                WHERE tenant_id=%s AND account_id=%s AND idempotency_key='default'
                """,
                (principal.tenant_id, account["account_id"]),
            )
            wallet["strategy_id"] = cur.fetchone()["strategy_id"]
            conn.commit()
            return wallet

    @staticmethod
    def _account_read_permission() -> Any:
        from .tenant_platform import PaperPermission

        return PaperPermission.ACCOUNT_READ

    def get_default_wallet(self, principal: TenantPrincipal) -> dict[str, Any]:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT wallet.*,strategy.strategy_id,account.ledger_strategy_id,
                       ledger.cash_balance,ledger.cash_reserved,ledger.realized_pnl
                FROM quant.paper_virtual_wallets wallet
                JOIN quant.paper_account_registry account
                  ON account.tenant_id=wallet.tenant_id
                 AND account.account_id=wallet.account_id
                JOIN quant.paper_accounts ledger
                  ON ledger.strategy_id=account.ledger_strategy_id
                JOIN quant.paper_strategies strategy
                  ON strategy.tenant_id=wallet.tenant_id
                 AND strategy.account_id=wallet.account_id
                 AND strategy.idempotency_key='default'
                WHERE wallet.tenant_id=%s AND wallet.owner_user_id=%s
                  AND wallet.is_default
                """,
                (principal.tenant_id, principal.subject_user_id),
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise LookupError("default virtual wallet does not exist")
        return dict(row)

    def list_wallets(self, principal: TenantPrincipal) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT wallet.*,strategy.strategy_id,account.ledger_strategy_id,
                       ledger.cash_balance,ledger.cash_reserved,ledger.realized_pnl
                FROM quant.paper_virtual_wallets wallet
                JOIN quant.paper_account_registry account
                  ON account.tenant_id=wallet.tenant_id
                 AND account.account_id=wallet.account_id
                JOIN quant.paper_accounts ledger
                  ON ledger.strategy_id=account.ledger_strategy_id
                JOIN quant.paper_strategies strategy
                  ON strategy.tenant_id=wallet.tenant_id
                 AND strategy.account_id=wallet.account_id
                 AND strategy.idempotency_key='default'
                WHERE wallet.tenant_id=%s AND wallet.owner_user_id=%s
                ORDER BY wallet.is_default DESC,wallet.created_at,wallet.virtual_wallet_id
                """,
                (principal.tenant_id, principal.subject_user_id),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return rows

    def set_default_wallet(
        self, principal: TenantPrincipal, virtual_wallet_id: str
    ) -> dict[str, Any]:
        selected = str(virtual_wallet_id).strip()
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT virtual_wallet_id FROM quant.paper_virtual_wallets
                WHERE tenant_id=%s AND owner_user_id=%s AND virtual_wallet_id=%s
                  AND status='ACTIVE'
                FOR UPDATE
                """,
                (principal.tenant_id, principal.subject_user_id, selected),
            )
            if cur.fetchone() is None:
                raise LookupError("virtual wallet was not found")
            cur.execute(
                """
                UPDATE quant.paper_virtual_wallets SET is_default=FALSE,
                       updated_at=clock_timestamp()
                WHERE tenant_id=%s AND owner_user_id=%s AND is_default
                """,
                (principal.tenant_id, principal.subject_user_id),
            )
            cur.execute(
                """
                UPDATE quant.paper_virtual_wallets SET is_default=TRUE,
                       updated_at=clock_timestamp()
                WHERE tenant_id=%s AND owner_user_id=%s AND virtual_wallet_id=%s
                """,
                (principal.tenant_id, principal.subject_user_id, selected),
            )
            conn.commit()
        return self.get_default_wallet(principal)

    def fork_virtual_wallet(
        self,
        principal: TenantPrincipal,
        *,
        name: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        parent = self.get_default_wallet(principal)
        account = self.tenant_store.fork_account(
            principal,
            parent_account_id=UUID(str(parent["account_id"])),
            name=str(name).strip() or f"{parent['display_name']} copy",
            idempotency_key=f"retail-wallet-fork:{idempotency_key}",
        )
        wallet_uuid = uuid5(
            principal.tenant_id,
            f"paper-virtual-wallet:{principal.subject_user_id}:fork:{idempotency_key}",
        )
        virtual_wallet_id = f"pwallet_{wallet_uuid.hex[:26]}"
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT initial_cash FROM quant.paper_accounts WHERE strategy_id=%s
                """,
                (str(account["ledger_strategy_id"]),),
            )
            initial_cash = cur.fetchone()["initial_cash"]
            cur.execute(
                """
                INSERT INTO quant.paper_virtual_wallets (
                    virtual_wallet_id,tenant_id,owner_user_id,account_id,
                    display_name,initial_balance,is_default,wallet_generation
                ) VALUES (%s,%s,%s,%s,%s,%s,FALSE,%s)
                ON CONFLICT (tenant_id,account_id) DO UPDATE SET
                    display_name=EXCLUDED.display_name,
                    updated_at=clock_timestamp()
                RETURNING *
                """,
                (
                    virtual_wallet_id,
                    principal.tenant_id,
                    principal.subject_user_id,
                    account["account_id"],
                    str(name).strip() or f"{parent['display_name']} copy",
                    initial_cash,
                    int(account["current_generation"]),
                ),
            )
            wallet = dict(cur.fetchone())
            cur.execute(
                """
                SELECT strategy_id FROM quant.paper_strategies
                WHERE tenant_id=%s AND account_id=%s AND idempotency_key='default'
                """,
                (principal.tenant_id, account["account_id"]),
            )
            wallet["strategy_id"] = cur.fetchone()["strategy_id"]
            wallet["ledger_strategy_id"] = account["ledger_strategy_id"]
            conn.commit()
        return wallet

    def reset_virtual_wallet(
        self,
        principal: TenantPrincipal,
        *,
        confirm_virtual_wallet_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        parent = self.get_default_wallet(principal)
        if str(confirm_virtual_wallet_id) != str(parent["virtual_wallet_id"]):
            raise ValueError("confirm_virtual_wallet_id does not match default wallet")
        generation = int(parent["wallet_generation"]) + 1
        name = f"{parent['display_name']} reset {generation}"
        account = self.tenant_store.reset_account(
            principal,
            parent_account_id=UUID(str(parent["account_id"])),
            name=name,
            idempotency_key=f"retail-wallet-reset:{idempotency_key}",
        )
        wallet_uuid = uuid5(
            principal.tenant_id,
            f"paper-virtual-wallet:{principal.subject_user_id}:reset:{idempotency_key}",
        )
        virtual_wallet_id = f"pwallet_{wallet_uuid.hex[:26]}"
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                UPDATE quant.paper_virtual_wallets
                SET is_default=FALSE,status='FROZEN',updated_at=clock_timestamp()
                WHERE tenant_id=%s AND owner_user_id=%s
                  AND virtual_wallet_id=%s
                """,
                (
                    principal.tenant_id,
                    principal.subject_user_id,
                    parent["virtual_wallet_id"],
                ),
            )
            cur.execute(
                """
                INSERT INTO quant.paper_virtual_wallets (
                    virtual_wallet_id,tenant_id,owner_user_id,account_id,
                    display_name,initial_balance,is_default,wallet_generation
                ) VALUES (%s,%s,%s,%s,%s,%s,TRUE,%s)
                ON CONFLICT (tenant_id,account_id) DO UPDATE SET
                    is_default=TRUE,updated_at=clock_timestamp()
                RETURNING *
                """,
                (
                    virtual_wallet_id,
                    principal.tenant_id,
                    principal.subject_user_id,
                    account["account_id"],
                    name,
                    parent["initial_balance"],
                    generation,
                ),
            )
            wallet = dict(cur.fetchone())
            cur.execute(
                """
                SELECT strategy_id FROM quant.paper_strategies
                WHERE tenant_id=%s AND account_id=%s AND idempotency_key='default'
                """,
                (principal.tenant_id, account["account_id"]),
            )
            wallet["strategy_id"] = cur.fetchone()["strategy_id"]
            wallet["ledger_strategy_id"] = account["ledger_strategy_id"]
            wallet["reset_from_virtual_wallet_id"] = parent["virtual_wallet_id"]
            conn.commit()
        return wallet

    def issue_wallet_challenge(
        self,
        *,
        wallet_address: str,
        domain: str,
        ttl: timedelta = timedelta(minutes=5),
    ) -> dict[str, Any]:
        address = normalize_evm_address(wallet_address)
        now = datetime.now(timezone.utc)
        nonce = secrets.token_urlsafe(24)
        challenge_id = uuid4()
        expires_at = now + ttl
        message = (
            "Polymarket Paper sign-in\n"
            f"Domain: {str(domain).strip()}\n"
            f"Address: {address}\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {now.isoformat()}\n"
            f"Expiration Time: {expires_at.isoformat()}\n\n"
            "This signature creates a chain-off paper wallet only. "
            "It never authorizes a real order or asset transfer."
        )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_wallet_login_challenges (
                    challenge_id,wallet_address,nonce_hash,message,expires_at
                ) VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    challenge_id,
                    address,
                    hashlib.sha256(nonce.encode()).hexdigest(),
                    message,
                    expires_at,
                ),
            )
            conn.commit()
        return {
            "challenge_id": challenge_id,
            "wallet_address": address,
            "message": message,
            "expires_at": expires_at,
        }

    def consume_wallet_challenge(
        self,
        *,
        challenge_id: UUID,
        wallet_address: str,
        signature: str,
    ) -> str:
        address = normalize_evm_address(wallet_address)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.paper_wallet_login_challenges
                WHERE challenge_id=%s FOR UPDATE
                """,
                (challenge_id,),
            )
            row = cur.fetchone()
            if (
                row is None
                or str(row["wallet_address"]) != address
                or row["consumed_at"] is not None
                or row["expires_at"] <= datetime.now(timezone.utc)
            ):
                conn.rollback()
                raise ValueError("wallet challenge is invalid or expired")
            try:
                from eth_account import Account
                from eth_account.messages import encode_defunct
            except ImportError as exc:  # pragma: no cover - deployment gate
                conn.rollback()
                raise RuntimeError("eth-account is required for wallet login") from exc
            recovered = Account.recover_message(
                encode_defunct(text=str(row["message"])), signature=str(signature)
            ).lower()
            if recovered != address:
                conn.rollback()
                raise ValueError("wallet signature does not match the requested address")
            cur.execute(
                """
                UPDATE quant.paper_wallet_login_challenges
                SET consumed_at=clock_timestamp()
                WHERE challenge_id=%s
                """,
                (challenge_id,),
            )
            conn.commit()
        return address

    def list_markets(
        self,
        *,
        principal: TenantPrincipal | None = None,
        query: str = "",
        category: str | None = None,
        state: str = "LIVE",
        collection: str = "all",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200 or offset < 0:
            raise ValueError("invalid market pagination")
        selected_query = str(query).strip()
        selected_category = str(category).strip() if category else None
        selected_state = str(state or "LIVE").upper()
        selected_collection = str(collection or "all").strip().casefold()
        if selected_collection not in {
            "all",
            "watchlist",
            "recent",
            "positions",
            "hot",
            "new",
            "ending",
        }:
            raise ValueError("invalid market collection")
        collection_slugs, activity_by_slug = self._market_collection_scope(
            principal=principal,
            collection=selected_collection,
        )
        if collection_slugs == []:
            return []
        candidate_limit = min(
            10000,
            max(200, limit * 20, len(collection_slugs or ()) * 2),
        )
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT token.asset_id,token.condition_id,token.market_id,
                       token.market_slug,token.market_title,token.outcome_name,
                       token.market_state,token.execution_eligible,
                       COALESCE(meta.category,
                                token.raw_metadata->>'category') AS category,
                       COALESCE(meta.event_id,
                                token.raw_metadata->>'event_id',
                                token.raw_metadata->>'eventId',
                                token.condition_id) AS event_id,
                       meta.end_date,meta.event_title,meta.created_at,
                       best_bid,best_ask,current_tick_size,min_order_size,
                       book_quality,book_age_ms,
                       COALESCE(meta.enable_neg_risk,
                                lower(COALESCE(token.raw_metadata->>'enable_neg_risk',''))
                                  IN ('true','1','yes'),
                                lower(COALESCE(token.raw_metadata->>'enableNegRisk',''))
                                  IN ('true','1','yes'),
                                FALSE) AS enable_neg_risk,
                       token.updated_at
                FROM quant.paper_market_registry_tokens token
                LEFT JOIN LATERAL (
                    SELECT market.category,market.event_id,
                           market.enable_neg_risk,market.end_date,
                           market.event_title,market.created_at
                    FROM core.markets market
                    WHERE (token.condition_id IS NOT NULL
                           AND market.condition_id=token.condition_id)
                       OR (token.gamma_market_id IS NOT NULL
                           AND market.gamma_market_id=token.gamma_market_id)
                       OR (token.market_slug IS NOT NULL
                           AND market.slug=token.market_slug)
                    ORDER BY CASE
                        WHEN market.condition_id=token.condition_id THEN 0
                        WHEN market.gamma_market_id=token.gamma_market_id THEN 1
                        ELSE 2
                    END,market.id DESC
                    LIMIT 1
                ) meta ON TRUE
                WHERE (%s='' OR market_title ILIKE '%%'||%s||'%%'
                              OR market_slug ILIKE '%%'||%s||'%%'
                              OR COALESCE(meta.event_title,'') ILIKE '%%'||%s||'%%')
                  AND (%s='ALL' OR market_state=%s)
                  AND (%s::text[] IS NULL OR market_slug=ANY(%s::text[]))
                ORDER BY
                  CASE WHEN %s='NEW' THEN meta.created_at END DESC NULLS LAST,
                  CASE WHEN %s='ENDING' THEN meta.end_date END ASC NULLS LAST,
                  CASE WHEN execution_eligible THEN 0 ELSE 1 END,
                  COALESCE(book_age_ms,9223372036854775807),market_slug,outcome_name
                LIMIT %s
                """,
                (
                    selected_query,
                    selected_query,
                    selected_query,
                    selected_query,
                    selected_state,
                    selected_state,
                    collection_slugs,
                    collection_slugs,
                    selected_collection.upper(),
                    selected_collection.upper(),
                    candidate_limit,
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
        grouped = self._group_markets(rows, limit=10000)
        for market in grouped:
            activity = activity_by_slug.get(str(market["market_slug"]), {})
            market["observed_trade_count_24h"] = int(
                activity.get("trade_count") or 0
            )
            market["observed_trade_size_24h"] = _decimal(
                activity.get("trade_size") or 0
            )
            market["activity_source"] = (
                "LOCAL_OBSERVED_TRADE_TAPE_24H"
                if selected_collection == "hot"
                else None
            )
        if selected_category:
            grouped = [
                market
                for market in grouped
                if str(market["category"]).casefold()
                == selected_category.casefold()
            ]
        if selected_collection == "new":
            grouped.sort(
                key=lambda market: market.get("created_at")
                or market.get("updated_at")
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
        elif selected_collection == "ending":
            now = datetime.now(timezone.utc)
            grouped = [
                market
                for market in grouped
                if isinstance(market.get("end_date"), datetime)
                and market["end_date"] > now
            ]
            grouped.sort(key=lambda market: market["end_date"])
        elif selected_collection == "hot":
            grouped.sort(
                key=lambda market: (
                    int(market["observed_trade_count_24h"]),
                    _decimal(market["observed_trade_size_24h"]) or Decimal(0),
                ),
                reverse=True,
            )
        return grouped[offset : offset + limit]

    def get_market(
        self, market_slug: str, *, principal: TenantPrincipal | None = None
    ) -> dict[str, Any]:
        slug = str(market_slug).strip()
        if not slug:
            raise ValueError("market_slug is required")
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT token.asset_id,token.condition_id,token.market_id,
                       token.market_slug,token.market_title,token.outcome_name,
                       token.market_state,token.execution_eligible,
                       COALESCE(meta.category,
                                token.raw_metadata->>'category') AS category,
                       COALESCE(meta.event_id,
                                token.raw_metadata->>'event_id',
                                token.raw_metadata->>'eventId',
                                token.condition_id) AS event_id,
                       token.best_bid,token.best_ask,
                       token.current_tick_size,token.min_order_size,
                       token.book_quality,token.book_age_ms,
                       COALESCE(meta.enable_neg_risk,
                                lower(COALESCE(token.raw_metadata->>'enable_neg_risk',''))
                                  IN ('true','1','yes'),
                                lower(COALESCE(token.raw_metadata->>'enableNegRisk',''))
                                  IN ('true','1','yes'),
                                FALSE) AS enable_neg_risk,
                       token.updated_at,book.bids,book.asks,
                       book.observed_at AS book_observed_at,
                       book.transport_state,
                       book.book_fingerprint AS checkpoint_id
                FROM quant.paper_market_registry_tokens token
                LEFT JOIN LATERAL (
                    SELECT market.category,market.event_id,
                           market.enable_neg_risk
                    FROM core.markets market
                    WHERE (token.condition_id IS NOT NULL
                           AND market.condition_id=token.condition_id)
                       OR (token.gamma_market_id IS NOT NULL
                           AND market.gamma_market_id=token.gamma_market_id)
                       OR (token.market_slug IS NOT NULL
                           AND market.slug=token.market_slug)
                    ORDER BY CASE
                        WHEN market.condition_id=token.condition_id THEN 0
                        WHEN market.gamma_market_id=token.gamma_market_id THEN 1
                        ELSE 2
                    END,market.id DESC
                    LIMIT 1
                ) meta ON TRUE
                LEFT JOIN quant.paper_live_current_books book
                  ON book.asset_id=token.asset_id
                WHERE token.market_slug=%s
                ORDER BY token.outcome_name,token.asset_id
                """,
                (slug,),
            )
            rows = [dict(row) for row in cur.fetchall()]
            condition_ids = sorted(
                {str(row["condition_id"]) for row in rows if row.get("condition_id")}
            )
            asset_ids = sorted(
                {str(row["asset_id"]) for row in rows if row.get("asset_id")}
            )
            cur.execute(
                """
                SELECT description,oracle,end_date,tags,event_title,event_slug
                FROM core.markets
                WHERE slug=%s
                ORDER BY id DESC LIMIT 1
                """,
                (slug,),
            )
            market_metadata_row = cur.fetchone()
            market_metadata = (
                dict(market_metadata_row) if market_metadata_row is not None else {}
            )
            terms_by_asset: dict[str, dict[str, Any]] = {}
            if asset_ids:
                cur.execute(
                    """
                    SELECT DISTINCT ON (asset_id) asset_id,fee_rate_bps,fee_rate,
                           fee_exponent,fee_taker_only,itode,seconds_delay,
                           taker_delay_ms,delay_source,source,observed_at,expires_at
                    FROM quant.paper_market_terms
                    WHERE asset_id=ANY(%s::text[])
                    ORDER BY asset_id,observed_at DESC
                    """,
                    (asset_ids,),
                )
                terms_by_asset = {
                    str(row["asset_id"]): dict(row) for row in cur.fetchall()
                }
            lifecycle: dict[str, Any] = {}
            clarifications: list[dict[str, Any]] = []
            if condition_ids:
                cur.execute(
                    """
                    SELECT * FROM quant.market_resolution_states
                    WHERE condition_id=ANY(%s::text[])
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (condition_ids,),
                )
                lifecycle_row = cur.fetchone()
                lifecycle = dict(lifecycle_row) if lifecycle_row is not None else {}
                cur.execute(
                    """
                    SELECT source_event_id,clarified_at,payload_hash,payload,
                           canceled_intent_ids
                    FROM quant.paper_market_clarifications
                    WHERE condition_id=ANY(%s::text[])
                    ORDER BY clarified_at DESC LIMIT 20
                    """,
                    (condition_ids,),
                )
                clarifications = [dict(row) for row in cur.fetchall()]
            recent_trades: list[dict[str, Any]] = []
            if asset_ids:
                cur.execute(
                    """
                    SELECT asset_id,price,size,aggressor_side,event_ts,
                           transaction_hash
                    FROM quant.paper_live_maker_trade_events
                    WHERE asset_id=ANY(%s::text[])
                      AND processing_state IN ('PROCESSED','PENDING')
                    ORDER BY event_ts DESC,event_id DESC LIMIT 50
                    """,
                    (asset_ids,),
                )
                recent_trades = [dict(row) for row in cur.fetchall()]
        if not rows:
            raise LookupError("market was not found")
        market = self._group_markets(rows, limit=1)[0]
        market.update(
            {
                "description": market_metadata.get("description"),
                "oracle": market_metadata.get("oracle"),
                "end_date": market_metadata.get("end_date"),
                "tags": market_metadata.get("tags") or [],
                "event_title": market_metadata.get("event_title"),
                "event_slug": market_metadata.get("event_slug"),
                "lifecycle": lifecycle,
                "clarifications": clarifications,
                "recent_trades": recent_trades,
            }
        )
        for outcome in market["outcomes"]:
            outcome["terms"] = terms_by_asset.get(str(outcome["asset_id"]))
        if principal is not None:
            self.record_market_view(principal, market_slug=slug)
        return market

    def record_market_view(
        self, principal: TenantPrincipal, *, market_slug: str
    ) -> None:
        slug = str(market_slug).strip()
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                INSERT INTO quant.paper_market_recent_views (
                    tenant_id,user_id,market_slug
                ) VALUES (%s,%s,%s)
                ON CONFLICT (tenant_id,user_id,market_slug) DO UPDATE SET
                    viewed_at=clock_timestamp(),
                    view_count=quant.paper_market_recent_views.view_count+1
                """,
                (principal.tenant_id, principal.subject_user_id, slug),
            )
            conn.commit()

    def _market_collection_scope(
        self,
        *,
        principal: TenantPrincipal | None,
        collection: str,
    ) -> tuple[list[str] | None, dict[str, dict[str, Any]]]:
        if collection in {"all", "new", "ending"}:
            return None, {}
        if collection == "hot":
            with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT registry.market_slug,count(*) AS trade_count,
                           sum(trade.size) AS trade_size
                    FROM quant.paper_live_maker_trade_events trade
                    JOIN quant.paper_market_registry_tokens registry
                      ON registry.asset_id=trade.asset_id
                    WHERE trade.event_ts>=clock_timestamp()-interval '24 hours'
                      AND trade.processing_state IN ('PROCESSED','PENDING')
                      AND registry.market_slug IS NOT NULL
                    GROUP BY registry.market_slug
                    ORDER BY count(*) DESC,sum(trade.size) DESC
                    LIMIT 10000
                    """
                )
                rows = [dict(row) for row in cur.fetchall()]
            return [str(row["market_slug"]) for row in rows], {
                str(row["market_slug"]): row for row in rows
            }
        if principal is None:
            raise ValueError(f"market collection {collection} requires a user session")
        wallet = self.get_default_wallet(principal)
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            if collection == "watchlist":
                cur.execute(
                    """
                    SELECT market_slug FROM quant.paper_user_watchlist
                    WHERE tenant_id=%s AND user_id=%s
                    ORDER BY created_at DESC,market_slug
                    """,
                    (principal.tenant_id, principal.subject_user_id),
                )
            elif collection == "recent":
                cur.execute(
                    """
                    SELECT market_slug FROM quant.paper_market_recent_views
                    WHERE tenant_id=%s AND user_id=%s
                    ORDER BY viewed_at DESC,market_slug LIMIT 500
                    """,
                    (principal.tenant_id, principal.subject_user_id),
                )
            else:
                cur.execute(
                    """
                    SELECT DISTINCT registry.market_slug
                    FROM quant.paper_positions position
                    JOIN quant.paper_market_registry_tokens registry
                      ON registry.asset_id=position.asset_id
                    WHERE position.strategy_id=%s AND position.quantity<>0
                      AND registry.market_slug IS NOT NULL
                    ORDER BY registry.market_slug
                    """,
                    (str(wallet["ledger_strategy_id"]),),
                )
            rows = [str(row["market_slug"]) for row in cur.fetchall()]
            conn.commit()
        return rows, {}

    @staticmethod
    def _group_markets(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            slug = str(row.get("market_slug") or row.get("condition_id") or "")
            market = grouped.setdefault(
                slug,
                {
                    "market_slug": slug,
                    "market_id": row.get("market_id"),
                    "condition_id": row.get("condition_id"),
                    "title": row.get("market_title") or slug,
                    "event_id": row.get("event_id"),
                    "category": PostgresRetailPaperService._market_category(row),
                    "state": row.get("market_state"),
                    "end_date": row.get("end_date"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "event_title": row.get("event_title"),
                    "enable_neg_risk": bool(row.get("enable_neg_risk")),
                    "outcomes": [],
                },
            )
            market["outcomes"].append(
                {
                    "asset_id": str(row.get("asset_id") or ""),
                    "name": row.get("outcome_name"),
                    "best_bid": row.get("best_bid"),
                    "best_ask": row.get("best_ask"),
                    "tick_size": row.get("current_tick_size"),
                    "min_order_size": row.get("min_order_size"),
                    "book_quality": row.get("book_quality"),
                    "book_age_ms": row.get("book_age_ms"),
                    "execution_eligible": bool(row.get("execution_eligible")),
                    "bids": row.get("bids"),
                    "asks": row.get("asks"),
                    "book_observed_at": row.get("book_observed_at"),
                    "transport_state": row.get("transport_state"),
                    "checkpoint_id": row.get("checkpoint_id"),
                }
            )
            if isinstance(row.get("updated_at"), datetime) and (
                not isinstance(market.get("updated_at"), datetime)
                or row["updated_at"] > market["updated_at"]
            ):
                market["updated_at"] = row["updated_at"]
        return list(grouped.values())[:limit]

    @staticmethod
    def _market_category(row: Mapping[str, Any]) -> str:
        explicit = str(row.get("category") or "").strip().casefold()
        aliases = {
            "politics": "politics",
            "political": "politics",
            "sports": "sports",
            "sport": "sports",
            "weather": "weather",
            "climate": "weather",
            "crypto": "crypto",
            "cryptocurrency": "crypto",
        }
        if explicit:
            return aliases.get(explicit, explicit)
        text = f"{row.get('market_slug') or ''} {row.get('market_title') or ''}".casefold()
        keyword_groups = (
            ("crypto", ("bitcoin", "btc", "ethereum", "eth", "solana", "crypto")),
            ("weather", ("temperature", "weather", "rain", "snow", "hurricane", "storm")),
            ("sports", ("nba", "nfl", "mlb", "nhl", "soccer", "tennis", "corners", "match", "game")),
            ("politics", ("election", "president", "congress", "senate", "governor", "prime-minister")),
        )
        for category, keywords in keyword_groups:
            if any(keyword in text for keyword in keywords):
                return category
        return "other"

    def set_watchlist(
        self, principal: TenantPrincipal, *, market_slug: str, enabled: bool
    ) -> bool:
        slug = str(market_slug).strip()
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            if enabled:
                cur.execute(
                    """
                    INSERT INTO quant.paper_user_watchlist (tenant_id,user_id,market_slug)
                    VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
                    """,
                    (principal.tenant_id, principal.subject_user_id, slug),
                )
            else:
                cur.execute(
                    """
                    DELETE FROM quant.paper_user_watchlist
                    WHERE tenant_id=%s AND user_id=%s AND market_slug=%s
                    """,
                    (principal.tenant_id, principal.subject_user_id, slug),
                )
            conn.commit()
        return enabled

    def list_watchlist(self, principal: TenantPrincipal) -> list[str]:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT market_slug FROM quant.paper_user_watchlist
                WHERE tenant_id=%s AND user_id=%s ORDER BY created_at DESC
                """,
                (principal.tenant_id, principal.subject_user_id),
            )
            rows = [str(row["market_slug"]) for row in cur.fetchall()]
            conn.commit()
        return rows

    def get_preferences(self, principal: TenantPrincipal) -> dict[str, Any]:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                INSERT INTO quant.paper_user_preferences (tenant_id,user_id)
                VALUES (%s,%s) ON CONFLICT (tenant_id,user_id) DO NOTHING
                """,
                (principal.tenant_id, principal.subject_user_id),
            )
            cur.execute(
                """
                SELECT locale,timezone_name,reduce_motion,high_contrast,updated_at
                FROM quant.paper_user_preferences
                WHERE tenant_id=%s AND user_id=%s
                """,
                (principal.tenant_id, principal.subject_user_id),
            )
            row = dict(cur.fetchone())
            conn.commit()
        return row

    def update_preferences(
        self, principal: TenantPrincipal, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        locale = str(payload.get("locale") or "zh-CN")
        if locale not in {"zh-CN", "en-US"}:
            raise ValueError("locale must be zh-CN or en-US")
        timezone_name = str(payload.get("timezone_name") or "Asia/Shanghai")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone_name must be a valid IANA timezone") from exc
        reduce_motion = _parse_bool(payload.get("reduce_motion"), default=False)
        high_contrast = _parse_bool(payload.get("high_contrast"), default=False)
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                INSERT INTO quant.paper_user_preferences (
                    tenant_id,user_id,locale,timezone_name,reduce_motion,
                    high_contrast
                ) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id,user_id) DO UPDATE SET
                    locale=EXCLUDED.locale,
                    timezone_name=EXCLUDED.timezone_name,
                    reduce_motion=EXCLUDED.reduce_motion,
                    high_contrast=EXCLUDED.high_contrast,
                    updated_at=clock_timestamp()
                RETURNING locale,timezone_name,reduce_motion,high_contrast,
                          updated_at
                """,
                (
                    principal.tenant_id,
                    principal.subject_user_id,
                    locale,
                    timezone_name,
                    reduce_motion,
                    high_contrast,
                ),
            )
            row = dict(cur.fetchone())
            conn.commit()
        return row

    def create_prediction(
        self, principal: TenantPrincipal, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        wallet = self.get_default_wallet(principal)
        probability = _decimal(payload.get("subjective_probability"), minimum=Decimal("0"))
        if probability is None or probability > 1:
            raise ValueError("subjective_probability must be within [0,1]")
        decision_price = _decimal(payload.get("decision_market_price"), minimum=Decimal("0"))
        if decision_price is None or decision_price > 1:
            raise ValueError("decision_market_price must be within [0,1]")
        decision_ts = payload.get("decision_ts") or datetime.now(timezone.utc)
        if isinstance(decision_ts, str):
            decision_ts = datetime.fromisoformat(decision_ts.replace("Z", "+00:00"))
        if not isinstance(decision_ts, datetime) or decision_ts.tzinfo is None:
            raise ValueError("decision_ts must include a timezone")
        prediction_id = uuid4()
        evidence = payload.get("evidence_sources") or []
        if not isinstance(evidence, list):
            raise ValueError("evidence_sources must be an array")
        visibility = str(payload.get("visibility") or "PRIVATE").upper()
        if visibility not in {"PRIVATE", "DELAYED", "PUBLIC"}:
            raise ValueError("prediction visibility is invalid")
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                INSERT INTO quant.paper_prediction_journal (
                    prediction_id,tenant_id,user_id,virtual_wallet_id,event_id,
                    market_id,market_slug,category,outcome_name,
                    subjective_probability,confidence,thesis,evidence_sources,
                    invalidation_condition,exit_plan,decision_market_price,
                    capital_at_risk,decision_ts,visibility
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                    %s,%s,%s,%s,%s,%s
                ) RETURNING *
                """,
                (
                    prediction_id,
                    principal.tenant_id,
                    principal.subject_user_id,
                    wallet["virtual_wallet_id"],
                    str(payload.get("event_id") or payload.get("market_id") or ""),
                    str(payload.get("market_id") or ""),
                    str(payload.get("market_slug") or ""),
                    str(payload.get("category") or "other"),
                    str(payload.get("outcome_name") or ""),
                    probability,
                    _decimal(payload.get("confidence"), minimum=Decimal("0")),
                    str(payload.get("thesis") or ""),
                    json.dumps(evidence, sort_keys=True),
                    str(payload.get("invalidation_condition") or ""),
                    str(payload.get("exit_plan") or ""),
                    decision_price,
                    _decimal(payload.get("capital_at_risk"), minimum=Decimal("0")) or 0,
                    decision_ts.astimezone(timezone.utc),
                    visibility,
                ),
            )
            result = dict(cur.fetchone())
            conn.commit()
        return result

    def list_predictions(self, principal: TenantPrincipal, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT * FROM quant.paper_prediction_journal
                WHERE tenant_id=%s AND user_id=%s
                ORDER BY decision_ts DESC,prediction_id LIMIT %s
                """,
                (principal.tenant_id, principal.subject_user_id, limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return rows

    def prediction_report(self, principal: TenantPrincipal) -> dict[str, Any]:
        rows = self.list_predictions(principal, limit=10000)
        observations = []
        for row in rows:
            if row.get("resolved_outcome") is None or row.get("resolution_ts") is None:
                continue
            observations.append(
                PredictionObservation(
                    event_id=str(row["event_id"]),
                    market_id=str(row["market_id"]),
                    category=str(row["category"]),
                    decision_ts=row["decision_ts"],
                    resolution_ts=row["resolution_ts"],
                    probability=Decimal(str(row["subjective_probability"])),
                    outcome=Decimal(str(row["resolved_outcome"])),
                    decision_market_price=Decimal(str(row["decision_market_price"])),
                    final_pre_resolution_price=(
                        Decimal(str(row["final_pre_resolution_price"]))
                        if row.get("final_pre_resolution_price") is not None
                        else None
                    ),
                    capital_at_risk=Decimal(str(row["capital_at_risk"])),
                    confidence=(
                        Decimal(str(row["confidence"]))
                        if row.get("confidence") is not None
                        else None
                    ),
                )
            )
        report = build_prediction_quality_report(observations)
        report["pending_resolution_count"] = len(rows) - len(observations)
        return report

    def upsert_risk_profile(
        self, principal: TenantPrincipal, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        wallet = self.get_default_wallet(principal)
        values = {
            "max_order_notional": _decimal(payload.get("max_order_notional"), minimum=Decimal("0")),
            "max_event_exposure": _decimal(payload.get("max_event_exposure"), minimum=Decimal("0")),
            "max_category_exposure": _decimal(payload.get("max_category_exposure"), minimum=Decimal("0")),
            "max_daily_loss": _decimal(payload.get("max_daily_loss"), minimum=Decimal("0")),
            "max_drawdown_pct": _decimal(payload.get("max_drawdown_pct"), minimum=Decimal("0")),
            "max_book_participation": _decimal(payload.get("max_book_participation"), minimum=Decimal("0")),
            "max_orders_per_hour": (
                int(payload["max_orders_per_hour"])
                if payload.get("max_orders_per_hour") not in (None, "")
                else None
            ),
        }
        for key in ("max_drawdown_pct", "max_book_participation"):
            if values[key] is not None and values[key] > 1:
                raise ValueError(f"{key} must be within [0,1]")
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                INSERT INTO quant.paper_user_risk_profiles (
                    tenant_id,user_id,virtual_wallet_id,max_order_notional,
                    max_event_exposure,max_category_exposure,max_daily_loss,
                    max_drawdown_pct,max_book_participation,max_orders_per_hour
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id,user_id,virtual_wallet_id) DO UPDATE SET
                    max_order_notional=EXCLUDED.max_order_notional,
                    max_event_exposure=EXCLUDED.max_event_exposure,
                    max_category_exposure=EXCLUDED.max_category_exposure,
                    max_daily_loss=EXCLUDED.max_daily_loss,
                    max_drawdown_pct=EXCLUDED.max_drawdown_pct,
                    max_book_participation=EXCLUDED.max_book_participation,
                    max_orders_per_hour=EXCLUDED.max_orders_per_hour,
                    updated_at=clock_timestamp()
                RETURNING *
                """,
                (
                    principal.tenant_id,
                    principal.subject_user_id,
                    wallet["virtual_wallet_id"],
                    values["max_order_notional"],
                    values["max_event_exposure"],
                    values["max_category_exposure"],
                    values["max_daily_loss"],
                    values["max_drawdown_pct"],
                    values["max_book_participation"],
                    values["max_orders_per_hour"],
                ),
            )
            row = dict(cur.fetchone())
            conn.commit()
        return row

    def get_risk_profile(self, principal: TenantPrincipal) -> dict[str, Any] | None:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT profile.*
                FROM quant.paper_virtual_wallets wallet
                JOIN quant.paper_user_risk_profiles profile
                  ON profile.tenant_id=wallet.tenant_id
                 AND profile.user_id=wallet.owner_user_id
                 AND profile.virtual_wallet_id=wallet.virtual_wallet_id
                WHERE wallet.tenant_id=%s AND wallet.owner_user_id=%s
                  AND wallet.is_default
                """,
                (principal.tenant_id, principal.subject_user_id),
            )
            row = cur.fetchone()
            conn.commit()
        return dict(row) if row is not None else None

    def evaluate_order_risk(
        self,
        principal: TenantPrincipal,
        *,
        ledger_strategy_id: str,
        asset_id: str,
        side: str,
        limit_price: Decimal,
        requested_shares: Decimal,
        order_notional: Decimal,
        time_in_force: str,
        post_only: bool,
    ) -> dict[str, Any]:
        """Evaluate user limits from authoritative server-side state.

        A configured limit never silently passes when the required NAV, registry,
        or book evidence is unavailable.
        """

        competition_reasons, competition_observed = (
            self._competition_order_constraints(
                principal,
                ledger_strategy_id=ledger_strategy_id,
                asset_id=asset_id,
            )
        )
        profile = self.get_risk_profile(principal)
        if profile is None:
            return {
                "allowed": not competition_reasons,
                "reason_codes": competition_reasons,
                "profile_configured": False,
                "observed": competition_observed,
            }

        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT COALESCE(meta.event_id,registry.condition_id) AS event_id,
                       meta.category,registry.market_slug,registry.market_title,
                       registry.best_bid,registry.best_ask,book.bids,book.asks,
                       book.observed_at AS book_observed_at,book.has_gap,
                       book.transport_state,position.quantity,
                       position.cost_basis,mark.conservative_mark
                FROM quant.paper_market_registry_tokens registry
                LEFT JOIN LATERAL (
                    SELECT market.category,market.event_id
                    FROM core.markets market
                    WHERE (registry.condition_id IS NOT NULL
                           AND market.condition_id=registry.condition_id)
                       OR (registry.gamma_market_id IS NOT NULL
                           AND market.gamma_market_id=registry.gamma_market_id)
                       OR (registry.market_slug IS NOT NULL
                           AND market.slug=registry.market_slug)
                    ORDER BY CASE
                        WHEN market.condition_id=registry.condition_id THEN 0
                        WHEN market.gamma_market_id=registry.gamma_market_id THEN 1
                        ELSE 2
                    END,market.id DESC
                    LIMIT 1
                ) meta ON TRUE
                LEFT JOIN quant.paper_live_current_books book
                  ON book.asset_id=registry.asset_id
                LEFT JOIN quant.paper_positions position
                  ON position.strategy_id=%s AND position.asset_id=registry.asset_id
                LEFT JOIN quant.paper_position_marks mark
                  ON mark.strategy_id=%s AND mark.asset_id=registry.asset_id
                WHERE registry.asset_id=%s
                """,
                (ledger_strategy_id, ledger_strategy_id, asset_id),
            )
            market = cur.fetchone()
            cur.execute(
                """
                SELECT COALESCE(meta.event_id,registry.condition_id) AS event_id,
                       meta.category,registry.market_slug,registry.market_title,
                       position.asset_id,position.quantity,position.cost_basis,
                       mark.conservative_mark
                FROM quant.paper_positions position
                LEFT JOIN quant.paper_market_registry_tokens registry
                  ON registry.asset_id=position.asset_id
                LEFT JOIN LATERAL (
                    SELECT market.category,market.event_id
                    FROM core.markets market
                    WHERE (registry.condition_id IS NOT NULL
                           AND market.condition_id=registry.condition_id)
                       OR (registry.gamma_market_id IS NOT NULL
                           AND market.gamma_market_id=registry.gamma_market_id)
                       OR (registry.market_slug IS NOT NULL
                           AND market.slug=registry.market_slug)
                    ORDER BY CASE
                        WHEN market.condition_id=registry.condition_id THEN 0
                        WHEN market.gamma_market_id=registry.gamma_market_id THEN 1
                        ELSE 2
                    END,market.id DESC
                    LIMIT 1
                ) meta ON TRUE
                LEFT JOIN quant.paper_position_marks mark
                  ON mark.strategy_id=position.strategy_id
                 AND mark.asset_id=position.asset_id
                WHERE position.strategy_id=%s AND position.quantity<>0
                """,
                (ledger_strategy_id,),
            )
            positions = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT drawdown_pct,total_pnl,observed_at
                FROM quant.paper_portfolio_nav_current
                WHERE strategy_id=%s
                """,
                (ledger_strategy_id,),
            )
            nav = cur.fetchone()
            cur.execute(
                """
                SELECT total_pnl
                FROM quant.paper_portfolio_nav_snapshots
                WHERE strategy_id=%s
                  AND observed_at>=date_trunc('day',clock_timestamp())
                ORDER BY observed_at ASC,nav_id ASC LIMIT 1
                """,
                (ledger_strategy_id,),
            )
            day_start = cur.fetchone()
            cur.execute(
                """
                SELECT count(*) AS count
                FROM quant.paper_live_order_intents
                WHERE strategy_id=%s
                  AND created_at>=clock_timestamp()-interval '1 hour'
                """,
                (ledger_strategy_id,),
            )
            orders_last_hour = int(cur.fetchone()["count"])
            conn.commit()

        reasons: list[str] = list(competition_reasons)
        observed: dict[str, Any] = {
            "order_notional": order_notional,
            "requested_shares": requested_shares,
            "orders_last_hour": orders_last_hour,
            **competition_observed,
        }
        max_order = _decimal(profile.get("max_order_notional"))
        if max_order is not None and order_notional > max_order:
            reasons.append("MAX_ORDER_NOTIONAL_EXCEEDED")

        max_orders = profile.get("max_orders_per_hour")
        if max_orders is not None and orders_last_hour >= int(max_orders):
            reasons.append("MAX_ORDERS_PER_HOUR_EXCEEDED")

        max_drawdown = _decimal(profile.get("max_drawdown_pct"))
        if max_drawdown is not None:
            if nav is None:
                reasons.append("DRAWDOWN_EVIDENCE_UNAVAILABLE")
            else:
                current_drawdown = Decimal(str(nav["drawdown_pct"]))
                observed["drawdown_pct"] = current_drawdown
                if current_drawdown >= max_drawdown:
                    reasons.append("MAX_DRAWDOWN_REACHED")

        max_daily_loss = _decimal(profile.get("max_daily_loss"))
        if max_daily_loss is not None:
            if nav is None or day_start is None or nav.get("total_pnl") is None:
                reasons.append("DAILY_PNL_EVIDENCE_UNAVAILABLE")
            else:
                daily_pnl = Decimal(str(nav["total_pnl"])) - Decimal(
                    str(day_start["total_pnl"])
                )
                observed["daily_pnl"] = daily_pnl
                if daily_pnl <= -max_daily_loss:
                    reasons.append("MAX_DAILY_LOSS_REACHED")

        if market is None:
            if any(
                profile.get(key) is not None
                for key in (
                    "max_event_exposure",
                    "max_category_exposure",
                    "max_book_participation",
                )
            ):
                reasons.append("MARKET_RISK_EVIDENCE_UNAVAILABLE")
        else:
            market = dict(market)
            market["category"] = self._market_category(market)
            for position in positions:
                position["category"] = self._market_category(position)
            current_quantity = Decimal(str(market.get("quantity") or 0))
            current_value = self._risk_position_value(market)
            value_delta = (
                order_notional
                if side == "BUY"
                else -min(current_value, order_notional)
            )
            event_id = str(market.get("event_id") or asset_id)
            category = str(market.get("category") or "other")
            event_before = sum(
                (self._risk_position_value(row) for row in positions
                 if str(row.get("event_id") or row.get("asset_id") or "") == event_id),
                Decimal(0),
            )
            category_before = sum(
                (self._risk_position_value(row) for row in positions
                 if str(row.get("category") or "other") == category),
                Decimal(0),
            )
            event_after = max(Decimal(0), event_before + value_delta)
            category_after = max(Decimal(0), category_before + value_delta)
            observed.update(
                {
                    "position_quantity_before": current_quantity,
                    "event_exposure_before": event_before,
                    "event_exposure_after": event_after,
                    "category_exposure_before": category_before,
                    "category_exposure_after": category_after,
                }
            )
            max_event = _decimal(profile.get("max_event_exposure"))
            if max_event is not None and event_after > max_event:
                reasons.append("MAX_EVENT_EXPOSURE_EXCEEDED")
            max_category = _decimal(profile.get("max_category_exposure"))
            if max_category is not None and category_after > max_category:
                reasons.append("MAX_CATEGORY_EXPOSURE_EXCEEDED")

            max_participation = _decimal(profile.get("max_book_participation"))
            if max_participation is not None and not post_only:
                available = self._visible_executable_depth(
                    market.get("asks") if side == "BUY" else market.get("bids"),
                    side=side,
                    limit_price=limit_price,
                )
                observed["visible_executable_shares"] = available
                if (
                    available is None
                    or bool(market.get("has_gap"))
                    or str(market.get("transport_state") or "").upper()
                    not in {"LIVE", "CONNECTED", "HEALTHY"}
                ):
                    reasons.append("BOOK_PARTICIPATION_EVIDENCE_UNAVAILABLE")
                elif available <= 0 or requested_shares / available > max_participation:
                    reasons.append("MAX_BOOK_PARTICIPATION_EXCEEDED")

        return {
            "allowed": not reasons,
            "reason_codes": sorted(set(reasons)),
            "profile_configured": True,
            "profile_updated_at": profile.get("updated_at"),
            "observed": observed,
            "time_in_force": time_in_force,
        }

    def _competition_order_constraints(
        self,
        principal: TenantPrincipal,
        *,
        ledger_strategy_id: str,
        asset_id: str,
    ) -> tuple[list[str], dict[str, Any]]:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT competition.competition_id,competition.starts_at,
                       competition.ends_at,competition.status,
                       competition.market_scope,competition.rules_hash,
                       registry.market_slug,registry.condition_id,
                       COALESCE(meta.category,
                                registry.raw_metadata->>'category') AS category,
                       COALESCE(meta.event_id,registry.condition_id) AS event_id
                FROM quant.paper_virtual_wallets wallet
                JOIN quant.paper_account_registry account
                  ON account.tenant_id=wallet.tenant_id
                 AND account.account_id=wallet.account_id
                JOIN quant.paper_competition_memberships membership
                  ON membership.virtual_wallet_id=wallet.virtual_wallet_id
                 AND membership.status='ACTIVE'
                JOIN quant.paper_competitions competition
                  ON competition.competition_id=membership.competition_id
                LEFT JOIN quant.paper_market_registry_tokens registry
                  ON registry.asset_id=%s
                LEFT JOIN LATERAL (
                    SELECT market.category,market.event_id
                    FROM core.markets market
                    WHERE market.condition_id=registry.condition_id
                       OR market.slug=registry.market_slug
                    ORDER BY CASE WHEN market.condition_id=registry.condition_id
                                  THEN 0 ELSE 1 END,market.id DESC
                    LIMIT 1
                ) meta ON TRUE
                WHERE wallet.tenant_id=%s AND wallet.owner_user_id=%s
                  AND account.ledger_strategy_id=%s
                ORDER BY competition.created_at DESC LIMIT 1
                """,
                (
                    asset_id,
                    principal.tenant_id,
                    principal.subject_user_id,
                    ledger_strategy_id,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:
            return [], {}

        competition = dict(row)
        now = datetime.now(timezone.utc)
        reasons: list[str] = []
        if competition["status"] == "CANCELLED":
            reasons.append("COMPETITION_CANCELLED")
        elif now < competition["starts_at"]:
            reasons.append("COMPETITION_NOT_STARTED")
        elif now >= competition["ends_at"] or competition["status"] == "ENDED":
            reasons.append("COMPETITION_ENDED")

        scope = competition.get("market_scope") or {}
        if not isinstance(scope, Mapping):
            reasons.append("COMPETITION_SCOPE_INVALID")
            scope = {}
        checks = {
            "asset_ids": str(asset_id),
            "market_slugs": competition.get("market_slug"),
            "condition_ids": competition.get("condition_id"),
            "event_ids": competition.get("event_id"),
            "categories": competition.get("category"),
        }
        for scope_key, actual in checks.items():
            allowed_values = scope.get(scope_key)
            if not allowed_values:
                continue
            if actual in (None, ""):
                reasons.append("COMPETITION_SCOPE_EVIDENCE_UNAVAILABLE")
                continue
            normalized = {str(value).casefold() for value in allowed_values}
            if str(actual).casefold() not in normalized:
                reasons.append("COMPETITION_MARKET_OUT_OF_SCOPE")

        return sorted(set(reasons)), {
            "competition_id": competition["competition_id"],
            "competition_status": competition["status"],
            "competition_starts_at": competition["starts_at"],
            "competition_ends_at": competition["ends_at"],
            "competition_rules_hash": competition["rules_hash"],
            "competition_market_scope": dict(scope),
        }

    @staticmethod
    def _risk_position_value(row: Mapping[str, Any]) -> Decimal:
        quantity = abs(Decimal(str(row.get("quantity") or 0)))
        mark = _decimal(row.get("conservative_mark"))
        if mark is not None:
            return quantity * mark
        return abs(Decimal(str(row.get("cost_basis") or 0)))

    @staticmethod
    def _visible_executable_depth(
        raw_levels: Any, *, side: str, limit_price: Decimal
    ) -> Decimal | None:
        if not isinstance(raw_levels, list):
            return None
        total = Decimal(0)
        for raw in raw_levels:
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                raw_price, raw_size = raw[0], raw[1]
            elif isinstance(raw, Mapping):
                raw_price = raw.get("price", raw.get("p"))
                raw_size = raw.get("size", raw.get("quantity", raw.get("q")))
            else:
                continue
            if raw_price in (None, "") or raw_size in (None, ""):
                continue
            price = Decimal(str(raw_price))
            size = Decimal(str(raw_size))
            if (side == "BUY" and price <= limit_price) or (
                side == "SELL" and price >= limit_price
            ):
                total += max(Decimal(0), size)
        return total

    def record_order_risk_decision(
        self,
        principal: TenantPrincipal,
        *,
        request_key: str,
        asset_id: str,
        side: str,
        decision: Mapping[str, Any],
        intent_id: int | None = None,
    ) -> dict[str, Any]:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT virtual_wallet_id FROM quant.paper_virtual_wallets
                WHERE tenant_id=%s AND owner_user_id=%s AND is_default
                """,
                (principal.tenant_id, principal.subject_user_id),
            )
            wallet = cur.fetchone()
            if wallet is None:
                conn.commit()
                return {}
            cur.execute(
                """
                INSERT INTO quant.paper_retail_order_admissions (
                    admission_id,tenant_id,user_id,virtual_wallet_id,request_key,
                    intent_id,asset_id,side,allowed,reason_codes,observed,
                    profile_updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                ON CONFLICT (tenant_id,request_key) DO UPDATE SET
                    intent_id=COALESCE(EXCLUDED.intent_id,
                                       quant.paper_retail_order_admissions.intent_id),
                    allowed=EXCLUDED.allowed,
                    reason_codes=EXCLUDED.reason_codes,
                    observed=EXCLUDED.observed,
                    profile_updated_at=EXCLUDED.profile_updated_at
                RETURNING *
                """,
                (
                    uuid4(),
                    principal.tenant_id,
                    principal.subject_user_id,
                    wallet["virtual_wallet_id"],
                    str(request_key),
                    intent_id,
                    str(asset_id),
                    str(side).upper(),
                    bool(decision.get("allowed")),
                    json.dumps(decision.get("reason_codes") or [], sort_keys=True),
                    json.dumps(
                        decision.get("observed") or {}, sort_keys=True, default=str
                    ),
                    decision.get("profile_updated_at"),
                ),
            )
            row = dict(cur.fetchone())
            conn.commit()
        return row

    def get_pnl_attribution(self, principal: TenantPrincipal) -> dict[str, Any]:
        """Explain account economics by product dimensions from immutable entries."""

        wallet = self.get_default_wallet(principal)
        strategy_id = str(wallet["ledger_strategy_id"])
        as_of = datetime.now(timezone.utc)
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT ledger.*,registry.market_slug,registry.market_title,
                       registry.outcome_name,
                       COALESCE(meta.category,'other') AS category,
                       COALESCE(meta.event_id,ledger.condition_id) AS event_id,
                       intent.post_only
                FROM quant.paper_ledger_entries ledger
                LEFT JOIN quant.paper_market_registry_tokens registry
                  ON registry.asset_id=ledger.asset_id
                LEFT JOIN LATERAL (
                    SELECT market.category,market.event_id
                    FROM core.markets market
                    WHERE market.condition_id=ledger.condition_id
                       OR market.slug=registry.market_slug
                    ORDER BY CASE WHEN market.condition_id=ledger.condition_id
                                  THEN 0 ELSE 1 END,market.id DESC
                    LIMIT 1
                ) meta ON TRUE
                LEFT JOIN LATERAL (
                    SELECT candidate.post_only
                    FROM quant.paper_live_order_intents candidate
                    WHERE candidate.strategy_id=ledger.strategy_id
                      AND candidate.client_order_id=ledger.client_order_id
                    ORDER BY candidate.created_at DESC,candidate.intent_id DESC
                    LIMIT 1
                ) intent ON TRUE
                WHERE ledger.strategy_id=%s
                ORDER BY ledger.event_ts,ledger.entry_id
                """,
                (strategy_id,),
            )
            ledger = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT position.*,registry.market_slug,registry.market_title,
                       registry.outcome_name,
                       COALESCE(meta.category,'other') AS category,
                       COALESCE(meta.event_id,position.condition_id) AS event_id,
                       mark.research_mark,mark.liquidation_mark,mark.mark_quality
                FROM quant.paper_positions position
                LEFT JOIN quant.paper_market_registry_tokens registry
                  ON registry.asset_id=position.asset_id
                LEFT JOIN LATERAL (
                    SELECT market.category,market.event_id
                    FROM core.markets market
                    WHERE market.condition_id=position.condition_id
                       OR market.slug=registry.market_slug
                    ORDER BY CASE WHEN market.condition_id=position.condition_id
                                  THEN 0 ELSE 1 END,market.id DESC
                    LIMIT 1
                ) meta ON TRUE
                LEFT JOIN quant.paper_position_marks mark
                  ON mark.strategy_id=position.strategy_id
                 AND mark.asset_id=position.asset_id
                WHERE position.strategy_id=%s AND position.quantity<>0
                ORDER BY position.asset_id
                """,
                (strategy_id,),
            )
            positions = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT cash_balance,cash_reserved,realized_pnl,initial_cash
                FROM quant.paper_accounts WHERE strategy_id=%s
                """,
                (strategy_id,),
            )
            account = dict(cur.fetchone())
            cur.execute(
                """
                SELECT rebate_type,state,sum(amount) AS amount,count(*) AS count
                FROM quant.paper_rebates WHERE strategy_id=%s
                GROUP BY rebate_type,state ORDER BY rebate_type,state
                """,
                (strategy_id,),
            )
            rebates = [dict(row) for row in cur.fetchall()]
            conn.commit()

        return _build_pnl_attribution(
            ledger=ledger,
            positions=positions,
            account=account,
            rebates=rebates,
            as_of=as_of,
            strategy_id=strategy_id,
        )

    def get_account_truth(self, principal: TenantPrincipal) -> dict[str, Any]:
        """Return the latest official-vs-Paper evidence without mutating the ledger."""

        wallet = self.get_default_wallet(principal)
        strategy_id = str(wallet["ledger_strategy_id"])
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT wallet_address FROM quant.paper_identity_bindings
                WHERE tenant_id=%s AND user_id=%s AND provider='EVM'
                  AND verified_at IS NOT NULL AND wallet_address IS NOT NULL
                ORDER BY verified_at DESC LIMIT 1
                """,
                (principal.tenant_id, principal.subject_user_id),
            )
            binding = cur.fetchone()
            if binding is None:
                conn.commit()
                return {
                    "status": "NOT_LINKED",
                    "comparison_item_count": 0,
                    "reason": "No verified EVM wallet is bound to this Paper wallet.",
                }
            account_address = str(binding["wallet_address"]).lower()
            cur.execute(
                """
                SELECT to_regclass('quant.paper_official_account_snapshot_runs')
                         AS snapshot_table,
                       to_regclass('quant.paper_account_truth_reconciliation_runs')
                         AS reconciliation_table
                """
            )
            availability = cur.fetchone()
            if not availability or not availability["snapshot_table"]:
                conn.commit()
                return {
                    "status": "NOT_CONFIGURED",
                    "account_address": account_address,
                    "comparison_item_count": 0,
                }
            cur.execute(
                """
                SELECT run_id,source_as_of,observed_at,status,position_count,
                       closed_position_count,accounting_position_count,
                       positions_payload_hash,closed_positions_payload_hash,
                       accounting_zip_sha256,positions_csv_sha256,equity_csv_sha256,
                       errors
                FROM quant.paper_official_account_snapshot_runs
                WHERE account_address=%s
                ORDER BY source_as_of DESC,observed_at DESC LIMIT 1
                """,
                (account_address,),
            )
            official_row = cur.fetchone()
            official = dict(official_row) if official_row is not None else None
            reconciliation = None
            mismatches: list[dict[str, Any]] = []
            if availability["reconciliation_table"]:
                cur.execute(
                    """
                    SELECT * FROM quant.paper_account_truth_reconciliation_runs
                    WHERE account_address=%s
                      AND strategy_ids @> %s::jsonb
                    ORDER BY as_of DESC,generated_at DESC LIMIT 1
                    """,
                    (account_address, json.dumps([strategy_id])),
                )
                reconciliation_row = cur.fetchone()
                if reconciliation_row is not None:
                    reconciliation = dict(reconciliation_row)
                    cur.execute(
                        """
                        SELECT mismatch_type,comparison_type,comparison_key,
                               field_name,official_value,paper_value,delta,tolerance,
                               reason,severity,retryable,evidence
                        FROM quant.paper_account_truth_reconciliation_items
                        WHERE reconciliation_id=%s
                        ORDER BY severity DESC,mismatch_type,comparison_key,field_name
                        LIMIT 200
                        """,
                        (reconciliation["reconciliation_id"],),
                    )
                    mismatches = [dict(row) for row in cur.fetchall()]
            conn.commit()
        if reconciliation is None:
            return {
                "status": "OFFICIAL_ONLY" if official else "NO_OFFICIAL_EVIDENCE",
                "account_address": account_address,
                "strategy_id": strategy_id,
                "official_snapshot": official,
                "comparison_item_count": 0,
                "mismatches": [],
            }
        summary = reconciliation.get("summary") or {}
        comparison_count = int(
            summary.get("comparison_item_count")
            or summary.get("comparison_count")
            or len(mismatches)
        )
        return {
            "status": reconciliation["status"],
            "account_address": account_address,
            "strategy_id": strategy_id,
            "official_snapshot": official,
            "reconciliation": reconciliation,
            "comparison_item_count": comparison_count,
            "mismatches": mismatches,
            "ledger_overwritten": False,
        }

    def get_event_risk(self, principal: TenantPrincipal) -> dict[str, Any]:
        """Expose the same payout scenarios used by the central admission gate."""

        wallet = self.get_default_wallet(principal)
        strategy_id = str(wallet["ledger_strategy_id"])
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT asset_id,condition_id FROM quant.paper_positions
                WHERE strategy_id=%s AND quantity>0
                ORDER BY asset_id LIMIT 1
                """,
                (strategy_id,),
            )
            first = cur.fetchone()
            if first is None:
                conn.commit()
                return {
                    "status": "NO_POSITIONS",
                    "scenario_count": 0,
                    "relationships": [],
                    "event_worst_case": {},
                }
            risk_input = load_event_risk_input(
                cur,
                SimpleNamespace(
                    strategy_id=strategy_id,
                    asset_id=str(first["asset_id"]),
                    condition_id=str(first["condition_id"]),
                ),
                candidate_event_id=str(first["condition_id"]),
                candidate_category="unknown",
            )
            conn.commit()
        results = evaluate_event_scenarios(risk_input.positions, risk_input.scenarios)
        worst: dict[str, Decimal] = {}
        best: dict[str, Decimal] = {}
        for result in results:
            worst[result.event_id] = min(
                worst.get(result.event_id, result.pnl), result.pnl
            )
            best[result.event_id] = max(best.get(result.event_id, result.pnl), result.pnl)
        event_conditions: dict[str, set[str]] = {}
        for scenario in risk_input.scenarios:
            event_conditions.setdefault(scenario.event_id, set()).add(
                scenario.condition_id or "__EVENT__"
            )
        relationships = [
            {
                "event_id": event_id,
                "relationship": (
                    "MUTUALLY_EXCLUSIVE_EVENT"
                    if len(conditions - {"__EVENT__"}) > 1
                    else "BINARY_CONDITION"
                ),
                "condition_ids": sorted(conditions - {"__EVENT__"}),
            }
            for event_id, conditions in sorted(event_conditions.items())
        ]
        return {
            "status": risk_input.status,
            "reason_codes": list(risk_input.reasons),
            "model_version": risk_input.model_version,
            "position_count": len(risk_input.positions),
            "scenario_count": len(risk_input.scenarios),
            "event_worst_case": worst,
            "event_best_case": best,
            "relationships": relationships,
            "scenarios": [asdict(row) for row in results],
        }

    def get_event_relationship_graph(
        self,
        principal: TenantPrincipal,
        *,
        market_slug: str | None = None,
    ) -> dict[str, Any]:
        """Return only relationships supported by binary structure or evidence."""

        wallet = self.get_default_wallet(principal)
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            if market_slug:
                cur.execute(
                    """
                    SELECT event_id,condition_id
                    FROM core.markets WHERE slug=%s
                    ORDER BY id DESC LIMIT 1
                    """,
                    (str(market_slug),),
                )
            else:
                cur.execute(
                    """
                    SELECT market.event_id,position.condition_id
                    FROM quant.paper_positions position
                    LEFT JOIN core.markets market
                      ON market.condition_id=position.condition_id
                    WHERE position.strategy_id=%s AND position.quantity<>0
                    ORDER BY position.updated_at DESC LIMIT 1
                    """,
                    (str(wallet["ledger_strategy_id"]),),
                )
            selected = cur.fetchone()
            if selected is None or not (
                selected.get("event_id") or selected.get("condition_id")
            ):
                conn.commit()
                return {
                    "status": "NO_CONTEXT",
                    "model_version": "paper_event_relationship_graph_v1",
                    "nodes": [],
                    "edges": [],
                    "unsupported_relationships": [
                        "IMPLIES",
                        "TEMPORALLY_NESTED",
                    ],
                }
            event_id = str(selected.get("event_id") or selected["condition_id"])
            if selected.get("event_id"):
                market_filter = "market.event_id=%s"
            else:
                market_filter = "market.condition_id=%s"
            cur.execute(
                f"""
                SELECT market.id::text AS market_id,market.slug,market.title,
                       market.condition_id,market.event_id,market.oracle,
                       market.end_date,market.category,registry.asset_id,
                       registry.outcome_name,registry.outcome_index
                FROM core.markets market
                LEFT JOIN quant.paper_market_registry_tokens registry
                  ON registry.condition_id=market.condition_id
                WHERE {market_filter}
                ORDER BY market.id,registry.outcome_index,registry.asset_id
                """,
                (event_id,),
            )
            market_rows = [dict(row) for row in cur.fetchall()]
            node_ids = {
                f"condition:{row['condition_id']}"
                for row in market_rows
                if row.get("condition_id")
            } | {
                f"asset:{row['asset_id']}"
                for row in market_rows
                if row.get("asset_id")
            }
            explicit: list[dict[str, Any]] = []
            if node_ids:
                cur.execute(
                    """
                    SELECT relationship_id,event_id,source_node_id,target_node_id,
                           relationship_type,source_kind,source_hash,confidence,
                           metadata,observed_at,valid_until
                    FROM quant.paper_event_relationships
                    WHERE event_id=%s
                      AND source_node_id=ANY(%s::text[])
                      AND target_node_id=ANY(%s::text[])
                      AND (valid_until IS NULL OR valid_until>clock_timestamp())
                    ORDER BY relationship_type,source_node_id,target_node_id
                    """,
                    (event_id, sorted(node_ids), sorted(node_ids)),
                )
                explicit = [dict(row) for row in cur.fetchall()]
            conn.commit()

        conditions: dict[str, dict[str, Any]] = {}
        outcomes: list[dict[str, Any]] = []
        for row in market_rows:
            condition_id = str(row.get("condition_id") or "")
            if not condition_id:
                continue
            conditions.setdefault(
                condition_id,
                {
                    "node_id": f"condition:{condition_id}",
                    "node_type": "CONDITION",
                    "market_id": row.get("market_id"),
                    "market_slug": row.get("slug"),
                    "title": row.get("title"),
                    "condition_id": condition_id,
                    "oracle": row.get("oracle"),
                    "end_date": row.get("end_date"),
                    "category": row.get("category"),
                },
            )
            if row.get("asset_id"):
                outcomes.append(
                    {
                        "node_id": f"asset:{row['asset_id']}",
                        "node_type": "OUTCOME_TOKEN",
                        "asset_id": str(row["asset_id"]),
                        "condition_id": condition_id,
                        "outcome_name": str(row.get("outcome_name") or "UNKNOWN"),
                        "outcome_index": row.get("outcome_index"),
                    }
                )

        edges: list[dict[str, Any]] = []
        by_condition: dict[str, list[dict[str, Any]]] = {}
        for node in outcomes:
            by_condition.setdefault(str(node["condition_id"]), []).append(node)
        for condition_id, rows in sorted(by_condition.items()):
            if len(rows) != 2:
                continue
            ordered = sorted(rows, key=lambda row: str(row["node_id"]))
            source_hash = _stable_hash(
                {
                    "condition_id": condition_id,
                    "assets": [row["asset_id"] for row in ordered],
                    "rule": "binary-outcomes-sum-to-one",
                }
            )
            for relationship_type in (
                "OPPOSITE",
                "MUTUALLY_EXCLUSIVE",
                "COLLECTIVELY_EXHAUSTIVE",
            ):
                edges.append(
                    {
                        "relationship_id": str(
                            uuid5(
                                UUID("d37f68b9-c467-47a8-9f2d-113e91e1ee42"),
                                f"{event_id}:{condition_id}:{relationship_type}",
                            )
                        ),
                        "source_node_id": ordered[0]["node_id"],
                        "target_node_id": ordered[1]["node_id"],
                        "relationship_type": relationship_type,
                        "source_kind": "DERIVED_BINARY",
                        "source_hash": source_hash,
                        "confidence": Decimal(1),
                        "metadata": {
                            "condition_id": condition_id,
                            "collateral_payout_sum": "1",
                        },
                    }
                )

        condition_rows = list(conditions.values())
        oracle_nodes: dict[str, dict[str, Any]] = {}
        for condition in condition_rows:
            oracle = str(condition.get("oracle") or "").strip()
            if not oracle:
                continue
            oracle_hash = hashlib.sha256(oracle.encode()).hexdigest()
            oracle_node_id = f"oracle:{oracle_hash[:16]}"
            oracle_nodes.setdefault(
                oracle_node_id,
                {
                    "node_id": oracle_node_id,
                    "node_type": "ORACLE",
                    "oracle": oracle,
                },
            )
            source_hash = _stable_hash(
                {
                    "event_id": event_id,
                    "oracle": oracle,
                    "condition_id": condition["condition_id"],
                }
            )
            edges.append(
                {
                    "relationship_id": str(
                        uuid5(
                            UUID("68a2b38d-29ff-4b2f-a54e-d8733156f771"),
                            source_hash,
                        )
                    ),
                    "source_node_id": condition["node_id"],
                    "target_node_id": oracle_node_id,
                    "relationship_type": "SHARED_ORACLE",
                    "source_kind": "OFFICIAL",
                    "source_hash": source_hash,
                    "confidence": Decimal(1),
                    "metadata": {"oracle": oracle},
                }
            )
        edges.extend(explicit)
        proved_types = {str(row["relationship_type"]) for row in edges}
        return {
            "status": "READY" if conditions else "NO_MARKETS",
            "model_version": "paper_event_relationship_graph_v1",
            "event_id": event_id,
            "nodes": condition_rows + outcomes + list(oracle_nodes.values()),
            "edges": edges,
            "unsupported_relationships": sorted(
                {"IMPLIES", "TEMPORALLY_NESTED", "MUTUALLY_EXCLUSIVE"}
                - proved_types
            ),
            "proof_boundary": (
                "Only binary token structure and versioned relationship evidence "
                "are included; title similarity is never treated as logic."
            ),
        }

    def get_account_lifecycle(self, principal: TenantPrincipal) -> dict[str, Any]:
        """Return resolution, dispute, settlement and redeem state for held assets."""

        wallet = self.get_default_wallet(principal)
        strategy_id = str(wallet["ledger_strategy_id"])
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT position.asset_id,position.market_id,position.condition_id,
                       position.quantity,position.cost_basis,position.settled_at,
                       registry.market_slug,registry.market_title,
                       registry.outcome_name,registry.market_state,
                       resolution.phase,resolution.expected_resolution_at,
                       resolution.trading_stopped_at,resolution.actual_finalized_at,
                       resolution.redeem_started_at,resolution.redeemed_at,
                       resolution.proposal_count,resolution.dispute_round,
                       resolution.truth_hash,receivable.state AS receivable_state,
                       receivable.expected_payout,receivable.expected_realized_pnl,
                       receivable.cash_applied
                FROM quant.paper_positions position
                LEFT JOIN quant.paper_market_registry_tokens registry
                  ON registry.asset_id=position.asset_id
                LEFT JOIN quant.market_resolution_states resolution
                  ON resolution.condition_id=position.condition_id
                LEFT JOIN quant.paper_settlement_receivables receivable
                  ON receivable.strategy_id=position.strategy_id
                 AND receivable.asset_id=position.asset_id
                 AND receivable.truth_hash=resolution.truth_hash
                WHERE position.strategy_id=%s
                  AND (position.quantity<>0 OR position.settled_at IS NOT NULL)
                ORDER BY COALESCE(resolution.updated_at,position.updated_at) DESC
                """,
                (strategy_id,),
            )
            positions = [dict(row) for row in cur.fetchall()]
            conditions = sorted(
                {str(row["condition_id"]) for row in positions if row.get("condition_id")}
            )
            events: list[dict[str, Any]] = []
            payouts: list[dict[str, Any]] = []
            clarifications: list[dict[str, Any]] = []
            if conditions:
                cur.execute(
                    """
                    SELECT condition_id,from_phase,to_phase,event_ts,proposal_count,
                           dispute_round,truth_hash,source,reason
                    FROM quant.market_resolution_events
                    WHERE condition_id=ANY(%s::text[])
                    ORDER BY event_ts DESC,created_at DESC LIMIT 500
                    """,
                    (conditions,),
                )
                events = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                    SELECT condition_id,asset_id,payout_per_share,resolution_source,
                           oracle_finalized_at,truth_hash
                    FROM quant.market_settlement_payouts
                    WHERE condition_id=ANY(%s::text[])
                    ORDER BY oracle_finalized_at DESC
                    """,
                    (conditions,),
                )
                payouts = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                    SELECT condition_id,source_event_id,clarified_at,payload_hash,
                           payload,canceled_intent_ids
                    FROM quant.paper_market_clarifications
                    WHERE condition_id=ANY(%s::text[])
                    ORDER BY clarified_at DESC LIMIT 200
                    """,
                    (conditions,),
                )
                clarifications = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return {
            "status": "READY",
            "positions": positions,
            "events": events,
            "payouts": payouts,
            "clarifications": clarifications,
            "redeemable_count": sum(
                1
                for row in positions
                if str(row.get("phase") or "").upper() == "REDEEMABLE"
            ),
        }

    def get_maker_workbench(
        self, principal: TenantPrincipal, *, limit: int = 100
    ) -> dict[str, Any]:
        """Return strict and research Maker state without promoting probabilities."""

        wallet = self.get_default_wallet(principal)
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT intent.intent_id,intent.asset_id,intent.side,
                       intent.limit_price,intent.size,intent.remaining_size,
                       intent.status,intent.order_state,intent.created_at,
                       intent.updated_at,queue.queue_model_version,
                       queue.displayed_size_at_accept,queue.own_orders_ahead,
                       queue.estimated_external_queue_ahead,
                       queue.cumulative_trade_volume_at_price,
                       queue.cumulative_cancel_ahead_estimate,
                       queue.fill_probability,queue.expected_fill_size,
                       queue.queue_confidence,queue.accepted_at,
                       queue.accepted_book_generation,queue.queue_epoch,
                       queue.model_domain_decision,queue.model_domain_decision_hash,
                       tca.implementation_shortfall,tca.markouts_json,
                       tca.fidelity_level,tca.capacity_status
                FROM quant.paper_intent_ownership ownership
                JOIN quant.paper_live_order_intents intent
                  ON intent.intent_id=ownership.intent_id
                LEFT JOIN quant.maker_queue_states queue
                  ON queue.paper_order_id=intent.intent_id::text
                LEFT JOIN quant.execution_tca tca
                  ON tca.intent_id=intent.intent_id
                WHERE ownership.tenant_id=%s AND ownership.account_id=%s
                  AND intent.post_only
                ORDER BY intent.created_at DESC,intent.intent_id DESC LIMIT %s
                """,
                (principal.tenant_id, wallet["account_id"], limit),
            )
            orders = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT rebate_type,state,sum(amount) AS amount,count(*) AS count,
                       max(received_at) AS last_received_at
                FROM quant.paper_rebates
                WHERE strategy_id=%s
                GROUP BY rebate_type,state ORDER BY rebate_type,state
                """,
                (str(wallet["ledger_strategy_id"]),),
            )
            rebates = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return {
            "status": "READY",
            "authority_model": "STRICT_TRADE_EVIDENCE",
            "research_model": "PROBABILISTIC_QUEUE",
            "exact_fifo_claimed": False,
            "orders": orders,
            "rebates": rebates,
        }

    def _sync_notifications(self, principal: TenantPrincipal, cur: Any) -> None:
        cur.execute(
            """
            SELECT 'order:'||event.event_id::text AS source_key,
                   'ORDER_'||event.event_type AS notification_type,
                   CASE WHEN event.to_state IN ('REJECTED','FAILED')
                        THEN 'WARNING' ELSE 'INFO' END AS severity,
                   event.to_state AS title,
                   COALESCE(event.reason,'Order state changed') AS message,
                   'ORDER' AS resource_type,event.intent_id::text AS resource_id,
                   jsonb_build_object('event_type',event.event_type,
                                      'state',event.to_state,
                                      'event_ts',event.event_ts) AS payload,
                   event.event_ts AS created_at
            FROM quant.paper_intent_ownership ownership
            JOIN quant.paper_order_events event ON event.intent_id=ownership.intent_id
            WHERE ownership.tenant_id=%s AND ownership.submitted_by=%s
              AND (event.to_state IN ('PARTIAL','FILLED','REJECTED','EXPIRED','CANCELED')
                   OR event.event_type IN ('RISK_REJECTED','FINALITY_FAILED'))
            UNION ALL
            SELECT 'risk:'||admission.admission_id::text,'RISK_REJECTED','WARNING',
                   'Risk limit rejected order',
                   array_to_string(ARRAY(SELECT jsonb_array_elements_text(
                       admission.reason_codes)),','),
                   'RISK',COALESCE(admission.intent_id::text,admission.request_key),
                   admission.observed,admission.created_at
            FROM quant.paper_retail_order_admissions admission
            WHERE admission.tenant_id=%s AND admission.user_id=%s
              AND NOT admission.allowed
            """,
            (
                principal.tenant_id,
                principal.subject_user_id,
                principal.tenant_id,
                principal.subject_user_id,
            ),
        )
        rows = list(cur.fetchall())
        cur.execute(
            """
            SELECT 'book:'||position.asset_id||':'||
                       COALESCE(book.generation::text,'missing')||':'||
                       COALESCE(book.book_status,'MISSING')||':'||
                       COALESCE(book.transport_state,'MISSING') AS source_key,
                   'BOOK_DATA_DEGRADED' AS notification_type,
                   'WARNING' AS severity,
                   'Position market data degraded' AS title,
                   CASE WHEN book.asset_id IS NULL THEN 'Book baseline is unavailable'
                        WHEN book.has_gap THEN 'Book has an unresolved data gap'
                        WHEN book.book_status<>'READY' THEN 'Book is not ready'
                        ELSE 'Book transport is disconnected' END AS message,
                   'POSITION' AS resource_type,position.asset_id AS resource_id,
                   jsonb_build_object(
                       'asset_id',position.asset_id,
                       'quantity',position.quantity,
                       'generation',book.generation,
                       'book_status',book.book_status,
                       'transport_state',book.transport_state,
                       'has_gap',book.has_gap,
                       'observed_at',book.observed_at
                   ) AS payload,
                   COALESCE(book.updated_at,position.updated_at) AS created_at
            FROM quant.paper_virtual_wallets wallet
            JOIN quant.paper_account_registry account
              ON account.tenant_id=wallet.tenant_id
             AND account.account_id=wallet.account_id
            JOIN quant.paper_positions position
              ON position.strategy_id=account.ledger_strategy_id
             AND position.quantity<>0
            LEFT JOIN quant.paper_live_current_books book
              ON book.asset_id=position.asset_id
            WHERE wallet.tenant_id=%s AND wallet.owner_user_id=%s
              AND (book.asset_id IS NULL OR book.has_gap
                   OR book.book_status<>'READY'
                   OR book.transport_state NOT IN ('LIVE','CONNECTED','HEALTHY'))
            UNION ALL
            SELECT 'resolution:'||position.condition_id||':'||resolution.phase||':'||
                       COALESCE(resolution.truth_hash,'pending') AS source_key,
                   CASE WHEN resolution.phase='REDEEMABLE' THEN 'POSITION_REDEEMABLE'
                        WHEN resolution.phase LIKE 'DISPUTED%%' THEN 'MARKET_DISPUTED'
                        WHEN resolution.phase='CHALLENGE_WINDOW' THEN 'CHALLENGE_WINDOW'
                        ELSE 'MARKET_RESOLUTION_UPDATED' END AS notification_type,
                   CASE WHEN resolution.phase LIKE 'DISPUTED%%' THEN 'WARNING'
                        ELSE 'INFO' END AS severity,
                   CASE WHEN resolution.phase='REDEEMABLE' THEN 'Position is redeemable'
                        WHEN resolution.phase LIKE 'DISPUTED%%' THEN 'Market is disputed'
                        WHEN resolution.phase='CHALLENGE_WINDOW' THEN 'Challenge window open'
                        ELSE 'Market resolution updated' END AS title,
                   'Resolution phase: '||resolution.phase AS message,
                   'MARKET' AS resource_type,position.condition_id AS resource_id,
                   jsonb_build_object(
                       'condition_id',position.condition_id,
                       'asset_id',position.asset_id,
                       'quantity',position.quantity,
                       'phase',resolution.phase,
                       'truth_hash',resolution.truth_hash,
                       'expected_resolution_at',resolution.expected_resolution_at
                   ) AS payload,
                   resolution.updated_at AS created_at
            FROM quant.paper_virtual_wallets wallet
            JOIN quant.paper_account_registry account
              ON account.tenant_id=wallet.tenant_id
             AND account.account_id=wallet.account_id
            JOIN quant.paper_positions position
              ON position.strategy_id=account.ledger_strategy_id
             AND position.quantity<>0
            JOIN quant.market_resolution_states resolution
              ON resolution.condition_id=position.condition_id
            WHERE wallet.tenant_id=%s AND wallet.owner_user_id=%s
              AND resolution.phase IN (
                  'CHALLENGE_WINDOW','DISPUTED_ROUND_1','DISPUTED_ROUND_2',
                  'DISPUTED','RESOLUTION_FINAL','FINALIZED','REDEEMABLE'
              )
            UNION ALL
            SELECT 'near-settlement:'||position.condition_id||':'||
                       market.end_date::text AS source_key,
                   'MARKET_ENDING_SOON' AS notification_type,
                   'INFO' AS severity,'Market ending soon' AS title,
                   'A held market is scheduled to end within 24 hours' AS message,
                   'MARKET' AS resource_type,position.condition_id AS resource_id,
                   jsonb_build_object(
                       'condition_id',position.condition_id,
                       'asset_id',position.asset_id,
                       'quantity',position.quantity,
                       'end_date',market.end_date
                   ) AS payload,
                   clock_timestamp() AS created_at
            FROM quant.paper_virtual_wallets wallet
            JOIN quant.paper_account_registry account
              ON account.tenant_id=wallet.tenant_id
             AND account.account_id=wallet.account_id
            JOIN quant.paper_positions position
              ON position.strategy_id=account.ledger_strategy_id
             AND position.quantity<>0
            JOIN core.markets market ON market.condition_id=position.condition_id
            WHERE wallet.tenant_id=%s AND wallet.owner_user_id=%s
              AND market.end_date>clock_timestamp()
              AND market.end_date<=clock_timestamp()+interval '24 hours'
            """,
            (
                principal.tenant_id,
                principal.subject_user_id,
                principal.tenant_id,
                principal.subject_user_id,
                principal.tenant_id,
                principal.subject_user_id,
            ),
        )
        rows.extend(cur.fetchall())
        for row in rows:
            source_key = str(row["source_key"])
            cur.execute(
                """
                INSERT INTO quant.paper_user_notifications (
                    notification_id,tenant_id,user_id,notification_type,severity,
                    title,message,resource_type,resource_id,payload,source_key,created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                ON CONFLICT (tenant_id,user_id,source_key) WHERE source_key IS NOT NULL
                DO NOTHING
                """,
                (
                    uuid5(principal.tenant_id, source_key),
                    principal.tenant_id,
                    principal.subject_user_id,
                    row["notification_type"],
                    row["severity"],
                    row["title"],
                    row["message"],
                    row["resource_type"],
                    row["resource_id"],
                    json.dumps(row.get("payload") or {}, default=str, sort_keys=True),
                    source_key,
                    row["created_at"],
                ),
            )

    def list_notifications(
        self, principal: TenantPrincipal, *, unread_only: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            self._sync_notifications(principal, cur)
            cur.execute(
                """
                SELECT * FROM quant.paper_user_notifications
                WHERE tenant_id=%s AND user_id=%s
                  AND (NOT %s OR read_at IS NULL)
                ORDER BY created_at DESC LIMIT %s
                """,
                (principal.tenant_id, principal.subject_user_id, unread_only, limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        return rows

    def mark_notification_read(
        self, principal: TenantPrincipal, notification_id: UUID
    ) -> bool:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                UPDATE quant.paper_user_notifications
                SET read_at=COALESCE(read_at,clock_timestamp())
                WHERE tenant_id=%s AND user_id=%s AND notification_id=%s
                RETURNING notification_id
                """,
                (
                    principal.tenant_id,
                    principal.subject_user_id,
                    notification_id,
                ),
            )
            changed = cur.fetchone() is not None
            conn.commit()
        return changed

    def set_portfolio_visibility(
        self, principal: TenantPrincipal, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        wallet = self.get_default_wallet(principal)
        visibility = str(payload.get("visibility") or "PRIVATE").upper()
        if visibility not in {"PRIVATE", "DELAYED", "PUBLIC"}:
            raise ValueError("invalid portfolio visibility")
        delay = int(payload.get("disclosure_delay_minutes") or 60)
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                INSERT INTO quant.paper_public_portfolios (
                    virtual_wallet_id,tenant_id,user_id,display_name,visibility,
                    disclosure_delay_minutes,public_bio
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (virtual_wallet_id) DO UPDATE SET
                    display_name=EXCLUDED.display_name,
                    visibility=EXCLUDED.visibility,
                    disclosure_delay_minutes=EXCLUDED.disclosure_delay_minutes,
                    public_bio=EXCLUDED.public_bio,
                    updated_at=clock_timestamp()
                RETURNING *
                """,
                (
                    wallet["virtual_wallet_id"],
                    principal.tenant_id,
                    principal.subject_user_id,
                    str(payload.get("display_name") or wallet["display_name"]),
                    visibility,
                    delay,
                    str(payload.get("public_bio") or ""),
                ),
            )
            row = dict(cur.fetchone())
            conn.commit()
        return row

    def get_public_prediction_journal(
        self,
        principal: TenantPrincipal,
        *,
        virtual_wallet_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        selected_wallet = str(virtual_wallet_id)
        now = datetime.now(timezone.utc)
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT portfolio.*,wallet.owner_user_id
                FROM quant.paper_public_portfolios portfolio
                JOIN quant.paper_virtual_wallets wallet
                  ON wallet.virtual_wallet_id=portfolio.virtual_wallet_id
                WHERE portfolio.virtual_wallet_id=%s
                """,
                (selected_wallet,),
            )
            portfolio_row = cur.fetchone()
            if portfolio_row is None:
                raise LookupError("public portfolio was not found")
            portfolio = dict(portfolio_row)
            own = (
                portfolio["tenant_id"] == principal.tenant_id
                and portfolio["user_id"] == principal.subject_user_id
            )
            if not own and str(portfolio["visibility"]) == "PRIVATE":
                raise LookupError("public portfolio was not found")
            delay = timedelta(minutes=int(portfolio["disclosure_delay_minutes"] or 0))
            delayed_cutoff = now - delay
            cur.execute(
                """
                SELECT prediction_id,event_id,market_id,market_slug,category,
                       outcome_name,subjective_probability,confidence,thesis,
                       evidence_sources,invalidation_condition,exit_plan,
                       decision_market_price,capital_at_risk,decision_ts,
                       resolution_ts,resolved_outcome,
                       final_pre_resolution_price,visibility
                FROM quant.paper_prediction_journal
                WHERE virtual_wallet_id=%s
                  AND (
                    %s OR visibility='PUBLIC'
                    OR (visibility='DELAYED' AND decision_ts<=%s)
                  )
                  AND (%s OR %s<>'DELAYED' OR decision_ts<=%s)
                ORDER BY decision_ts DESC,prediction_id DESC LIMIT %s
                """,
                (
                    selected_wallet,
                    own,
                    delayed_cutoff,
                    own,
                    portfolio["visibility"],
                    delayed_cutoff,
                    max(1, min(int(limit), 500)),
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]
        return {
            "virtual_wallet_id": selected_wallet,
            "display_name": portfolio["display_name"],
            "visibility": portfolio["visibility"],
            "disclosure_delay_minutes": portfolio["disclosure_delay_minutes"],
            "as_of": now,
            "items": rows,
        }

    def get_public_portfolio(
        self,
        principal: TenantPrincipal,
        *,
        virtual_wallet_id: str,
        activity_limit: int = 100,
    ) -> dict[str, Any]:
        """Return an as-of public portfolio without leaking delayed state."""

        selected_wallet = str(virtual_wallet_id)
        now = datetime.now(timezone.utc)
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT portfolio.*,wallet.initial_balance,wallet.account_id,
                       account.ledger_strategy_id
                FROM quant.paper_public_portfolios portfolio
                JOIN quant.paper_virtual_wallets wallet
                  ON wallet.virtual_wallet_id=portfolio.virtual_wallet_id
                JOIN quant.paper_account_registry account
                  ON account.tenant_id=wallet.tenant_id
                 AND account.account_id=wallet.account_id
                WHERE portfolio.virtual_wallet_id=%s
                """,
                (selected_wallet,),
            )
            portfolio_row = cur.fetchone()
            if portfolio_row is None:
                raise LookupError("public portfolio was not found")
            portfolio = dict(portfolio_row)
            own = (
                portfolio["tenant_id"] == principal.tenant_id
                and portfolio["user_id"] == principal.subject_user_id
            )
            visibility = str(portfolio["visibility"])
            if not own and visibility == "PRIVATE":
                raise LookupError("public portfolio was not found")
            delay = timedelta(minutes=int(portfolio["disclosure_delay_minutes"] or 0))
            as_of = now if own or visibility == "PUBLIC" else now - delay
            strategy_id = str(portfolio["ledger_strategy_id"])
            cur.execute(
                """
                SELECT observed_at,equity,conservative_equity,total_pnl,
                       conservative_total_pnl,drawdown_pct,nav_complete
                FROM quant.paper_portfolio_nav_snapshots
                WHERE strategy_id=%s AND observed_at<=%s
                ORDER BY observed_at DESC,nav_id DESC LIMIT 1
                """,
                (strategy_id, as_of),
            )
            nav_row = cur.fetchone()
            nav = dict(nav_row) if nav_row is not None else {
                "observed_at": None,
                "equity": portfolio["initial_balance"],
                "conservative_equity": portfolio["initial_balance"],
                "total_pnl": Decimal(0),
                "conservative_total_pnl": Decimal(0),
                "drawdown_pct": Decimal(0),
                "nav_complete": False,
            }
            cur.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (ledger.asset_id)
                           ledger.asset_id,ledger.market_id,ledger.condition_id,
                           ledger.position_after AS quantity,
                           ledger.cost_basis_after AS cost_basis,
                           ledger.event_ts
                    FROM quant.paper_ledger_entries ledger
                    WHERE ledger.strategy_id=%s AND ledger.event_ts<=%s
                    ORDER BY ledger.asset_id,ledger.event_ts DESC,ledger.entry_id DESC
                )
                SELECT latest.*,registry.market_slug,registry.market_title,
                       registry.outcome_name
                FROM latest
                LEFT JOIN quant.paper_market_registry_tokens registry
                  ON registry.asset_id=latest.asset_id
                WHERE latest.quantity<>0
                ORDER BY COALESCE(registry.market_title,latest.asset_id)
                """,
                (strategy_id, as_of),
            )
            positions = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT ledger.entry_id,ledger.event_ts,ledger.event_type,
                       ledger.asset_id,ledger.price,ledger.shares_delta,
                       ledger.cash_delta,ledger.fee,ledger.realized_pnl_delta,
                       registry.market_slug,registry.market_title,
                       registry.outcome_name
                FROM quant.paper_ledger_entries ledger
                LEFT JOIN quant.paper_market_registry_tokens registry
                  ON registry.asset_id=ledger.asset_id
                WHERE ledger.strategy_id=%s AND ledger.event_ts<=%s
                ORDER BY ledger.event_ts DESC,ledger.entry_id DESC LIMIT %s
                """,
                (strategy_id, as_of, max(1, min(int(activity_limit), 500))),
            )
            activity = [dict(row) for row in cur.fetchall()]
        return {
            "virtual_wallet_id": selected_wallet,
            "display_name": portfolio["display_name"],
            "public_bio": portfolio["public_bio"],
            "visibility": visibility,
            "disclosure_delay_minutes": portfolio["disclosure_delay_minutes"],
            "as_of": as_of,
            "generated_at": now,
            "nav": nav,
            "positions": positions,
            "activity": activity,
            "current_state_disclosed": own or visibility == "PUBLIC",
        }

    def refresh_public_metrics(self, principal: TenantPrincipal) -> dict[str, Any]:
        """Publish only server-recomputed metrics for the current public portfolio."""

        wallet = self.get_default_wallet(principal)
        prediction = self.prediction_report(principal)
        strategy_id = str(wallet["ledger_strategy_id"])
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT COALESCE(equity,conservative_equity) AS nav,
                       CASE WHEN equity IS NOT NULL THEN 'RESEARCH_MID'
                            ELSE 'CONSERVATIVE' END AS nav_basis,
                       total_pnl,drawdown_pct,observed_at
                FROM quant.paper_portfolio_nav_current WHERE strategy_id=%s
                """,
                (strategy_id,),
            )
            nav = cur.fetchone()
            cur.execute(
                """
                SELECT avg(abs(implementation_shortfall)) AS mean_shortfall,
                       count(*) AS sample_count
                FROM quant.execution_tca
                WHERE strategy_id=%s AND status IN ('COMPLETE','LEGACY_COMPLETE')
                """,
                (strategy_id,),
            )
            tca = cur.fetchone()
            execution_score = None
            if tca is not None and int(tca["sample_count"] or 0) > 0:
                mean_shortfall = Decimal(str(tca["mean_shortfall"] or 0))
                execution_score = Decimal(1) / (Decimal(1) + mean_shortfall)
            if nav is not None:
                initial = Decimal(str(wallet["initial_balance"]))
                nav_value = Decimal(str(nav["nav"]))
                total_return = (
                    (nav_value - initial) / initial if initial > 0 else Decimal(0)
                )
                cur.execute(
                    """
                    UPDATE quant.paper_public_portfolios SET
                        last_public_nav=%s,last_public_return=%s,
                        last_public_drawdown_pct=%s,last_brier=%s,
                        last_execution_score=%s,metrics_as_of=%s,
                        updated_at=clock_timestamp()
                    WHERE virtual_wallet_id=%s AND tenant_id=%s AND user_id=%s
                    RETURNING *
                    """,
                    (
                        nav_value,
                        total_return,
                        nav["drawdown_pct"],
                        prediction.get("brier_score"),
                        execution_score,
                        nav["observed_at"],
                        wallet["virtual_wallet_id"],
                        principal.tenant_id,
                        principal.subject_user_id,
                    ),
                )
                row = cur.fetchone()
            else:
                row = None
            conn.commit()
        return dict(row) if row is not None else {
            "status": "NO_NAV_OR_PRIVATE_PORTFOLIO",
            "virtual_wallet_id": wallet["virtual_wallet_id"],
        }

    def set_follow(
        self,
        principal: TenantPrincipal,
        *,
        followed_wallet_id: str,
        enabled: bool,
    ) -> bool:
        follower = self.get_default_wallet(principal)
        followed = str(followed_wallet_id).strip()
        if followed == str(follower["virtual_wallet_id"]):
            raise ValueError("a portfolio cannot follow itself")
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT virtual_wallet_id FROM quant.paper_public_portfolios
                WHERE virtual_wallet_id=%s AND visibility IN ('PUBLIC','DELAYED')
                """,
                (followed,),
            )
            if cur.fetchone() is None:
                raise LookupError("public portfolio was not found")
            cur.execute(
                """
                INSERT INTO quant.paper_public_portfolios (
                    virtual_wallet_id,tenant_id,user_id,display_name,visibility
                ) VALUES (%s,%s,%s,%s,'PRIVATE')
                ON CONFLICT (virtual_wallet_id) DO NOTHING
                """,
                (
                    follower["virtual_wallet_id"],
                    principal.tenant_id,
                    principal.subject_user_id,
                    follower["display_name"],
                ),
            )
            if enabled:
                cur.execute(
                    """
                    INSERT INTO quant.paper_portfolio_follows (
                        follower_wallet_id,followed_wallet_id
                    ) VALUES (%s,%s) ON CONFLICT DO NOTHING
                    """,
                    (follower["virtual_wallet_id"], followed),
                )
            else:
                cur.execute(
                    """
                    DELETE FROM quant.paper_portfolio_follows
                    WHERE follower_wallet_id=%s AND followed_wallet_id=%s
                    """,
                    (follower["virtual_wallet_id"], followed),
                )
            conn.commit()
        return enabled

    def list_following(self, principal: TenantPrincipal) -> list[str]:
        wallet = self.get_default_wallet(principal)
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT followed_wallet_id FROM quant.paper_portfolio_follows
                WHERE follower_wallet_id=%s ORDER BY followed_wallet_id
                """,
                (wallet["virtual_wallet_id"],),
            )
            rows = [str(row["followed_wallet_id"]) for row in cur.fetchall()]
            conn.commit()
        return rows

    def create_competition(
        self,
        principal: TenantPrincipal,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("competition name is required")
        starts_at = _as_utc_datetime(payload.get("starts_at"), "starts_at")
        ends_at = _as_utc_datetime(payload.get("ends_at"), "ends_at")
        if ends_at <= starts_at:
            raise ValueError("competition ends_at must be after starts_at")
        initial_balance = _decimal(
            payload.get("initial_balance", "10000"), minimum=Decimal("0.01")
        )
        market_scope = _normalize_competition_scope(payload.get("market_scope"))
        rules = {
            "name": name,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "initial_balance": str(initial_balance),
            "market_scope": dict(market_scope),
            "score_version": "paper_competition_return_v1",
            "orders_are_server_timestamped": True,
            "history_backfill_allowed": False,
        }
        competition_id = uuid5(
            principal.tenant_id,
            f"paper-competition:{principal.subject_user_id}:{idempotency_key}",
        )
        status = "OPEN" if starts_at > datetime.now(timezone.utc) else "RUNNING"
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                INSERT INTO quant.paper_competitions (
                    competition_id,owner_tenant_id,owner_user_id,name,starts_at,
                    ends_at,initial_balance,market_scope,status,rules_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                ON CONFLICT (competition_id) DO UPDATE SET
                    name=EXCLUDED.name
                RETURNING *
                """,
                (
                    competition_id,
                    principal.tenant_id,
                    principal.subject_user_id,
                    name,
                    starts_at,
                    ends_at,
                    initial_balance,
                    json.dumps(dict(market_scope), sort_keys=True),
                    status,
                    _stable_hash(rules),
                ),
            )
            row = dict(cur.fetchone())
            conn.commit()
        return row

    def join_competition(
        self,
        principal: TenantPrincipal,
        competition_id: UUID,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.paper_competitions WHERE competition_id=%s
                """,
                (competition_id,),
            )
            competition_row = cur.fetchone()
        if competition_row is None:
            raise LookupError("competition was not found")
        competition = dict(competition_row)
        now = datetime.now(timezone.utc)
        if competition["ends_at"] <= now or competition["status"] in {
            "ENDED",
            "CANCELLED",
        }:
            raise ValueError("competition is not joinable")
        account = self.tenant_store.create_account(
            principal,
            name=f"Competition: {competition['name']}",
            initial_cash=Decimal(str(competition["initial_balance"])),
            idempotency_key=f"competition:{competition_id}:{idempotency_key}",
        )
        wallet_uuid = uuid5(
            competition_id,
            f"paper-competition-wallet:{principal.tenant_id}:{principal.subject_user_id}",
        )
        virtual_wallet_id = f"pwallet_comp_{wallet_uuid.hex[:21]}"
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                INSERT INTO quant.paper_virtual_wallets (
                    virtual_wallet_id,tenant_id,owner_user_id,account_id,
                    display_name,initial_balance,is_default
                ) VALUES (%s,%s,%s,%s,%s,%s,FALSE)
                ON CONFLICT (tenant_id,account_id) DO UPDATE SET
                    display_name=EXCLUDED.display_name
                RETURNING *
                """,
                (
                    virtual_wallet_id,
                    principal.tenant_id,
                    principal.subject_user_id,
                    account["account_id"],
                    f"Competition: {competition['name']}",
                    competition["initial_balance"],
                ),
            )
            wallet = dict(cur.fetchone())
            cur.execute(
                """
                INSERT INTO quant.paper_competition_memberships (
                    competition_id,virtual_wallet_id,starting_nav,server_score
                ) VALUES (%s,%s,%s,%s::jsonb)
                ON CONFLICT (competition_id,virtual_wallet_id) DO NOTHING
                """,
                (
                    competition_id,
                    virtual_wallet_id,
                    competition["initial_balance"],
                    json.dumps(
                        {
                            "return": "0",
                            "score_version": "paper_competition_return_v1",
                        },
                        sort_keys=True,
                    ),
                ),
            )
            cur.execute(
                """
                SELECT strategy_id FROM quant.paper_strategies
                WHERE tenant_id=%s AND account_id=%s AND idempotency_key='default'
                """,
                (principal.tenant_id, account["account_id"]),
            )
            wallet["strategy_id"] = cur.fetchone()["strategy_id"]
            conn.commit()
        return {"competition": competition, "wallet": wallet}

    def list_competitions(self, principal: TenantPrincipal) -> list[dict[str, Any]]:
        wallets = self.list_wallets(principal)
        wallet_ids = [str(row["virtual_wallet_id"]) for row in wallets]
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT competition.*,
                       membership.virtual_wallet_id,membership.joined_at,
                       membership.starting_nav,membership.status AS member_status,
                       membership.server_score,membership.score_as_of,
                       COALESCE(nav.equity,nav.conservative_equity)
                         AS current_nav,
                       CASE WHEN nav.equity IS NOT NULL THEN 'RESEARCH_MID'
                            WHEN nav.conservative_equity IS NOT NULL THEN 'CONSERVATIVE'
                            ELSE NULL END AS current_nav_basis,
                       nav.drawdown_pct
                FROM quant.paper_competitions competition
                LEFT JOIN quant.paper_competition_memberships membership
                  ON membership.competition_id=competition.competition_id
                 AND membership.virtual_wallet_id=ANY(%s::text[])
                LEFT JOIN quant.paper_virtual_wallets wallet
                  ON wallet.virtual_wallet_id=membership.virtual_wallet_id
                LEFT JOIN quant.paper_account_registry account
                  ON account.tenant_id=wallet.tenant_id
                 AND account.account_id=wallet.account_id
                LEFT JOIN quant.paper_portfolio_nav_current nav
                  ON nav.strategy_id=account.ledger_strategy_id
                WHERE competition.status<>'DRAFT'
                   OR (competition.owner_tenant_id=%s AND competition.owner_user_id=%s)
                ORDER BY competition.starts_at DESC,competition.competition_id
                """,
                (wallet_ids, principal.tenant_id, principal.subject_user_id),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        now = datetime.now(timezone.utc)
        for row in rows:
            row["effective_status"] = (
                "ENDED"
                if row["ends_at"] <= now
                else "RUNNING"
                if row["starts_at"] <= now
                else "OPEN"
            )
            current_nav = _decimal(row.get("current_nav"))
            starting_nav = _decimal(row.get("starting_nav"))
            row["server_return"] = (
                (current_nav - starting_nav) / starting_nav
                if current_nav is not None
                and starting_nav is not None
                and starting_nav > 0
                else None
            )
        return rows

    def get_competition_standings(
        self,
        principal: TenantPrincipal,
        *,
        competition_id: UUID,
        metric: str = "return",
    ) -> dict[str, Any]:
        selected_metric = str(metric or "return").casefold()
        if selected_metric not in {"return", "risk_adjusted", "brier", "execution"}:
            raise ValueError("invalid competition standings metric")
        own_wallet_ids = {
            str(row["virtual_wallet_id"]) for row in self.list_wallets(principal)
        }
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT * FROM quant.paper_competitions
                WHERE competition_id=%s
                  AND (status<>'DRAFT'
                       OR (owner_tenant_id=%s AND owner_user_id=%s))
                """,
                (
                    competition_id,
                    principal.tenant_id,
                    principal.subject_user_id,
                ),
            )
            competition_row = cur.fetchone()
            if competition_row is None:
                conn.rollback()
                raise LookupError("competition was not found")
            competition = dict(competition_row)
            cutoff = min(datetime.now(timezone.utc), competition["ends_at"])
            cur.execute(
                """
                SELECT membership.*,
                       CASE WHEN wallet.owner_user_id=%s THEN wallet.display_name
                            WHEN public.visibility='PUBLIC' THEN public.display_name
                            ELSE 'Paper participant' END AS display_name,
                       wallet.owner_user_id,
                       wallet.tenant_id,account.ledger_strategy_id,
                       COALESCE(snapshot.equity,snapshot.conservative_equity,
                                membership.starting_nav) AS score_nav,
                       snapshot.drawdown_pct,snapshot.observed_at AS nav_as_of,
                       prediction.brier_score,prediction.resolved_count,
                       execution.execution_score,execution.execution_count
                FROM quant.paper_competition_memberships membership
                JOIN quant.paper_competitions competition
                  ON competition.competition_id=membership.competition_id
                JOIN quant.paper_virtual_wallets wallet
                  ON wallet.virtual_wallet_id=membership.virtual_wallet_id
                JOIN quant.paper_account_registry account
                  ON account.tenant_id=wallet.tenant_id
                 AND account.account_id=wallet.account_id
                LEFT JOIN quant.paper_public_portfolios public
                  ON public.virtual_wallet_id=wallet.virtual_wallet_id
                LEFT JOIN LATERAL (
                    SELECT nav.equity,nav.conservative_equity,nav.drawdown_pct,
                           nav.observed_at
                    FROM quant.paper_portfolio_nav_snapshots nav
                    WHERE nav.strategy_id=account.ledger_strategy_id
                      AND nav.observed_at<=%s
                    ORDER BY nav.observed_at DESC,nav.nav_id DESC LIMIT 1
                ) snapshot ON TRUE
                LEFT JOIN LATERAL (
                    SELECT avg(power(journal.subjective_probability-
                                     journal.resolved_outcome,2)) AS brier_score,
                           count(*) AS resolved_count
                    FROM quant.paper_prediction_journal journal
                    WHERE journal.virtual_wallet_id=wallet.virtual_wallet_id
                      AND journal.resolution_ts IS NOT NULL
                      AND journal.decision_ts>=competition.starts_at
                      AND journal.decision_ts<=competition.ends_at
                ) prediction ON TRUE
                LEFT JOIN LATERAL (
                    SELECT CASE WHEN count(*)>0 THEN
                               1/(1+avg(abs(tca.implementation_shortfall)))
                           ELSE NULL END AS execution_score,
                           count(*) AS execution_count
                    FROM quant.execution_tca tca
                    WHERE tca.strategy_id=account.ledger_strategy_id
                      AND tca.status IN ('COMPLETE','LEGACY_COMPLETE')
                      AND tca.updated_at>=competition.starts_at
                      AND tca.updated_at<=competition.ends_at
                ) execution ON TRUE
                WHERE membership.competition_id=%s
                  AND membership.status='ACTIVE'
                ORDER BY membership.joined_at,membership.virtual_wallet_id
                """,
                (principal.subject_user_id, cutoff, competition_id),
            )
            rows = [dict(row) for row in cur.fetchall()]
            for row in rows:
                starting_nav = Decimal(str(row["starting_nav"]))
                score_nav = Decimal(str(row["score_nav"]))
                total_return = (
                    (score_nav - starting_nav) / starting_nav
                    if starting_nav > 0
                    else None
                )
                drawdown = _decimal(row.get("drawdown_pct")) or Decimal(0)
                risk_adjusted = (
                    total_return / (Decimal(1) + drawdown)
                    if total_return is not None
                    else None
                )
                score = {
                    "score_version": "paper_competition_multimetric_v1",
                    "cutoff": cutoff.isoformat(),
                    "nav": str(score_nav),
                    "return": str(total_return) if total_return is not None else None,
                    "drawdown_pct": str(drawdown),
                    "risk_adjusted": (
                        str(risk_adjusted) if risk_adjusted is not None else None
                    ),
                    "brier": (
                        str(row["brier_score"])
                        if row.get("brier_score") is not None
                        else None
                    ),
                    "resolved_prediction_count": int(
                        row.get("resolved_count") or 0
                    ),
                    "execution": (
                        str(row["execution_score"])
                        if row.get("execution_score") is not None
                        else None
                    ),
                    "execution_count": int(row.get("execution_count") or 0),
                    "nav_as_of": (
                        row["nav_as_of"].isoformat()
                        if isinstance(row.get("nav_as_of"), datetime)
                        else None
                    ),
                }
                row["computed_score"] = score
                cur.execute(
                    """
                    UPDATE quant.paper_competition_memberships SET
                        server_score=%s::jsonb,score_as_of=%s
                    WHERE competition_id=%s AND virtual_wallet_id=%s
                    """,
                    (
                        json.dumps(score, sort_keys=True),
                        cutoff,
                        competition_id,
                        row["virtual_wallet_id"],
                    ),
                )
            conn.commit()

        def sort_key(row: Mapping[str, Any]) -> tuple[bool, Decimal]:
            raw = row["computed_score"].get(selected_metric)
            value = _decimal(raw)
            if value is None:
                return True, Decimal(0)
            return False, value if selected_metric == "brier" else -value

        rows.sort(key=sort_key)
        standings: list[dict[str, Any]] = []
        for rank, row in enumerate(rows, start=1):
            wallet_id = str(row["virtual_wallet_id"])
            own = wallet_id in own_wallet_ids
            standings.append(
                {
                    "rank": rank,
                    "participant_id": "player_"
                    + hashlib.sha256(
                        f"{competition_id}:{wallet_id}".encode()
                    ).hexdigest()[:12],
                    "display_name": str(row["display_name"]),
                    "is_current_user": own,
                    "virtual_wallet_id": wallet_id if own else None,
                    "score": row["computed_score"],
                }
            )
        return {
            "competition_id": str(competition_id),
            "name": competition["name"],
            "effective_status": (
                "ENDED"
                if competition["ends_at"] <= datetime.now(timezone.utc)
                else "RUNNING"
                if competition["starts_at"] <= datetime.now(timezone.utc)
                else "OPEN"
            ),
            "metric": selected_metric,
            "score_cutoff": cutoff,
            "score_version": "paper_competition_multimetric_v1",
            "items": standings,
        }

    def create_data_subject_request(
        self,
        principal: TenantPrincipal,
        *,
        request_type: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        selected = str(request_type).upper()
        if selected not in {"EXPORT", "DELETE"}:
            raise ValueError("request_type must be EXPORT or DELETE")
        request_id = uuid5(
            principal.tenant_id,
            f"paper-data-request:{principal.subject_user_id}:{idempotency_key}",
        )
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                INSERT INTO quant.paper_data_subject_requests (
                    request_id,tenant_id,user_id,request_type,reason
                ) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (request_id) DO UPDATE SET reason=EXCLUDED.reason
                RETURNING *
                """,
                (
                    request_id,
                    principal.tenant_id,
                    principal.subject_user_id,
                    selected,
                    str(reason),
                ),
            )
            row = dict(cur.fetchone())
            conn.commit()
        return row

    def list_data_subject_requests(
        self, principal: TenantPrincipal
    ) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT * FROM quant.paper_data_subject_requests
                WHERE tenant_id=%s AND user_id=%s
                ORDER BY requested_at DESC,request_id
                """,
                (principal.tenant_id, principal.subject_user_id),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        for row in rows:
            row.pop("artifact_path", None)
        return rows

    def get_data_subject_export(
        self, principal: TenantPrincipal, request_id: UUID
    ) -> tuple[dict[str, Any], bytes]:
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT * FROM quant.paper_data_subject_requests
                WHERE tenant_id=%s AND user_id=%s AND request_id=%s
                  AND request_type='EXPORT'
                """,
                (principal.tenant_id, principal.subject_user_id, request_id),
            )
            selected = cur.fetchone()
            conn.commit()
        if selected is None:
            raise LookupError("data export request was not found")
        row = dict(selected)
        if row["status"] != "COMPLETED" or not row.get("artifact_path"):
            raise ValueError("data export is not ready")
        allowed_root = Path(
            os.environ.get(
                "PAPER_RETAIL_EXPORT_ROOT", "runtime_outputs/paper_retail/exports"
            )
        ).resolve()
        path = Path(str(row["artifact_path"])).resolve()
        if not path.is_relative_to(allowed_root) or not path.is_file():
            raise ValueError("data export artifact path is invalid")
        payload = path.read_bytes()
        observed_hash = hashlib.sha256(payload).hexdigest()
        if observed_hash != str(row.get("artifact_sha256") or ""):
            raise ValueError("data export artifact checksum mismatch")
        return row, payload

    def create_official_history_sync_request(
        self,
        principal: TenantPrincipal,
        *,
        window_start: datetime,
        window_end: datetime,
        compare_to_paper: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Queue an immutable official-wallet shadow import and optional comparison."""

        start = _as_utc_datetime(window_start, "window_start")
        end = _as_utc_datetime(window_end, "window_end")
        if end <= start:
            raise ValueError("official history window_end must be after window_start")
        if end > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("official history window_end cannot be in the future")
        wallet = self.get_default_wallet(principal)
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT wallet_address FROM quant.paper_identity_bindings
                WHERE tenant_id=%s AND user_id=%s AND provider='EVM'
                  AND verified_at IS NOT NULL AND wallet_address IS NOT NULL
                ORDER BY verified_at DESC LIMIT 1
                """,
                (principal.tenant_id, principal.subject_user_id),
            )
            binding = cur.fetchone()
            if binding is None:
                conn.rollback()
                raise ValueError(
                    "official history sync requires a verified EVM wallet login"
                )
            account_address = str(binding["wallet_address"]).lower()
            request_id = uuid5(
                principal.tenant_id,
                f"retail-official-history:{principal.subject_user_id}:{idempotency_key}",
            )
            shadow_strategy_id = (
                "official-shadow:"
                + hashlib.sha256(
                    f"{wallet['virtual_wallet_id']}:{account_address}".encode()
                ).hexdigest()[:32]
            )
            cur.execute(
                """
                INSERT INTO quant.paper_official_history_sync_requests (
                    request_id,tenant_id,user_id,virtual_wallet_id,
                    account_address,paper_strategy_id,shadow_strategy_id,
                    window_start,window_end,comparison_scope,request_key
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id,user_id,request_key) DO UPDATE SET
                    updated_at=quant.paper_official_history_sync_requests.updated_at
                RETURNING *
                """,
                (
                    request_id,
                    principal.tenant_id,
                    principal.subject_user_id,
                    wallet["virtual_wallet_id"],
                    account_address,
                    str(wallet["ledger_strategy_id"]),
                    shadow_strategy_id,
                    start,
                    end,
                    "WHOLE_ACCOUNT" if compare_to_paper else "OFFICIAL_ONLY",
                    str(idempotency_key),
                ),
            )
            row = dict(cur.fetchone())
            conn.commit()
        row["ledger_overwritten"] = False
        row["shadow_only"] = True
        return row

    def list_official_history_sync_requests(
        self, principal: TenantPrincipal, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("invalid official history request limit")
        with self.tenant_store._transaction(
            principal, self._account_read_permission()
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT * FROM quant.paper_official_history_sync_requests
                WHERE tenant_id=%s AND user_id=%s
                ORDER BY requested_at DESC,request_id DESC LIMIT %s
                """,
                (principal.tenant_id, principal.subject_user_id, limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
            conn.commit()
        for row in rows:
            row["ledger_overwritten"] = False
            row["shadow_only"] = True
        return rows

    def list_leaderboard(self, *, metric: str = "return", limit: int = 100) -> list[dict[str, Any]]:
        columns = {
            "return": "last_public_return",
            "drawdown": "last_public_drawdown_pct",
            "brier": "last_brier",
            "execution": "last_execution_score",
        }
        column = columns.get(str(metric).lower())
        if column is None:
            raise ValueError("unsupported leaderboard metric")
        direction = "ASC" if metric in {"drawdown", "brier"} else "DESC"
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT portfolio.virtual_wallet_id,portfolio.display_name,
                       portfolio.last_public_nav,
                       last_public_return,last_public_drawdown_pct,last_brier,
                       last_execution_score,metrics_as_of,
                       (SELECT count(*) FROM quant.paper_portfolio_follows follow
                        WHERE follow.followed_wallet_id=portfolio.virtual_wallet_id)
                         AS follower_count
                FROM quant.paper_public_portfolios portfolio
                WHERE portfolio.visibility='PUBLIC' AND {column} IS NOT NULL
                ORDER BY {column} {direction},virtual_wallet_id LIMIT %s
                """,
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]


def _build_pnl_attribution(
    *,
    ledger: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    account: Mapping[str, Any],
    rebates: list[dict[str, Any]],
    as_of: datetime,
    strategy_id: str,
) -> dict[str, Any]:
    first_entry: dict[str, datetime] = {}
    by_asset: dict[str, dict[str, Any]] = {}
    components: dict[str, Decimal] = {
        "trade_realized_pnl": Decimal(0),
        "settlement_realized_pnl": Decimal(0),
        "fees": Decimal(0),
        "received_rebates_rewards": Decimal(0),
        "external_capital_flow": Decimal(0),
    }
    traded_notional = Decimal(0)
    close_event_count = 0
    profitable_close_event_count = 0
    breakeven_close_event_count = 0
    gross_profit = Decimal(0)
    gross_loss = Decimal(0)
    for row in ledger:
        asset_id = str(row["asset_id"])
        if Decimal(str(row["shares_delta"])) > 0:
            first_entry.setdefault(asset_id, row["event_ts"])
        event_type = str(row["event_type"]).upper()
        fee = Decimal(str(row.get("fee") or 0))
        realized = Decimal(str(row.get("realized_pnl_delta") or 0))
        price = Decimal(str(row.get("price") or 0))
        shares_delta = Decimal(str(row.get("shares_delta") or 0))
        if event_type in {"BUY", "SELL"}:
            traded_notional += abs(price * shares_delta)
        if event_type in {"SELL", "SETTLEMENT", "REDEEM", "SETTLEMENT_REDEEM"}:
            close_event_count += 1
            if realized > 0:
                profitable_close_event_count += 1
                gross_profit += realized
            elif realized < 0:
                gross_loss += abs(realized)
            else:
                breakeven_close_event_count += 1
        components["fees"] += fee
        if event_type in {"SETTLEMENT", "REDEEM", "SETTLEMENT_REDEEM"}:
            components["settlement_realized_pnl"] += realized
        else:
            components["trade_realized_pnl"] += realized
        if event_type in {
            "DEPOSIT",
            "BRIDGE_DEPOSIT",
            "WITHDRAWAL",
            "BRIDGE_WITHDRAWAL",
        }:
            components["external_capital_flow"] += Decimal(
                str(row.get("cash_delta") or 0)
            )
        selected = by_asset.setdefault(asset_id, _empty_attribution_row(row))
        selected["realized_pnl"] += realized
        selected["fees"] += fee
        selected["cash_delta"] += Decimal(str(row.get("cash_delta") or 0))
        if row.get("post_only") is True:
            selected["maker_event_count"] += 1
        elif row.get("client_order_id"):
            selected["taker_event_count"] += 1

    for rebate in rebates:
        if str(rebate["state"]).upper() == "RECEIVED":
            components["received_rebates_rewards"] += Decimal(
                str(rebate["amount"] or 0)
            )

    unpriced_quantity = Decimal(0)
    for position in positions:
        asset_id = str(position["asset_id"])
        selected = by_asset.setdefault(asset_id, _empty_attribution_row(position))
        qty = Decimal(str(position["quantity"]))
        basis = Decimal(str(position["cost_basis"]))
        selected["quantity"] = qty
        selected["gross_basis"] = basis
        mark = _decimal(position.get("research_mark"))
        selected["mark_quality"] = position.get("mark_quality")
        if mark is None:
            selected["unrealized_pnl"] = None
            selected["marked_value"] = None
            unpriced_quantity += abs(qty)
        else:
            selected["marked_value"] = qty * mark
            selected["unrealized_pnl"] = selected["marked_value"] - basis
        entered = first_entry.get(asset_id)
        selected["holding_period"] = _holding_period_bucket(
            as_of - entered if entered is not None else None
        )

    asset_rows = []
    for selected in by_asset.values():
        unrealized = selected["unrealized_pnl"]
        selected["economic_pnl"] = (
            selected["realized_pnl"] + unrealized
            if unrealized is not None
            else None
        )
        selected["execution_role"] = (
            "MIXED"
            if selected["maker_event_count"] and selected["taker_event_count"]
            else "MAKER"
            if selected["maker_event_count"]
            else "TAKER"
            if selected["taker_event_count"]
            else "ACCOUNT_OPERATION"
        )
        asset_rows.append(selected)

    dimensions = {
        "market": _group_attribution(asset_rows, "market_slug", "market_title"),
        "event": _group_attribution(asset_rows, "event_id", "event_id"),
        "category": _group_attribution(asset_rows, "category", "category"),
        "outcome": _group_attribution(asset_rows, "outcome_name", "outcome_name"),
        "execution_role": _group_attribution(
            asset_rows, "execution_role", "execution_role"
        ),
        "holding_period": _group_attribution(
            asset_rows, "holding_period", "holding_period"
        ),
    }
    attributed_realized = sum(
        (row["realized_pnl"] for row in asset_rows), Decimal(0)
    )
    authoritative_realized = Decimal(str(account["realized_pnl"]))
    initial_cash = Decimal(str(account.get("initial_cash") or 0))
    fee_total = components["fees"]
    received_rewards = components["received_rebates_rewards"]
    return {
        "status": "COMPLETE" if unpriced_quantity == 0 else "DEGRADED",
        "as_of": as_of,
        "strategy_id": strategy_id,
        "components": components,
        "asset_rows": asset_rows,
        "dimensions": dimensions,
        "unpriced_quantity": unpriced_quantity,
        "metrics": {
            "traded_notional": traded_notional,
            "turnover": (
                traded_notional / initial_cash if initial_cash > 0 else None
            ),
            "fee_to_notional": (
                fee_total / traded_notional if traded_notional > 0 else None
            ),
            "close_event_count": close_event_count,
            "profitable_close_event_count": profitable_close_event_count,
            "breakeven_close_event_count": breakeven_close_event_count,
            "win_rate": (
                Decimal(profitable_close_event_count) / Decimal(close_event_count)
                if close_event_count > 0
                else None
            ),
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": (
                gross_profit / gross_loss if gross_loss > 0 else None
            ),
            "realized_plus_received_rewards": (
                authoritative_realized + received_rewards
            ),
            "fee_semantics": "ALREADY_INCLUDED_IN_CASH_COST_AND_REALIZED_PNL",
        },
        "reconciliation": {
            "authoritative_realized_pnl": authoritative_realized,
            "attributed_realized_pnl": attributed_realized,
            "delta": authoritative_realized - attributed_realized,
            "status": (
                "MATCH"
                if authoritative_realized == attributed_realized
                else "MISMATCH"
            ),
        },
    }


def _normalize_competition_scope(value: Any) -> dict[str, list[str]]:
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("market_scope must be an object")
    allowed_keys = {
        "asset_ids",
        "market_slugs",
        "condition_ids",
        "event_ids",
        "categories",
    }
    unknown = sorted(set(value) - allowed_keys)
    if unknown:
        raise ValueError(f"unsupported market_scope keys: {','.join(unknown)}")
    normalized: dict[str, list[str]] = {}
    for key, raw_values in value.items():
        if not isinstance(raw_values, (list, tuple, set)):
            raise ValueError(f"market_scope.{key} must be an array")
        selected = sorted(
            {
                str(item).strip()
                for item in raw_values
                if item is not None and str(item).strip()
            }
        )
        if selected:
            normalized[str(key)] = selected
    return normalized


def _parse_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean preference must be true or false")


def _empty_attribution_row(source: Mapping[str, Any]) -> dict[str, Any]:
    market_slug = str(source.get("market_slug") or source.get("market_id") or "")
    market_title = str(source.get("market_title") or market_slug or "Unknown market")
    return {
        "asset_id": str(source.get("asset_id") or ""),
        "market_id": str(source.get("market_id") or ""),
        "market_slug": market_slug,
        "market_title": market_title,
        "condition_id": str(source.get("condition_id") or ""),
        "event_id": str(source.get("event_id") or source.get("condition_id") or ""),
        "category": str(source.get("category") or "other"),
        "outcome_name": str(source.get("outcome_name") or "UNKNOWN"),
        "quantity": Decimal(0),
        "gross_basis": Decimal(0),
        "marked_value": Decimal(0),
        "realized_pnl": Decimal(0),
        "unrealized_pnl": Decimal(0),
        "economic_pnl": Decimal(0),
        "fees": Decimal(0),
        "cash_delta": Decimal(0),
        "maker_event_count": 0,
        "taker_event_count": 0,
        "execution_role": "ACCOUNT_OPERATION",
        "holding_period": "NO_OPEN_POSITION",
        "mark_quality": None,
    }


def _holding_period_bucket(age: timedelta | None) -> str:
    if age is None:
        return "NO_OPEN_POSITION"
    seconds = max(age.total_seconds(), 0)
    if seconds < 3600:
        return "LT_1H"
    if seconds < 86_400:
        return "1H_1D"
    if seconds < 604_800:
        return "1D_7D"
    if seconds < 2_592_000:
        return "7D_30D"
    return "GE_30D"


def _group_attribution(
    rows: list[dict[str, Any]], key_field: str, label_field: str
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get(key_field) or "UNKNOWN")
        selected = grouped.setdefault(
            key,
            {
                "key": key,
                "label": str(row.get(label_field) or key),
                "asset_count": 0,
                "quantity": Decimal(0),
                "gross_basis": Decimal(0),
                "marked_value": Decimal(0),
                "realized_pnl": Decimal(0),
                "unrealized_pnl": Decimal(0),
                "economic_pnl": Decimal(0),
                "fees": Decimal(0),
                "unpriced_asset_count": 0,
            },
        )
        selected["asset_count"] += 1
        selected["quantity"] += Decimal(str(row.get("quantity") or 0))
        selected["gross_basis"] += Decimal(str(row.get("gross_basis") or 0))
        selected["realized_pnl"] += Decimal(str(row.get("realized_pnl") or 0))
        selected["fees"] += Decimal(str(row.get("fees") or 0))
        marked_value = row.get("marked_value")
        unrealized = row.get("unrealized_pnl")
        economic = row.get("economic_pnl")
        if marked_value is None or unrealized is None or economic is None:
            selected["unpriced_asset_count"] += 1
            continue
        selected["marked_value"] += Decimal(str(marked_value))
        selected["unrealized_pnl"] += Decimal(str(unrealized))
        selected["economic_pnl"] += Decimal(str(economic))
    for selected in grouped.values():
        if selected["unpriced_asset_count"]:
            selected["marked_value"] = None
            selected["unrealized_pnl"] = None
            selected["economic_pnl"] = None
    return sorted(grouped.values(), key=lambda row: (row["label"], row["key"]))


def retail_identity_key(provider: str, subject: str) -> str:
    return hashlib.sha256(
        f"paper-retail:{provider.upper()}:{subject.lower()}".encode()
    ).hexdigest()


__all__ = [
    "PostgresRetailPaperService",
    "RETAIL_SCHEMA_STATEMENTS",
    "normalize_evm_address",
    "retail_identity_key",
]
