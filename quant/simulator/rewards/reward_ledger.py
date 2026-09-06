"""Durable reward ledger with append-only cash and clawback semantics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from .models import (
    OfficialRewardRecord,
    RewardAccrual,
    RewardClawback,
    RewardPayout,
    RewardReconciliation,
    RewardSchedule,
    RewardStatus,
    deterministic_reward_id,
    payload_hash,
)

REWARD_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_reward_schedules (
        schedule_id TEXT PRIMARY KEY,
        reward_type TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        condition_id TEXT,
        asset_id TEXT,
        category TEXT,
        currency TEXT NOT NULL,
        effective_from TIMESTAMPTZ NOT NULL,
        effective_until TIMESTAMPTZ,
        source TEXT NOT NULL,
        source_event_id TEXT,
        source_payload_hash TEXT NOT NULL,
        economics_regime_id TEXT NOT NULL,
        parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        CHECK (effective_until IS NULL OR effective_until > effective_from)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS paper_reward_schedules_source_event_idx
    ON quant.paper_reward_schedules (source,source_event_id)
    WHERE source_event_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_reward_schedules_scope_effective_idx
    ON quant.paper_reward_schedules (reward_type,scope_key,effective_from DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_reward_accruals (
        accrual_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        reward_type TEXT NOT NULL,
        schedule_id TEXT,
        condition_id TEXT,
        asset_id TEXT,
        period_start TIMESTAMPTZ NOT NULL,
        period_end TIMESTAMPTZ NOT NULL,
        amount NUMERIC NOT NULL CHECK (amount >= 0),
        currency TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ESTIMATED','ACCRUED','PAYABLE')),
        effective_ts TIMESTAMPTZ NOT NULL,
        source TEXT NOT NULL,
        source_event_id TEXT,
        model_version TEXT NOT NULL,
        economics_regime_id TEXT,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        CHECK (period_end > period_start)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS paper_reward_accruals_source_event_idx
    ON quant.paper_reward_accruals (source,source_event_id)
    WHERE source_event_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_reward_accruals_strategy_period_idx
    ON quant.paper_reward_accruals (strategy_id,reward_type,period_end)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_reward_payouts (
        payout_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        reward_type TEXT NOT NULL,
        schedule_id TEXT,
        condition_id TEXT,
        asset_id TEXT,
        reward_date DATE NOT NULL,
        amount NUMERIC NOT NULL CHECK (amount >= 0),
        currency TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('PAYABLE','RECEIVED','FAILED','VOIDED')),
        effective_ts TIMESTAMPTZ NOT NULL,
        received_at TIMESTAMPTZ,
        source TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        source_tx_hash TEXT,
        source_payload_hash TEXT,
        economics_regime_id TEXT,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (source,source_event_id)
    )
    """,
    """
    ALTER TABLE quant.paper_reward_payouts
    ADD COLUMN IF NOT EXISTS rule_version TEXT NOT NULL DEFAULT 'reward-ledger-v1'
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_reward_payouts_strategy_date_idx
    ON quant.paper_reward_payouts (strategy_id,reward_type,reward_date)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_reward_reconciliations (
        reconciliation_id TEXT PRIMARY KEY,
        accrual_id TEXT NOT NULL,
        payout_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('PASS','MISMATCH')),
        modeled_amount NUMERIC NOT NULL,
        official_amount NUMERIC NOT NULL,
        allocated_amount NUMERIC NOT NULL,
        amount_delta NUMERIC NOT NULL,
        tolerance NUMERIC NOT NULL,
        reconciled_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (accrual_id,payout_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_reward_clawbacks (
        clawback_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        original_payout_id TEXT NOT NULL,
        reward_type TEXT NOT NULL,
        amount NUMERIC NOT NULL CHECK (amount > 0),
        currency TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('PAYABLE','RECEIVED')),
        effective_ts TIMESTAMPTZ NOT NULL,
        reason TEXT NOT NULL,
        source TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        source_tx_hash TEXT,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (source,source_event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_account_economic_events (
        event_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        condition_id TEXT,
        asset_id TEXT,
        event_type TEXT NOT NULL,
        amount NUMERIC NOT NULL,
        currency TEXT NOT NULL,
        quantity NUMERIC,
        status TEXT NOT NULL,
        effective_ts TIMESTAMPTZ NOT NULL,
        confirmed_ts TIMESTAMPTZ,
        source TEXT NOT NULL,
        source_event_id TEXT,
        source_tx_hash TEXT,
        economics_regime_id TEXT,
        model_version TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_account_economic_events_replay_idx
    ON quant.paper_account_economic_events (strategy_id,effective_ts,event_id)
    """,
)


class PostgresRewardLedgerStore:
    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS quant")
            for statement in REWARD_SCHEMA_STATEMENTS:
                cur.execute(statement)
            conn.commit()

    def register_schedule(self, schedule: RewardSchedule) -> None:
        schedule_payload = {
            "asset_id": schedule.asset_id,
            "category": schedule.category,
            "condition_id": schedule.condition_id,
            "currency": schedule.currency,
            "effective_from": schedule.effective_from.isoformat(),
            "effective_until": (
                schedule.effective_until.isoformat()
                if schedule.effective_until is not None
                else None
            ),
            "parameters": dict(schedule.parameters),
            "reward_type": schedule.reward_type.value,
            "scope_key": schedule.scope_key,
            "source": schedule.source,
            "source_event_id": schedule.source_event_id,
        }
        source_hash = payload_hash(schedule_payload)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_reward_schedules (
                    schedule_id,reward_type,scope_key,condition_id,asset_id,
                    category,currency,effective_from,effective_until,source,
                    source_event_id,source_payload_hash,economics_regime_id,parameters
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (schedule_id) DO NOTHING
                """,
                (
                    schedule.schedule_id,
                    schedule.reward_type.value,
                    schedule.scope_key,
                    schedule.condition_id,
                    schedule.asset_id,
                    schedule.category,
                    schedule.currency,
                    schedule.effective_from,
                    schedule.effective_until,
                    schedule.source,
                    schedule.source_event_id,
                    source_hash,
                    schedule.regime_id,
                    _json(schedule.parameters),
                ),
            )
            cur.execute(
                "SELECT source_payload_hash FROM quant.paper_reward_schedules "
                "WHERE schedule_id=%s",
                (schedule.schedule_id,),
            )
            row = cur.fetchone()
            if row is None or str(row["source_payload_hash"]) != source_hash:
                raise ValueError("reward schedule id collision")
            conn.commit()

    def record_accrual(self, accrual: RewardAccrual) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._require_account(cur, accrual.strategy_id)
            cur.execute(
                """
                INSERT INTO quant.paper_reward_accruals (
                    accrual_id,strategy_id,account_id,reward_type,schedule_id,
                    condition_id,asset_id,period_start,period_end,amount,currency,
                    status,effective_ts,source,source_event_id,model_version,
                    economics_regime_id,idempotency_key,metadata
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                ) ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING accrual_id
                """,
                (
                    accrual.accrual_id,
                    accrual.strategy_id,
                    accrual.account_id,
                    accrual.reward_type.value,
                    accrual.schedule_id,
                    accrual.condition_id,
                    accrual.asset_id,
                    accrual.period_start,
                    accrual.period_end,
                    accrual.amount,
                    accrual.currency,
                    accrual.status.value,
                    accrual.effective_ts,
                    accrual.source,
                    accrual.source_event_id,
                    accrual.model_version,
                    accrual.economics_regime_id,
                    accrual.idempotency_key,
                    _json(accrual.metadata),
                ),
            )
            inserted = cur.fetchone() is not None
            if inserted:
                self._insert_economic_event(
                    cur,
                    event_id=deterministic_reward_id(
                        "economic-event", {"accrual_id": accrual.accrual_id}
                    ),
                    account_id=accrual.account_id,
                    strategy_id=accrual.strategy_id,
                    condition_id=accrual.condition_id,
                    asset_id=accrual.asset_id,
                    event_type=f"{accrual.reward_type.value}_ESTIMATED",
                    amount=accrual.amount,
                    currency=accrual.currency,
                    status=accrual.status,
                    effective_ts=accrual.effective_ts,
                    source=accrual.source,
                    source_event_id=accrual.source_event_id,
                    source_tx_hash=None,
                    economics_regime_id=accrual.economics_regime_id,
                    model_version=accrual.model_version,
                    idempotency_key=f"reward-accrual-event:{accrual.idempotency_key}",
                    metadata=accrual.metadata,
                )
            else:
                self._assert_accrual(cur, accrual)
            conn.commit()
        return inserted

    def ingest_official(
        self,
        record: OfficialRewardRecord,
        *,
        strategy_id: str,
        cash_confirmed: bool = False,
        effective_ts: datetime | None = None,
    ) -> RewardAccrual | RewardPayout:
        timestamp = effective_ts or datetime.combine(
            record.reward_date, time.min, tzinfo=timezone.utc
        )
        if record.status is RewardStatus.ACCRUED and not cash_confirmed:
            end = timestamp + timedelta(days=1)
            accrual_id = deterministic_reward_id(
                "official-reward-accrual",
                {"source": record.source, "source_event_id": record.source_event_id},
            )
            accrual = RewardAccrual(
                accrual_id=accrual_id,
                strategy_id=strategy_id,
                account_id=record.account_id,
                reward_type=record.reward_type,
                status=RewardStatus.ACCRUED,
                amount=record.amount,
                currency=record.currency,
                period_start=timestamp,
                period_end=end,
                effective_ts=end,
                idempotency_key=f"official-accrual:{record.source}:{record.source_event_id}",
                condition_id=record.condition_id,
                asset_id=record.asset_id,
                source=record.source,
                source_event_id=record.source_event_id,
                model_version="official-reward-ingest-v1",
                metadata={
                    **dict(record.metadata),
                    "source_payload_hash": record.raw_payload_hash,
                },
            )
            self.record_accrual(accrual)
            return accrual

        status = RewardStatus.PAYABLE
        payout_id = deterministic_reward_id(
            "official-reward-payout",
            {"source": record.source, "source_event_id": record.source_event_id},
        )
        payout = RewardPayout(
            payout_id=payout_id,
            strategy_id=strategy_id,
            account_id=record.account_id,
            reward_type=record.reward_type,
            status=status,
            amount=record.amount,
            currency=record.currency,
            reward_date=record.reward_date,
            effective_ts=timestamp,
            idempotency_key=f"official-payout:{record.source}:{record.source_event_id}",
            source=record.source,
            source_event_id=record.source_event_id,
            condition_id=record.condition_id,
            asset_id=record.asset_id,
            source_tx_hash=record.source_tx_hash,
            raw_payload_hash=record.raw_payload_hash,
            rule_version=record.rule_version,
            metadata=record.metadata,
        )
        self.record_payout(payout)
        if not cash_confirmed:
            return payout
        self.confirm_payout_received(
            payout.payout_id,
            confirmed_at=timestamp,
            source_tx_hash=record.source_tx_hash,
        )
        return replace(payout, status=RewardStatus.RECEIVED)

    def record_payout(self, payout: RewardPayout) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._require_account(cur, payout.strategy_id)
            cur.execute(
                """
                INSERT INTO quant.paper_reward_payouts (
                    payout_id,strategy_id,account_id,reward_type,schedule_id,
                    condition_id,asset_id,reward_date,amount,currency,status,
                    effective_ts,received_at,source,source_event_id,source_tx_hash,
                    source_payload_hash,economics_regime_id,rule_version,
                    idempotency_key,metadata
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                ) ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING payout_id
                """,
                (
                    payout.payout_id,
                    payout.strategy_id,
                    payout.account_id,
                    payout.reward_type.value,
                    payout.schedule_id,
                    payout.condition_id,
                    payout.asset_id,
                    payout.reward_date,
                    payout.amount,
                    payout.currency,
                    payout.status.value,
                    payout.effective_ts,
                    payout.effective_ts
                    if payout.status is RewardStatus.RECEIVED
                    else None,
                    payout.source,
                    payout.source_event_id,
                    payout.source_tx_hash,
                    payout.raw_payload_hash,
                    payout.economics_regime_id,
                    payout.rule_version,
                    payout.idempotency_key,
                    _json(payout.metadata),
                ),
            )
            inserted = cur.fetchone() is not None
            if not inserted:
                self._assert_payout(cur, payout)
                cur.execute(
                    """
                    UPDATE quant.paper_reward_payouts
                    SET rule_version=%s
                    WHERE idempotency_key=%s AND rule_version='reward-ledger-v1'
                    """,
                    (payout.rule_version, payout.idempotency_key),
                )
                conn.commit()
                return False
            self._insert_economic_event(
                cur,
                event_id=deterministic_reward_id(
                    "economic-event", {"payout_id": payout.payout_id}
                ),
                account_id=payout.account_id,
                strategy_id=payout.strategy_id,
                condition_id=payout.condition_id,
                asset_id=payout.asset_id,
                event_type=(
                    f"{payout.reward_type.value}_RECEIVED"
                    if payout.status is RewardStatus.RECEIVED
                    else f"{payout.reward_type.value}_ESTIMATED"
                ),
                amount=payout.amount,
                currency=payout.currency,
                status=payout.status,
                effective_ts=payout.effective_ts,
                source=payout.source,
                source_event_id=payout.source_event_id,
                source_tx_hash=payout.source_tx_hash,
                economics_regime_id=payout.economics_regime_id,
                model_version="official-reward-ingest-v1",
                idempotency_key=f"reward-payout-event:{payout.idempotency_key}",
                metadata=payout.metadata,
            )
            if payout.status is RewardStatus.RECEIVED:
                cur.execute(
                    """
                    UPDATE quant.paper_accounts
                    SET cash_balance=cash_balance + %s, updated_at=clock_timestamp()
                    WHERE strategy_id=%s
                    """,
                    (payout.amount, payout.strategy_id),
                )
            conn.commit()
        return True

    def record_reconciliation(self, item: RewardReconciliation) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.paper_reward_reconciliations (
                    reconciliation_id,accrual_id,payout_id,status,modeled_amount,
                    official_amount,allocated_amount,amount_delta,tolerance,reconciled_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (accrual_id,payout_id) DO NOTHING
                RETURNING reconciliation_id
                """,
                (
                    item.reconciliation_id,
                    item.accrual_id,
                    item.payout_id,
                    item.status,
                    item.modeled_amount,
                    item.official_amount,
                    item.allocated_amount,
                    item.amount_delta,
                    item.tolerance,
                    item.reconciled_at,
                ),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
        return inserted

    def confirm_payout_received(
        self,
        payout_id: str,
        *,
        confirmed_at: datetime,
        source_tx_hash: str | None = None,
    ) -> bool:
        if confirmed_at.tzinfo is None:
            raise ValueError("reward payout confirmation must be timezone-aware")
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.paper_reward_payouts WHERE payout_id=%s FOR UPDATE",
                (payout_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise LookupError("reward payout not found")
            if str(row["status"]) == RewardStatus.RECEIVED.value:
                existing_hash = str(row["source_tx_hash"] or "") or None
                if source_tx_hash and existing_hash and source_tx_hash != existing_hash:
                    raise ValueError("reward payout confirmation hash collision")
                conn.commit()
                return False
            if str(row["status"]) != RewardStatus.PAYABLE.value:
                raise ValueError("only PAYABLE rewards can become RECEIVED")
            cur.execute(
                """
                UPDATE quant.paper_reward_payouts
                SET status='RECEIVED', received_at=%s,
                    source_tx_hash=COALESCE(%s,source_tx_hash)
                WHERE payout_id=%s
                """,
                (confirmed_at, source_tx_hash, payout_id),
            )
            self._insert_economic_event(
                cur,
                event_id=deterministic_reward_id(
                    "economic-event", {"payout_received": payout_id}
                ),
                account_id=str(row["account_id"]),
                strategy_id=str(row["strategy_id"]),
                condition_id=str(row["condition_id"] or "") or None,
                asset_id=str(row["asset_id"] or "") or None,
                event_type=f"{row['reward_type']!s}_RECEIVED",
                amount=Decimal(row["amount"]),
                currency=str(row["currency"]),
                status=RewardStatus.RECEIVED,
                effective_ts=confirmed_at,
                source=str(row["source"]),
                source_event_id=str(row["source_event_id"]),
                source_tx_hash=source_tx_hash
                or (str(row["source_tx_hash"] or "") or None),
                economics_regime_id=str(row["economics_regime_id"] or "") or None,
                model_version="official-reward-ingest-v1",
                idempotency_key=f"reward-payout-received:{payout_id}",
                metadata={"payout_id": payout_id},
            )
            cur.execute(
                """
                UPDATE quant.paper_accounts
                SET cash_balance=cash_balance + %s, updated_at=clock_timestamp()
                WHERE strategy_id=%s
                """,
                (Decimal(row["amount"]), str(row["strategy_id"])),
            )
            conn.commit()
        return True

    def record_clawback(self, clawback: RewardClawback) -> bool:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._require_account(cur, clawback.strategy_id)
            cur.execute(
                "SELECT * FROM quant.paper_reward_clawbacks WHERE idempotency_key=%s",
                (clawback.idempotency_key,),
            )
            existing = cur.fetchone()
            if existing is not None:
                if (
                    str(existing["clawback_id"]) != clawback.clawback_id
                    or Decimal(existing["amount"]) != clawback.amount
                ):
                    raise ValueError("reward clawback idempotency collision")
                conn.commit()
                return False
            cur.execute(
                "SELECT * FROM quant.paper_reward_payouts WHERE payout_id=%s FOR UPDATE",
                (clawback.original_payout_id,),
            )
            original = cur.fetchone()
            if original is None:
                raise LookupError("original reward payout not found")
            if str(original["strategy_id"]) != clawback.strategy_id:
                raise ValueError("reward clawback strategy mismatch")
            cur.execute(
                """
                SELECT COALESCE(sum(amount),0) AS amount
                FROM quant.paper_reward_clawbacks
                WHERE original_payout_id=%s AND status IN ('PAYABLE','RECEIVED')
                """,
                (clawback.original_payout_id,),
            )
            already = Decimal(cur.fetchone()["amount"])
            if already + clawback.amount > Decimal(original["amount"]):
                raise ValueError("reward clawback exceeds original payout")
            cur.execute(
                """
                INSERT INTO quant.paper_reward_clawbacks (
                    clawback_id,strategy_id,account_id,original_payout_id,
                    reward_type,amount,currency,status,effective_ts,reason,source,
                    source_event_id,source_tx_hash,idempotency_key,metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    clawback.clawback_id,
                    clawback.strategy_id,
                    clawback.account_id,
                    clawback.original_payout_id,
                    clawback.reward_type.value,
                    clawback.amount,
                    clawback.currency,
                    clawback.status.value,
                    clawback.effective_ts,
                    clawback.reason,
                    clawback.source,
                    clawback.source_event_id,
                    clawback.source_tx_hash,
                    clawback.idempotency_key,
                    _json(clawback.metadata),
                ),
            )
            self._insert_economic_event(
                cur,
                event_id=deterministic_reward_id(
                    "economic-event", {"clawback_id": clawback.clawback_id}
                ),
                account_id=clawback.account_id,
                strategy_id=clawback.strategy_id,
                condition_id=str(original["condition_id"] or "") or None,
                asset_id=str(original["asset_id"] or "") or None,
                event_type="REWARD_CLAWBACK",
                amount=-clawback.amount,
                currency=clawback.currency,
                status=(
                    RewardStatus.CLAWED_BACK
                    if clawback.status is RewardStatus.RECEIVED
                    else RewardStatus.PAYABLE
                ),
                effective_ts=clawback.effective_ts,
                source=clawback.source,
                source_event_id=clawback.source_event_id,
                source_tx_hash=clawback.source_tx_hash,
                economics_regime_id=str(original["economics_regime_id"] or "") or None,
                model_version="reward-clawback-v1",
                idempotency_key=f"reward-clawback-event:{clawback.idempotency_key}",
                metadata={
                    **dict(clawback.metadata),
                    "original_payout_id": clawback.original_payout_id,
                    "reason": clawback.reason,
                },
            )
            if clawback.status is RewardStatus.RECEIVED:
                cur.execute(
                    """
                    UPDATE quant.paper_accounts
                    SET cash_balance=cash_balance - %s, updated_at=clock_timestamp()
                    WHERE strategy_id=%s
                    """,
                    (clawback.amount, clawback.strategy_id),
                )
            conn.commit()
        return True

    def replay_hash(self, strategy_id: str) -> str:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_id,event_type,amount,currency,status,effective_ts,
                       source,source_event_id,economics_regime_id,idempotency_key
                FROM quant.paper_account_economic_events
                WHERE strategy_id=%s
                ORDER BY effective_ts,event_id
                """,
                (strategy_id,),
            )
            rows = [
                {
                    key: str(value) if value is not None else None
                    for key, value in row.items()
                }
                for row in cur.fetchall()
            ]
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _require_account(self, cur: Any, strategy_id: str) -> None:
        cur.execute(
            "SELECT strategy_id FROM quant.paper_accounts WHERE strategy_id=%s FOR UPDATE",
            (strategy_id,),
        )
        if cur.fetchone() is None:
            raise LookupError(f"paper account not found: {strategy_id}")

    def _assert_accrual(self, cur: Any, accrual: RewardAccrual) -> None:
        cur.execute(
            "SELECT * FROM quant.paper_reward_accruals WHERE idempotency_key=%s",
            (accrual.idempotency_key,),
        )
        row = cur.fetchone()
        if row is None or (
            str(row["accrual_id"]) != accrual.accrual_id
            or Decimal(row["amount"]) != accrual.amount
            or str(row["status"]) != accrual.status.value
        ):
            raise ValueError("reward accrual idempotency collision")

    def _assert_payout(self, cur: Any, payout: RewardPayout) -> None:
        cur.execute(
            "SELECT * FROM quant.paper_reward_payouts WHERE idempotency_key=%s",
            (payout.idempotency_key,),
        )
        row = cur.fetchone()
        existing_status = str(row["status"]) if row is not None else ""
        compatible_status = existing_status == payout.status.value or (
            payout.status is RewardStatus.PAYABLE
            and existing_status == RewardStatus.RECEIVED.value
        )
        if row is None or (
            str(row["payout_id"]) != payout.payout_id
            or Decimal(row["amount"]) != payout.amount
            or not compatible_status
        ):
            raise ValueError("reward payout idempotency collision")

    @staticmethod
    def _insert_economic_event(
        cur: Any,
        *,
        event_id: str,
        account_id: str,
        strategy_id: str,
        condition_id: str | None,
        asset_id: str | None,
        event_type: str,
        amount: Decimal,
        currency: str,
        status: RewardStatus,
        effective_ts: datetime,
        source: str,
        source_event_id: str | None,
        source_tx_hash: str | None,
        economics_regime_id: str | None,
        model_version: str,
        idempotency_key: str,
        metadata: Mapping[str, Any],
    ) -> None:
        cur.execute(
            """
            INSERT INTO quant.paper_account_economic_events (
                event_id,account_id,strategy_id,condition_id,asset_id,event_type,
                amount,currency,status,effective_ts,confirmed_ts,source,
                source_event_id,source_tx_hash,economics_regime_id,model_version,
                idempotency_key,metadata
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
            ) ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                event_id,
                account_id,
                strategy_id,
                condition_id,
                asset_id,
                event_type,
                amount,
                currency,
                status.value,
                effective_ts,
                effective_ts
                if status in {RewardStatus.RECEIVED, RewardStatus.CLAWED_BACK}
                else None,
                source,
                source_event_id,
                source_tx_hash,
                economics_regime_id,
                model_version,
                idempotency_key,
                _json(metadata),
            ),
        )


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, default=str)
