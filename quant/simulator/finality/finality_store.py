"""PostgreSQL truth for fragment-level fill finality and compensation."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from decimal import Decimal
from typing import Any

from quant.core.db import postgres_connection

from .fill_void import void_entry
from .finality_model import (
    FillFinalityState,
    FillFragment,
    FinalityJournalEntry,
    FinalityReconciliationCandidate,
    FinalityTrade,
)
from .ledger_compensation import compensation_entries
from .position_rebuild import FinalityNav, rebuild_nav

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_fill_finality_trades (
        trade_id TEXT PRIMARY KEY, audit_key TEXT, fill_index INTEGER,
        account_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
        asset_id TEXT NOT NULL, side TEXT NOT NULL,
        size NUMERIC NOT NULL, price NUMERIC NOT NULL, fee NUMERIC NOT NULL,
        matched_at TIMESTAMPTZ NOT NULL, state TEXT NOT NULL,
        finality_ts TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (audit_key, fill_index)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS simulator_fill_finality_state_idx
        ON quant.simulator_fill_finality_trades (state, matched_at, trade_id)
    """,
    """
    ALTER TABLE quant.simulator_fill_finality_trades
        ADD COLUMN IF NOT EXISTS paper_intent_id BIGINT
    """,
    """
    CREATE INDEX IF NOT EXISTS simulator_fill_finality_intent_idx
        ON quant.simulator_fill_finality_trades (paper_intent_id, state)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_fill_finality_events (
        event_id TEXT PRIMARY KEY, trade_id TEXT NOT NULL,
        event_type TEXT NOT NULL, state TEXT NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        cash_delta NUMERIC NOT NULL DEFAULT 0,
        shares_delta NUMERIC NOT NULL DEFAULT 0,
        fee_delta NUMERIC NOT NULL DEFAULT 0,
        reason TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS simulator_fill_finality_events_trade_idx
        ON quant.simulator_fill_finality_events (trade_id, event_ts, event_id)
    """,
)


class PostgresFillFinalityStore:
    """Append-only finality state shared across reconciliation workers."""

    def __init__(
        self,
        connection_factory: Callable[..., AbstractContextManager[Any]] = (
            postgres_connection
        ),
    ) -> None:
        self.connection_factory = connection_factory

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)

    def record_match(
        self,
        fragment: FillFragment,
        *,
        event_id: str,
        audit_key: str | None = None,
        fill_index: int | None = None,
        paper_intent_id: int | None = None,
    ) -> FinalityTrade:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._lock(cur, fragment.trade_id)
            current = self._trade(cur, fragment.trade_id, lock=True)
            if current is not None:
                if current.fragment != fragment:
                    raise ValueError("trade id collision with different fragment")
                if paper_intent_id is not None:
                    cur.execute(
                        """
                        UPDATE quant.simulator_fill_finality_trades
                        SET paper_intent_id=COALESCE(paper_intent_id,%s),
                            updated_at=clock_timestamp()
                        WHERE trade_id=%s
                          AND (paper_intent_id IS NULL OR paper_intent_id=%s)
                        RETURNING trade_id
                        """,
                        (int(paper_intent_id), fragment.trade_id, int(paper_intent_id)),
                    )
                    if cur.fetchone() is None:
                        raise ValueError("trade id is bound to a different paper intent")
                return current
            cur.execute(
                """
                INSERT INTO quant.simulator_fill_finality_trades (
                    trade_id,audit_key,fill_index,account_id,strategy_id,asset_id,
                    side,size,price,fee,matched_at,state,paper_intent_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    fragment.trade_id,
                    audit_key,
                    fill_index,
                    fragment.account_id,
                    fragment.strategy_id,
                    fragment.asset_id,
                    fragment.side,
                    fragment.size,
                    fragment.price,
                    fragment.fee,
                    fragment.matched_at,
                    FillFinalityState.MATCHED_PROVISIONAL.value,
                    paper_intent_id,
                ),
            )
            entry = FinalityJournalEntry(
                event_id=event_id,
                trade_id=fragment.trade_id,
                event_type="PROVISIONAL_FILL",
                state=FillFinalityState.MATCHED_PROVISIONAL,
                event_ts=fragment.matched_at,
                cash_delta=fragment.signed_cash,
                shares_delta=fragment.signed_shares,
                fee_delta=fragment.fee,
                reason="venue_match_not_economic_finality",
            )
            self._append(cur, entry)
            return FinalityTrade(fragment, FillFinalityState.MATCHED_PROVISIONAL)

    def mark_retrying(
        self,
        trade_id: str,
        *,
        event_id: str,
        event_ts: Any,
    ) -> FinalityTrade:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            trade = self._require(cur, trade_id, lock=True)
            if trade.state is FillFinalityState.RETRYING:
                return trade
            if trade.state is not FillFinalityState.MATCHED_PROVISIONAL:
                raise ValueError(f"trade is not retryable: {trade.state.value}")
            self._append(
                cur,
                FinalityJournalEntry(
                    event_id,
                    trade_id,
                    "FINALITY_RETRYING",
                    FillFinalityState.RETRYING,
                    event_ts,
                    reason="venue_confirmation_retry",
                ),
            )
            return self._set_state(
                cur,
                trade,
                FillFinalityState.RETRYING,
                event_ts,
            )

    def confirm(
        self,
        trade_id: str,
        *,
        event_id: str,
        event_ts: Any,
    ) -> FinalityTrade:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            trade = self._require(cur, trade_id, lock=True)
            if trade.state is FillFinalityState.CONFIRMED_FINAL:
                return trade
            if trade.state not in {
                FillFinalityState.MATCHED_PROVISIONAL,
                FillFinalityState.RETRYING,
            }:
                raise ValueError(f"trade is not confirmable: {trade.state.value}")
            self._append(
                cur,
                FinalityJournalEntry(
                    event_id,
                    trade_id,
                    "FILL_CONFIRMED",
                    FillFinalityState.CONFIRMED_FINAL,
                    event_ts,
                    reason="economic_finality_confirmed",
                ),
            )
            return self._set_state(
                cur,
                trade,
                FillFinalityState.CONFIRMED_FINAL,
                event_ts,
            )

    def fail_and_void(
        self,
        trade_id: str,
        *,
        event_id: str,
        event_ts: Any,
        reason: str,
    ) -> FinalityTrade:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            trade = self._require(cur, trade_id, lock=True)
            if trade.state is FillFinalityState.REVERSAL_APPLIED:
                return trade
            if trade.state not in {
                FillFinalityState.MATCHED_PROVISIONAL,
                FillFinalityState.RETRYING,
                FillFinalityState.CONFIRMED_FINAL,
            }:
                raise ValueError(f"trade is not voidable: {trade.state.value}")
            self._append(
                cur,
                FinalityJournalEntry(
                    f"{event_id}:failed",
                    trade_id,
                    "FINALITY_FAILED",
                    FillFinalityState.FAILED_FINAL,
                    event_ts,
                    reason=str(reason),
                ),
            )
            self._append(
                cur,
                void_entry(
                    trade.fragment,
                    event_id=f"{event_id}:void",
                    event_ts=event_ts,
                    reason=str(reason),
                ),
            )
            for entry in compensation_entries(
                trade.fragment,
                event_prefix=f"{event_id}:reversal",
                event_ts=event_ts,
            ):
                self._append(cur, entry)
            return self._set_state(
                cur,
                trade,
                FillFinalityState.REVERSAL_APPLIED,
                event_ts,
            )

    def trade(self, trade_id: str) -> FinalityTrade | None:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            return self._trade(cur, str(trade_id), lock=False)

    def journal(self, trade_id: str) -> tuple[FinalityJournalEntry, ...]:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.simulator_fill_finality_events
                WHERE trade_id=%s ORDER BY event_ts,created_at,event_id
                """,
                (str(trade_id),),
            )
            return tuple(_entry_from_row(row) for row in cur.fetchall())

    def nav(
        self,
        *,
        account_id: str,
        confirmed_only: bool,
    ) -> FinalityNav:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM quant.simulator_fill_finality_trades
                WHERE account_id=%s ORDER BY matched_at,trade_id
                """,
                (str(account_id),),
            )
            trades = tuple(_trade_from_row(row) for row in cur.fetchall())
        return rebuild_nav(trades, confirmed_only=confirmed_only)

    def pending_lifecycle_reconciliation(
        self,
        *,
        limit: int = 1000,
    ) -> tuple[FinalityReconciliationCandidate, ...]:
        """Map provisional fills to the durable paper order/account lifecycle."""

        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.trade_id,
                       CASE
                         WHEN i.status IN ('COMPLETED','WORKING')
                          AND i.result_audit_key=f.audit_key
                          AND p.audit_key IS NOT NULL
                           THEN 'CONFIRMED'
                         WHEN i.status='FAILED' THEN 'FAILED'
                         ELSE NULL
                       END AS outcome,
                       COALESCE(i.completed_at,i.updated_at,f.matched_at) AS event_ts,
                       CASE
                         WHEN i.status='FAILED'
                           THEN 'paper_order_lifecycle_failed'
                         ELSE 'paper_order_lifecycle_and_ledger_confirmed'
                       END AS reason
                FROM quant.simulator_fill_finality_trades f
                JOIN quant.paper_live_order_intents i
                  ON i.intent_id=f.paper_intent_id
                LEFT JOIN quant.paper_portfolio_applied_results p
                  ON p.audit_key=f.audit_key
                WHERE f.state IN ('MATCHED_PROVISIONAL','RETRYING')
                  AND (
                       i.status='FAILED'
                       OR (
                            i.status IN ('COMPLETED','WORKING')
                        AND i.result_audit_key=f.audit_key
                        AND p.audit_key IS NOT NULL
                       )
                  )
                ORDER BY f.matched_at,f.trade_id
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
            return tuple(
                FinalityReconciliationCandidate(
                    trade_id=str(row["trade_id"]),
                    outcome=str(row["outcome"]),
                    event_ts=row["event_ts"],
                    reason=str(row["reason"]),
                )
                for row in cur.fetchall()
            )

    @staticmethod
    def _lock(cur: Any, trade_id: str) -> None:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"simulator-fill-finality:{trade_id}",),
        )

    def _require(self, cur: Any, trade_id: str, *, lock: bool) -> FinalityTrade:
        trade = self._trade(cur, str(trade_id), lock=lock)
        if trade is None:
            raise KeyError(f"unknown finality trade: {trade_id}")
        return trade

    @staticmethod
    def _trade(cur: Any, trade_id: str, *, lock: bool) -> FinalityTrade | None:
        cur.execute(
            "SELECT * FROM quant.simulator_fill_finality_trades WHERE trade_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (str(trade_id),),
        )
        row = cur.fetchone()
        return _trade_from_row(row) if row is not None else None

    @staticmethod
    def _set_state(
        cur: Any,
        trade: FinalityTrade,
        state: FillFinalityState,
        event_ts: Any,
    ) -> FinalityTrade:
        cur.execute(
            """
            UPDATE quant.simulator_fill_finality_trades
            SET state=%s,finality_ts=%s,updated_at=clock_timestamp()
            WHERE trade_id=%s
            """,
            (state.value, event_ts, trade.fragment.trade_id),
        )
        return FinalityTrade(trade.fragment, state, event_ts)

    @staticmethod
    def _append(cur: Any, entry: FinalityJournalEntry) -> None:
        cur.execute(
            """
            INSERT INTO quant.simulator_fill_finality_events (
                event_id,trade_id,event_type,state,event_ts,
                cash_delta,shares_delta,fee_delta,reason
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_id) DO NOTHING RETURNING event_id
            """,
            (
                entry.event_id,
                entry.trade_id,
                entry.event_type,
                entry.state.value,
                entry.event_ts,
                entry.cash_delta,
                entry.shares_delta,
                entry.fee_delta,
                entry.reason,
            ),
        )
        if cur.fetchone() is not None:
            return
        cur.execute(
            "SELECT * FROM quant.simulator_fill_finality_events WHERE event_id=%s",
            (entry.event_id,),
        )
        existing = cur.fetchone()
        if existing is None or _entry_from_row(existing) != entry:
            raise ValueError("finality journal event id collision")


def _trade_from_row(row: Any) -> FinalityTrade:
    fragment = FillFragment(
        trade_id=str(row["trade_id"]),
        account_id=str(row["account_id"]),
        strategy_id=str(row["strategy_id"]),
        asset_id=str(row["asset_id"]),
        side=str(row["side"]),
        size=Decimal(row["size"]),
        price=Decimal(row["price"]),
        fee=Decimal(row["fee"]),
        matched_at=row["matched_at"],
    )
    return FinalityTrade(
        fragment,
        FillFinalityState(str(row["state"])),
        row.get("finality_ts"),
    )


def _entry_from_row(row: Any) -> FinalityJournalEntry:
    return FinalityJournalEntry(
        event_id=str(row["event_id"]),
        trade_id=str(row["trade_id"]),
        event_type=str(row["event_type"]),
        state=FillFinalityState(str(row["state"])),
        event_ts=row["event_ts"],
        cash_delta=Decimal(row["cash_delta"]),
        shares_delta=Decimal(row["shares_delta"]),
        fee_delta=Decimal(row["fee_delta"]),
        reason=str(row["reason"]),
    )
