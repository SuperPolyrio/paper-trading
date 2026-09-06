"""Idempotent PostgreSQL and artifact storage for account truth."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import (
    AccountingEquity,
    AccountingPosition,
    AccountTruthMismatch,
    AccountTruthReport,
    ClosedPosition,
    MismatchType,
    OfficialAccountBundle,
    OfficialPosition,
    ParsedAccountingSnapshot,
)
from .official_account_client import OfficialFetchResult

ACCOUNT_TRUTH_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_account_fetch_artifacts (
        artifact_id TEXT PRIMARY KEY,
        account_address TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        http_status INTEGER NOT NULL,
        content_type TEXT NOT NULL,
        request_parameters JSONB NOT NULL,
        response_headers JSONB NOT NULL,
        payload_hash TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        artifact_path TEXT NOT NULL,
        byte_count BIGINT NOT NULL,
        schema_version TEXT NOT NULL,
        fetch_status TEXT NOT NULL,
        error_code TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (account_address,endpoint,content_sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_account_snapshot_runs (
        run_id TEXT PRIMARY KEY,
        account_address TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        source_as_of TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL,
        positions_artifact_id TEXT NOT NULL,
        closed_positions_artifact_id TEXT NOT NULL,
        accounting_artifact_id TEXT NOT NULL,
        positions_payload_hash TEXT NOT NULL,
        closed_positions_payload_hash TEXT NOT NULL,
        accounting_zip_sha256 TEXT NOT NULL,
        positions_csv_sha256 TEXT NOT NULL,
        equity_csv_sha256 TEXT NOT NULL,
        position_count INTEGER NOT NULL,
        closed_position_count INTEGER NOT NULL,
        accounting_position_count INTEGER NOT NULL,
        errors JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (
            account_address,positions_payload_hash,
            closed_positions_payload_hash,accounting_zip_sha256
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_official_account_snapshot_latest_idx
    ON quant.paper_official_account_snapshot_runs
       (account_address,source_as_of DESC,observed_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_account_truth_baselines (
        baseline_id TEXT PRIMARY KEY,
        scope_id TEXT NOT NULL UNIQUE,
        account_address TEXT NOT NULL,
        official_run_id TEXT NOT NULL,
        strategy_ids JSONB NOT NULL,
        source_as_of TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL,
        baseline_hash TEXT NOT NULL,
        baseline_payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_account_truth_baseline_account_idx
    ON quant.paper_account_truth_baselines (account_address,source_as_of DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_position_snapshots (
        run_id TEXT NOT NULL,
        account_address TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        size NUMERIC NOT NULL,
        avg_price NUMERIC NOT NULL,
        initial_value NUMERIC NOT NULL,
        gross_initial_value NUMERIC,
        entry_fees_usdc NUMERIC,
        current_value NUMERIC NOT NULL,
        cash_pnl NUMERIC NOT NULL,
        realized_pnl NUMERIC NOT NULL,
        current_price NUMERIC NOT NULL,
        total_bought NUMERIC NOT NULL,
        redeemable BOOLEAN NOT NULL,
        mergeable BOOLEAN NOT NULL,
        title TEXT NOT NULL,
        slug TEXT NOT NULL,
        outcome TEXT NOT NULL,
        outcome_index INTEGER,
        raw_payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (run_id,asset_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_closed_position_snapshots (
        run_id TEXT NOT NULL,
        row_key TEXT NOT NULL,
        account_address TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        avg_price NUMERIC NOT NULL,
        total_bought NUMERIC NOT NULL,
        realized_pnl NUMERIC NOT NULL,
        current_price NUMERIC NOT NULL,
        closed_at TIMESTAMPTZ,
        title TEXT NOT NULL,
        slug TEXT NOT NULL,
        outcome TEXT NOT NULL,
        outcome_index INTEGER,
        raw_payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (run_id,row_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_accounting_position_snapshots (
        run_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        size NUMERIC NOT NULL,
        current_price NUMERIC NOT NULL,
        current_value NUMERIC NOT NULL,
        valuation_time TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (run_id,asset_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_equity_snapshots (
        run_id TEXT PRIMARY KEY,
        cash_balance NUMERIC NOT NULL,
        positions_value NUMERIC NOT NULL,
        equity NUMERIC NOT NULL,
        valuation_time TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_account_truth_reconciliation_runs (
        reconciliation_id TEXT PRIMARY KEY,
        official_run_id TEXT NOT NULL,
        account_address TEXT NOT NULL,
        strategy_ids JSONB NOT NULL,
        as_of TIMESTAMPTZ NOT NULL,
        generated_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL,
        official_source_status TEXT NOT NULL,
        comparison_scope TEXT NOT NULL,
        mismatch_count INTEGER NOT NULL,
        report_hash TEXT NOT NULL,
        summary JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (official_run_id,report_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_account_truth_reconciliation_items (
        reconciliation_id TEXT NOT NULL,
        mismatch_id TEXT NOT NULL,
        mismatch_type TEXT NOT NULL,
        comparison_type TEXT NOT NULL,
        comparison_key TEXT NOT NULL,
        field_name TEXT NOT NULL,
        official_value TEXT,
        paper_value TEXT,
        delta TEXT,
        tolerance TEXT NOT NULL,
        reason TEXT NOT NULL,
        severity TEXT NOT NULL,
        retryable BOOLEAN NOT NULL,
        evidence JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (reconciliation_id,mismatch_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_account_truth_mismatch_latest_idx
    ON quant.paper_account_truth_reconciliation_items
       (mismatch_type,created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_account_truth_mismatch_states (
        mismatch_state_id TEXT PRIMARY KEY,
        account_address TEXT NOT NULL,
        comparison_scope TEXT NOT NULL,
        strategy_scope_hash TEXT NOT NULL,
        comparison_type TEXT NOT NULL,
        comparison_key TEXT NOT NULL,
        field_name TEXT NOT NULL,
        original_mismatch_type TEXT NOT NULL,
        first_seen_at TIMESTAMPTZ NOT NULL,
        last_seen_at TIMESTAMPTZ NOT NULL,
        convergence_deadline TIMESTAMPTZ NOT NULL,
        occurrence_count INTEGER NOT NULL,
        status TEXT NOT NULL,
        last_observation_id TEXT NOT NULL,
        last_mismatch_id TEXT NOT NULL,
        last_evidence JSONB NOT NULL,
        resolved_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_account_truth_mismatch_state_active_idx
    ON quant.paper_account_truth_mismatch_states (
        account_address,comparison_scope,strategy_scope_hash,status,last_seen_at DESC
    )
    """,
)


class PostgresAccountTruthStore:
    def __init__(
        self,
        connection_factory: Any,
        *,
        artifact_root: Path | str = "runtime_outputs/account_truth/raw",
    ) -> None:
        self.connection_factory = connection_factory
        self.artifact_root = Path(artifact_root)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS quant")
            for statement in ACCOUNT_TRUTH_SCHEMA_STATEMENTS:
                cur.execute(statement)
            conn.commit()

    def persist_fetch_artifact(
        self, *, account_address: str, result: OfficialFetchResult
    ) -> str:
        if result.fetch_status == "FAIL":
            suffix = ".error"
        else:
            suffix = ".zip" if "zip" in result.content_type.lower() else ".json"
        endpoint_slug = result.endpoint.strip("/").replace("/", "-") or "root"
        path = (
            self.artifact_root
            / account_address.lower()
            / endpoint_slug
            / f"{result.payload_hash}{suffix}"
        )
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT artifact_id,artifact_path,content_sha256
                FROM quant.paper_official_account_fetch_artifacts
                WHERE account_address=%s AND endpoint=%s AND content_sha256=%s
                """,
                (account_address.lower(), result.endpoint, result.payload_hash),
            )
            existing = cur.fetchone()
        if existing is not None:
            existing_path = Path(str(existing["artifact_path"]))
            if not existing_path.exists():
                raise RuntimeError(
                    f"account truth artifact manifest points to a missing file: {existing_path}"
                )
            if hashlib.sha256(existing_path.read_bytes()).hexdigest() != str(
                existing["content_sha256"]
            ):
                raise RuntimeError(
                    f"account truth artifact manifest hash mismatch: {existing_path}"
                )
            # A capture-specific artifact root must remain self-contained even
            # when the global DB row deduplicates this payload against an older
            # capture stored elsewhere.
            _atomic_content_addressed_write(path, result.content, result.payload_hash)
            return str(existing["artifact_id"])
        artifact_id = _identifier(
            "official-fetch",
            account_address.lower(),
            result.endpoint,
            result.payload_hash,
        )
        _atomic_content_addressed_write(path, result.content, result.payload_hash)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_official_account_fetch_artifacts (
                    artifact_id,account_address,endpoint,observed_at,http_status,
                    content_type,request_parameters,response_headers,payload_hash,
                    content_sha256,artifact_path,byte_count,schema_version,
                    fetch_status,error_code
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,
                    'official-account-fetch-v1',%s,%s
                ) ON CONFLICT DO NOTHING
                RETURNING artifact_id
                """,
                (
                    artifact_id,
                    account_address.lower(),
                    result.endpoint,
                    result.observed_at,
                    result.http_status,
                    result.content_type,
                    _json(result.request_parameters),
                    _json(result.response_headers),
                    result.payload_hash,
                    result.payload_hash,
                    str(path.resolve()),
                    len(result.content),
                    result.fetch_status,
                    result.error_code,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        if row is not None:
            return str(row["artifact_id"])

        # Another capture can commit the same content between the optimistic
        # read above and this insert. Re-read after the conflicting transaction
        # has completed, then verify that this was idempotency rather than an
        # identifier collision.
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT artifact_id,account_address,endpoint,content_sha256,
                       artifact_path
                FROM quant.paper_official_account_fetch_artifacts
                WHERE artifact_id=%s
                   OR (account_address=%s AND endpoint=%s AND content_sha256=%s)
                """,
                (
                    artifact_id,
                    account_address.lower(),
                    result.endpoint,
                    result.payload_hash,
                ),
            )
            conflict = cur.fetchone()
        if conflict is None:
            raise RuntimeError("account truth artifact conflict row disappeared")
        if (
            str(conflict["account_address"]) != account_address.lower()
            or str(conflict["endpoint"]) != result.endpoint
            or str(conflict["content_sha256"]) != result.payload_hash
        ):
            raise RuntimeError("account truth artifact identifier collision")
        conflict_path = Path(str(conflict["artifact_path"]))
        if not conflict_path.exists():
            raise RuntimeError(
                f"account truth artifact manifest points to a missing file: {conflict_path}"
            )
        if hashlib.sha256(conflict_path.read_bytes()).hexdigest() != result.payload_hash:
            raise RuntimeError(
                f"account truth artifact manifest hash mismatch: {conflict_path}"
            )
        return str(conflict["artifact_id"])

    def persist_bundle(
        self,
        bundle: OfficialAccountBundle,
        *,
        positions_artifact_id: str,
        closed_artifact_id: str,
        accounting_artifact_id: str,
    ) -> str:
        manifest = dict(bundle.fetch_manifest)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_official_account_snapshot_runs (
                    run_id,account_address,observed_at,source_as_of,status,
                    positions_artifact_id,closed_positions_artifact_id,
                    accounting_artifact_id,positions_payload_hash,
                    closed_positions_payload_hash,accounting_zip_sha256,
                    positions_csv_sha256,equity_csv_sha256,position_count,
                    closed_position_count,accounting_position_count,errors
                ) VALUES (
                    %s,%s,%s,%s,'PASS',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'[]'::jsonb
                ) ON CONFLICT (run_id) DO NOTHING
                """,
                (
                    bundle.run_id,
                    bundle.account_address.lower(),
                    bundle.observed_at,
                    bundle.source_as_of,
                    positions_artifact_id,
                    closed_artifact_id,
                    accounting_artifact_id,
                    str(manifest["positions_payload_hash"]),
                    str(manifest["closed_positions_payload_hash"]),
                    bundle.accounting.zip_sha256,
                    bundle.accounting.positions_csv_sha256,
                    bundle.accounting.equity_csv_sha256,
                    len(bundle.positions),
                    len(bundle.closed_positions),
                    len(bundle.accounting.positions),
                ),
            )
            for position in bundle.positions:
                cur.execute(
                    """
                    INSERT INTO quant.paper_official_position_snapshots (
                        run_id,account_address,asset_id,condition_id,size,avg_price,
                        initial_value,gross_initial_value,entry_fees_usdc,
                        current_value,cash_pnl,realized_pnl,current_price,total_bought,
                        redeemable,mergeable,title,slug,outcome,outcome_index,raw_payload
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s::jsonb
                    ) ON CONFLICT (run_id,asset_id) DO NOTHING
                    """,
                    (
                        bundle.run_id,
                        position.account_address,
                        position.asset_id,
                        position.condition_id,
                        position.size,
                        position.avg_price,
                        position.initial_value,
                        position.gross_initial_value,
                        position.entry_fees_usdc,
                        position.current_value,
                        position.cash_pnl,
                        position.realized_pnl,
                        position.current_price,
                        position.total_bought,
                        position.redeemable,
                        position.mergeable,
                        position.title,
                        position.slug,
                        position.outcome,
                        position.outcome_index,
                        _json(position.raw),
                    ),
                )
            for position in bundle.closed_positions:
                row_key = _identifier(
                    "closed-position",
                    position.asset_id,
                    position.condition_id,
                    position.closed_at.isoformat() if position.closed_at else "",
                )
                cur.execute(
                    """
                    INSERT INTO quant.paper_official_closed_position_snapshots (
                        run_id,row_key,account_address,asset_id,condition_id,
                        avg_price,total_bought,realized_pnl,current_price,closed_at,
                        title,slug,outcome,outcome_index,raw_payload
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                    ) ON CONFLICT (run_id,row_key) DO NOTHING
                    """,
                    (
                        bundle.run_id,
                        row_key,
                        position.account_address,
                        position.asset_id,
                        position.condition_id,
                        position.avg_price,
                        position.total_bought,
                        position.realized_pnl,
                        position.current_price,
                        position.closed_at,
                        position.title,
                        position.slug,
                        position.outcome,
                        position.outcome_index,
                        _json(position.raw),
                    ),
                )
            for position in bundle.accounting.positions:
                cur.execute(
                    """
                    INSERT INTO quant.paper_official_accounting_position_snapshots (
                        run_id,asset_id,condition_id,size,current_price,current_value,
                        valuation_time
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (run_id,asset_id) DO NOTHING
                    """,
                    (
                        bundle.run_id,
                        position.asset_id,
                        position.condition_id,
                        position.size,
                        position.current_price,
                        position.current_value,
                        position.valuation_time,
                    ),
                )
            equity = bundle.accounting.equity
            cur.execute(
                """
                INSERT INTO quant.paper_official_equity_snapshots (
                    run_id,cash_balance,positions_value,equity,valuation_time
                ) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (
                    bundle.run_id,
                    equity.cash_balance,
                    equity.positions_value,
                    equity.equity,
                    equity.valuation_time,
                ),
            )
            conn.commit()
        return bundle.run_id

    def load_latest_bundle(self, *, account_address: str) -> OfficialAccountBundle:
        """Load the latest normalized official snapshot without another HTTP fetch."""

        account = str(account_address).strip().lower()
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.paper_official_account_snapshot_runs
                WHERE account_address=%s AND status='PASS'
                ORDER BY source_as_of DESC,observed_at DESC LIMIT 1
                """,
                (account,),
            )
            run = cur.fetchone()
            if run is None:
                raise ValueError(f"official account snapshot not found: {account}")
            run = dict(run)
            run_id = str(run["run_id"])
            cur.execute(
                """
                SELECT * FROM quant.paper_official_position_snapshots
                WHERE run_id=%s ORDER BY asset_id
                """,
                (run_id,),
            )
            position_rows = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT * FROM quant.paper_official_closed_position_snapshots
                WHERE run_id=%s ORDER BY row_key
                """,
                (run_id,),
            )
            closed_rows = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT * FROM quant.paper_official_accounting_position_snapshots
                WHERE run_id=%s ORDER BY asset_id
                """,
                (run_id,),
            )
            accounting_rows = [dict(row) for row in cur.fetchall()]
            cur.execute(
                "SELECT * FROM quant.paper_official_equity_snapshots WHERE run_id=%s",
                (run_id,),
            )
            equity_row = cur.fetchone()
        if equity_row is None:
            raise RuntimeError(f"official account equity row missing: {run_id}")
        positions = tuple(
            OfficialPosition(
                account_address=str(row["account_address"]),
                asset_id=str(row["asset_id"]),
                condition_id=str(row["condition_id"]),
                size=Decimal(row["size"]),
                avg_price=Decimal(row["avg_price"]),
                initial_value=Decimal(row["initial_value"]),
                gross_initial_value=(
                    Decimal(row["gross_initial_value"])
                    if row["gross_initial_value"] is not None
                    else None
                ),
                entry_fees_usdc=(
                    Decimal(row["entry_fees_usdc"])
                    if row["entry_fees_usdc"] is not None
                    else None
                ),
                current_value=Decimal(row["current_value"]),
                cash_pnl=Decimal(row["cash_pnl"]),
                realized_pnl=Decimal(row["realized_pnl"]),
                current_price=Decimal(row["current_price"]),
                total_bought=Decimal(row["total_bought"]),
                redeemable=bool(row["redeemable"]),
                mergeable=bool(row["mergeable"]),
                title=str(row["title"]),
                slug=str(row["slug"]),
                outcome=str(row["outcome"]),
                outcome_index=row["outcome_index"],
                raw=dict(row.get("raw_payload") or {}),
            )
            for row in position_rows
        )
        closed = tuple(
            ClosedPosition(
                account_address=str(row["account_address"]),
                asset_id=str(row["asset_id"]),
                condition_id=str(row["condition_id"]),
                avg_price=Decimal(row["avg_price"]),
                total_bought=Decimal(row["total_bought"]),
                realized_pnl=Decimal(row["realized_pnl"]),
                current_price=Decimal(row["current_price"]),
                closed_at=row["closed_at"],
                title=str(row["title"]),
                slug=str(row["slug"]),
                outcome=str(row["outcome"]),
                outcome_index=row["outcome_index"],
                raw=dict(row.get("raw_payload") or {}),
            )
            for row in closed_rows
        )
        accounting_positions = tuple(
            AccountingPosition(
                condition_id=str(row["condition_id"]),
                asset_id=str(row["asset_id"]),
                size=Decimal(row["size"]),
                current_price=Decimal(row["current_price"]),
                valuation_time=row["valuation_time"],
            )
            for row in accounting_rows
        )
        equity_row = dict(equity_row)
        accounting = ParsedAccountingSnapshot(
            positions=accounting_positions,
            equity=AccountingEquity(
                cash_balance=Decimal(equity_row["cash_balance"]),
                positions_value=Decimal(equity_row["positions_value"]),
                equity=Decimal(equity_row["equity"]),
                valuation_time=equity_row["valuation_time"],
            ),
            positions_csv_sha256=str(run["positions_csv_sha256"]),
            equity_csv_sha256=str(run["equity_csv_sha256"]),
            zip_sha256=str(run["accounting_zip_sha256"]),
        )
        manifest = {
            "schema_version": "official-account-fetch-manifest-v1",
            "account_address": account,
            "observed_at": run["observed_at"].isoformat(),
            "source_as_of": run["source_as_of"].isoformat(),
            "positions_payload_hash": str(run["positions_payload_hash"]),
            "closed_positions_payload_hash": str(
                run["closed_positions_payload_hash"]
            ),
            "accounting_zip_sha256": accounting.zip_sha256,
            "positions_csv_sha256": accounting.positions_csv_sha256,
            "equity_csv_sha256": accounting.equity_csv_sha256,
            "position_count": len(positions),
            "closed_position_count": len(closed),
            "accounting_position_count": len(accounting_positions),
            "loaded_from_persisted_snapshot": True,
        }
        return OfficialAccountBundle(
            run_id=run_id,
            account_address=account,
            observed_at=run["observed_at"],
            source_as_of=run["source_as_of"],
            positions=positions,
            closed_positions=closed,
            accounting=accounting,
            fetch_manifest=manifest,
        )

    def persist_reconciliation(self, report: AccountTruthReport) -> str:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_account_truth_reconciliation_runs (
                    reconciliation_id,official_run_id,account_address,strategy_ids,
                    as_of,generated_at,status,official_source_status,
                    comparison_scope,mismatch_count,report_hash,summary
                ) VALUES (
                    %s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                ) ON CONFLICT (reconciliation_id) DO NOTHING
                """,
                (
                    report.reconciliation_id,
                    report.official_run_id,
                    report.account_address,
                    _json(report.strategy_ids),
                    report.as_of,
                    report.generated_at,
                    report.status.value,
                    report.official_source_status,
                    report.comparison_scope,
                    len(report.mismatches),
                    report.content_sha256,
                    _json(report.summary),
                ),
            )
            for item in report.mismatches:
                cur.execute(
                    """
                    INSERT INTO quant.paper_account_truth_reconciliation_items (
                        reconciliation_id,mismatch_id,mismatch_type,
                        comparison_type,comparison_key,field_name,official_value,
                        paper_value,delta,tolerance,reason,severity,retryable,evidence
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                    ) ON CONFLICT (reconciliation_id,mismatch_id) DO NOTHING
                    """,
                    (
                        report.reconciliation_id,
                        item.mismatch_id,
                        item.mismatch_type.value,
                        item.comparison_type,
                        item.comparison_key,
                        item.field_name,
                        item.official_value,
                        item.paper_value,
                        item.delta,
                        item.tolerance,
                        item.reason,
                        item.severity,
                        item.retryable,
                        _json(item.evidence),
                    ),
                )
            conn.commit()
        return report.reconciliation_id

    def apply_convergence_policy(
        self,
        report: AccountTruthReport,
        *,
        convergence_window_seconds: float,
    ) -> tuple[AccountTruthMismatch, ...]:
        """Persist mismatch continuity and classify fresh differences as pending."""

        window = timedelta(seconds=max(0.0, float(convergence_window_seconds)))
        strategy_scope_hash = hashlib.sha256(
            _json(tuple(report.strategy_ids)).encode()
        ).hexdigest()
        now = report.generated_at
        classified: list[AccountTruthMismatch] = []
        active_ids: list[str] = []
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for item in report.mismatches:
                state_id = _identifier(
                    "account-truth-mismatch-state",
                    report.account_address,
                    report.comparison_scope,
                    strategy_scope_hash,
                    item.mismatch_id,
                )
                active_ids.append(state_id)
                cur.execute(
                    """
                    SELECT first_seen_at,occurrence_count,status,last_observation_id
                    FROM quant.paper_account_truth_mismatch_states
                    WHERE mismatch_state_id=%s
                    """,
                    (state_id,),
                )
                existing = cur.fetchone()
                continuing = existing is not None and existing["status"] == "ACTIVE"
                first_seen = existing["first_seen_at"] if continuing else now
                deadline = first_seen + window
                same_observation = (
                    continuing
                    and str(existing["last_observation_id"]) == report.reconciliation_id
                )
                occurrence_count = (
                    int(existing["occurrence_count"])
                    if same_observation
                    else int(existing["occurrence_count"] or 0) + 1
                    if continuing
                    else 1
                )
                is_pending = now < deadline
                remains_advisory = item.mismatch_type is MismatchType.TIMING_LAG
                evidence = dict(item.evidence)
                evidence.update(
                    {
                        "original_mismatch_type": item.mismatch_type.value,
                        "first_seen_at": first_seen.isoformat(),
                        "convergence_deadline": deadline.isoformat(),
                        "occurrence_count": occurrence_count,
                        "convergence_expired": not is_pending,
                    }
                )
                classified_item = replace(
                    item,
                    mismatch_type=(
                        MismatchType.PENDING_CONVERGENCE
                        if is_pending
                        else item.mismatch_type
                    ),
                    severity=("WARNING" if is_pending or remains_advisory else "ERROR"),
                    retryable=is_pending or remains_advisory,
                    evidence=evidence,
                )
                classified.append(classified_item)
                cur.execute(
                    """
                    INSERT INTO quant.paper_account_truth_mismatch_states (
                        mismatch_state_id,account_address,comparison_scope,
                        strategy_scope_hash,comparison_type,comparison_key,
                        field_name,original_mismatch_type,first_seen_at,last_seen_at,
                        convergence_deadline,occurrence_count,status,
                        last_observation_id,last_mismatch_id,last_evidence,resolved_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',
                        %s,%s,%s::jsonb,NULL
                    )
                    ON CONFLICT (mismatch_state_id) DO UPDATE SET
                        first_seen_at=EXCLUDED.first_seen_at,
                        last_seen_at=EXCLUDED.last_seen_at,
                        convergence_deadline=EXCLUDED.convergence_deadline,
                        occurrence_count=EXCLUDED.occurrence_count,
                        status='ACTIVE',
                        last_observation_id=EXCLUDED.last_observation_id,
                        last_mismatch_id=EXCLUDED.last_mismatch_id,
                        last_evidence=EXCLUDED.last_evidence,
                        resolved_at=NULL,
                        updated_at=clock_timestamp()
                    """,
                    (
                        state_id,
                        report.account_address,
                        report.comparison_scope,
                        strategy_scope_hash,
                        item.comparison_type,
                        item.comparison_key,
                        item.field_name,
                        item.mismatch_type.value,
                        first_seen,
                        now,
                        deadline,
                        occurrence_count,
                        report.reconciliation_id,
                        item.mismatch_id,
                        _json(evidence),
                    ),
                )
            if active_ids:
                cur.execute(
                    """
                    UPDATE quant.paper_account_truth_mismatch_states
                    SET status='RESOLVED',resolved_at=%s,updated_at=clock_timestamp()
                    WHERE account_address=%s AND comparison_scope=%s
                      AND strategy_scope_hash=%s AND status='ACTIVE'
                      AND NOT (mismatch_state_id=ANY(%s))
                    """,
                    (
                        now,
                        report.account_address,
                        report.comparison_scope,
                        strategy_scope_hash,
                        active_ids,
                    ),
                )
            else:
                cur.execute(
                    """
                    UPDATE quant.paper_account_truth_mismatch_states
                    SET status='RESOLVED',resolved_at=%s,updated_at=clock_timestamp()
                    WHERE account_address=%s AND comparison_scope=%s
                      AND strategy_scope_hash=%s AND status='ACTIVE'
                    """,
                    (
                        now,
                        report.account_address,
                        report.comparison_scope,
                        strategy_scope_hash,
                    ),
                )
            conn.commit()
        return tuple(classified)

    def create_baseline(
        self,
        *,
        scope_id: str,
        official: OfficialAccountBundle,
        strategy_ids: tuple[str, ...] = (),
    ) -> Mapping[str, Any]:
        scope = str(scope_id).strip()
        if not scope:
            raise ValueError("account truth baseline scope_id is required")
        payload = _official_baseline_payload(official)
        payload_hash = hashlib.sha256(_json(payload).encode()).hexdigest()
        baseline_id = _identifier(
            "account-truth-baseline",
            official.account_address,
            scope,
            official.run_id,
            payload_hash,
        )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_account_truth_baselines (
                    baseline_id,scope_id,account_address,official_run_id,
                    strategy_ids,source_as_of,status,baseline_hash,baseline_payload
                ) VALUES (%s,%s,%s,%s,%s::jsonb,%s,'ACTIVE',%s,%s::jsonb)
                ON CONFLICT (scope_id) DO NOTHING
                RETURNING baseline_id,scope_id,account_address,official_run_id,
                          strategy_ids,source_as_of,status,baseline_hash,baseline_payload
                """,
                (
                    baseline_id,
                    scope,
                    official.account_address,
                    official.run_id,
                    _json(strategy_ids),
                    official.source_as_of,
                    payload_hash,
                    _json(payload),
                ),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    SELECT baseline_id,scope_id,account_address,official_run_id,
                           strategy_ids,source_as_of,status,baseline_hash,baseline_payload
                    FROM quant.paper_account_truth_baselines WHERE scope_id=%s
                    """,
                    (scope,),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("account truth baseline insert disappeared")
                existing_strategies = tuple(str(item) for item in row["strategy_ids"])
                if str(row["account_address"]) != official.account_address:
                    raise ValueError(
                        "account truth scope belongs to a different wallet"
                    )
                if existing_strategies != tuple(strategy_ids):
                    raise ValueError(
                        "account truth scope has a different immutable strategy scope"
                    )
            conn.commit()
        return dict(row)

    def load_baseline(
        self, *, scope_id: str, account_address: str
    ) -> Mapping[str, Any]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT baseline_id,scope_id,account_address,official_run_id,
                       strategy_ids,source_as_of,status,baseline_hash,baseline_payload
                FROM quant.paper_account_truth_baselines
                WHERE scope_id=%s AND account_address=%s
                """,
                (str(scope_id).strip(), account_address.lower()),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"account truth baseline not found: {scope_id}")
        return dict(row)


def _atomic_content_addressed_write(
    path: Path, content: bytes, expected_hash: str
) -> None:
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError("account truth artifact content hash mismatch")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise RuntimeError(f"existing account truth artifact is corrupt: {path}")
        return
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.chmod(temporary, 0o600)
    if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected_hash:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("account truth artifact failed post-write hash verification")
    temporary.replace(path)


def _identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]
    return f"{prefix}:{digest}"


def _json(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "value"):
        return _jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _official_baseline_payload(official: OfficialAccountBundle) -> dict[str, Any]:
    closed_realized: dict[str, Decimal] = {}
    for row in official.closed_positions:
        closed_realized[row.asset_id] = (
            closed_realized.get(row.asset_id, Decimal(0)) + row.realized_pnl
        )
    positions = {}
    for row in official.positions:
        closed_value = closed_realized.pop(row.asset_id, Decimal(0))
        if closed_value == row.realized_pnl:
            closed_value = Decimal(0)
        positions[row.asset_id] = {
            "condition_id": row.condition_id,
            "size": row.size,
            "gross_initial_value": row.gross_initial_value,
            "entry_fees_usdc": row.entry_fees_usdc,
            "open_realized_pnl": row.realized_pnl,
            "closed_realized_pnl": closed_value,
            "redeemable": row.redeemable,
            "mergeable": row.mergeable,
        }
    for asset_id, realized in closed_realized.items():
        positions[asset_id] = {
            "condition_id": "",
            "size": Decimal(0),
            "gross_initial_value": Decimal(0),
            "entry_fees_usdc": Decimal(0),
            "open_realized_pnl": Decimal(0),
            "closed_realized_pnl": realized,
            "redeemable": False,
            "mergeable": False,
        }
    return _jsonable(
        {
            "schema_version": "official-account-truth-baseline-v1",
            "official_run_id": official.run_id,
            "account_address": official.account_address,
            "source_as_of": official.source_as_of,
            "cash_balance": official.accounting.equity.cash_balance,
            "positions": positions,
        }
    )
