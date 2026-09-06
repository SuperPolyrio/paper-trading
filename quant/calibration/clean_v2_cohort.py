"""Post-CLOB-V2 clean-cohort orchestration for live versus Paper truth."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant.maker.own_order_truth import decode_orderfilled_logs
from quant.paper.paper_ledger import PostgresPaperLedgerSink
from quant.paper.taker_execution import paper_execution_result_from_payload
from quant.simulator.account_truth import OfficialAccountTruthService
from quant.simulator.account_truth.models import AccountTruthReport
from quant.simulator.account_truth.report import write_account_truth_report

from .calibration_domain import canonical_json, payload_hash
from .order_rest_reconciler import correlates_order
from .paired_probe_bridge import sync_calibration_probe_to_paired
from .reconcile import reconcile_probe
from .signed_order_prediction import signed_execution_result_payload
from .store import DEFAULT_VENUE_REGIME_ID, CalibrationStore

# CLOB V2 launched on 2026-04-28. The cohort starts after the later async-commit
# rollout so every sample also shares the same order-response/finality semantics.
CLOB_V2_PRODUCTION_AT = datetime(2026, 4, 28, 11, tzinfo=timezone.utc)
CLEAN_V2_COHORT_EFFECTIVE_AT = datetime(2026, 7, 24, 4, tzinfo=timezone.utc)

ACTIVE_OPERATION_STATES = frozenset(
    {"PREPARED", "RUNNING", "AWAITING_RECEIPT", "AWAITING_ACCOUNT_TRUTH"}
)
SETTLED_OPERATION_STATES = frozenset(
    {
        "EVIDENCE_READY",
        "NO_FILL_VERIFIED",
        "REJECT_VERIFIED",
        "ABORTED_NO_SUBMIT",
    }
)

SCHEMA_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS quant",
    """
    CREATE TABLE IF NOT EXISTS quant.paper_clean_v2_cohorts (
        cohort_id TEXT PRIMARY KEY,
        scope_id TEXT NOT NULL UNIQUE,
        account_address TEXT NOT NULL,
        paper_strategy_id TEXT NOT NULL UNIQUE,
        venue_regime_id TEXT NOT NULL,
        venue_cutover_at TIMESTAMPTZ NOT NULL,
        baseline_id TEXT NOT NULL,
        baseline_official_run_id TEXT NOT NULL,
        baseline_as_of TIMESTAMPTZ NOT NULL,
        baseline_hash TEXT NOT NULL,
        baseline_asset_ids JSONB NOT NULL,
        manifest JSONB NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        max_gross_notional NUMERIC NOT NULL,
        committed_gross_notional NUMERIC NOT NULL DEFAULT 0,
        operation_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        last_reconciliation_id TEXT,
        last_checkpoint_status TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        CHECK (max_gross_notional > 0),
        CHECK (committed_gross_notional >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_clean_v2_cohort_operations (
        operation_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL REFERENCES quant.paper_clean_v2_cohorts(cohort_id),
        sequence INTEGER NOT NULL,
        run_id TEXT NOT NULL UNIQUE,
        paired_probe_id TEXT,
        paper_intent_id BIGINT,
        asset_id TEXT NOT NULL,
        market_id TEXT,
        condition_id TEXT,
        side TEXT NOT NULL,
        order_type TEXT NOT NULL,
        amount NUMERIC NOT NULL,
        amount_unit TEXT NOT NULL,
        planned_gross_notional NUMERIC NOT NULL,
        status TEXT NOT NULL,
        order_id TEXT,
        trade_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        transaction_hashes JSONB NOT NULL DEFAULT '[]'::jsonb,
        paper_staging_strategy_id TEXT,
        staged_paper_audit_key TEXT,
        signed_paper_audit_key TEXT,
        signed_paper_result JSONB,
        signed_prediction_frozen_at TIMESTAMPTZ,
        committed_paper_audit_key TEXT,
        paper_committed_at TIMESTAMPTZ,
        evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
        evidence_sha256 TEXT,
        submitted_at TIMESTAMPTZ,
        terminal_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (cohort_id,sequence),
        CHECK (side IN ('BUY','SELL')),
        CHECK (order_type IN ('FOK','FAK')),
        CHECK (amount > 0),
        CHECK (planned_gross_notional > 0)
    )
    """,
    "ALTER TABLE quant.paper_clean_v2_cohort_operations ADD COLUMN IF NOT EXISTS paper_staging_strategy_id TEXT",
    "ALTER TABLE quant.paper_clean_v2_cohort_operations ADD COLUMN IF NOT EXISTS staged_paper_audit_key TEXT",
    "ALTER TABLE quant.paper_clean_v2_cohort_operations ADD COLUMN IF NOT EXISTS signed_paper_audit_key TEXT",
    "ALTER TABLE quant.paper_clean_v2_cohort_operations ADD COLUMN IF NOT EXISTS signed_paper_result JSONB",
    "ALTER TABLE quant.paper_clean_v2_cohort_operations ADD COLUMN IF NOT EXISTS signed_prediction_frozen_at TIMESTAMPTZ",
    "ALTER TABLE quant.paper_clean_v2_cohort_operations ADD COLUMN IF NOT EXISTS committed_paper_audit_key TEXT",
    "ALTER TABLE quant.paper_clean_v2_cohort_operations ADD COLUMN IF NOT EXISTS paper_committed_at TIMESTAMPTZ",
    "ALTER TABLE quant.paper_clean_v2_cohorts ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ",
    "ALTER TABLE quant.paper_clean_v2_cohorts ADD COLUMN IF NOT EXISTS invalidated_reason TEXT",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS paper_clean_v2_one_active_operation_idx
    ON quant.paper_clean_v2_cohort_operations (cohort_id)
    WHERE status IN ('PREPARED','RUNNING','AWAITING_RECEIPT','AWAITING_ACCOUNT_TRUTH')
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_clean_v2_cohort_checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL REFERENCES quant.paper_clean_v2_cohorts(cohort_id),
        official_run_id TEXT NOT NULL,
        reconciliation_id TEXT NOT NULL UNIQUE,
        source_as_of TIMESTAMPTZ NOT NULL,
        account_truth_status TEXT NOT NULL,
        pnl_truth_status TEXT NOT NULL,
        checkpoint_status TEXT NOT NULL,
        report JSONB NOT NULL,
        report_sha256 TEXT NOT NULL,
        artifact_paths JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_clean_v2_checkpoint_cohort_idx
    ON quant.paper_clean_v2_cohort_checkpoints (cohort_id,source_as_of DESC)
    """,
)


class CleanV2CohortStore:
    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)
            conn.commit()

    def assert_strategy_zero_delta(self, strategy_id: str) -> Mapping[str, Any]:
        strategy = str(strategy_id).strip()
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT initial_cash,cash_balance,cash_reserved,realized_pnl
                FROM quant.paper_accounts WHERE strategy_id=%s
                """,
                (strategy,),
            )
            account = cur.fetchone()
            if account is None:
                raise ValueError(f"paper strategy account not found: {strategy}")
            cur.execute(
                """
                SELECT
                    (SELECT count(*) FROM quant.paper_ledger_entries
                     WHERE strategy_id=%s) AS ledger_count,
                    (SELECT count(*) FROM quant.paper_live_order_intents
                     WHERE strategy_id=%s) AS intent_count,
                    (SELECT count(*) FROM quant.paper_positions
                     WHERE strategy_id=%s AND (
                         quantity<>0 OR reserved_quantity<>0 OR cost_basis<>0
                     )) AS position_count
                """,
                (strategy, strategy, strategy),
            )
            counts = dict(cur.fetchone() or {})
        initial_cash = Decimal(str(account["initial_cash"]))
        checks = {
            "cash_equals_initial": Decimal(str(account["cash_balance"]))
            == initial_cash,
            "cash_reserved_zero": Decimal(str(account["cash_reserved"])) == 0,
            "realized_pnl_zero": Decimal(str(account["realized_pnl"])) == 0,
            "ledger_empty": int(counts.get("ledger_count") or 0) == 0,
            "intent_empty": int(counts.get("intent_count") or 0) == 0,
            "positions_empty": int(counts.get("position_count") or 0) == 0,
        }
        if not all(checks.values()):
            failed = ",".join(key for key, value in checks.items() if not value)
            raise ValueError(f"paper strategy is not a zero-delta cohort: {failed}")
        return {
            "strategy_id": strategy,
            "initial_cash": format(initial_cash, "f"),
            "normalized_initial_delta": "0",
            "checks": checks,
        }

    def create_cohort(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        stable = dict(manifest)
        manifest_sha256 = _sha256_payload(stable)
        max_notional = Decimal(str(stable["max_gross_notional_usd"]))
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_clean_v2_cohorts (
                    cohort_id,scope_id,account_address,paper_strategy_id,
                    venue_regime_id,venue_cutover_at,baseline_id,
                    baseline_official_run_id,baseline_as_of,baseline_hash,
                    baseline_asset_ids,manifest,manifest_sha256,
                    max_gross_notional,status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,'READY')
                ON CONFLICT (cohort_id) DO NOTHING
                RETURNING *
                """,
                (
                    stable["cohort_id"],
                    stable["scope_id"],
                    stable["account_address"],
                    stable["paper_strategy_id"],
                    stable["venue_regime_id"],
                    stable["venue_cutover_at"],
                    stable["baseline_id"],
                    stable["baseline_official_run_id"],
                    stable["baseline_as_of"],
                    stable["baseline_hash"],
                    canonical_json(stable["baseline_asset_ids"]),
                    canonical_json(stable),
                    manifest_sha256,
                    max_notional,
                ),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT * FROM quant.paper_clean_v2_cohorts WHERE cohort_id=%s",
                    (stable["cohort_id"],),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("clean V2 cohort insert disappeared")
                if str(row["manifest_sha256"]) != manifest_sha256:
                    raise ValueError(
                        "cohort id already has a different immutable manifest"
                    )
            conn.commit()
            return dict(row)

    def load_cohort(
        self, cohort_id: str, *, for_update: bool = False
    ) -> dict[str, Any]:
        suffix = " FOR UPDATE" if for_update else ""
        with (
            self.connection_factory(readonly=not for_update) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                f"SELECT * FROM quant.paper_clean_v2_cohorts WHERE cohort_id=%s{suffix}",
                (str(cohort_id),),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"clean V2 cohort not found: {cohort_id}")
            return dict(row)

    def load_operation_by_run(self, run_id: str) -> dict[str, Any]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.paper_clean_v2_cohort_operations
                WHERE run_id=%s
                """,
                (str(run_id),),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"clean cohort operation not found for run: {run_id}")
        return dict(row)

    def invalidate_cohort(self, cohort_id: str, *, reason: str) -> dict[str, Any]:
        explanation = str(reason).strip()
        if not explanation:
            raise ValueError("cohort invalidation requires a reason")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_clean_v2_cohorts
                SET status='INVALIDATED',invalidated_at=clock_timestamp(),
                    invalidated_reason=%s,updated_at=clock_timestamp()
                WHERE cohort_id=%s
                RETURNING *
                """,
                (explanation, str(cohort_id)),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"clean V2 cohort not found: {cohort_id}")
            conn.commit()
            return dict(row)

    def prepare_staging_strategy(self, *, run_id: str) -> dict[str, Any]:
        """Clone the cohort account before prediction without mutating cohort PnL."""

        staging_strategy_id = f"post-v2-stage-{str(run_id)}"
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT operation.*,cohort.paper_strategy_id,cohort.status AS cohort_status
                FROM quant.paper_clean_v2_cohort_operations operation
                JOIN quant.paper_clean_v2_cohorts cohort USING (cohort_id)
                WHERE operation.run_id=%s
                FOR UPDATE OF operation,cohort
                """,
                (str(run_id),),
            )
            operation = cur.fetchone()
            if operation is None:
                raise ValueError(f"clean cohort operation not found for run: {run_id}")
            if str(operation["cohort_status"]) not in {"READY", "ACTIVE"}:
                raise ValueError(
                    f"clean V2 cohort is not active: {operation['cohort_status']}"
                )
            if str(operation["status"]) not in {"PREPARED", "RUNNING"}:
                raise ValueError(
                    "staging strategy requires a prepared or running operation"
                )
            existing_stage = str(operation.get("paper_staging_strategy_id") or "")
            if existing_stage:
                if existing_stage != staging_strategy_id:
                    raise ValueError(
                        "operation is bound to a different staging strategy"
                    )
                conn.commit()
                return {
                    "run_id": str(run_id),
                    "source_strategy_id": str(operation["paper_strategy_id"]),
                    "staging_strategy_id": existing_stage,
                    "baseline_hash": _mapping(operation.get("evidence")).get(
                        "staging_baseline_hash"
                    ),
                    "idempotent": True,
                }
            source_strategy_id = str(operation["paper_strategy_id"])
            cur.execute(
                """
                SELECT base_currency,initial_cash,cash_balance,cash_reserved,
                       realized_pnl
                FROM quant.paper_accounts
                WHERE strategy_id=%s
                FOR UPDATE
                """,
                (source_strategy_id,),
            )
            account = cur.fetchone()
            if account is None:
                raise ValueError("clean cohort Paper account is missing")
            if Decimal(str(account["cash_reserved"])) != 0:
                raise ValueError("clean cohort has reserved cash before staging")
            cur.execute(
                """
                SELECT count(*) AS active_count
                FROM quant.paper_order_reservations
                WHERE strategy_id=%s AND status='ACTIVE'
                """,
                (source_strategy_id,),
            )
            if int(cur.fetchone()["active_count"] or 0) != 0:
                raise ValueError("clean cohort has active order reservations")
            cur.execute(
                """
                SELECT asset_id,market_id,condition_id,quantity,
                       reserved_quantity,cost_basis,realized_pnl,settled_at
                FROM quant.paper_positions
                WHERE strategy_id=%s
                ORDER BY asset_id
                """,
                (source_strategy_id,),
            )
            positions = [dict(row) for row in cur.fetchall()]
            if any(Decimal(str(row["reserved_quantity"])) != 0 for row in positions):
                raise ValueError("clean cohort has reserved positions before staging")
            baseline_payload = {
                "source_strategy_id": source_strategy_id,
                "account": dict(account),
                "positions": positions,
            }
            baseline_hash = _sha256_payload(baseline_payload)
            cur.execute(
                "SELECT 1 FROM quant.paper_accounts WHERE strategy_id=%s",
                (staging_strategy_id,),
            )
            if cur.fetchone() is not None:
                raise ValueError("unbound staging strategy already exists")
            cur.execute(
                """
                INSERT INTO quant.paper_accounts (
                    strategy_id,base_currency,initial_cash,cash_balance,
                    cash_reserved,realized_pnl
                ) VALUES (%s,%s,%s,%s,0,%s)
                """,
                (
                    staging_strategy_id,
                    account["base_currency"],
                    account["initial_cash"],
                    account["cash_balance"],
                    account["realized_pnl"],
                ),
            )
            if positions:
                cur.executemany(
                    """
                    INSERT INTO quant.paper_positions (
                        strategy_id,asset_id,market_id,condition_id,quantity,
                        reserved_quantity,cost_basis,realized_pnl,settled_at
                    ) VALUES (%s,%s,%s,%s,%s,0,%s,%s,%s)
                    """,
                    [
                        (
                            staging_strategy_id,
                            row["asset_id"],
                            row["market_id"],
                            row["condition_id"],
                            row["quantity"],
                            row["cost_basis"],
                            row["realized_pnl"],
                            row["settled_at"],
                        )
                        for row in positions
                    ],
                )
            baseline_entries = _staging_baseline_entries(
                run_id=str(run_id),
                staging_strategy_id=staging_strategy_id,
                source_strategy_id=source_strategy_id,
                baseline_hash=baseline_hash,
                account=account,
                positions=positions,
            )
            cur.executemany(
                """
                INSERT INTO quant.paper_ledger_entries (
                    idempotency_key,strategy_id,event_type,market_id,condition_id,
                    asset_id,event_ts,shares_delta,cash_delta,fee,
                    realized_pnl_delta,cash_after,position_after,cost_basis_after,
                    metadata
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,clock_timestamp(),%s,%s,0,%s,%s,%s,%s,%s::jsonb
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                [
                    (
                        entry["idempotency_key"],
                        staging_strategy_id,
                        entry["event_type"],
                        entry["market_id"],
                        entry["condition_id"],
                        entry["asset_id"],
                        entry["shares_delta"],
                        entry["cash_delta"],
                        entry["realized_pnl_delta"],
                        account["cash_balance"],
                        entry["position_after"],
                        entry["cost_basis_after"],
                        json.dumps(entry["metadata"], sort_keys=True),
                    )
                    for entry in baseline_entries
                ],
            )
            cur.execute(
                """
                UPDATE quant.paper_clean_v2_cohort_operations
                SET paper_staging_strategy_id=%s,
                    evidence=evidence || jsonb_build_object(
                        'staging_baseline_hash',%s::text,
                        'staging_source_strategy_id',%s::text
                    ),updated_at=clock_timestamp()
                WHERE run_id=%s
                """,
                (
                    staging_strategy_id,
                    baseline_hash,
                    source_strategy_id,
                    str(run_id),
                ),
            )
            conn.commit()
        return {
            "run_id": str(run_id),
            "source_strategy_id": source_strategy_id,
            "staging_strategy_id": staging_strategy_id,
            "baseline_hash": baseline_hash,
            "idempotent": False,
        }

    def record_staged_prediction(
        self,
        *,
        run_id: str,
        paired_probe_id: str,
        paper_intent_id: int,
    ) -> dict[str, Any]:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT operation.paper_staging_strategy_id,intent.strategy_id,
                       intent.result_audit_key,intent.result
                FROM quant.paper_clean_v2_cohort_operations operation
                JOIN quant.paper_live_order_intents intent
                  ON intent.intent_id=%s
                WHERE operation.run_id=%s
                FOR UPDATE OF operation
                """,
                (int(paper_intent_id), str(run_id)),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("staged Paper intent or cohort operation is missing")
            if str(row["strategy_id"]) != str(row["paper_staging_strategy_id"]):
                raise ValueError("Paper prediction did not use the staging strategy")
            if not row.get("result") or not row.get("result_audit_key"):
                raise ValueError("staged Paper prediction has no durable result")
            cur.execute(
                """
                UPDATE quant.paper_clean_v2_cohort_operations
                SET paired_probe_id=%s,paper_intent_id=%s,
                    staged_paper_audit_key=%s,
                    evidence=evidence || jsonb_build_object(
                        'paper_prediction_staged',true
                    ),updated_at=clock_timestamp()
                WHERE run_id=%s
                RETURNING *
                """,
                (
                    str(paired_probe_id),
                    int(paper_intent_id),
                    str(row["result_audit_key"]),
                    str(run_id),
                ),
            )
            updated = dict(cur.fetchone())
            conn.commit()
            return updated

    def freeze_signed_prediction(
        self,
        *,
        run_id: str,
        normalized_prediction: Mapping[str, Any],
        signed_order_audit: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Freeze the exact signed amounts before any exchange submission."""

        if bool(signed_order_audit.get("exchange_submit_called")):
            raise ValueError("signed prediction must be frozen before HTTP submission")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT operation.*,intent.strategy_id AS intent_strategy_id,
                       intent.result_audit_key AS intent_result_audit_key,
                       intent.result AS intent_result
                FROM quant.paper_clean_v2_cohort_operations operation
                JOIN quant.paper_live_order_intents intent
                  ON intent.intent_id=operation.paper_intent_id
                WHERE operation.run_id=%s
                FOR UPDATE OF operation,intent
                """,
                (str(run_id),),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("staged Paper intent or cohort operation is missing")
            if str(row["status"]) != "RUNNING":
                raise ValueError(
                    "signed prediction requires a running cohort operation"
                )
            if row.get("committed_paper_audit_key"):
                raise ValueError("Paper prediction was already committed")
            if not row.get("staged_paper_audit_key") or not row.get("paired_probe_id"):
                raise ValueError("raw staged Paper prediction is not registered")
            if str(row["intent_strategy_id"]) != str(row["paper_staging_strategy_id"]):
                raise ValueError("Paper prediction did not use the staging strategy")
            if str(row["intent_result_audit_key"]) != str(
                row["staged_paper_audit_key"]
            ):
                raise ValueError("raw staged Paper audit key changed before signing")

            signed_payload = signed_execution_result_payload(
                _mapping(row["intent_result"]),
                normalized_prediction,
                signed_order_audit,
            )
            signed_result = paper_execution_result_from_payload(signed_payload)
            if signed_result.intent.strategy_id != str(
                row["paper_staging_strategy_id"]
            ):
                raise ValueError("signed Paper result has a foreign strategy")
            if signed_result.total_fee != sum(
                (fill.fee for fill in signed_result.fills), Decimal(0)
            ):
                raise ValueError("signed Paper result fee total is inconsistent")
            existing_payload = _mapping(row.get("signed_paper_result"))
            if existing_payload:
                if _sha256_payload(existing_payload) != _sha256_payload(signed_payload):
                    raise ValueError(
                        "signed Paper prediction is already frozen differently"
                    )
                conn.commit()
                return {
                    "run_id": str(run_id),
                    "audit_key": signed_result.audit_key,
                    "prediction": dict(normalized_prediction),
                    "idempotent": True,
                }

            cur.execute(
                """
                SELECT probe_id,strategy_id,paper_intent_id
                FROM quant.paper_paired_probes
                WHERE probe_id=%s
                FOR UPDATE
                """,
                (str(row["paired_probe_id"]),),
            )
            paired = cur.fetchone()
            if paired is None:
                raise ValueError("paired Paper probe is missing before signed freeze")
            if str(paired["strategy_id"]) != str(row["paper_staging_strategy_id"]):
                raise ValueError("paired Paper probe has a foreign strategy")
            if int(paired["paper_intent_id"] or 0) != int(row["paper_intent_id"]):
                raise ValueError("paired Paper probe points to another intent")

            signed_projection = {
                **dict(normalized_prediction),
                "audit_key": signed_result.audit_key,
                "staged_paper_audit_key": str(row["staged_paper_audit_key"]),
            }
            cur.execute(
                """
                UPDATE quant.paper_clean_v2_cohort_operations
                SET signed_paper_audit_key=%s,signed_paper_result=%s::jsonb,
                    signed_prediction_frozen_at=clock_timestamp(),
                    evidence=evidence || jsonb_build_object(
                        'signed_prediction_frozen',true,
                        'signed_paper_audit_key',%s::text,
                        'signed_order_hash',%s::text,
                        'signed_order_fingerprint',%s::text
                    ),updated_at=clock_timestamp()
                WHERE run_id=%s AND signed_paper_audit_key IS NULL
                """,
                (
                    signed_result.audit_key,
                    canonical_json(signed_payload),
                    signed_result.audit_key,
                    str(signed_order_audit.get("order_hash") or ""),
                    str(signed_order_audit.get("signed_order_fingerprint") or ""),
                    str(run_id),
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError("signed Paper prediction freeze lost its lock")
            cur.execute(
                """
                UPDATE quant.paper_paired_probes
                SET paper_prediction=%s::jsonb,
                    audit=audit || jsonb_build_object(
                        'signed_prediction_frozen',true,
                        'signed_paper_audit_key',%s::text
                    ),updated_at=clock_timestamp()
                WHERE probe_id=%s
                """,
                (
                    canonical_json(signed_projection),
                    signed_result.audit_key,
                    str(row["paired_probe_id"]),
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError("paired Paper prediction freeze was not persisted")
            conn.commit()
        return {
            "run_id": str(run_id),
            "audit_key": signed_result.audit_key,
            "prediction": signed_projection,
            "idempotent": False,
        }

    def commit_staged_prediction(self, *, run_id: str) -> dict[str, Any]:
        """Apply a staged result once after the real submit boundary is crossed."""

        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT operation.*,cohort.paper_strategy_id,intent.result
                FROM quant.paper_clean_v2_cohort_operations operation
                JOIN quant.paper_clean_v2_cohorts cohort USING (cohort_id)
                JOIN quant.paper_live_order_intents intent
                  ON intent.intent_id=operation.paper_intent_id
                WHERE operation.run_id=%s
                """,
                (str(run_id),),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError("staged Paper prediction is unavailable")
        if row.get("committed_paper_audit_key"):
            return {
                "run_id": str(run_id),
                "audit_key": str(row["committed_paper_audit_key"]),
                "strategy_id": str(row["paper_strategy_id"]),
                "idempotent": True,
            }
        if str(row["status"]) not in {
            "RUNNING",
            "AWAITING_RECEIPT",
            "AWAITING_ACCOUNT_TRUTH",
            "EVIDENCE_READY",
            "NO_FILL_VERIFIED",
            "REJECT_VERIFIED",
        }:
            raise ValueError(
                f"operation cannot commit Paper prediction in state {row['status']}"
            )
        signed_payload = _mapping(row.get("signed_paper_result"))
        if not signed_payload or not row.get("signed_paper_audit_key"):
            raise ValueError(
                "staged Paper prediction was not frozen to signed order amounts"
            )
        staged = paper_execution_result_from_payload(signed_payload)
        if staged.audit_key != str(row["signed_paper_audit_key"]):
            raise ValueError("signed Paper prediction audit key is inconsistent")
        if staged.intent.strategy_id != str(row["paper_staging_strategy_id"]):
            raise ValueError("durable signed result has a foreign strategy")
        committed_audit_key = hashlib.sha256(
            canonical_json(
                {
                    "cohort_id": row["cohort_id"],
                    "run_id": run_id,
                    "staged_audit_key": row["staged_paper_audit_key"],
                    "signed_audit_key": staged.audit_key,
                }
            ).encode("utf-8")
        ).hexdigest()
        committed_intent = replace(
            staged.intent,
            strategy_id=str(row["paper_strategy_id"]),
            client_order_id=f"clean-v2:{run_id}",
        )
        committed_fills = tuple(
            replace(
                fill,
                fee_charge_id=(
                    f"fee-charge:{committed_audit_key}:{index}"
                    if fill.fee_charge_id is not None
                    else None
                ),
            )
            for index, fill in enumerate(staged.fills)
        )
        committed = replace(
            staged,
            audit_key=committed_audit_key,
            intent=committed_intent,
            fills=committed_fills,
            fidelity={
                **dict(staged.fidelity),
                "clean_v2_cohort_id": str(row["cohort_id"]),
                "clean_v2_run_id": str(run_id),
                "staged_paper_audit_key": str(row["staged_paper_audit_key"]),
                "signed_paper_audit_key": staged.audit_key,
                "staging_strategy_id": staged.intent.strategy_id,
                "promotion_reason": "real_exchange_submit_boundary_crossed",
            },
        )
        PostgresPaperLedgerSink(
            connection_factory=self.connection_factory,
            ensure_schema=False,
        ).append(committed)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_clean_v2_cohort_operations
                SET committed_paper_audit_key=%s,
                    paper_committed_at=COALESCE(paper_committed_at,clock_timestamp()),
                    evidence=evidence || jsonb_build_object(
                        'paper_prediction_committed',true,
                        'committed_paper_audit_key',%s::text
                    ),updated_at=clock_timestamp()
                WHERE run_id=%s
                  AND committed_paper_audit_key IS NULL
                RETURNING paper_committed_at
                """,
                (committed_audit_key, committed_audit_key, str(run_id)),
            )
            inserted = cur.fetchone()
            if inserted is None:
                cur.execute(
                    """
                    SELECT committed_paper_audit_key,paper_committed_at
                    FROM quant.paper_clean_v2_cohort_operations WHERE run_id=%s
                    """,
                    (str(run_id),),
                )
                existing = cur.fetchone()
                if (
                    existing is None
                    or str(existing["committed_paper_audit_key"]) != committed_audit_key
                ):
                    raise RuntimeError(
                        "Paper promotion state conflicts after ledger apply"
                    )
                committed_at = existing["paper_committed_at"]
            else:
                committed_at = inserted["paper_committed_at"]
            conn.commit()
        return {
            "run_id": str(run_id),
            "audit_key": committed_audit_key,
            "strategy_id": str(row["paper_strategy_id"]),
            "paper_committed_at": committed_at,
            "idempotent": inserted is None,
        }

    def cohort_ledger_integrity(self, cohort_id: str) -> dict[str, Any]:
        cohort = self.load_cohort(cohort_id)
        operations = self.list_operations(cohort_id)
        expected = {
            str(row["committed_paper_audit_key"])
            for row in operations
            if row.get("committed_paper_audit_key")
        }
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT audit_key FROM quant.paper_portfolio_applied_results
                WHERE strategy_id=%s ORDER BY audit_key
                """,
                (str(cohort["paper_strategy_id"]),),
            )
            actual = {str(row["audit_key"]) for row in cur.fetchall()}
        aborted_commits = sorted(
            str(row["run_id"])
            for row in operations
            if str(row["status"]) == "ABORTED_NO_SUBMIT"
            and row.get("committed_paper_audit_key")
        )
        checks = {
            "no_orphan_cohort_results": actual <= expected,
            "all_recorded_promotions_applied": expected <= actual,
            "aborted_operations_not_committed": not aborted_commits,
        }
        return {
            "status": "PASS" if all(checks.values()) else "FAIL",
            "checks": checks,
            "expected_audit_keys": sorted(expected),
            "actual_audit_keys": sorted(actual),
            "orphan_audit_keys": sorted(actual - expected),
            "missing_audit_keys": sorted(expected - actual),
            "aborted_committed_run_ids": aborted_commits,
        }

    def assert_target_allowed(
        self, *, cohort_id: str, strategy_id: str, asset_id: str
    ) -> dict[str, Any]:
        cohort = self.load_cohort(cohort_id)
        if str(cohort["paper_strategy_id"]) != str(strategy_id):
            raise ValueError("paper strategy does not belong to the clean V2 cohort")
        if str(cohort["status"]) not in {"READY", "ACTIVE", "PASS"}:
            raise ValueError(f"clean V2 cohort is not active: {cohort['status']}")
        baseline_assets = {str(item) for item in cohort["baseline_asset_ids"]}
        if str(asset_id) in baseline_assets:
            raise ValueError("target asset existed in the official baseline history")
        return cohort

    def register_operation(
        self,
        *,
        cohort_id: str,
        run_id: str,
        strategy_id: str,
        asset_id: str,
        market_id: str,
        condition_id: str,
        side: str,
        order_type: str,
        amount: Decimal,
        amount_unit: str,
        planned_gross_notional: Decimal,
    ) -> dict[str, Any]:
        operation_id = payload_hash(
            {"cohort_id": cohort_id, "run_id": run_id}, prefix="v2op-"
        )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.paper_clean_v2_cohorts WHERE cohort_id=%s FOR UPDATE",
                (str(cohort_id),),
            )
            cohort = cur.fetchone()
            if cohort is None:
                raise ValueError(f"clean V2 cohort not found: {cohort_id}")
            if str(cohort["paper_strategy_id"]) != str(strategy_id):
                raise ValueError("operation strategy differs from cohort strategy")
            if str(cohort["status"]) not in {"READY", "ACTIVE", "PASS"}:
                raise ValueError(f"clean V2 cohort is not active: {cohort['status']}")
            if str(asset_id) in {str(item) for item in cohort["baseline_asset_ids"]}:
                raise ValueError(
                    "operation target existed in the official baseline history"
                )
            cur.execute(
                "SELECT * FROM quant.paper_clean_v2_cohort_operations WHERE run_id=%s",
                (str(run_id),),
            )
            existing = cur.fetchone()
            if existing is not None:
                if str(existing["cohort_id"]) != str(cohort_id):
                    raise ValueError(
                        "calibration run is already bound to another cohort"
                    )
                conn.commit()
                return dict(existing)
            cur.execute(
                """
                SELECT run_id,status
                FROM quant.paper_clean_v2_cohort_operations
                WHERE cohort_id=%s AND status=ANY(%s)
                FOR UPDATE
                """,
                (str(cohort_id), list(ACTIVE_OPERATION_STATES)),
            )
            active = cur.fetchone()
            if active is not None:
                raise ValueError(
                    "clean V2 cohort already has an active operation: "
                    f"{active['run_id']}:{active['status']}"
                )
            cur.execute(
                """
                SELECT COALESCE(MAX(sequence),0) AS latest_filled_sequence
                FROM quant.paper_clean_v2_cohort_operations
                WHERE cohort_id=%s AND status='EVIDENCE_READY'
                """,
                (str(cohort_id),),
            )
            latest_filled_sequence = int(cur.fetchone()["latest_filled_sequence"] or 0)
            cur.execute(
                """
                SELECT COALESCE(MAX(
                    CASE
                        WHEN report->>'operation_count' ~ '^[0-9]+$'
                        THEN (report->>'operation_count')::integer
                        ELSE 0
                    END
                ),0) AS checkpointed_operation_count
                FROM quant.paper_clean_v2_cohort_checkpoints
                WHERE cohort_id=%s AND checkpoint_status='PASS'
                """,
                (str(cohort_id),),
            )
            checkpointed_operation_count = int(
                cur.fetchone()["checkpointed_operation_count"] or 0
            )
            if latest_filled_sequence > checkpointed_operation_count:
                raise ValueError(
                    "filled clean-cohort operation requires a passed account-truth "
                    "checkpoint before the next operation"
                )
            projected = Decimal(str(cohort["committed_gross_notional"])) + Decimal(
                planned_gross_notional
            )
            if projected > Decimal(str(cohort["max_gross_notional"])):
                raise ValueError("clean V2 cohort gross-notional cap would be exceeded")
            sequence = int(cohort["operation_count"]) + 1
            cur.execute(
                """
                INSERT INTO quant.paper_clean_v2_cohort_operations (
                    operation_id,cohort_id,sequence,run_id,asset_id,market_id,
                    condition_id,side,order_type,amount,amount_unit,
                    planned_gross_notional,status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PREPARED')
                RETURNING *
                """,
                (
                    operation_id,
                    cohort_id,
                    sequence,
                    run_id,
                    asset_id,
                    market_id,
                    condition_id,
                    side,
                    order_type,
                    amount,
                    amount_unit,
                    planned_gross_notional,
                ),
            )
            operation = dict(cur.fetchone())
            cur.execute(
                """
                UPDATE quant.paper_clean_v2_cohorts
                SET status='ACTIVE',operation_count=%s,
                    committed_gross_notional=%s,updated_at=clock_timestamp()
                WHERE cohort_id=%s
                """,
                (sequence, projected, cohort_id),
            )
            conn.commit()
            return operation

    def transition_operation(
        self,
        *,
        run_id: str,
        expected: Sequence[str],
        target: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        allowed = tuple(dict.fromkeys(str(item) for item in expected))
        if not allowed:
            raise ValueError("operation transition requires an expected state")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_clean_v2_cohort_operations
                SET status=%s,
                    evidence=CASE
                        WHEN %s::text IS NULL THEN evidence
                        ELSE evidence || jsonb_build_object('transition_reason',%s::text)
                    END,
                    updated_at=clock_timestamp()
                WHERE run_id=%s AND status=ANY(%s)
                RETURNING *
                """,
                (target, reason, reason, str(run_id), list(allowed)),
            )
            row = cur.fetchone()
            transitioned = row is not None
            if row is None:
                cur.execute(
                    """
                    SELECT * FROM quant.paper_clean_v2_cohort_operations
                    WHERE run_id=%s
                    """,
                    (str(run_id),),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError(
                        f"clean cohort operation not found for run: {run_id}"
                    )
                if str(row["status"]) != str(target):
                    raise ValueError(
                        "clean cohort operation transition conflict: "
                        f"expected={allowed},actual={row['status']},target={target}"
                    )
            if transitioned and str(target) == "ABORTED_NO_SUBMIT":
                cur.execute(
                    """
                    UPDATE quant.paper_clean_v2_cohorts
                    SET committed_gross_notional=GREATEST(
                            0,committed_gross_notional-%s
                        ),updated_at=clock_timestamp()
                    WHERE cohort_id=%s
                    """,
                    (row["planned_gross_notional"], row["cohort_id"]),
                )
            conn.commit()
            return dict(row)

    def record_operation_evidence(
        self,
        *,
        run_id: str,
        status: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        stable = dict(evidence)
        digest = _sha256_payload(stable)
        lifecycle = _mapping(stable.get("lifecycle"))
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT status FROM quant.paper_clean_v2_cohort_operations
                WHERE run_id=%s FOR UPDATE
                """,
                (str(run_id),),
            )
            current = cur.fetchone()
            if current is None:
                raise ValueError(f"clean cohort operation not found for run: {run_id}")
            current_status = str(current["status"])
            if (
                current_status in SETTLED_OPERATION_STATES
                and str(status) != current_status
            ):
                raise ValueError(
                    "terminal clean cohort evidence cannot be downgraded: "
                    f"current={current_status},target={status}"
                )
            cur.execute(
                """
                UPDATE quant.paper_clean_v2_cohort_operations
                SET paired_probe_id=%s,paper_intent_id=%s,status=%s,order_id=%s,
                    trade_ids=%s::jsonb,transaction_hashes=%s::jsonb,
                    evidence=%s::jsonb,evidence_sha256=%s,
                    submitted_at=COALESCE(submitted_at,%s),terminal_at=%s,
                    updated_at=clock_timestamp()
                WHERE run_id=%s
                RETURNING *
                """,
                (
                    stable.get("paired_probe_id"),
                    stable.get("paper_intent_id"),
                    status,
                    lifecycle.get("order_id"),
                    canonical_json(lifecycle.get("trade_ids") or []),
                    canonical_json(lifecycle.get("transaction_hashes") or []),
                    canonical_json(stable),
                    digest,
                    stable.get("submitted_at"),
                    stable.get("terminal_at"),
                    run_id,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row)

    def list_operations(self, cohort_id: str) -> list[dict[str, Any]]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.paper_clean_v2_cohort_operations
                WHERE cohort_id=%s ORDER BY sequence
                """,
                (str(cohort_id),),
            )
            return [dict(row) for row in cur.fetchall()]

    def record_checkpoint(
        self,
        *,
        cohort_id: str,
        report: AccountTruthReport,
        checkpoint_status: str,
        payload: Mapping[str, Any],
        artifact_paths: Mapping[str, Any],
    ) -> dict[str, Any]:
        stable = dict(payload)
        digest = _sha256_payload(stable)
        checkpoint_id = payload_hash(
            {
                "cohort_id": cohort_id,
                "reconciliation_id": report.reconciliation_id,
                "report_sha256": digest,
            },
            prefix="v2checkpoint-",
        )
        pnl_status = str(
            _mapping(report.summary.get("pnl_truth_contract")).get("status")
            or "UNKNOWN"
        )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_clean_v2_cohort_checkpoints (
                    checkpoint_id,cohort_id,official_run_id,reconciliation_id,
                    source_as_of,account_truth_status,pnl_truth_status,
                    checkpoint_status,report,report_sha256,artifact_paths
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)
                ON CONFLICT (reconciliation_id) DO UPDATE SET
                    checkpoint_status=EXCLUDED.checkpoint_status,
                    report=EXCLUDED.report,report_sha256=EXCLUDED.report_sha256,
                    artifact_paths=EXCLUDED.artifact_paths
                RETURNING *
                """,
                (
                    checkpoint_id,
                    cohort_id,
                    report.official_run_id,
                    report.reconciliation_id,
                    report.as_of,
                    report.status.value,
                    pnl_status,
                    checkpoint_status,
                    canonical_json(stable),
                    digest,
                    canonical_json(artifact_paths),
                ),
            )
            row = dict(cur.fetchone())
            cur.execute(
                """
                UPDATE quant.paper_clean_v2_cohorts
                SET last_reconciliation_id=%s,last_checkpoint_status=%s,
                    status=CASE WHEN %s='PASS' THEN 'PASS' ELSE status END,
                    updated_at=clock_timestamp()
                WHERE cohort_id=%s
                """,
                (
                    report.reconciliation_id,
                    checkpoint_status,
                    checkpoint_status,
                    cohort_id,
                ),
            )
            conn.commit()
            return row


class CleanV2CohortService:
    def __init__(
        self,
        *,
        store: CleanV2CohortStore,
        account_truth: OfficialAccountTruthService,
        calibration_store: CalibrationStore,
        shadow_store: Any,
        live_adapter: Any | None = None,
        receipt_source: Any | None = None,
        output_root: Path | str = "runtime_outputs/clean_v2_cohort",
    ) -> None:
        self.store = store
        self.account_truth = account_truth
        self.calibration_store = calibration_store
        self.shadow_store = shadow_store
        self.live_adapter = live_adapter
        self.receipt_source = receipt_source
        self.output_root = Path(output_root)

    def initialize(
        self,
        *,
        cohort_id: str,
        account_address: str,
        max_gross_notional: Decimal = Decimal("20"),
    ) -> dict[str, Any]:
        cohort = str(cohort_id).strip()
        if not cohort:
            raise ValueError("cohort_id is required")
        if Decimal(max_gross_notional) <= 0:
            raise ValueError("max_gross_notional must be positive")
        strategy_id = f"post-v2-clean-{cohort}"
        scope_id = f"post-v2-clean:{cohort}"
        self.store.ensure_schema()
        self.calibration_store.ensure_schema()
        ledger = PostgresPaperLedgerSink(
            connection_factory=self.shadow_store.connection_factory,
        )
        ledger.ensure_account(strategy_id)
        paper_baseline = self.store.assert_strategy_zero_delta(strategy_id)
        official, baseline = self.account_truth.create_baseline(
            account_address=account_address,
            scope_id=scope_id,
            strategy_ids=(strategy_id,),
        )
        baseline_as_of = _timestamp(baseline["source_as_of"])
        if baseline_as_of < CLEAN_V2_COHORT_EFFECTIVE_AT:
            raise ValueError("official baseline predates the clean V2 venue regime")
        baseline_payload = _mapping(baseline.get("baseline_payload"))
        baseline_assets = sorted(
            str(item) for item in _mapping(baseline_payload.get("positions"))
        )
        manifest = {
            "schema_version": "post-clob-v2-clean-cohort-v1",
            "cohort_id": cohort,
            "scope_id": scope_id,
            "account_address": str(account_address).lower(),
            "paper_strategy_id": strategy_id,
            "venue_regime_id": DEFAULT_VENUE_REGIME_ID,
            "clob_v2_production_at": CLOB_V2_PRODUCTION_AT,
            "venue_cutover_at": CLEAN_V2_COHORT_EFFECTIVE_AT,
            "baseline_id": baseline["baseline_id"],
            "baseline_official_run_id": baseline["official_run_id"],
            "baseline_as_of": baseline_as_of,
            "baseline_hash": baseline["baseline_hash"],
            "baseline_asset_ids": baseline_assets,
            "paper_baseline": paper_baseline,
            "normalized_initial_delta": "0",
            "valuation_policy": "OFFICIAL_ACCOUNTING_SNAPSHOT_MARKS",
            "max_gross_notional_usd": format(Decimal(max_gross_notional), "f"),
            "captured_official_run_id": official.run_id,
        }
        row = self.store.create_cohort(manifest)
        return {"status": "READY", "cohort": row, "manifest": manifest}

    def sync_run_evidence(self, run_id: str) -> dict[str, Any]:
        operation = self.store.load_operation_by_run(run_id)
        cohort = self.store.load_cohort(str(operation["cohort_id"]))
        run = self.calibration_store.load_run(run_id)
        if run is None:
            raise ValueError(f"calibration run not found: {run_id}")
        run_plan = _mapping(run.get("plan"))
        if str(run_plan.get("clean_cohort_id") or "") != str(cohort["cohort_id"]):
            raise ValueError("calibration run is not bound to this clean cohort")
        if str(run_plan.get("paper_strategy_id") or "") != str(
            cohort["paper_strategy_id"]
        ):
            raise ValueError("calibration run has a foreign Paper strategy")
        probes = self.calibration_store.load_probes(run_id)
        if len(probes) != 1:
            raise ValueError(
                f"clean cohort run must contain exactly one probe: {len(probes)}"
            )
        probe = probes[0]
        probe, user_ws_events = self._refresh_probe_truth(probe)
        promotion_error = None
        if bool(probe.get("exchange_submit_called")) and not operation.get(
            "committed_paper_audit_key"
        ):
            try:
                self.store.commit_staged_prediction(run_id=run_id)
            except Exception as exc:  # noqa: BLE001 - evidence remains fail-closed.
                promotion_error = f"{exc.__class__.__name__}:{str(exc)[:300]}"
            operation = self.store.load_operation_by_run(run_id)
        paired_id = str(probe.get("paired_probe_id") or "")
        paired = self.shadow_store.load_paired_probe(paired_id) if paired_id else None
        receipt_manifest: list[dict[str, Any]] = []
        lifecycle = _mapping(probe.get("lifecycle"))
        receipt_errors: list[str] = []
        if promotion_error:
            receipt_errors.append(f"paper_promotion:{promotion_error}")
        order_id = str(
            lifecycle.get("order_id")
            or _mapping(probe.get("signed_order_audit")).get("order_hash")
            or ""
        )
        asset_id = str(probe.get("asset_id") or "")
        correlated_trades = _correlated_trades(
            lifecycle.get("rest_trades") or (), order_id=order_id
        )
        tx_hashes = _operation_transaction_hashes(
            lifecycle=lifecycle,
            user_ws_events=user_ws_events,
            correlated_trades=correlated_trades,
            order_id=order_id,
        )
        for tx_hash in tx_hashes:
            if self.receipt_source is None:
                receipt_errors.append(f"receipt_source_missing:{tx_hash}")
                continue
            try:
                receipt = self.receipt_source.get(tx_hash)
                status = _transaction_receipt_status(receipt.payload)
                if status not in {"0x1", "1"}:
                    raise ValueError(
                        f"transaction receipt status is {status or 'missing'}"
                    )
                receipt_payload = _transaction_receipt_payload(receipt.payload)
                orderfilled_rows = decode_orderfilled_logs(
                    receipt_payload.get("logs"),
                    transaction_hash=str(receipt.transaction_hash).lower(),
                    order_id=order_id,
                    asset_id=asset_id,
                    block_number=receipt_payload.get("blockNumber"),
                )
                receipt_manifest.append(
                    {
                        "transaction_hash": receipt.transaction_hash,
                        "content_sha256": receipt.content_sha256,
                        "artifact_path": receipt.artifact_path,
                        "rpc_source": receipt.rpc_source,
                        "status": "CONFIRMED",
                        "orderfilled_rows": orderfilled_rows,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - evidence remains pending.
                receipt_errors.append(f"{tx_hash}:{type(exc).__name__}:{exc}")
        fee_finality: dict[str, Any] | None = None
        onchain_fee = _onchain_fee_total(receipt_manifest)
        committed_audit_key = str(operation.get("committed_paper_audit_key") or "")
        if onchain_fee is not None and committed_audit_key:
            fee_evidence_payload = [
                {
                    "transaction_hash": row.get("transaction_hash"),
                    "content_sha256": row.get("content_sha256"),
                    "orderfilled_rows": row.get("orderfilled_rows") or [],
                }
                for row in receipt_manifest
                if row.get("orderfilled_rows")
            ]
            fee_evidence_sha256 = _sha256_payload(fee_evidence_payload)
            try:
                fee_finality = PostgresPaperLedgerSink(
                    connection_factory=self.shadow_store.connection_factory,
                ).reconcile_official_fill_fee(
                    audit_key=committed_audit_key,
                    official_total_fee=onchain_fee,
                    evidence_id=(
                        f"polygon-orderfilled-v2:{order_id}:"
                        f"{fee_evidence_sha256[:16]}"
                    ),
                    evidence_sha256=fee_evidence_sha256,
                )
            except Exception as exc:  # noqa: BLE001 - evidence remains fail-closed.
                fee_finality = {
                    "status": "ERROR",
                    "error": f"{exc.__class__.__name__}:{str(exc)[:300]}",
                    "evidence_sha256": fee_evidence_sha256,
                }
                receipt_errors.append(f"paper_fee_finality:{fee_finality['error']}")
        evidence = build_operation_evidence(
            probe=probe,
            paired=paired,
            receipt_manifest=receipt_manifest,
            receipt_errors=receipt_errors,
            expected_paper_strategy_id=str(cohort["paper_strategy_id"]),
            expected_staging_strategy_id=str(
                operation.get("paper_staging_strategy_id") or ""
            ),
            committed_paper_audit_key=str(
                operation.get("committed_paper_audit_key") or ""
            ),
            signed_paper_audit_key=str(operation.get("signed_paper_audit_key") or ""),
            user_ws_events=user_ws_events,
            fee_finality=fee_finality,
        )
        operation_root = (
            self.output_root / str(operation["cohort_id"]) / "operations" / str(run_id)
        )
        immutable_digest = _sha256_payload(evidence)
        evidence_path = operation_root / f"operation-evidence-{immutable_digest}.json"
        _atomic_json(evidence_path, evidence)
        _atomic_json(operation_root / "operation-evidence.json", evidence)
        evidence = {
            **evidence,
            "artifact_path": str(evidence_path.resolve()),
            "artifact_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        }
        status = str(evidence["evidence_status"])
        return self.store.record_operation_evidence(
            run_id=run_id,
            status=status,
            evidence=evidence,
        )

    def _refresh_probe_truth(
        self, probe: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        row = dict(probe)
        events = self.calibration_store.load_events(str(row["probe_id"]))
        user_ws_events = [
            dict(event)
            for event in events
            if "user-ws" in str(event.get("source") or "").lower()
        ]
        lifecycle = dict(_mapping(row.get("lifecycle")))
        order_id = str(lifecycle.get("order_id") or "")
        terminal_truth_frozen = _terminal_probe_truth_is_frozen(row)
        accounting_reconciled = bool(
            _mapping(_mapping(row.get("reconciliation")).get("accounting")).get(
                "accounting_reconciled"
            )
        )
        if self.live_adapter is not None and order_id and not terminal_truth_frozen:
            decision = _timestamp(row.get("decision_ts"))
            now = datetime.now(timezone.utc)
            rest = self.live_adapter.get_order_reconciliation_snapshot(
                order_id=order_id,
                condition_id=str(row.get("condition_id") or ""),
                asset_id=str(row.get("asset_id") or ""),
                after=int(decision.timestamp()) - 60,
                before=int(now.timestamp()) + 60,
            )
            trades = _correlated_trades(rest.get("trades") or (), order_id=order_id)
            next_lifecycle = {
                **lifecycle,
                "rest_order": rest.get("order") or {},
                "rest_trades": trades,
                "open_orders_after": rest.get("open_orders") or [],
                "trade_ids": _identifiers(trades, "trade_id", "id"),
            }
            next_lifecycle["transaction_hashes"] = list(
                _operation_transaction_hashes(
                    lifecycle=next_lifecycle,
                    user_ws_events=user_ws_events,
                    correlated_trades=trades,
                    order_id=order_id,
                )
            )
            if not accounting_reconciled:
                next_lifecycle["account_after"] = (
                    self.live_adapter.get_account_snapshot(
                        asset_id=str(row.get("asset_id") or "")
                    ).as_dict()
                )
            lifecycle = next_lifecycle
            row["lifecycle"] = next_lifecycle
        if terminal_truth_frozen:
            reconciled = dict(row)
            pnl = dict(
                _mapping(_mapping(row.get("reconciliation")).get("pnl"))
            )
        else:
            reconciled = reconcile_probe(row, events)
            pnl = self.calibration_store.apply_probe_pnl(reconciled)
        operation = self.store.load_operation_by_run(str(row["run_id"]))
        clean_reconciliation = {
            "clean_cohort_staging": {
                "status": (
                    "PASS" if operation.get("staged_paper_audit_key") else "MISSING"
                ),
                "staging_strategy_id": operation.get("paper_staging_strategy_id"),
                "audit_key": operation.get("staged_paper_audit_key"),
            },
            "clean_cohort_signed_prediction": {
                "status": (
                    "PASS" if operation.get("signed_paper_audit_key") else "MISSING"
                ),
                "audit_key": operation.get("signed_paper_audit_key"),
                "frozen_at": operation.get("signed_prediction_frozen_at"),
            },
            "clean_cohort_paper_commit": {
                "status": (
                    "PASS"
                    if operation.get("committed_paper_audit_key")
                    else "RETRY_REQUIRED"
                ),
                "audit_key": operation.get("committed_paper_audit_key"),
            },
        }
        reconciled["reconciliation"] = {
            **dict(_mapping(reconciled.get("reconciliation"))),
            **clean_reconciliation,
            "pnl": pnl,
        }
        persisted = self.calibration_store.upsert_probe(reconciled)
        try:
            paired = sync_calibration_probe_to_paired(
                persisted,
                store=self.shadow_store,
            )
            reconciliation = dict(_mapping(persisted.get("reconciliation")))
            reconciliation["paired_probe_sync"] = {
                "status": "PASS",
                "paired_probe_id": paired.get("probe_id"),
                "paired_probe_status": paired.get("status"),
            }
            persisted = self.calibration_store.upsert_probe(
                {**persisted, "reconciliation": reconciliation}
            )
        except Exception as exc:
            reconciliation = dict(_mapping(persisted.get("reconciliation")))
            reconciliation["paired_probe_sync"] = {
                "status": "RETRY_REQUIRED",
                "error": f"{exc.__class__.__name__}:{str(exc)[:300]}",
            }
            persisted = self.calibration_store.upsert_probe(
                {**persisted, "reconciliation": reconciliation}
            )
        return persisted, user_ws_events

    def checkpoint(self, cohort_id: str) -> dict[str, Any]:
        cohort = self.store.load_cohort(cohort_id)
        operations = self.store.list_operations(cohort_id)
        ledger_integrity = self.store.cohort_ledger_integrity(cohort_id)
        asset_ids = tuple(dict.fromkeys(str(row["asset_id"]) for row in operations))
        if not operations:
            raise ValueError("clean V2 cohort has no registered operations")
        official, report = self.account_truth.capture_and_reconcile_clean_v2_cohort(
            account_address=str(cohort["account_address"]),
            scope_id=str(cohort["scope_id"]),
            strategy_id=str(cohort["paper_strategy_id"]),
            asset_ids=asset_ids,
            venue_cutover_at=_timestamp(cohort["venue_cutover_at"]),
        )
        output = (
            self.output_root / str(cohort_id) / report.as_of.strftime("%Y%m%dT%H%M%SZ")
        )
        paths = write_account_truth_report(
            output_root=output,
            official=official,
            report=report,
            execution_gate_paths=(),
        )
        submitted = [row for row in operations if _operation_was_submitted(row)]
        filled = [
            row
            for row in submitted
            if str(row["status"]) not in {"NO_FILL_VERIFIED", "REJECT_VERIFIED"}
        ]
        operation_checks = {
            "registered_operation_present": bool(operations),
            "submitted_operation_present": bool(submitted),
            "filled_operation_present": bool(filled),
            "all_operations_terminal": all(
                str(row["status"]) in SETTLED_OPERATION_STATES for row in operations
            ),
            "all_submitted_evidence_ready": bool(submitted)
            and all(
                str(row["status"]) in SETTLED_OPERATION_STATES for row in submitted
            ),
            "all_filled_receipts_confirmed": bool(filled)
            and all(str(row["status"]) == "EVIDENCE_READY" for row in filled),
            "official_snapshot_follows_operations": bool(submitted)
            and all(
                row.get("terminal_at") is not None
                and _timestamp(row["terminal_at"]) <= report.as_of
                for row in submitted
            ),
            "account_truth_pass": report.status.value == "PASS",
            "pnl_truth_contract_pass": str(
                _mapping(report.summary.get("pnl_truth_contract")).get("status")
            )
            == "PASS",
            "cohort_ledger_integrity_pass": ledger_integrity["status"] == "PASS",
        }
        checkpoint_status = "PASS" if all(operation_checks.values()) else "FAIL"
        payload = {
            "schema_version": "post-clob-v2-clean-cohort-checkpoint-v1",
            "status": checkpoint_status,
            "cohort_id": cohort_id,
            "paper_strategy_id": cohort["paper_strategy_id"],
            "scope_id": cohort["scope_id"],
            "official_run_id": report.official_run_id,
            "reconciliation_id": report.reconciliation_id,
            "source_as_of": report.as_of,
            "operation_checks": operation_checks,
            "operation_count": len(operations),
            "submitted_operation_count": len(submitted),
            "filled_operation_count": len(filled),
            "operation_states": {
                str(row["operation_id"]): str(row["status"]) for row in operations
            },
            "cohort_ledger_integrity": ledger_integrity,
            "account_truth_status": report.status.value,
            "pnl_truth_contract": report.summary.get("pnl_truth_contract"),
            "normalized_pnl": {
                key: report.summary.get(key)
                for key in (
                    "official_cash_delta",
                    "paper_cash_delta",
                    "official_cohort_marked_value",
                    "paper_cohort_marked_value",
                    "official_cohort_realized_pnl_delta",
                    "official_cohort_reported_realized_pnl_delta",
                    "official_cohort_realized_pnl_source",
                    "paper_cohort_realized_pnl_delta",
                )
            },
            "account_truth_content_sha256": report.content_sha256,
            "accounting_snapshot_zip_sha256": official.accounting.zip_sha256,
            "artifact_paths": paths,
        }
        payload["content_sha256"] = _sha256_payload(payload)
        report_path = output / "clean-v2-cohort-checkpoint.json"
        _atomic_json(report_path, payload)
        paths = {**paths, "clean_v2_checkpoint": str(report_path.resolve())}
        self.store.record_checkpoint(
            cohort_id=cohort_id,
            report=report,
            checkpoint_status=checkpoint_status,
            payload=payload,
            artifact_paths=paths,
        )
        return payload


def build_operation_evidence(
    *,
    probe: Mapping[str, Any],
    paired: Mapping[str, Any] | None,
    receipt_manifest: Sequence[Mapping[str, Any]],
    receipt_errors: Sequence[str] = (),
    expected_paper_strategy_id: str | None = None,
    expected_staging_strategy_id: str | None = None,
    committed_paper_audit_key: str | None = None,
    signed_paper_audit_key: str | None = None,
    user_ws_events: Sequence[Mapping[str, Any]] = (),
    fee_finality: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    lifecycle = _mapping(probe.get("lifecycle"))
    reconciliation = _mapping(probe.get("reconciliation"))
    prediction = _mapping(probe.get("prediction"))
    order_truth = _mapping(reconciliation.get("order"))
    accounting = _mapping(reconciliation.get("accounting"))
    pnl = _mapping(reconciliation.get("pnl"))
    user_ws = _mapping(lifecycle.get("user_ws_capture"))
    fee_finality_row = _mapping(fee_finality)
    paired_row = _mapping(paired)
    predicted_class = str(reconciliation.get("predicted_class") or "").upper()
    actual_class = str(reconciliation.get("actual_class") or "").upper()
    filled = actual_class in {"PARTIAL", "FULL"}
    no_fill = actual_class == "NO_FILL"
    rejected = actual_class == "REJECT"
    paper_size = _optional_decimal(prediction.get("filled_size"))
    actual_size = _optional_decimal(order_truth.get("actual_matched_size"))
    price_error_ticks = _optional_decimal(reconciliation.get("price_error_ticks"))
    reconstructed_fee_error = _optional_decimal(reconciliation.get("fee_error"))
    paper_fee = _optional_decimal(prediction.get("total_fee"))
    onchain_rows = [
        dict(orderfilled)
        for receipt in receipt_manifest
        for orderfilled in receipt.get("orderfilled_rows") or ()
        if isinstance(orderfilled, Mapping)
    ]
    onchain_size = sum(
        (_optional_decimal(row.get("size")) or Decimal(0) for row in onchain_rows),
        Decimal(0),
    )
    onchain_fee = _onchain_fee_total(receipt_manifest)
    fee_error = (
        abs(paper_fee - onchain_fee)
        if paper_fee is not None and onchain_fee is not None
        else reconstructed_fee_error
    )
    fee_truth_source = (
        "POLYGON_ORDER_FILLED_V2"
        if onchain_fee is not None
        else "REST_TRADE_OR_FEE_SCHEDULE_RECONSTRUCTION"
    )
    size_match = (
        paper_size is not None
        and actual_size is not None
        and abs(paper_size - actual_size) <= Decimal("0.000001")
    )
    price_match = not filled or (
        price_error_ticks is not None and price_error_ticks <= Decimal("1")
    )
    fee_match = fee_error is not None and fee_error <= Decimal("0.00001")
    onchain_size_match = not filled or (
        actual_size is not None
        and bool(onchain_rows)
        and abs(onchain_size - actual_size) <= Decimal("0.000001")
    )
    order_id = str(lifecycle.get("order_id") or "")
    rest_trades = _correlated_trades(
        lifecycle.get("rest_trades") or (), order_id=order_id
    )
    tx_hashes = set(
        _operation_transaction_hashes(
            lifecycle=lifecycle,
            user_ws_events=user_ws_events,
            correlated_trades=rest_trades,
            order_id=order_id,
        )
    )
    receipt_hashes = {
        str(row.get("transaction_hash") or "").lower()
        for row in receipt_manifest
        if str(row.get("status") or "") == "CONFIRMED"
    }
    checks = {
        "live_submit_boundary_crossed": bool(probe.get("exchange_submit_called")),
        "probe_is_calibratable": str(probe.get("probe_state") or "") == "CALIBRATABLE",
        "paired_probe_present": bool(paired_row),
        "paper_intent_present": bool(paired_row.get("paper_intent_id")),
        "paper_prediction_present": bool(prediction),
        "paper_live_fill_class_matches": bool(predicted_class)
        and predicted_class == actual_class,
        "paper_live_filled_size_matches": size_match,
        "paper_live_vwap_within_one_tick": price_match,
        "paper_live_fee_matches": fee_match,
        "paper_strategy_matches": bool(expected_paper_strategy_id)
        and bool(expected_staging_strategy_id)
        and str(paired_row.get("strategy_id") or "")
        == str(expected_staging_strategy_id),
        "cohort_paper_prediction_committed": bool(committed_paper_audit_key),
        "signed_paper_prediction_frozen": bool(signed_paper_audit_key),
        "user_ws_recording_present": str(user_ws.get("status") or "")
        in {"TERMINAL", "TIMEOUT"},
        "user_ws_event_present_when_filled": bool(user_ws_events) if filled else True,
        "rest_order_present": bool(_mapping(lifecycle.get("rest_order"))),
        "rest_trade_present_when_filled": bool(rest_trades) if filled else True,
        "transaction_hash_present_when_filled": bool(tx_hashes) if filled else True,
        "all_transaction_receipts_confirmed": (
            bool(tx_hashes) and tx_hashes <= receipt_hashes if filled else True
        ),
        "onchain_orderfilled_present_when_filled": bool(onchain_rows)
        if filled
        else True,
        "onchain_orderfilled_size_matches_rest": onchain_size_match,
        "onchain_fee_present_when_filled": onchain_fee is not None if filled else True,
        "paper_ledger_fee_finalized": (
            str(fee_finality_row.get("status") or "")
            in {"FINALITY_EXACT", "FINALITY_CORRECTED"}
            if filled
            else True
        ),
        "paired_sync_pass": str(
            _mapping(reconciliation.get("paired_probe_sync")).get("status") or ""
        )
        == "PASS",
        "live_accounting_reconciled": bool(accounting.get("accounting_reconciled")),
        "pnl_reconciliation_pass": bool(pnl.get("pnl_reconciled")),
    }
    if not bool(probe.get("exchange_submit_called")):
        status = "PREPARED"
    elif filled and not checks["all_transaction_receipts_confirmed"]:
        status = "AWAITING_RECEIPT"
    elif all(checks.values()) and filled:
        status = "EVIDENCE_READY"
    elif all(checks.values()) and no_fill:
        status = "NO_FILL_VERIFIED"
    elif all(checks.values()) and rejected:
        status = "REJECT_VERIFIED"
    else:
        status = "AWAITING_ACCOUNT_TRUTH"
    timestamps = _mapping(probe.get("timestamps"))
    return {
        "schema_version": "post-clob-v2-operation-evidence-v1",
        "run_id": probe.get("run_id"),
        "probe_id": probe.get("probe_id"),
        "paired_probe_id": probe.get("paired_probe_id"),
        "paper_intent_id": paired_row.get("paper_intent_id"),
        "actual_class": actual_class,
        "paper_status": prediction.get("status"),
        "predicted_class": predicted_class,
        "checks": checks,
        "evidence_status": status,
        "lifecycle": {
            "order_id": lifecycle.get("order_id"),
            "trade_ids": lifecycle.get("trade_ids") or [],
            "transaction_hashes": sorted(tx_hashes),
            "user_ws_capture": user_ws,
            "rest_order_hash": _sha256_payload(_mapping(lifecycle.get("rest_order"))),
            "rest_trades_hash": _sha256_payload(rest_trades),
        },
        "raw_sources": {
            "paper_prediction": dict(prediction),
            "paired_probe": dict(paired_row),
            "user_ws_events": [dict(row) for row in user_ws_events],
            "rest_order": dict(_mapping(lifecycle.get("rest_order"))),
            "rest_trades": rest_trades,
            "onchain_orderfilled": onchain_rows,
            "paper_fee_finality": dict(fee_finality_row),
        },
        "receipt_manifest": [dict(row) for row in receipt_manifest],
        "receipt_errors": list(receipt_errors),
        "execution_comparison": {
            "paper_filled_size": (
                format(paper_size, "f") if paper_size is not None else None
            ),
            "actual_filled_size": (
                format(actual_size, "f") if actual_size is not None else None
            ),
            "price_error_ticks": (
                format(price_error_ticks, "f")
                if price_error_ticks is not None
                else None
            ),
            "fee_error": format(fee_error, "f") if fee_error is not None else None,
            "paper_fee": format(paper_fee, "f") if paper_fee is not None else None,
            "onchain_fee": (
                format(onchain_fee, "f") if onchain_fee is not None else None
            ),
            "fee_truth_source": fee_truth_source,
            "onchain_filled_size": (
                format(onchain_size, "f") if onchain_rows else None
            ),
        },
        "submitted_at": timestamps.get("http_send_started_ts"),
        "terminal_at": (
            timestamps.get("trade_confirmed_ts")
            or timestamps.get("trade_mined_ts")
            or timestamps.get("http_response_completed_ts")
        ),
    }


def _onchain_fee_total(
    receipt_manifest: Sequence[Mapping[str, Any]],
) -> Decimal | None:
    rows = [
        row
        for receipt in receipt_manifest
        for row in receipt.get("orderfilled_rows") or ()
        if isinstance(row, Mapping)
    ]
    if not rows or any(row.get("fee_raw") in (None, "") for row in rows):
        return None
    return sum(
        (Decimal(str(row["fee_raw"])) / Decimal("1000000") for row in rows),
        Decimal(0),
    )


def _transaction_receipt_status(payload: Mapping[str, Any]) -> str:
    """Read status from either a JSON-RPC envelope or a bare receipt."""

    receipt = _transaction_receipt_payload(payload)
    return str(receipt.get("status") or "").lower()


def _transaction_receipt_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(payload.get("result")) if "result" in payload else payload


def _operation_was_submitted(row: Mapping[str, Any]) -> bool:
    evidence = _mapping(row.get("evidence"))
    return bool(_mapping(evidence.get("checks")).get("live_submit_boundary_crossed"))


def _terminal_probe_truth_is_frozen(probe: Mapping[str, Any]) -> bool:
    reconciliation = _mapping(probe.get("reconciliation"))
    return (
        str(probe.get("probe_state") or "") == "CALIBRATABLE"
        and bool(
            _mapping(reconciliation.get("accounting")).get(
                "accounting_reconciled"
            )
        )
        and bool(
            _mapping(reconciliation.get("order")).get(
                "final_trade_status_present"
            )
        )
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _staging_baseline_entries(
    *,
    run_id: str,
    staging_strategy_id: str,
    source_strategy_id: str,
    baseline_hash: str,
    account: Mapping[str, Any],
    positions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Journal a cloned staging state without treating it as new economics."""

    initial_cash = Decimal(str(account["initial_cash"]))
    cash_balance = Decimal(str(account["cash_balance"]))
    account_realized = Decimal(str(account["realized_pnl"]))
    position_realized = sum(
        (Decimal(str(row["realized_pnl"])) for row in positions), Decimal(0)
    )
    metadata = {
        "source": "clean_v2_staging_clone",
        "source_strategy_id": str(source_strategy_id),
        "staging_strategy_id": str(staging_strategy_id),
        "staging_baseline_hash": str(baseline_hash),
        "non_economic_clone": True,
    }
    entries: list[dict[str, Any]] = [
        {
            "idempotency_key": f"clean-v2-staging-baseline:{run_id}:cash",
            "event_type": "STAGING_CASH_BASELINE",
            "market_id": "__STAGING_BASELINE__",
            "condition_id": "__STAGING_BASELINE__",
            "asset_id": "__CASH__",
            "shares_delta": Decimal(0),
            "cash_delta": cash_balance - initial_cash,
            "realized_pnl_delta": account_realized - position_realized,
            "position_after": Decimal(0),
            "cost_basis_after": Decimal(0),
            "metadata": metadata,
        }
    ]
    entries.extend(
        {
            "idempotency_key": (
                f"clean-v2-staging-baseline:{run_id}:position:{row['asset_id']}"
            ),
            "event_type": "STAGING_POSITION_BASELINE",
            "market_id": str(row["market_id"]),
            "condition_id": str(row["condition_id"]),
            "asset_id": str(row["asset_id"]),
            "shares_delta": Decimal(str(row["quantity"])),
            "cash_delta": Decimal(0),
            "realized_pnl_delta": Decimal(str(row["realized_pnl"])),
            "position_after": Decimal(str(row["quantity"])),
            "cost_basis_after": Decimal(str(row["cost_basis"])),
            "metadata": metadata,
        }
        for row in positions
    )
    return entries


def _optional_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except Exception:
        return None


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sha256_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _identifiers(rows: Sequence[Mapping[str, Any]], *keys: str) -> list[str]:
    return sorted(
        {
            str(row.get(key))
            for row in rows
            for key in keys
            if row.get(key) not in (None, "")
        }
    )


def _correlated_trades(rows: Any, *, order_id: str) -> list[dict[str, Any]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    return [
        dict(row)
        for row in rows
        if isinstance(row, Mapping) and correlates_order(row, order_id)
    ]


def _operation_transaction_hashes(
    *,
    lifecycle: Mapping[str, Any],
    user_ws_events: Sequence[Mapping[str, Any]],
    correlated_trades: Sequence[Mapping[str, Any]],
    order_id: str,
) -> tuple[str, ...]:
    hashes = _identifiers(
        correlated_trades,
        "transaction_hash",
        "transactionHash",
        "tx_hash",
    )
    for event in user_ws_events:
        payload = _mapping(event.get("payload")) or event
        if correlates_order(payload, order_id):
            hashes.extend(
                _identifiers(
                    [payload],
                    "transaction_hash",
                    "transactionHash",
                    "tx_hash",
                )
            )
    response = _mapping(lifecycle.get("http_response"))
    for key in ("transaction_hashes", "transactionsHashes"):
        values = response.get(key)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
            hashes.extend(str(value) for value in values if str(value).strip())
    for key in ("transaction_hash", "transactionHash", "tx_hash"):
        if response.get(key) not in (None, ""):
            hashes.append(str(response[key]))
    return tuple(dict.fromkeys(value.lower() for value in hashes if value.strip()))
