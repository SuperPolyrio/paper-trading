"""Official account activity, reward, bridge and return reconciliation service."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant.simulator.economics import (
    AccountCashflowOperation,
    AccountCashflowState,
    AccountCashflowType,
    PostgresAccountCashflowStore,
    PostgresAccountReturnStore,
    account_cashflow_operation_id,
)

from .models import (
    OfficialRewardRecord,
    RewardStatus,
    RewardType,
    deterministic_reward_id,
    payload_hash,
)
from .official_program_rules import bundled_program_rule_rows
from .official_reward_client import (
    EarningsCompletenessResult,
    PolymarketOfficialRewardClient,
    normalize_maker_rebates,
    normalize_user_earnings,
    reconcile_user_earnings_totals,
)
from .reward_ledger import PostgresRewardLedgerStore

OFFICIAL_ACTIVITY_TYPES = (
    "TRADE",
    "SPLIT",
    "MERGE",
    "REDEEM",
    "REWARD",
    "CONVERSION",
    "DEPOSIT",
    "WITHDRAWAL",
    "YIELD",
    "MAKER_REBATE",
    "TAKER_REBATE",
    "REFERRAL_REWARD",
)

ACTIVITY_REWARD_TYPES = {
    "MAKER_REBATE": RewardType.MAKER_REBATE,
    "TAKER_REBATE": RewardType.TAKER_REBATE,
    "REWARD": RewardType.LIQUIDITY_REWARD,
    "YIELD": RewardType.HOLDING_REWARD,
    "REFERRAL_REWARD": RewardType.REFERRAL_REWARD,
}

ACTIVITY_CASHFLOW_TYPES = {
    "DEPOSIT": AccountCashflowType.DEPOSIT,
    "WITHDRAWAL": AccountCashflowType.WITHDRAWAL,
}

OFFICIAL_SYNC_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_account_activities (
        source_event_id TEXT PRIMARY KEY,
        account_address TEXT NOT NULL,
        activity_type TEXT NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        activity_date DATE NOT NULL,
        condition_id TEXT,
        asset_id TEXT,
        amount NUMERIC,
        token_quantity NUMERIC,
        currency TEXT NOT NULL DEFAULT 'USDC',
        transaction_hash TEXT,
        raw_payload_hash TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        source TEXT NOT NULL,
        raw_payload JSONB NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_official_activity_account_ts_idx
    ON quant.paper_official_account_activities
    (account_address,event_ts,activity_type,source_event_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_reward_rules (
        rule_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        effective_date DATE NOT NULL,
        raw_payload_hash TEXT NOT NULL,
        raw_payload JSONB NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (source,source_event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_bridge_transactions (
        source_event_id TEXT PRIMARY KEY,
        bridge_address TEXT NOT NULL,
        status TEXT NOT NULL,
        transaction_hash TEXT,
        created_ts TIMESTAMPTZ,
        raw_payload_hash TEXT NOT NULL,
        raw_payload JSONB NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_account_sync_runs (
        run_id TEXT PRIMARY KEY,
        account_address TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ,
        status TEXT NOT NULL,
        window_start TIMESTAMPTZ NOT NULL,
        window_end TIMESTAMPTZ NOT NULL,
        activity_rows INTEGER NOT NULL DEFAULT 0,
        activity_inserted INTEGER NOT NULL DEFAULT 0,
        rewards_ingested INTEGER NOT NULL DEFAULT 0,
        reconciliations INTEGER NOT NULL DEFAULT 0,
        cashflows_ingested INTEGER NOT NULL DEFAULT 0,
        bridge_rows INTEGER NOT NULL DEFAULT 0,
        account_return_report_id TEXT,
        calibration_report_id TEXT,
        errors JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    ALTER TABLE quant.paper_official_account_sync_runs
    ADD COLUMN IF NOT EXISTS reconciliations INTEGER NOT NULL DEFAULT 0
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_account_sync_checkpoints (
        account_address TEXT NOT NULL,
        source TEXT NOT NULL,
        stream_key TEXT NOT NULL,
        watermark_ts TIMESTAMPTZ NOT NULL,
        last_success_at TIMESTAMPTZ NOT NULL,
        raw_payload_hash TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (account_address,source,stream_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_official_reward_source_checks (
        check_id TEXT PRIMARY KEY,
        account_address TEXT NOT NULL,
        source TEXT NOT NULL,
        reward_date DATE NOT NULL,
        status TEXT NOT NULL,
        detail_count INTEGER NOT NULL,
        total_count INTEGER NOT NULL,
        tolerance NUMERIC NOT NULL,
        detail_by_asset JSONB NOT NULL,
        total_by_asset JSONB NOT NULL,
        delta_by_asset JSONB NOT NULL,
        checked_at TIMESTAMPTZ NOT NULL,
        UNIQUE (account_address,source,reward_date)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_official_reward_source_checks_latest_idx
    ON quant.paper_official_reward_source_checks
       (account_address,source,reward_date DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_reward_aggregate_reconciliations (
        reconciliation_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        reward_type TEXT NOT NULL,
        reward_date DATE NOT NULL,
        scope_key TEXT NOT NULL,
        status TEXT NOT NULL,
        modeled_amount NUMERIC NOT NULL,
        official_amount NUMERIC NOT NULL,
        amount_delta NUMERIC NOT NULL,
        tolerance NUMERIC NOT NULL,
        modeled_samples INTEGER NOT NULL,
        official_samples INTEGER NOT NULL,
        reconciled_at TIMESTAMPTZ NOT NULL,
        UNIQUE (strategy_id,account_id,reward_type,reward_date,scope_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_reward_calibration_reports (
        report_id TEXT PRIMARY KEY,
        account_address TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        as_of TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL,
        official_sample_count INTEGER NOT NULL,
        modeled_sample_count INTEGER NOT NULL,
        report_hash TEXT NOT NULL,
        report JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (account_address,strategy_id,as_of)
    )
    """,
)


@dataclass(frozen=True)
class OfficialSyncResult:
    run_id: str
    status: str
    window_start: datetime
    window_end: datetime
    activity_rows: int
    activity_inserted: int
    rewards_ingested: int
    reconciliations: int
    cashflows_ingested: int
    bridge_rows: int
    account_return_report_id: str | None
    calibration_report_id: str | None
    errors: tuple[str, ...]


class PostgresOfficialAccountSyncStore:
    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS quant")
            for statement in OFFICIAL_SYNC_SCHEMA_STATEMENTS:
                cur.execute(statement)
            conn.commit()

    def ensure_account(self, strategy_id: str) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_accounts (strategy_id,initial_cash,cash_balance)
                VALUES (%s,0,0) ON CONFLICT (strategy_id) DO NOTHING
                """,
                (strategy_id,),
            )
            conn.commit()

    def recover_interrupted_runs(
        self, *, account_address: str, strategy_id: str, observed_at: datetime
    ) -> int:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_official_account_sync_runs
                SET status='FAILED',completed_at=%s,
                    errors=errors || %s::jsonb
                WHERE account_address=%s AND strategy_id=%s AND status='RUNNING'
                """,
                (
                    observed_at,
                    json.dumps(["process_interrupted_before_run_completion"]),
                    account_address.lower(),
                    strategy_id,
                ),
            )
            updated = int(cur.rowcount or 0)
            conn.commit()
        return updated

    def start_run(
        self,
        *,
        run_id: str,
        account_address: str,
        strategy_id: str,
        started_at: datetime,
        window_start: datetime,
        window_end: datetime,
    ) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_official_account_sync_runs (
                    run_id,account_address,strategy_id,started_at,status,
                    window_start,window_end
                ) VALUES (%s,%s,%s,%s,'RUNNING',%s,%s)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (
                    run_id,
                    account_address.lower(),
                    strategy_id,
                    started_at,
                    window_start,
                    window_end,
                ),
            )
            conn.commit()

    def finish_run(self, result: OfficialSyncResult) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quant.paper_official_account_sync_runs SET
                    completed_at=clock_timestamp(),status=%s,activity_rows=%s,
                    activity_inserted=%s,rewards_ingested=%s,reconciliations=%s,
                    cashflows_ingested=%s,
                    bridge_rows=%s,account_return_report_id=%s,
                    calibration_report_id=%s,errors=%s::jsonb
                WHERE run_id=%s
                """,
                (
                    result.status,
                    result.activity_rows,
                    result.activity_inserted,
                    result.rewards_ingested,
                    result.reconciliations,
                    result.cashflows_ingested,
                    result.bridge_rows,
                    result.account_return_report_id,
                    result.calibration_report_id,
                    json.dumps(result.errors),
                    result.run_id,
                ),
            )
            conn.commit()

    def record_activity(
        self,
        row: Mapping[str, Any],
        *,
        account_address: str,
        observed_at: datetime,
    ) -> tuple[str, bool]:
        source_event_id = activity_source_event_id(row, account_address)
        event_ts = activity_timestamp(row)
        raw_hash = payload_hash(row)
        activity_type = str(row.get("type") or "UNKNOWN").upper()
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_official_account_activities (
                    source_event_id,account_address,activity_type,event_ts,
                    activity_date,condition_id,asset_id,amount,token_quantity,
                    transaction_hash,raw_payload_hash,rule_version,source,
                    raw_payload,observed_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (source_event_id) DO NOTHING RETURNING source_event_id
                """,
                (
                    source_event_id,
                    account_address.lower(),
                    activity_type,
                    event_ts,
                    event_ts.date(),
                    _text(row.get("conditionId")),
                    _text(row.get("asset")),
                    _decimal_or_none(row.get("usdcSize")),
                    _decimal_or_none(row.get("size")),
                    _text(row.get("transactionHash")),
                    raw_hash,
                    "polymarket-data-api-activity-v1",
                    "POLYMARKET_DATA_API_ACTIVITY",
                    _json(row),
                    observed_at,
                ),
            )
            inserted = cur.fetchone() is not None
            if not inserted:
                cur.execute(
                    """
                    SELECT raw_payload_hash FROM quant.paper_official_account_activities
                    WHERE source_event_id=%s
                    """,
                    (source_event_id,),
                )
                existing = cur.fetchone()
                if existing is None or str(existing["raw_payload_hash"]) != raw_hash:
                    raise ValueError("official activity identity collision")
            conn.commit()
        return source_event_id, inserted

    def record_rule(
        self, row: Mapping[str, Any], *, observed_at: datetime
    ) -> bool:
        raw_hash = payload_hash(row)
        source = str(row.get("_source") or "CLOB_REWARD_CONFIG")
        source_event_id = _stable_id(
            "official-reward-rule", {"payload": raw_hash, "source": source}
        )
        effective = _rule_date(row, observed_at.date())
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_official_reward_rules (
                    rule_id,source,source_event_id,effective_date,
                    raw_payload_hash,raw_payload,observed_at
                ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (source,source_event_id) DO NOTHING RETURNING rule_id
                """,
                (
                    source_event_id,
                    source,
                    source_event_id,
                    effective,
                    raw_hash,
                    _json(row),
                    observed_at,
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
        return inserted

    def record_rules(
        self, rows: Sequence[Mapping[str, Any]], *, observed_at: datetime
    ) -> int:
        values = []
        for row in rows:
            raw_hash = payload_hash(row)
            source = str(row.get("_source") or "CLOB_REWARD_CONFIG")
            source_event_id = _stable_id(
                "official-reward-rule", {"payload": raw_hash, "source": source}
            )
            values.append(
                (
                    source_event_id,
                    source,
                    source_event_id,
                    _rule_date(row, observed_at.date()),
                    raw_hash,
                    _json(row),
                    observed_at,
                )
            )
        if not values:
            return 0
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO quant.paper_official_reward_rules (
                    rule_id,source,source_event_id,effective_date,
                    raw_payload_hash,raw_payload,observed_at
                ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (source,source_event_id) DO NOTHING
                """,
                values,
            )
            inserted = max(0, int(cur.rowcount or 0))
            conn.commit()
        return inserted

    def reward_rules_are_fresh(
        self, *, as_of: datetime, max_age: timedelta = timedelta(hours=23)
    ) -> bool:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT max(last_success_at) AS last_success_at
                FROM quant.paper_official_account_sync_checkpoints
                WHERE account_address=%s AND source='CLOB_REWARD_CONFIG'
                  AND stream_key='current'
                """,
                (self.rule_checkpoint_account(),),
            )
            row = cur.fetchone()
        return bool(
            row
            and row["last_success_at"]
            and row["last_success_at"] >= as_of - max_age
        )

    @staticmethod
    def rule_checkpoint_account() -> str:
        return "official-global-reward-rules"

    def record_bridge_transaction(
        self,
        row: Mapping[str, Any],
        *,
        bridge_address: str,
        observed_at: datetime,
    ) -> bool:
        raw_hash = payload_hash(row)
        source_event_id = _stable_id(
            "official-bridge-transaction",
            {
                "address": bridge_address,
                "createdTimeMs": row.get("createdTimeMs"),
                "fromAmountBaseUnit": row.get("fromAmountBaseUnit"),
                "fromChainId": row.get("fromChainId"),
                "toChainId": row.get("toChainId"),
                "txHash": row.get("txHash"),
            },
        )
        created_ts = None
        if row.get("createdTimeMs") is not None:
            created_ts = datetime.fromtimestamp(
                int(row["createdTimeMs"]) / 1000, tz=timezone.utc
            )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_official_bridge_transactions (
                    source_event_id,bridge_address,status,transaction_hash,
                    created_ts,raw_payload_hash,raw_payload,observed_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (source_event_id) DO UPDATE SET
                    status=EXCLUDED.status,
                    transaction_hash=COALESCE(EXCLUDED.transaction_hash,
                                              quant.paper_official_bridge_transactions.transaction_hash),
                    raw_payload_hash=EXCLUDED.raw_payload_hash,
                    raw_payload=EXCLUDED.raw_payload,
                    observed_at=EXCLUDED.observed_at
                """,
                (
                    source_event_id,
                    bridge_address,
                    str(row.get("status") or "UNKNOWN"),
                    _text(row.get("txHash")),
                    created_ts,
                    raw_hash,
                    _json(row),
                    observed_at,
                ),
            )
            conn.commit()
        return True

    def checkpoint(
        self,
        *,
        account_address: str,
        source: str,
        stream_key: str,
        watermark_ts: datetime,
        raw_payload_hash: str | None = None,
    ) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_official_account_sync_checkpoints (
                    account_address,source,stream_key,watermark_ts,
                    last_success_at,raw_payload_hash
                ) VALUES (%s,%s,%s,%s,clock_timestamp(),%s)
                ON CONFLICT (account_address,source,stream_key) DO UPDATE SET
                    watermark_ts=GREATEST(
                        quant.paper_official_account_sync_checkpoints.watermark_ts,
                        EXCLUDED.watermark_ts
                    ),
                    last_success_at=EXCLUDED.last_success_at,
                    raw_payload_hash=COALESCE(EXCLUDED.raw_payload_hash,
                                              quant.paper_official_account_sync_checkpoints.raw_payload_hash),
                    updated_at=clock_timestamp()
                """,
                (
                    account_address.lower(),
                    source,
                    stream_key,
                    watermark_ts,
                    raw_payload_hash,
                ),
            )
            conn.commit()

    def record_earnings_completeness(
        self,
        result: EarningsCompletenessResult,
        *,
        account_address: str,
        checked_at: datetime,
    ) -> str:
        source = "CLOB_REWARDS_USER_TOTAL"
        check_id = deterministic_reward_id(
            "official-reward-source-check",
            {
                "account_address": account_address.lower(),
                "reward_date": result.reward_date.isoformat(),
                "source": source,
            },
        )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_official_reward_source_checks (
                    check_id,account_address,source,reward_date,status,
                    detail_count,total_count,tolerance,detail_by_asset,
                    total_by_asset,delta_by_asset,checked_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)
                ON CONFLICT (account_address,source,reward_date) DO UPDATE SET
                    status=EXCLUDED.status,
                    detail_count=EXCLUDED.detail_count,
                    total_count=EXCLUDED.total_count,
                    tolerance=EXCLUDED.tolerance,
                    detail_by_asset=EXCLUDED.detail_by_asset,
                    total_by_asset=EXCLUDED.total_by_asset,
                    delta_by_asset=EXCLUDED.delta_by_asset,
                    checked_at=EXCLUDED.checked_at
                """,
                (
                    check_id,
                    account_address.lower(),
                    source,
                    result.reward_date,
                    result.status,
                    result.detail_count,
                    result.total_count,
                    result.tolerance,
                    _json(result.detail_by_asset),
                    _json(result.total_by_asset),
                    _json(result.delta_by_asset),
                    checked_at,
                ),
            )
            conn.commit()
        return check_id

    def reconcile_model_to_official(
        self,
        *,
        strategy_id: str,
        account_id: str,
        tolerance: Decimal = Decimal("0.000001"),
    ) -> int:
        """Reconcile at each official program's settlement granularity."""

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH modeled AS (
                    SELECT reward_type,period_end::date AS reward_date,
                           CASE
                             WHEN reward_type IN (
                               'MAKER_REBATE','LIQUIDITY_REWARD',
                               'SPONSOR_REWARD','DISPUTE_REWARD'
                             ) AND NULLIF(condition_id,'') IS NOT NULL
                             THEN 'condition:' || lower(condition_id)
                             ELSE 'account'
                           END AS scope_key,
                           sum(amount) AS amount,count(*) AS samples
                    FROM quant.paper_reward_accruals a
                    WHERE strategy_id=%s AND account_id=%s
                      AND source NOT IN (
                          'CLOB_REWARDS_USER','POLYMARKET_DATA_API_ACTIVITY'
                      )
                    GROUP BY reward_type,period_end::date,scope_key
                ), official AS (
                    SELECT reward_type,reward_date,
                           CASE
                             WHEN reward_type IN (
                               'MAKER_REBATE','LIQUIDITY_REWARD',
                               'SPONSOR_REWARD','DISPUTE_REWARD'
                             ) AND NULLIF(condition_id,'') IS NOT NULL
                             THEN 'condition:' || lower(condition_id)
                             ELSE 'account'
                           END AS scope_key,
                           sum(amount) AS amount,count(*) AS samples
                    FROM quant.paper_reward_payouts p
                    WHERE strategy_id=%s AND account_id=%s
                      AND status IN ('PAYABLE','RECEIVED')
                    GROUP BY reward_type,reward_date,scope_key
                ), pairs AS (
                    SELECT COALESCE(m.reward_type,o.reward_type) AS reward_type,
                           COALESCE(m.reward_date,o.reward_date) AS reward_date,
                           COALESCE(m.scope_key,o.scope_key) AS scope_key,
                           COALESCE(m.amount,0) AS modeled_amount,
                           COALESCE(o.amount,0) AS official_amount,
                           COALESCE(m.samples,0) AS modeled_samples,
                           COALESCE(o.samples,0) AS official_samples
                    FROM modeled m FULL OUTER JOIN official o
                      ON o.reward_type=m.reward_type
                     AND o.reward_date=m.reward_date
                     AND o.scope_key=m.scope_key
                )
                INSERT INTO quant.paper_reward_aggregate_reconciliations (
                    reconciliation_id,strategy_id,account_id,reward_type,
                    reward_date,scope_key,status,modeled_amount,official_amount,
                    amount_delta,tolerance,modeled_samples,official_samples,
                    reconciled_at
                )
                SELECT 'official-aggregate-reconciliation:' || md5(
                           %s || ':' || %s || ':' || reward_type || ':' ||
                           reward_date::text || ':' || scope_key
                       ),
                       %s,%s,reward_type,reward_date,scope_key,
                       CASE
                         WHEN modeled_samples=0 THEN 'UNMODELED_OFFICIAL'
                         WHEN official_samples=0 THEN 'WAITING_FOR_OFFICIAL_EVIDENCE'
                         WHEN abs(official_amount-modeled_amount) <= %s THEN 'PASS'
                         ELSE 'MISMATCH'
                       END,
                       modeled_amount,official_amount,
                       official_amount-modeled_amount,%s,
                       modeled_samples,official_samples,clock_timestamp()
                FROM pairs
                ON CONFLICT (
                    strategy_id,account_id,reward_type,reward_date,scope_key
                ) DO UPDATE SET
                    status=EXCLUDED.status,
                    modeled_amount=EXCLUDED.modeled_amount,
                    official_amount=EXCLUDED.official_amount,
                    amount_delta=EXCLUDED.amount_delta,
                    tolerance=EXCLUDED.tolerance,
                    modeled_samples=EXCLUDED.modeled_samples,
                    official_samples=EXCLUDED.official_samples,
                    reconciled_at=EXCLUDED.reconciled_at
                """,
                (
                    strategy_id,
                    account_id,
                    strategy_id,
                    account_id,
                    strategy_id,
                    account_id,
                    strategy_id,
                    account_id,
                    tolerance,
                    tolerance,
                ),
            )
            inserted = int(cur.rowcount or 0)
            conn.commit()
        return inserted

    def reconcile_calibration_redemptions(
        self,
        *,
        account_address: str,
        tolerance: Decimal = Decimal("0.000001"),
    ) -> int:
        """Close stale calibration settlements from unique official REDEEM rows.

        A wallet can hold shares outside the calibration campaign, so a
        condition-only match is not sufficient.  We only accept one official
        activity row whose account, condition, payout, and event time all agree
        with the pending settlement.  Ambiguous matches remain pending for
        manual/on-chain reconciliation.
        """

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_regclass('quant.paper_calibration_pnl_settlements') AS settlements,
                       to_regclass('quant.paper_market_registry_tokens') AS tokens,
                       to_regclass('quant.paper_official_account_activities') AS activities
                """
            )
            relations = cur.fetchone()
            if not relations or not all(relations.values()):
                return 0
            cur.execute(
                """
                WITH raw_matches AS (
                    SELECT DISTINCT
                           s.settlement_key,
                           s.expected_real_payout,
                           a.amount AS observed_payout,
                           a.source_event_id,
                           a.transaction_hash,
                           a.event_ts,
                           lower(a.condition_id) AS condition_id
                    FROM quant.paper_calibration_pnl_settlements s
                    JOIN quant.paper_market_registry_tokens t
                      ON t.asset_id=s.asset_id
                    JOIN quant.paper_official_account_activities a
                      ON a.account_address=lower(s.account_id)
                     AND a.activity_type='REDEEM'
                     AND lower(a.condition_id)=lower(t.condition_id)
                    WHERE lower(s.account_id)=%s
                      AND s.cash_reconciliation_status='PENDING_REDEMPTION'
                      AND s.expected_real_payout > 0
                      AND a.amount IS NOT NULL
                      AND a.transaction_hash LIKE '0x%%'
                      AND a.event_ts >= s.resolved_at - interval '5 minutes'
                      AND abs(a.amount-s.expected_real_payout) <= %s
                ), unique_matches AS (
                    SELECT *,count(*) OVER (PARTITION BY settlement_key) AS match_count
                    FROM raw_matches
                )
                UPDATE quant.paper_calibration_pnl_settlements s
                SET observed_real_payout=m.observed_payout,
                    cash_reconciliation_status='PASS',
                    payload=s.payload || jsonb_build_object(
                        'observed_real_payout',m.observed_payout::text,
                        'payout_error',abs(m.observed_payout-s.expected_real_payout)::text,
                        'cash_reconciliation_status','PASS',
                        'tolerance',%s::text,
                        'redemption_evidence',jsonb_build_object(
                            'source','POLYMARKET_DATA_API_ACTIVITY',
                            'source_event_id',m.source_event_id,
                            'transaction_hash',m.transaction_hash,
                            'condition_id',m.condition_id,
                            'event_ts',m.event_ts,
                            'observed_payout',m.observed_payout::text,
                            'evidence_level','official_activity'
                        )
                    )
                FROM unique_matches m
                WHERE m.match_count=1
                  AND s.settlement_key=m.settlement_key
                  AND s.cash_reconciliation_status='PENDING_REDEMPTION'
                RETURNING s.settlement_key
                """,
                (account_address.lower(), tolerance, tolerance),
            )
            reconciled = len(cur.fetchall())
            conn.commit()
        return reconciled

    def calibration_report(
        self,
        *,
        account_address: str,
        strategy_id: str,
        as_of: datetime,
        tolerance: Decimal = Decimal("0.000001"),
    ) -> dict[str, Any]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT reward_type,reward_date,condition_id,asset_id,
                       sum(amount) AS amount,count(*) AS samples
                FROM quant.paper_reward_payouts
                WHERE strategy_id=%s AND account_id=%s AND status='RECEIVED'
                  AND effective_ts <= %s
                GROUP BY reward_type,reward_date,condition_id,asset_id
                ORDER BY reward_type,reward_date,condition_id,asset_id
                """,
                (strategy_id, account_address, as_of),
            )
            official_rows = tuple(cur.fetchall())
            cur.execute(
                """
                SELECT reward_type,period_end::date AS reward_date,
                       condition_id,asset_id,sum(amount) AS amount,count(*) AS samples
                FROM quant.paper_reward_accruals
                WHERE strategy_id=%s AND effective_ts <= %s
                  AND source NOT IN ('CLOB_REWARDS_USER','POLYMARKET_DATA_API_ACTIVITY')
                GROUP BY reward_type,period_end::date,condition_id,asset_id
                ORDER BY reward_type,reward_date,condition_id,asset_id
                """,
                (strategy_id, as_of),
            )
            modeled_rows = tuple(cur.fetchall())
            cur.execute(
                """
                SELECT stream_key,watermark_ts,last_success_at
                FROM quant.paper_official_account_sync_checkpoints
                WHERE account_address=%s
                  AND source='POLYMARKET_DATA_API_ACTIVITY'
                """,
                (account_address.lower(),),
            )
            activity_checkpoints = {
                str(row["stream_key"]): dict(row) for row in cur.fetchall()
            }
            cur.execute(
                """
                SELECT reward_date,status,detail_count,total_count,checked_at
                FROM quant.paper_official_reward_source_checks
                WHERE account_address=%s
                  AND source='CLOB_REWARDS_USER_TOTAL'
                  AND checked_at <= %s
                ORDER BY reward_date DESC LIMIT 1
                """,
                (account_address.lower(), as_of),
            )
            earnings_source_row = cur.fetchone()
            cur.execute(
                """
                SELECT reward_type,status,count(*) AS scopes
                FROM quant.paper_reward_aggregate_reconciliations
                WHERE strategy_id=%s AND account_id=%s AND reconciled_at <= %s
                GROUP BY reward_type,status
                """,
                (strategy_id, account_address, as_of),
            )
            aggregate_status_rows = tuple(cur.fetchall())
        official = _group_reward_rows(official_rows)
        modeled = _group_reward_rows(modeled_rows)
        checkpoint_fresh_after = as_of - timedelta(hours=2)
        aggregate_statuses: dict[str, dict[str, int]] = defaultdict(dict)
        for row in aggregate_status_rows:
            aggregate_statuses[str(row["reward_type"])][str(row["status"])] = int(
                row["scopes"]
            )
        by_type: dict[str, Any] = {}
        statuses: list[str] = []
        for reward_type in RewardType:
            key = reward_type.value
            official_amount = sum(
                (value["amount"] for group, value in official.items() if group[0] == key),
                Decimal(0),
            )
            modeled_amount = sum(
                (value["amount"] for group, value in modeled.items() if group[0] == key),
                Decimal(0),
            )
            official_samples = sum(
                (value["samples"] for group, value in official.items() if group[0] == key),
                0,
            )
            modeled_samples = sum(
                (value["samples"] for group, value in modeled.items() if group[0] == key),
                0,
            )
            activity_stream = _activity_stream_for_reward_type(key)
            checkpoint = (
                activity_checkpoints.get(activity_stream) if activity_stream else None
            )
            activity_source_complete = activity_stream is None or bool(
                checkpoint
                and checkpoint["last_success_at"] >= checkpoint_fresh_after
                and checkpoint["watermark_ts"] >= checkpoint_fresh_after
            )
            earnings_source_complete = True
            if reward_type is RewardType.LIQUIDITY_REWARD:
                earnings_source_complete = bool(
                    earnings_source_row
                    and str(earnings_source_row["status"])
                    in {"PASS", "PASS_NO_ACTIVITY"}
                    and earnings_source_row["checked_at"] >= checkpoint_fresh_after
                )
            source_complete = activity_source_complete and earnings_source_complete
            if not source_complete:
                status = "SOURCE_INCOMPLETE"
            elif official_samples == 0 and modeled_samples == 0:
                status = "NO_ELIGIBLE_ACTIVITY"
            elif modeled_samples == 0:
                status = "UNMODELED_OFFICIAL"
            elif official_samples == 0 and _below_official_minimum(
                reward_type, modeled_amount
            ):
                status = "BELOW_MINIMUM_PAYOUT"
            elif official_samples == 0:
                status = "WAITING_FOR_OFFICIAL_EVIDENCE"
            elif abs(official_amount - modeled_amount) <= tolerance:
                status = "PASS"
            else:
                status = "MISMATCH"
            statuses.append(status)
            by_type[key] = {
                "status": status,
                "official_amount": str(official_amount),
                "modeled_amount": str(modeled_amount),
                "amount_delta": str(official_amount - modeled_amount),
                "official_samples": official_samples,
                "modeled_samples": modeled_samples,
                "source_complete": source_complete,
                "activity_checkpoint": _jsonable(checkpoint),
                "aggregate_scope_statuses": aggregate_statuses.get(key, {}),
            }
        overall = _overall_calibration_status(statuses)
        body = {
            "schema_version": "official-reward-calibration-v2",
            "account_address": account_address,
            "strategy_id": strategy_id,
            "as_of": as_of.isoformat(),
            "status": overall,
            "tolerance": str(tolerance),
            "by_reward_type": by_type,
            "official_sample_count": sum(item["samples"] for item in official.values()),
            "modeled_sample_count": sum(item["samples"] for item in modeled.values()),
            "earnings_detail_total_source_check": _jsonable(earnings_source_row),
        }
        report_hash = payload_hash(body)
        report_id = f"official-reward-calibration:{report_hash}"
        body["report_id"] = report_id
        body["report_hash"] = report_hash
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_reward_calibration_reports (
                    report_id,account_address,strategy_id,as_of,status,
                    official_sample_count,modeled_sample_count,report_hash,report
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (account_address,strategy_id,as_of) DO NOTHING
                """,
                (
                    report_id,
                    account_address.lower(),
                    strategy_id,
                    as_of,
                    overall,
                    body["official_sample_count"],
                    body["modeled_sample_count"],
                    report_hash,
                    json.dumps(body, sort_keys=True),
                ),
            )
            conn.commit()
        return body


class OfficialAccountSyncService:
    def __init__(
        self,
        *,
        client: PolymarketOfficialRewardClient,
        connection_factory: Any,
        account_address: str,
        strategy_id: str,
        account_id: str | None = None,
        bridge_addresses: Sequence[str] = (),
        output_dir: Path | None = None,
    ) -> None:
        self.client = client
        self.account_address = account_address.lower()
        self.strategy_id = strategy_id
        self.account_id = account_id or self.account_address
        self.bridge_addresses = tuple(dict.fromkeys(bridge_addresses))
        self.output_dir = output_dir
        self.sync_store = PostgresOfficialAccountSyncStore(connection_factory)
        self.reward_store = PostgresRewardLedgerStore(connection_factory)
        self.cashflow_store = PostgresAccountCashflowStore(connection_factory)
        self.return_store = PostgresAccountReturnStore(connection_factory)
        self.sync_store.recover_interrupted_runs(
            account_address=self.account_address,
            strategy_id=self.strategy_id,
            observed_at=datetime.now(timezone.utc),
        )
        self.sync_store.ensure_account(strategy_id)

    def sync_once(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        activity_types: Sequence[str] = OFFICIAL_ACTIVITY_TYPES,
        include_daily_sources: bool = True,
        include_reward_rules: bool = True,
        strict_sources: bool = False,
    ) -> OfficialSyncResult:
        if any(item.tzinfo is None for item in (window_start, window_end)):
            raise ValueError("official sync window must be timezone-aware")
        if window_end < window_start:
            raise ValueError("official sync end must not precede start")
        started_at = datetime.now(timezone.utc)
        run_id = f"official-account-sync:{uuid.uuid4().hex}"
        self.sync_store.start_run(
            run_id=run_id,
            account_address=self.account_address,
            strategy_id=self.strategy_id,
            started_at=started_at,
            window_start=window_start,
            window_end=window_end,
        )
        errors: list[str] = []
        activity_rows = 0
        activity_inserted = 0
        rewards_ingested = 0
        reconciliations = 0
        cashflows_ingested = 0
        bridge_rows = 0
        try:
            rows = self.client.fetch_user_activities(
                user_address=self.account_address,
                activity_types=activity_types,
                start=int(window_start.timestamp()),
                end=int(window_end.timestamp()),
            )
            activity_rows = len(rows)
            for row in rows:
                _, inserted = self.sync_store.record_activity(
                    row, account_address=self.account_address, observed_at=started_at
                )
                activity_inserted += int(inserted)
                reward = normalize_activity_reward(
                    row,
                    account_id=self.account_id,
                    source_account_address=self.account_address,
                )
                if reward is not None:
                    self.reward_store.ingest_official(
                        reward,
                        strategy_id=self.strategy_id,
                        cash_confirmed=(
                            reward.status is RewardStatus.RECEIVED
                            and bool(reward.source_tx_hash)
                        ),
                        effective_ts=activity_timestamp(row),
                    )
                    rewards_ingested += 1
                cashflow = normalize_activity_cashflow(
                    row,
                    account_id=self.account_id,
                    strategy_id=self.strategy_id,
                    source_account_address=self.account_address,
                )
                if cashflow is not None:
                    self.cashflow_store.record(cashflow)
                    cashflows_ingested += 1
            for activity_type in activity_types:
                self.sync_store.checkpoint(
                    account_address=self.account_address,
                    source="POLYMARKET_DATA_API_ACTIVITY",
                    stream_key=str(activity_type),
                    watermark_ts=window_end,
                )
        except Exception as exc:
            errors.append(f"activity:{exc.__class__.__name__}:{str(exc)[:300]}")

        if include_daily_sources:
            self._sync_daily_sources(window_start.date(), window_end.date(), errors)
            if include_reward_rules:
                self._sync_rules(errors)
        for bridge_address in self.bridge_addresses:
            try:
                bridge = self.client.fetch_bridge_transactions(
                    bridge_address=bridge_address
                )
                for row in bridge:
                    self.sync_store.record_bridge_transaction(
                        row, bridge_address=bridge_address, observed_at=started_at
                    )
                    bridge_rows += 1
                self.sync_store.checkpoint(
                    account_address=self.account_address,
                    source="POLYMARKET_BRIDGE_API",
                    stream_key=bridge_address,
                    watermark_ts=window_end,
                )
            except Exception as exc:
                errors.append(
                    f"bridge:{bridge_address}:{exc.__class__.__name__}:{str(exc)[:240]}"
                )

        report_id: str | None = None
        calibration_id: str | None = None
        try:
            self.cashflow_store.apply_due(as_of=window_end)
            reconciliations = self.sync_store.reconcile_model_to_official(
                strategy_id=self.strategy_id,
                account_id=self.account_id,
            )
            reconciliations += self.sync_store.reconcile_calibration_redemptions(
                account_address=self.account_address,
            )
            report_as_of = window_end.astimezone(timezone.utc)
            account_report = self.return_store.build(
                strategy_id=self.strategy_id,
                account_id=self.account_id,
                as_of=report_as_of,
            )
            self.return_store.persist(account_report)
            report_id = account_report.report_id
            self._write_report(
                "account-return", report_as_of.date(), asdict(account_report)
            )
            # Source checks are observed during this run, after window_end was
            # captured. Use report-generation time so the just-persisted
            # completeness evidence participates in the current calibration.
            calibration_as_of = datetime.now(timezone.utc)
            calibration = self.sync_store.calibration_report(
                account_address=self.account_id,
                strategy_id=self.strategy_id,
                as_of=calibration_as_of,
            )
            calibration_id = str(calibration["report_id"])
            self._write_report(
                "reward-calibration", calibration_as_of.date(), calibration
            )
        except Exception as exc:
            errors.append(f"report:{exc.__class__.__name__}:{str(exc)[:300]}")

        status = "PASS" if not errors else "FAIL" if strict_sources else "DEGRADED"
        result = OfficialSyncResult(
            run_id=run_id,
            status=status,
            window_start=window_start,
            window_end=window_end,
            activity_rows=activity_rows,
            activity_inserted=activity_inserted,
            rewards_ingested=rewards_ingested,
            reconciliations=reconciliations,
            cashflows_ingested=cashflows_ingested,
            bridge_rows=bridge_rows,
            account_return_report_id=report_id,
            calibration_report_id=calibration_id,
            errors=tuple(errors),
        )
        self.sync_store.finish_run(result)
        if strict_sources and errors:
            raise RuntimeError("official account sync failed: " + " | ".join(errors))
        return result

    def _sync_daily_sources(
        self, start_date: date, end_date: date, errors: list[str]
    ) -> None:
        current = start_date
        while current <= end_date:
            try:
                rows = self.client.fetch_maker_rebates(
                    reward_date=current, maker_address=self.account_address
                )
                for record in normalize_maker_rebates(rows, account_id=self.account_id):
                    self.reward_store.ingest_official(
                        record, strategy_id=self.strategy_id, cash_confirmed=False
                    )
            except Exception as exc:
                errors.append(
                    f"maker_rebate:{current}:{exc.__class__.__name__}:{str(exc)[:180]}"
                )
            if self.client.sdk_client is not None:
                try:
                    rows = self.client.fetch_user_earnings(current)
                    total_rows = self.client.fetch_user_total_earnings(current)
                    completeness = reconcile_user_earnings_totals(
                        rows,
                        total_rows,
                        reward_date=current,
                    )
                    self.sync_store.record_earnings_completeness(
                        completeness,
                        account_address=self.account_address,
                        checked_at=datetime.now(timezone.utc),
                    )
                    if not completeness.complete:
                        errors.append(
                            f"user_earnings_total:{current}:{completeness.status}:"
                            f"{_json(completeness.delta_by_asset)[:180]}"
                        )
                    for record in normalize_user_earnings(
                        rows, account_id=self.account_id
                    ):
                        self.reward_store.ingest_official(
                            record, strategy_id=self.strategy_id, cash_confirmed=False
                        )
                except Exception as exc:
                    errors.append(
                        f"user_earnings:{current}:{exc.__class__.__name__}:{str(exc)[:180]}"
                    )
            current += timedelta(days=1)

    def _sync_rules(self, errors: list[str]) -> None:
        if self.client.sdk_client is None:
            return
        observed_at = datetime.now(timezone.utc)
        if self.sync_store.reward_rules_are_fresh(as_of=observed_at):
            return
        try:
            rows = (
                *bundled_program_rule_rows(),
                *self.client.fetch_reward_schedules(),
            )
            self.sync_store.record_rules(rows, observed_at=observed_at)
            self.sync_store.checkpoint(
                account_address=self.sync_store.rule_checkpoint_account(),
                source="CLOB_REWARD_CONFIG",
                stream_key="current",
                watermark_ts=observed_at,
            )
        except Exception as exc:
            errors.append(f"reward_rules:{exc.__class__.__name__}:{str(exc)[:240]}")

    def _write_report(self, prefix: str, day: date, payload: Mapping[str, Any]) -> None:
        if self.output_dir is None:
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{prefix}-{day.isoformat()}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(_jsonable(payload), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


def normalize_activity_reward(
    row: Mapping[str, Any], *, account_id: str, source_account_address: str | None = None
) -> OfficialRewardRecord | None:
    activity_type = str(row.get("type") or "").upper()
    reward_type = ACTIVITY_REWARD_TYPES.get(activity_type)
    if reward_type is None:
        return None
    timestamp = activity_timestamp(row)
    amount = _decimal_or_none(row.get("usdcSize"))
    if amount is None:
        amount = _decimal_or_none(row.get("size"))
    if amount is None or amount < 0:
        raise ValueError(f"official {activity_type} activity has invalid amount")
    tx_hash = _text(row.get("transactionHash"))
    return OfficialRewardRecord(
        source="POLYMARKET_DATA_API_ACTIVITY",
        source_event_id=activity_source_event_id(
            row, source_account_address or account_id
        ),
        reward_type=reward_type,
        account_id=account_id,
        amount=amount,
        currency="USDC",
        reward_date=timestamp.date(),
        status=RewardStatus.RECEIVED if tx_hash else RewardStatus.ACCRUED,
        condition_id=_text(row.get("conditionId")),
        asset_id=_text(row.get("asset")),
        source_tx_hash=tx_hash,
        raw_payload_hash=payload_hash(row),
        rule_version="polymarket-data-api-activity-v1",
        metadata={
            "activity_type": activity_type,
            "rule_version": "polymarket-data-api-activity-v1",
            "timestamp": timestamp.isoformat(),
        },
    )


def normalize_activity_cashflow(
    row: Mapping[str, Any], *, account_id: str, strategy_id: str,
    source_account_address: str | None = None,
) -> AccountCashflowOperation | None:
    activity_type = str(row.get("type") or "").upper()
    operation_type = ACTIVITY_CASHFLOW_TYPES.get(activity_type)
    if operation_type is None:
        return None
    amount = _decimal_or_none(row.get("usdcSize"))
    if amount is None:
        amount = _decimal_or_none(row.get("size"))
    if amount is None or amount <= 0:
        raise ValueError(f"official {activity_type} activity has invalid amount")
    timestamp = activity_timestamp(row)
    source_event_id = activity_source_event_id(
        row, source_account_address or account_id
    )
    tx_hash = _text(row.get("transactionHash"))
    state = (
        AccountCashflowState.CONFIRMED if tx_hash else AccountCashflowState.PROCESSING
    )
    operation_id = account_cashflow_operation_id(
        "POLYMARKET_DATA_API_ACTIVITY", source_event_id, operation_type
    )
    return AccountCashflowOperation(
        operation_id=operation_id,
        account_id=account_id,
        strategy_id=strategy_id,
        operation_type=operation_type,
        amount=amount,
        currency="USDC",
        state=state,
        effective_ts=timestamp,
        source="POLYMARKET_DATA_API_ACTIVITY",
        source_event_id=source_event_id,
        source_tx_hash=tx_hash,
        raw_payload_hash=payload_hash(row),
        idempotency_key=f"official-account-cashflow:{source_event_id}",
        condition_id=_text(row.get("conditionId")),
        asset_id=_text(row.get("asset")),
        rule_version="polymarket-data-api-activity-v1",
        metadata={"activity_type": activity_type},
    )


def activity_source_event_id(row: Mapping[str, Any], account_address: str) -> str:
    return deterministic_reward_id(
        "data-api-activity",
        {
            "account": account_address.lower(),
            "asset": row.get("asset"),
            "conditionId": row.get("conditionId"),
            "outcomeIndex": row.get("outcomeIndex"),
            "side": row.get("side"),
            "size": row.get("size"),
            "timestamp": row.get("timestamp"),
            "transactionHash": row.get("transactionHash"),
            "type": row.get("type"),
            "usdcSize": row.get("usdcSize"),
        },
    )


def activity_timestamp(row: Mapping[str, Any]) -> datetime:
    value = row.get("timestamp")
    if value is None:
        raise ValueError("official activity timestamp is missing")
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _group_reward_rows(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[Any, ...], dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = defaultdict(
        lambda: {"amount": Decimal(0), "samples": 0}
    )
    for row in rows:
        key = (
            str(row["reward_type"]),
            str(row["reward_date"]),
            str(row["condition_id"] or ""),
            str(row["asset_id"] or ""),
        )
        grouped[key]["amount"] += Decimal(row["amount"])
        grouped[key]["samples"] += int(row["samples"])
    return dict(grouped)


def _activity_stream_for_reward_type(reward_type: str) -> str | None:
    return {
        RewardType.MAKER_REBATE.value: "MAKER_REBATE",
        RewardType.TAKER_REBATE.value: "TAKER_REBATE",
        RewardType.LIQUIDITY_REWARD.value: "REWARD",
        RewardType.HOLDING_REWARD.value: "YIELD",
        RewardType.REFERRAL_REWARD.value: "REFERRAL_REWARD",
    }.get(reward_type)


def _below_official_minimum(
    reward_type: RewardType, modeled_amount: Decimal
) -> bool:
    return reward_type in {
        RewardType.MAKER_REBATE,
        RewardType.TAKER_REBATE,
        RewardType.LIQUIDITY_REWARD,
    } and modeled_amount < Decimal(1)


def _overall_calibration_status(statuses: Sequence[str]) -> str:
    priority = (
        "MISMATCH",
        "SOURCE_INCOMPLETE",
        "UNMODELED_OFFICIAL",
        "WAITING_FOR_OFFICIAL_EVIDENCE",
        "BELOW_MINIMUM_PAYOUT",
        "PASS",
        "NO_ELIGIBLE_ACTIVITY",
    )
    return next((status for status in priority if status in statuses), "NO_ELIGIBLE_ACTIVITY")


def _rule_date(row: Mapping[str, Any], fallback: date) -> date:
    for key in ("date", "effective_date", "start_date", "startDate"):
        value = _text(row.get(key))
        if value:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                continue
    return fallback


def _stable_id(prefix: str, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(value), sort_keys=True, default=str).encode()
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    return Decimal(str(value))


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, default=str)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
