"""Durable cash, position, fill, and settlement ledger for paper execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any

from quant.core.db import postgres_connection
from quant.execution.models.settlement_finality import (
    FinalityJournal,
    ProvisionalTrade,
    SettlementFinalityModel,
)
from quant.risk.event_risk import load_event_risk_input
from quant.simulator.analytics import NavPositionInput, build_nav_snapshot
from quant.simulator.complete_set import (
    COMPLETE_SET_SCHEMA_STATEMENTS,
    CompleteSetLotLegInput,
    CompleteSetLotStore,
    CompleteSetProvenance,
)
from quant.simulator.ctf import (
    CtfSettlementAudit,
    CtfSettlementMatchType,
    unknown_ctf_settlement_audit,
)

from .cash_reconciliation import PAPER_LEDGER, TOTAL, load_account_cash_delta_breakdown
from .paper_audit import ensure_paper_audit_schema, insert_paper_audit
from .portfolio_analytics import (
    PaperAssetMark,
    PaperNavPosition,
    calculate_portfolio_nav,
)
from .professional_execution import PaperRiskContext
from .taker_execution import (
    OrderIntent,
    PaperExecutionResult,
    PaperPortfolioSnapshot,
    TakerExecutionConfig,
    estimate_order_reservation,
)

LEDGER_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.paper_accounts (
        strategy_id TEXT PRIMARY KEY,
        base_currency TEXT NOT NULL DEFAULT 'USDC',
        initial_cash NUMERIC NOT NULL,
        cash_balance NUMERIC NOT NULL,
        cash_reserved NUMERIC NOT NULL DEFAULT 0,
        realized_pnl NUMERIC NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_positions (
        strategy_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        quantity NUMERIC NOT NULL DEFAULT 0,
        reserved_quantity NUMERIC NOT NULL DEFAULT 0,
        cost_basis NUMERIC NOT NULL DEFAULT 0,
        realized_pnl NUMERIC NOT NULL DEFAULT 0,
        settled_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (strategy_id, asset_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_order_reservations (
        intent_id BIGINT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        client_order_id TEXT NOT NULL,
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        side TEXT NOT NULL,
        reserved_cash NUMERIC NOT NULL DEFAULT 0,
        reserved_shares NUMERIC NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        release_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        released_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_reservation_release_events (
        release_event_key TEXT PRIMARY KEY,
        command_id TEXT NOT NULL,
        intent_id BIGINT NOT NULL,
        reason TEXT NOT NULL,
        event_ts_ns BIGINT NOT NULL,
        application_status TEXT NOT NULL,
        cash_released NUMERIC NOT NULL DEFAULT 0,
        shares_released NUMERIC NOT NULL DEFAULT 0,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_daily_accounting_snapshots (
        accounting_date DATE NOT NULL,
        strategy_id TEXT NOT NULL,
        initial_cash NUMERIC NOT NULL,
        ledger_cash_delta NUMERIC NOT NULL,
        expected_cash NUMERIC NOT NULL,
        actual_cash NUMERIC NOT NULL,
        cash_reserved NUMERIC NOT NULL,
        active_reserved_cash NUMERIC NOT NULL,
        position_count INTEGER NOT NULL,
        position_mismatch_count INTEGER NOT NULL,
        reserved_position_mismatch_count INTEGER NOT NULL,
        cash_difference NUMERIC NOT NULL,
        passed BOOLEAN NOT NULL,
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        generated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (accounting_date, strategy_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_strategy_risk_controls (
        strategy_id TEXT PRIMARY KEY,
        trading_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        kill_switch BOOLEAN NOT NULL DEFAULT FALSE,
        reason TEXT,
        limits JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_position_marks (
        strategy_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        research_mark NUMERIC,
        liquidation_mark NUMERIC,
        conservative_mark NUMERIC,
        mark_quality TEXT NOT NULL,
        mark_age_ms BIGINT,
        best_bid NUMERIC,
        best_ask NUMERIC,
        checkpoint_id TEXT,
        exit_levels JSONB NOT NULL DEFAULT '[]'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (strategy_id, asset_id)
    )
    """,
    """
    ALTER TABLE quant.paper_position_marks
    ADD COLUMN IF NOT EXISTS exit_levels JSONB NOT NULL DEFAULT '[]'::jsonb
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_portfolio_nav_snapshots (
        nav_id BIGSERIAL PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        initial_cash NUMERIC NOT NULL,
        cash_balance NUMERIC NOT NULL,
        cash_reserved NUMERIC NOT NULL,
        realized_pnl NUMERIC NOT NULL,
        position_market_value NUMERIC,
        conservative_position_value NUMERIC NOT NULL,
        equity NUMERIC,
        conservative_equity NUMERIC NOT NULL,
        unrealized_pnl NUMERIC,
        conservative_unrealized_pnl NUMERIC NOT NULL,
        total_pnl NUMERIC,
        conservative_total_pnl NUMERIC NOT NULL,
        gross_exposure NUMERIC NOT NULL,
        high_watermark NUMERIC NOT NULL,
        drawdown NUMERIC NOT NULL,
        drawdown_pct NUMERIC NOT NULL,
        open_positions INTEGER NOT NULL,
        unmarkable_positions INTEGER NOT NULL,
        nav_complete BOOLEAN NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_portfolio_nav_current (
        strategy_id TEXT PRIMARY KEY,
        observed_at TIMESTAMPTZ NOT NULL,
        initial_cash NUMERIC NOT NULL,
        cash_balance NUMERIC NOT NULL,
        cash_reserved NUMERIC NOT NULL,
        realized_pnl NUMERIC NOT NULL,
        position_market_value NUMERIC,
        conservative_position_value NUMERIC NOT NULL,
        equity NUMERIC,
        conservative_equity NUMERIC NOT NULL,
        unrealized_pnl NUMERIC,
        conservative_unrealized_pnl NUMERIC NOT NULL,
        total_pnl NUMERIC,
        conservative_total_pnl NUMERIC NOT NULL,
        gross_exposure NUMERIC NOT NULL,
        high_watermark NUMERIC NOT NULL,
        drawdown NUMERIC NOT NULL,
        drawdown_pct NUMERIC NOT NULL,
        open_positions INTEGER NOT NULL,
        unmarkable_positions INTEGER NOT NULL,
        nav_complete BOOLEAN NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_fills (
        audit_key TEXT NOT NULL,
        fill_index INTEGER NOT NULL,
        strategy_id TEXT NOT NULL,
        client_order_id TEXT NOT NULL,
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        side TEXT NOT NULL,
        price NUMERIC NOT NULL,
        size NUMERIC NOT NULL,
        notional NUMERIC NOT NULL,
        fee NUMERIC NOT NULL,
        arrival_ts TIMESTAMPTZ NOT NULL,
        arrival_checkpoint_id TEXT,
        settlement_match_type TEXT NOT NULL DEFAULT 'UNKNOWN',
        settlement_evidence_id TEXT,
        settlement_evidence_source TEXT NOT NULL DEFAULT 'PAPER_L2_NO_COUNTERPARTY',
        settlement_conservation_status TEXT NOT NULL DEFAULT 'NOT_PROVABLE',
        settlement_conservation_hash TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (audit_key, fill_index)
    )
    """,
    """
    ALTER TABLE quant.paper_fills
    ADD COLUMN IF NOT EXISTS settlement_match_type TEXT NOT NULL DEFAULT 'UNKNOWN'
    """,
    """
    ALTER TABLE quant.paper_fills
    ADD COLUMN IF NOT EXISTS settlement_evidence_id TEXT
    """,
    """
    ALTER TABLE quant.paper_fills
    ADD COLUMN IF NOT EXISTS settlement_evidence_source TEXT NOT NULL
        DEFAULT 'PAPER_L2_NO_COUNTERPARTY'
    """,
    """
    ALTER TABLE quant.paper_fills
    ADD COLUMN IF NOT EXISTS settlement_conservation_status TEXT NOT NULL
        DEFAULT 'NOT_PROVABLE'
    """,
    """
    ALTER TABLE quant.paper_fills
    ADD COLUMN IF NOT EXISTS settlement_conservation_hash TEXT
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_fill_ctf_settlement_audits (
        audit_key TEXT NOT NULL,
        fill_index INTEGER NOT NULL,
        evidence_id TEXT NOT NULL,
        settlement_match_type TEXT NOT NULL,
        evidence_source TEXT NOT NULL,
        conservation_status TEXT NOT NULL,
        conservation_hash TEXT NOT NULL,
        evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
        observed_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (audit_key,fill_index,evidence_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_fill_fee_charges (
        fee_charge_id TEXT PRIMARY KEY,
        audit_key TEXT NOT NULL,
        fill_index INTEGER NOT NULL,
        strategy_id TEXT NOT NULL,
        fill_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        liquidity_role TEXT NOT NULL,
        price NUMERIC NOT NULL,
        shares NUMERIC NOT NULL,
        platform_fee_rate NUMERIC NOT NULL,
        platform_fee_exponent NUMERIC NOT NULL,
        platform_fee NUMERIC NOT NULL,
        builder_code TEXT,
        builder_fee_rate_bps INTEGER NOT NULL DEFAULT 0,
        builder_fee NUMERIC NOT NULL,
        total_fee NUMERIC NOT NULL,
        rounding_policy TEXT NOT NULL,
        fee_schedule_id TEXT NOT NULL,
        economics_regime_id TEXT NOT NULL,
        source TEXT NOT NULL,
        reconciliation_status TEXT NOT NULL DEFAULT 'UNRECONCILED',
        official_platform_fee NUMERIC,
        official_builder_fee NUMERIC,
        official_total_fee NUMERIC,
        official_evidence_id TEXT,
        official_evidence_sha256 TEXT,
        reconciled_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (audit_key,fill_index)
    )
    """,
    """
    ALTER TABLE quant.paper_fill_fee_charges
    ADD COLUMN IF NOT EXISTS official_evidence_id TEXT
    """,
    """
    ALTER TABLE quant.paper_fill_fee_charges
    ADD COLUMN IF NOT EXISTS official_evidence_sha256 TEXT
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_fill_fee_charges_schedule_idx
    ON quant.paper_fill_fee_charges (fee_schedule_id,created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS paper_fill_ctf_settlement_audits_type_idx
        ON quant.paper_fill_ctf_settlement_audits
        (settlement_match_type,conservation_status,observed_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_ledger_entries (
        entry_id BIGSERIAL PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        strategy_id TEXT NOT NULL,
        audit_key TEXT,
        client_order_id TEXT,
        event_type TEXT NOT NULL,
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        price NUMERIC,
        shares_delta NUMERIC NOT NULL,
        cash_delta NUMERIC NOT NULL,
        fee NUMERIC NOT NULL DEFAULT 0,
        realized_pnl_delta NUMERIC NOT NULL DEFAULT 0,
        cash_after NUMERIC NOT NULL,
        position_after NUMERIC NOT NULL,
        cost_basis_after NUMERIC NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_portfolio_applied_results (
        audit_key TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        client_order_id TEXT NOT NULL,
        status TEXT NOT NULL,
        filled_size NUMERIC NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_settlements (
        settlement_key TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        winning_asset_id TEXT NOT NULL,
        quantity NUMERIC NOT NULL,
        payout_per_share NUMERIC NOT NULL,
        cash_delta NUMERIC NOT NULL,
        realized_pnl_delta NUMERIC NOT NULL,
        resolution_source TEXT,
        resolved_at TIMESTAMPTZ,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.market_settlement_payouts (
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        payout_per_share NUMERIC NOT NULL CHECK (
            payout_per_share >= 0 AND payout_per_share <= 1
        ),
        resolution_source TEXT NOT NULL,
        oracle_finalized_at TIMESTAMPTZ NOT NULL,
        truth_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (condition_id, asset_id, truth_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.market_resolution_states (
        condition_id TEXT PRIMARY KEY,
        phase TEXT NOT NULL,
        expected_resolution_at TIMESTAMPTZ,
        trading_stopped_at TIMESTAMPTZ,
        actual_finalized_at TIMESTAMPTZ,
        redeemed_at TIMESTAMPTZ,
        capital_locked_seconds BIGINT,
        truth_hash TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_complete_set_merges (
        merge_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        market_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        yes_asset_id TEXT NOT NULL,
        no_asset_id TEXT NOT NULL,
        quantity NUMERIC NOT NULL,
        cash_delta NUMERIC NOT NULL,
        realized_pnl_delta NUMERIC NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        merged_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_execution_finality (
        trade_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        state TEXT NOT NULL,
        provisional_size NUMERIC NOT NULL DEFAULT 0,
        confirmed_size NUMERIC NOT NULL DEFAULT 0,
        transaction_hash TEXT,
        matched_at TIMESTAMPTZ,
        finalized_at TIMESTAMPTZ,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_execution_finality_events (
        event_key TEXT PRIMARY KEY,
        trade_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        state TEXT NOT NULL,
        event_type TEXT NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        journal_key TEXT,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.paper_rebates (
        rebate_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        rebate_type TEXT NOT NULL,
        schedule_version TEXT NOT NULL,
        state TEXT NOT NULL,
        amount NUMERIC NOT NULL,
        estimated_at TIMESTAMPTZ NOT NULL,
        accrued_at TIMESTAMPTZ,
        received_at TIMESTAMPTZ,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.maker_queue_states (
        paper_order_id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL,
        side TEXT NOT NULL,
        price_tick NUMERIC NOT NULL,
        queue_model_version TEXT NOT NULL,
        displayed_size_at_accept NUMERIC NOT NULL,
        own_orders_ahead NUMERIC NOT NULL,
        estimated_external_queue_ahead NUMERIC NOT NULL,
        cumulative_trade_volume_at_price NUMERIC NOT NULL DEFAULT 0,
        cumulative_cancel_ahead_estimate NUMERIC NOT NULL DEFAULT 0,
        fill_probability NUMERIC NOT NULL DEFAULT 0,
        expected_fill_size NUMERIC NOT NULL DEFAULT 0,
        queue_confidence NUMERIC NOT NULL DEFAULT 0,
        last_event_id TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    "ALTER TABLE quant.maker_queue_states ADD COLUMN IF NOT EXISTS accepted_order_size NUMERIC NOT NULL DEFAULT 0",
    "ALTER TABLE quant.maker_queue_states ADD COLUMN IF NOT EXISTS cumulative_filled_size NUMERIC NOT NULL DEFAULT 0",
    "ALTER TABLE quant.maker_queue_states ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'WORKING'",
    "ALTER TABLE quant.maker_queue_states ADD COLUMN IF NOT EXISTS accepted_checkpoint_id TEXT",
    "ALTER TABLE quant.maker_queue_states ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ",
    "ALTER TABLE quant.maker_queue_states ADD COLUMN IF NOT EXISTS accepted_book_generation BIGINT",
    "ALTER TABLE quant.maker_queue_states ADD COLUMN IF NOT EXISTS queue_epoch BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE quant.maker_queue_states ADD COLUMN IF NOT EXISTS last_event_ts TIMESTAMPTZ",
    "ALTER TABLE quant.maker_queue_states ADD COLUMN IF NOT EXISTS model_domain_decision JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE quant.maker_queue_states ADD COLUMN IF NOT EXISTS model_domain_decision_hash TEXT",
    """
    CREATE TABLE IF NOT EXISTS quant.paper_journal_lines (
        journal_id TEXT NOT NULL,
        line_index INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        account_code TEXT NOT NULL,
        debit NUMERIC NOT NULL DEFAULT 0 CHECK (debit >= 0),
        credit NUMERIC NOT NULL DEFAULT 0 CHECK (credit >= 0),
        event_ts TIMESTAMPTZ NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (journal_id, line_index),
        CHECK (NOT (debit > 0 AND credit > 0))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_positions_open ON quant.paper_positions (strategy_id, updated_at DESC) WHERE quantity > 0",
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_ledger_strategy_time ON quant.paper_ledger_entries (strategy_id, event_ts, entry_id)",
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_fills_asset_time ON quant.paper_fills (asset_id, arrival_ts)",
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_reservations_active ON quant.paper_order_reservations (strategy_id, asset_id) WHERE status='ACTIVE'",
    "CREATE INDEX IF NOT EXISTS idx_quant_paper_nav_strategy_time ON quant.paper_portfolio_nav_snapshots (strategy_id, observed_at DESC, nav_id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_quant_execution_finality_state ON quant.paper_execution_finality (state, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_quant_execution_finality_events_trade ON quant.paper_execution_finality_events (trade_id, event_ts)",
    "CREATE INDEX IF NOT EXISTS idx_quant_maker_queue_asset ON quant.maker_queue_states (asset_id, side, price_tick)",
    "CREATE INDEX IF NOT EXISTS idx_quant_journal_strategy_time ON quant.paper_journal_lines (strategy_id, event_ts, journal_id)",
    "ALTER TABLE quant.paper_accounts ADD COLUMN IF NOT EXISTS cash_reserved NUMERIC NOT NULL DEFAULT 0",
    "ALTER TABLE quant.paper_positions ADD COLUMN IF NOT EXISTS reserved_quantity NUMERIC NOT NULL DEFAULT 0",
)


class PaperLedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperPortfolioState:
    cash_balance: Decimal
    position_size: Decimal
    cost_basis: Decimal
    realized_pnl: Decimal


@dataclass(frozen=True)
class PaperLedgerEntry:
    fill_index: int
    event_type: str
    price: Decimal
    shares_delta: Decimal
    cash_delta: Decimal
    fee: Decimal
    realized_pnl_delta: Decimal
    cash_after: Decimal
    position_after: Decimal
    cost_basis_after: Decimal


@dataclass(frozen=True)
class PaperPortfolioMutation:
    after: PaperPortfolioState
    entries: tuple[PaperLedgerEntry, ...]
    realized_pnl_delta: Decimal


@dataclass(frozen=True)
class PaperPositionState:
    quantity: Decimal
    cost_basis: Decimal
    realized_pnl: Decimal


@dataclass(frozen=True)
class PaperCompleteSetMergeMutation:
    quantity: Decimal
    payout: Decimal
    cash_after: Decimal
    realized_pnl_delta: Decimal
    yes_after: PaperPositionState
    no_after: PaperPositionState
    yes_realized_pnl_delta: Decimal
    no_realized_pnl_delta: Decimal


def apply_execution_result(
    state: PaperPortfolioState,
    result: PaperExecutionResult,
) -> PaperPortfolioMutation:
    cash = state.cash_balance
    position = state.position_size
    cost_basis = state.cost_basis
    realized = state.realized_pnl
    entries: list[PaperLedgerEntry] = []
    total_realized_delta = Decimal("0")

    for fill_index, fill in enumerate(result.fills):
        notional = fill.price * fill.size
        if result.intent.side == "BUY":
            cash_delta = -(notional + fill.fee)
            if cash + cash_delta < 0:
                raise PaperLedgerError("insufficient_paper_cash_at_commit")
            shares_delta = fill.size
            realized_delta = Decimal("0")
            cost_basis += notional + fill.fee
        else:
            if position < fill.size:
                raise PaperLedgerError("insufficient_paper_position_at_commit")
            average_cost = cost_basis / position if position > 0 else Decimal("0")
            allocated_cost = average_cost * fill.size
            cash_delta = notional - fill.fee
            shares_delta = -fill.size
            realized_delta = cash_delta - allocated_cost
            cost_basis = max(Decimal("0"), cost_basis - allocated_cost)
            realized += realized_delta
            total_realized_delta += realized_delta

        cash += cash_delta
        position += shares_delta
        if position == 0:
            cost_basis = Decimal("0")
        entries.append(
            PaperLedgerEntry(
                fill_index=fill_index,
                event_type=result.intent.side,
                price=fill.price,
                shares_delta=shares_delta,
                cash_delta=cash_delta,
                fee=fill.fee,
                realized_pnl_delta=realized_delta,
                cash_after=cash,
                position_after=position,
                cost_basis_after=cost_basis,
            )
        )

    return PaperPortfolioMutation(
        after=PaperPortfolioState(cash, position, cost_basis, realized),
        entries=tuple(entries),
        realized_pnl_delta=total_realized_delta,
    )


def apply_settlement(
    state: PaperPortfolioState,
    *,
    winning: bool | None = None,
    payout_per_share: Decimal | None = None,
) -> tuple[PaperPortfolioState, Decimal, Decimal]:
    if payout_per_share is None:
        if winning is None:
            raise ValueError("winning or payout_per_share is required")
        payout_per_share = Decimal("1") if winning else Decimal("0")
    if payout_per_share < 0 or payout_per_share > 1:
        raise ValueError("payout_per_share must be within [0, 1]")
    payout = state.position_size * payout_per_share
    realized_delta = payout - state.cost_basis
    return (
        PaperPortfolioState(
            cash_balance=state.cash_balance + payout,
            position_size=Decimal("0"),
            cost_basis=Decimal("0"),
            realized_pnl=state.realized_pnl + realized_delta,
        ),
        payout,
        realized_delta,
    )


def apply_complete_set_merge(
    *,
    cash_balance: Decimal,
    yes: PaperPositionState,
    no: PaperPositionState,
    quantity: Decimal,
) -> PaperCompleteSetMergeMutation:
    """Burn equal YES/NO shares and credit one unit of paper cash per pair."""

    if quantity <= 0:
        raise PaperLedgerError("merge_quantity_must_be_positive")
    if yes.quantity < quantity or no.quantity < quantity:
        raise PaperLedgerError("insufficient_complete_set_position_at_merge")

    yes_allocated_cost = yes.cost_basis * quantity / yes.quantity
    no_allocated_cost = no.cost_basis * quantity / no.quantity
    half_payout = quantity / Decimal("2")
    yes_realized_delta = half_payout - yes_allocated_cost
    no_realized_delta = half_payout - no_allocated_cost
    realized_delta = yes_realized_delta + no_realized_delta

    yes_quantity = yes.quantity - quantity
    no_quantity = no.quantity - quantity
    yes_cost_basis = max(Decimal("0"), yes.cost_basis - yes_allocated_cost)
    no_cost_basis = max(Decimal("0"), no.cost_basis - no_allocated_cost)
    if yes_quantity == 0:
        yes_cost_basis = Decimal("0")
    if no_quantity == 0:
        no_cost_basis = Decimal("0")

    return PaperCompleteSetMergeMutation(
        quantity=quantity,
        payout=quantity,
        cash_after=cash_balance + quantity,
        realized_pnl_delta=realized_delta,
        yes_after=PaperPositionState(
            quantity=yes_quantity,
            cost_basis=yes_cost_basis,
            realized_pnl=yes.realized_pnl + yes_realized_delta,
        ),
        no_after=PaperPositionState(
            quantity=no_quantity,
            cost_basis=no_cost_basis,
            realized_pnl=no.realized_pnl + no_realized_delta,
        ),
        yes_realized_pnl_delta=yes_realized_delta,
        no_realized_pnl_delta=no_realized_delta,
    )


class PostgresPaperLedgerSink:
    """Apply one paper result to audit and portfolio tables in one transaction."""

    def __init__(
        self,
        connection_factory: Any = postgres_connection,
        *,
        initial_cash: Decimal = Decimal("10000"),
        synthetic_finality_outcome: str = "CONFIRMED",
        ensure_schema: bool = True,
        transaction_fault_hook: Callable[[str, PaperExecutionResult], None]
        | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if synthetic_finality_outcome not in {"CONFIRMED", "FAILED_REVERSED"}:
            raise ValueError(
                "synthetic_finality_outcome must be CONFIRMED or FAILED_REVERSED"
            )
        self.connection_factory = connection_factory
        self.initial_cash = initial_cash
        self.synthetic_finality_outcome = synthetic_finality_outcome
        self.transaction_fault_hook = transaction_fault_hook
        if ensure_schema:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            ensure_paper_audit_schema(cur)
            conn.commit()
            for statement in LEDGER_SCHEMA_STATEMENTS:
                cur.execute(statement)
                conn.commit()
            for statement in COMPLETE_SET_SCHEMA_STATEMENTS:
                cur.execute(statement)
                conn.commit()

    def ensure_account(self, strategy_id: str) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._ensure_account(cur, strategy_id)
            conn.commit()

    def seed_calibration_position(
        self,
        *,
        strategy_id: str,
        asset_id: str,
        market_id: str,
        condition_id: str,
        quantity: Decimal,
        cost_basis: Decimal = Decimal("0"),
    ) -> PaperPortfolioSnapshot:
        """Idempotently seed an isolated calibration strategy from a real position."""

        if quantity <= 0 or cost_basis < 0:
            raise ValueError(
                "calibration position seed must have positive quantity and non-negative cost"
            )
        idempotency_key = f"calibration-position-seed:{strategy_id}:{asset_id}"
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._ensure_account(cur, strategy_id)
            cur.execute(
                """
                INSERT INTO quant.paper_positions (
                    strategy_id, asset_id, market_id, condition_id, quantity, cost_basis
                ) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (strategy_id, asset_id) DO NOTHING
                """,
                (
                    str(strategy_id),
                    str(asset_id),
                    str(market_id),
                    str(condition_id),
                    quantity,
                    cost_basis,
                ),
            )
            cur.execute(
                """
                SELECT a.cash_balance, p.quantity, p.cost_basis
                FROM quant.paper_accounts a
                JOIN quant.paper_positions p ON p.strategy_id=a.strategy_id
                WHERE a.strategy_id=%s AND p.asset_id=%s
                FOR UPDATE OF a, p
                """,
                (str(strategy_id), str(asset_id)),
            )
            row = cur.fetchone()
            if row is None:
                raise PaperLedgerError("calibration position seed was not persisted")
            if (
                Decimal(str(row["quantity"])) != quantity
                or Decimal(str(row["cost_basis"])) != cost_basis
            ):
                raise PaperLedgerError(
                    "calibration strategy already has a different paper position"
                )
            CompleteSetLotStore.ensure_position_coverage(
                cur,
                strategy_id=strategy_id,
                market_id=market_id,
                condition_id=condition_id,
                asset_id=asset_id,
                quantity=quantity,
                cost_basis=cost_basis,
                observed_at=datetime.now(timezone.utc),
            )
            cur.execute(
                """
                INSERT INTO quant.paper_ledger_entries (
                    idempotency_key, strategy_id, event_type, market_id, condition_id,
                    asset_id, event_ts, shares_delta, cash_delta, fee,
                    realized_pnl_delta, cash_after, position_after, cost_basis_after,
                    metadata
                ) VALUES (
                    %s,%s,'POSITION_SEED',%s,%s,%s,clock_timestamp(),%s,0,0,0,%s,%s,%s,
                    '{"source":"calibration_real_account_baseline"}'::jsonb
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    idempotency_key,
                    str(strategy_id),
                    str(market_id),
                    str(condition_id),
                    str(asset_id),
                    quantity,
                    row["cash_balance"],
                    quantity,
                    cost_basis,
                ),
            )
            conn.commit()
            return PaperPortfolioSnapshot(
                cash_balance=Decimal(str(row["cash_balance"])),
                position_size=quantity,
            )

    def reserve_order(
        self,
        intent_id: int,
        intent: OrderIntent,
        config: TakerExecutionConfig,
    ) -> dict[str, Decimal | str]:
        """Idempotently reserve paper cash or shares before execution."""

        required_cash, required_shares = estimate_order_reservation(intent, config)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._ensure_account(cur, intent.strategy_id)
            cur.execute(
                """
                INSERT INTO quant.paper_positions (
                    strategy_id, asset_id, market_id, condition_id
                ) VALUES (%s,%s,%s,%s)
                ON CONFLICT (strategy_id, asset_id) DO UPDATE SET
                    market_id=EXCLUDED.market_id,
                    condition_id=EXCLUDED.condition_id,
                    updated_at=clock_timestamp()
                """,
                (
                    intent.strategy_id,
                    intent.asset_id,
                    intent.market_id,
                    intent.condition_id,
                ),
            )
            cur.execute(
                """
                SELECT intent_id, status, reserved_cash, reserved_shares
                FROM quant.paper_order_reservations
                WHERE intent_id=%s
                FOR UPDATE
                """,
                (int(intent_id),),
            )
            existing = cur.fetchone()
            if existing is not None:
                conn.commit()
                return {
                    "status": str(existing["status"]),
                    "reserved_cash": Decimal(str(existing["reserved_cash"])),
                    "reserved_shares": Decimal(str(existing["reserved_shares"])),
                }
            cur.execute(
                """
                SELECT a.cash_balance, a.cash_reserved,
                       p.quantity, p.reserved_quantity
                FROM quant.paper_accounts a
                JOIN quant.paper_positions p ON p.strategy_id=a.strategy_id
                WHERE a.strategy_id=%s AND p.asset_id=%s
                FOR UPDATE OF a, p
                """,
                (intent.strategy_id, intent.asset_id),
            )
            row = cur.fetchone()
            assert row is not None
            available_cash = Decimal(str(row["cash_balance"])) - Decimal(
                str(row["cash_reserved"])
            )
            available_shares = Decimal(str(row["quantity"])) - Decimal(
                str(row["reserved_quantity"])
            )
            if required_cash > available_cash:
                raise PaperLedgerError(
                    f"insufficient_available_paper_cash:{available_cash}:{required_cash}"
                )
            if required_shares > available_shares:
                raise PaperLedgerError(
                    f"insufficient_available_paper_position:{available_shares}:{required_shares}"
                )
            cur.execute(
                """
                INSERT INTO quant.paper_order_reservations (
                    intent_id, strategy_id, client_order_id, market_id, condition_id,
                    asset_id, side, reserved_cash, reserved_shares
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    int(intent_id),
                    intent.strategy_id,
                    intent.client_order_id,
                    intent.market_id,
                    intent.condition_id,
                    intent.asset_id,
                    intent.side,
                    required_cash,
                    required_shares,
                ),
            )
            if required_cash:
                cur.execute(
                    """
                    UPDATE quant.paper_accounts
                    SET cash_reserved=cash_reserved + %s, updated_at=clock_timestamp()
                    WHERE strategy_id=%s
                    """,
                    (required_cash, intent.strategy_id),
                )
            if required_shares:
                cur.execute(
                    """
                    UPDATE quant.paper_positions
                    SET reserved_quantity=reserved_quantity + %s, updated_at=clock_timestamp()
                    WHERE strategy_id=%s AND asset_id=%s
                    """,
                    (required_shares, intent.strategy_id, intent.asset_id),
                )
            conn.commit()
        return {
            "status": "ACTIVE",
            "reserved_cash": required_cash,
            "reserved_shares": required_shares,
        }

    def portfolio_snapshot(
        self,
        strategy_id: str,
        asset_id: str,
        *,
        intent_id: int | None = None,
    ) -> PaperPortfolioSnapshot:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._ensure_account(cur, strategy_id)
            cur.execute(
                """
                SELECT a.cash_balance - a.cash_reserved
                           + COALESCE(r.reserved_cash, 0) AS cash_balance,
                       COALESCE(p.quantity - p.reserved_quantity, 0)
                           + COALESCE(r.reserved_shares, 0) AS position_size
                FROM quant.paper_accounts a
                LEFT JOIN quant.paper_positions p
                  ON p.strategy_id=a.strategy_id AND p.asset_id=%s
                LEFT JOIN quant.paper_order_reservations r
                  ON r.intent_id=%s AND r.status='ACTIVE'
                WHERE a.strategy_id=%s
                """,
                (
                    str(asset_id),
                    int(intent_id) if intent_id is not None else None,
                    str(strategy_id),
                ),
            )
            row = cur.fetchone()
            conn.commit()
        assert row is not None
        return PaperPortfolioSnapshot(
            cash_balance=Decimal(str(row["cash_balance"])),
            position_size=Decimal(str(row["position_size"])),
        )

    def risk_context(self, intent: OrderIntent) -> PaperRiskContext:
        """Build a point-in-time strategy risk view before any reservation."""

        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(c.trading_enabled, TRUE) AS trading_enabled,
                       COALESCE(c.kill_switch, FALSE) AS kill_switch,
                       COALESCE(catalog.event_id, %s) AS event_id,
                       COALESCE(NULLIF(catalog.category, ''), 'unknown') AS category,
                       COALESCE(catalog.enable_neg_risk, FALSE) AS neg_risk
                FROM (SELECT 1) seed
                LEFT JOIN quant.paper_strategy_risk_controls c
                  ON c.strategy_id=%s
                LEFT JOIN quant.paper_execution_market_catalog catalog
                  ON catalog.asset_id=%s
                """,
                (
                    intent.condition_id,
                    intent.strategy_id,
                    intent.asset_id,
                ),
            )
            metadata = dict(cur.fetchone() or {})
            event_id = str(metadata.get("event_id") or intent.condition_id)
            category = str(metadata.get("category") or "unknown")
            neg_risk_group = event_id if bool(metadata.get("neg_risk")) else None
            cur.execute(
                """
                SELECT p.condition_id,
                       COALESCE(catalog.event_id, p.condition_id) AS event_id,
                       COALESCE(NULLIF(catalog.category, ''), 'unknown') AS category,
                       COALESCE(catalog.enable_neg_risk, FALSE) AS neg_risk,
                       p.quantity
                FROM quant.paper_positions p
                LEFT JOIN quant.paper_execution_market_catalog catalog
                  ON catalog.asset_id=p.asset_id
                WHERE p.strategy_id=%s AND p.quantity>0
                """,
                (intent.strategy_id,),
            )
            positions = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT COALESCE(sum(realized_pnl_delta), 0) AS daily_realized_pnl
                FROM quant.paper_ledger_entries
                WHERE strategy_id=%s
                  AND event_ts >= date_trunc('day', statement_timestamp())
                """,
                (intent.strategy_id,),
            )
            daily_realized = Decimal(str(cur.fetchone()["daily_realized_pnl"] or 0))
            cur.execute(
                """
                SELECT count(*) FILTER (
                           WHERE status IN ('QUEUED','PROCESSING','WORKING')
                       ) AS open_orders,
                       count(*) FILTER (
                           WHERE created_at >= statement_timestamp() - interval '1 minute'
                       ) AS recent_orders
                FROM quant.paper_live_order_intents
                WHERE strategy_id=%s
                """,
                (intent.strategy_id,),
            )
            order_counts = dict(cur.fetchone() or {})
            event_risk_input = load_event_risk_input(
                cur,
                intent,
                candidate_event_id=event_id,
                candidate_category=category,
            )

        gross = sum(
            (Decimal(str(row["quantity"])) for row in positions),
            Decimal("0"),
        )
        condition = sum(
            (
                Decimal(str(row["quantity"]))
                for row in positions
                if str(row["condition_id"]) == intent.condition_id
            ),
            Decimal("0"),
        )
        event = sum(
            (
                Decimal(str(row["quantity"]))
                for row in positions
                if str(row["event_id"]) == event_id
            ),
            Decimal("0"),
        )
        category_notional = sum(
            (
                Decimal(str(row["quantity"]))
                for row in positions
                if str(row["category"]) == category
            ),
            Decimal("0"),
        )
        neg_risk_notional = (
            sum(
                (
                    Decimal(str(row["quantity"]))
                    for row in positions
                    if bool(row["neg_risk"]) and str(row["event_id"]) == event_id
                ),
                Decimal("0"),
            )
            if neg_risk_group
            else Decimal("0")
        )
        return PaperRiskContext(
            trading_enabled=bool(metadata.get("trading_enabled", True)),
            kill_switch=bool(metadata.get("kill_switch", False)),
            gross_notional=gross,
            condition_notional=condition,
            event_notional=event,
            neg_risk_group_notional=neg_risk_notional,
            category_notional=category_notional,
            daily_realized_pnl=daily_realized,
            open_order_count=int(order_counts.get("open_orders") or 0),
            recent_order_count=int(order_counts.get("recent_orders") or 0),
            event_id=event_id,
            category=category,
            neg_risk_group=neg_risk_group,
            event_risk_input=event_risk_input,
            event_risk_required=True,
        )

    def record_nav_snapshots(
        self,
        marks: dict[str, PaperAssetMark],
        *,
        observed_at: datetime,
        history_interval_seconds: float = 60.0,
        strategy_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Upsert realtime NAV and append bounded performance history."""

        selected_strategies = (
            tuple(dict.fromkeys(str(item) for item in strategy_ids))
            if strategy_ids is not None
            else None
        )
        if selected_strategies == ():
            return []
        account_filter = (
            " AND a.strategy_id = ANY(%s::text[])"
            if selected_strategies is not None
            else ""
        )
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '10s'")
            cur.execute(
                f"""
                SELECT strategy_id, initial_cash, cash_balance, cash_reserved,
                       realized_pnl
                FROM quant.paper_accounts a
                WHERE (
                      a.updated_at >= %s - interval '1 hour'
                   OR EXISTS (
                       SELECT 1
                       FROM quant.paper_positions p
                       WHERE p.strategy_id=a.strategy_id AND p.quantity>0
                   )
                )
                {account_filter}
                ORDER BY strategy_id
                """,
                (
                    (observed_at, list(selected_strategies))
                    if selected_strategies is not None
                    else (observed_at,)
                ),
            )
            accounts = [dict(row) for row in cur.fetchall()]
            nav_strategy_ids = [str(row["strategy_id"]) for row in accounts]
            if not nav_strategy_ids:
                conn.commit()
                return []
            cur.execute(
                """
                SELECT strategy_id, asset_id, quantity, cost_basis
                FROM quant.paper_positions
                WHERE quantity>0
                  AND strategy_id = ANY(%s::text[])
                ORDER BY strategy_id, asset_id
                """,
                (nav_strategy_ids,),
            )
            by_strategy: dict[str, list[PaperNavPosition]] = {}
            raw_positions = [dict(row) for row in cur.fetchall()]
            for row in raw_positions:
                by_strategy.setdefault(str(row["strategy_id"]), []).append(
                    PaperNavPosition(
                        asset_id=str(row["asset_id"]),
                        quantity=Decimal(str(row["quantity"])),
                        cost_basis=Decimal(str(row["cost_basis"])),
                    )
                )
            cur.execute(
                """
                SELECT strategy_id, observed_at, high_watermark,
                       conservative_equity, realized_pnl, open_positions,
                       unmarkable_positions, nav_complete
                FROM quant.paper_portfolio_nav_current
                WHERE strategy_id = ANY(%s::text[])
                ORDER BY strategy_id
                """,
                (nav_strategy_ids,),
            )
            latest_history = {
                str(row["strategy_id"]): dict(row) for row in cur.fetchall()
            }
            results: list[dict[str, Any]] = []
            mark_rows: list[tuple[Any, ...]] = []
            current_rows: list[tuple[Any, ...]] = []
            history_rows: list[tuple[Any, ...]] = []
            for account in accounts:
                strategy_id = str(account["strategy_id"])
                positions = by_strategy.get(strategy_id, [])
                previous = latest_history.get(strategy_id)
                nav = calculate_portfolio_nav(
                    initial_cash=Decimal(str(account["initial_cash"])),
                    cash_balance=Decimal(str(account["cash_balance"])),
                    positions=positions,
                    marks=marks,
                    previous_high_watermark=(
                        Decimal(str(previous["high_watermark"]))
                        if previous is not None
                        else Decimal(0)
                    ),
                )
                valuation_positions: list[NavPositionInput] = []
                for position in positions:
                    mark = marks.get(position.asset_id)
                    mid_mark = mark.result.research_mark if mark is not None else None
                    position_mark_value = (
                        position.quantity * mid_mark if mid_mark is not None else None
                    )
                    valuation_positions.append(
                        NavPositionInput(
                            asset_id=position.asset_id,
                            quantity=position.quantity,
                            mid_mark=mid_mark,
                            exit_levels=(mark.exit_levels if mark is not None else ()),
                            accounting_value=position.cost_basis,
                            expected_payout=mid_mark,
                            worst_case_payout=None,
                            provisional_value=position_mark_value,
                            confirmed_value=position_mark_value,
                        )
                    )
                    if mark is None:
                        continue
                    mark_rows.append(
                        (
                            strategy_id,
                            position.asset_id,
                            mark.observed_at,
                            mark.result.research_mark,
                            mark.result.liquidation_mark,
                            mark.result.conservative_mark,
                            mark.result.mark_quality.value,
                            mark.result.mark_age_ms,
                            mark.best_bid,
                            mark.best_ask,
                            mark.checkpoint_id,
                            json.dumps(
                                [
                                    {
                                        "price": format(level.price, "f"),
                                        "size": format(level.size, "f"),
                                    }
                                    for level in mark.exit_levels
                                ]
                            ),
                        )
                    )
                valuation = build_nav_snapshot(
                    accounting_cash=Decimal(str(account["cash_balance"])),
                    positions=valuation_positions,
                )
                valuation_metadata = {
                    "mark_count": len(marks),
                    "mark_source": "gcp_colocated_book_state",
                    "valuation_views": valuation.as_dict(),
                    "walk_book_source": "persisted_exit_levels",
                }
                nav_values = (
                    strategy_id,
                    observed_at,
                    account["initial_cash"],
                    account["cash_balance"],
                    account["cash_reserved"],
                    account["realized_pnl"],
                    nav.position_market_value,
                    nav.conservative_position_value,
                    nav.equity,
                    nav.conservative_equity,
                    nav.unrealized_pnl,
                    nav.conservative_unrealized_pnl,
                    nav.total_pnl,
                    nav.conservative_total_pnl,
                    nav.gross_exposure,
                    nav.high_watermark,
                    nav.drawdown,
                    nav.drawdown_pct,
                    nav.open_positions,
                    nav.unmarkable_positions,
                    nav.nav_complete,
                    json.dumps(valuation_metadata),
                )
                current_rows.append(nav_values)
                history_due = _nav_history_due(
                    previous,
                    observed_at=observed_at,
                    interval_seconds=history_interval_seconds,
                    realized_pnl=Decimal(str(account["realized_pnl"])),
                    open_positions=nav.open_positions,
                    unmarkable_positions=nav.unmarkable_positions,
                    nav_complete=nav.nav_complete,
                )
                if history_due:
                    history_rows.append(nav_values)
                results.append(
                    {
                        "strategy_id": strategy_id,
                        "equity": nav.equity,
                        "conservative_equity": nav.conservative_equity,
                        "unrealized_pnl": nav.unrealized_pnl,
                        "conservative_unrealized_pnl": (
                            nav.conservative_unrealized_pnl
                        ),
                        "drawdown_pct": nav.drawdown_pct,
                        "open_positions": nav.open_positions,
                        "unmarkable_positions": nav.unmarkable_positions,
                        "nav_complete": nav.nav_complete,
                        "valuation_views": valuation.as_dict(),
                        "history_persisted": history_due,
                    }
                )
            if mark_rows:
                cur.executemany(
                    """
                    INSERT INTO quant.paper_position_marks (
                        strategy_id, asset_id, observed_at,
                        research_mark, liquidation_mark, conservative_mark,
                        mark_quality, mark_age_ms, best_bid, best_ask,
                        checkpoint_id,exit_levels
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (strategy_id, asset_id) DO UPDATE SET
                        observed_at=EXCLUDED.observed_at,
                        research_mark=EXCLUDED.research_mark,
                        liquidation_mark=EXCLUDED.liquidation_mark,
                        conservative_mark=EXCLUDED.conservative_mark,
                        mark_quality=EXCLUDED.mark_quality,
                        mark_age_ms=EXCLUDED.mark_age_ms,
                        best_bid=EXCLUDED.best_bid,
                        best_ask=EXCLUDED.best_ask,
                        checkpoint_id=EXCLUDED.checkpoint_id,
                        exit_levels=EXCLUDED.exit_levels,
                        updated_at=clock_timestamp()
                    """,
                    mark_rows,
                )
            if current_rows:
                cur.executemany(
                    """
                    INSERT INTO quant.paper_portfolio_nav_current (
                        strategy_id, observed_at, initial_cash, cash_balance,
                        cash_reserved, realized_pnl, position_market_value,
                        conservative_position_value, equity,
                        conservative_equity, unrealized_pnl,
                        conservative_unrealized_pnl, total_pnl,
                        conservative_total_pnl, gross_exposure,
                        high_watermark, drawdown, drawdown_pct, open_positions,
                        unmarkable_positions, nav_complete, metadata
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s::jsonb
                    )
                    ON CONFLICT (strategy_id) DO UPDATE SET
                        observed_at=EXCLUDED.observed_at,
                        initial_cash=EXCLUDED.initial_cash,
                        cash_balance=EXCLUDED.cash_balance,
                        cash_reserved=EXCLUDED.cash_reserved,
                        realized_pnl=EXCLUDED.realized_pnl,
                        position_market_value=EXCLUDED.position_market_value,
                        conservative_position_value=EXCLUDED.conservative_position_value,
                        equity=EXCLUDED.equity,
                        conservative_equity=EXCLUDED.conservative_equity,
                        unrealized_pnl=EXCLUDED.unrealized_pnl,
                        conservative_unrealized_pnl=EXCLUDED.conservative_unrealized_pnl,
                        total_pnl=EXCLUDED.total_pnl,
                        conservative_total_pnl=EXCLUDED.conservative_total_pnl,
                        gross_exposure=EXCLUDED.gross_exposure,
                        high_watermark=EXCLUDED.high_watermark,
                        drawdown=EXCLUDED.drawdown,
                        drawdown_pct=EXCLUDED.drawdown_pct,
                        open_positions=EXCLUDED.open_positions,
                        unmarkable_positions=EXCLUDED.unmarkable_positions,
                        nav_complete=EXCLUDED.nav_complete,
                        metadata=EXCLUDED.metadata,
                        updated_at=clock_timestamp()
                    """,
                    current_rows,
                )
            if history_rows:
                cur.executemany(
                    """
                    INSERT INTO quant.paper_portfolio_nav_snapshots (
                        strategy_id, observed_at, initial_cash, cash_balance,
                        cash_reserved, realized_pnl, position_market_value,
                        conservative_position_value, equity,
                        conservative_equity, unrealized_pnl,
                        conservative_unrealized_pnl, total_pnl,
                        conservative_total_pnl, gross_exposure,
                        high_watermark, drawdown, drawdown_pct, open_positions,
                        unmarkable_positions, nav_complete, metadata
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s::jsonb
                    )
                    """,
                    history_rows,
                )
            conn.commit()
        return results

    def finalize_order_reservation(
        self,
        intent_id: int,
        result: PaperExecutionResult,
        config: TakerExecutionConfig,
    ) -> dict[str, Decimal | str]:
        keep_working = (
            result.intent.order_type in {"GTC", "GTD"}
            and result.status in {"WORKING", "PARTIAL"}
            and result.remaining_size > 0
        )
        desired_cash, desired_shares = (
            estimate_order_reservation(
                result.intent,
                config,
                remaining_size=result.remaining_size,
            )
            if keep_working
            else (Decimal("0"), Decimal("0"))
        )
        return self._update_order_reservation(
            intent_id,
            desired_cash=desired_cash,
            desired_shares=desired_shares,
            release_reason=None
            if keep_working
            else f"terminal:{result.status.lower()}",
        )

    def release_order_reservation(
        self, intent_id: int, *, reason: str
    ) -> dict[str, Decimal | str]:
        return self._update_order_reservation(
            intent_id,
            desired_cash=Decimal("0"),
            desired_shares=Decimal("0"),
            release_reason=reason,
        )

    def release_order_reservation_from_command(
        self,
        intent_id: int,
        *,
        release_event_key: str,
        command_id: str,
        event_ts_ns: int,
        reason: str,
    ) -> dict[str, Decimal | str]:
        """Atomically consume one command release event and free its reservation."""

        values = {
            "release_event_key": str(release_event_key).strip(),
            "command_id": str(command_id).strip(),
            "reason": str(reason).strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"reservation release event missing fields: {missing}")
        if int(event_ts_ns) < 0:
            raise ValueError("event_ts_ns must be non-negative")
        return self._update_order_reservation(
            intent_id,
            desired_cash=Decimal("0"),
            desired_shares=Decimal("0"),
            release_reason=values["reason"],
            release_event={
                **values,
                "event_ts_ns": int(event_ts_ns),
            },
        )

    def reconcile_terminal_reservations(self, *, limit: int = 1000) -> int:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.intent_id, i.status
                FROM quant.paper_order_reservations r
                JOIN quant.paper_live_order_intents i USING (intent_id)
                WHERE r.status='ACTIVE'
                  AND i.status IN ('COMPLETED','FAILED','CANCELED','EXPIRED')
                ORDER BY r.intent_id
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
            rows = [
                (int(row["intent_id"]), str(row["status"])) for row in cur.fetchall()
            ]
        for intent_id, status in rows:
            self.release_order_reservation(
                intent_id,
                reason=f"startup_reconcile_terminal:{status.lower()}",
            )
        return len(rows)

    def reconcile_stale_shadow_reservations(
        self,
        *,
        current_run_id: str,
        timeout_ns: int,
        limit: int = 1000,
    ) -> int:
        """Fail closed on reservations owned by a dead prior shadow run."""

        if not str(current_run_id).strip():
            raise ValueError("current_run_id is required")
        if int(timeout_ns) <= 0:
            raise ValueError("timeout_ns must be positive")
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH clock AS (
                    SELECT floor(
                        extract(epoch FROM clock_timestamp()) * 1000000000
                    )::bigint AS now_ns
                )
                SELECT r.intent_id, c.command_id,
                       c.payload_json->>'run_id' AS prior_run_id,
                       s.heartbeat_ts_ns, clock.now_ns
                FROM quant.paper_order_reservations r
                JOIN quant.paper_live_order_intents i USING (intent_id)
                JOIN quant.paper_inflight_commands c
                  ON c.command_id='paper-shadow:' || r.intent_id::text
                JOIN quant.paper_venue_shadow_runs s
                  ON s.run_id=c.payload_json->>'run_id'
                CROSS JOIN clock
                WHERE r.status='ACTIVE'
                  AND i.status IN ('QUEUED','PROCESSING','WORKING')
                  AND s.run_id<>%s
                  AND (
                      s.status<>'RUNNING'
                      OR s.heartbeat_ts_ns <= clock.now_ns - %s
                  )
                  AND c.state NOT IN (
                      'LOCAL_DENIED','ACKED_MATCHED','TERMINAL'
                  )
                ORDER BY r.intent_id
                LIMIT %s
                """,
                (
                    str(current_run_id),
                    int(timeout_ns),
                    max(1, int(limit)),
                ),
            )
            rows = [dict(row) for row in cur.fetchall()]

        applied = 0
        for row in rows:
            intent_id = int(row["intent_id"])
            command_id = str(row["command_id"])
            prior_run_id = str(row["prior_run_id"])
            heartbeat_ts_ns = int(row["heartbeat_ts_ns"])
            event_ts_ns = int(row["now_ns"])
            key_payload = f"{prior_run_id}|{command_id}|{intent_id}|{heartbeat_ts_ns}"
            release_event_key = (
                "paper-shadow-restart-release:"
                + hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
            )
            result = self.release_order_reservation_from_command(
                intent_id,
                release_event_key=release_event_key,
                command_id=command_id,
                event_ts_ns=event_ts_ns,
                reason=f"restart_reconcile_stale_shadow_run:{prior_run_id}",
            )
            applied += int(str(result.get("application_status")) == "APPLIED")
        return applied

    def _update_order_reservation(
        self,
        intent_id: int,
        *,
        desired_cash: Decimal,
        desired_shares: Decimal,
        release_reason: str | None,
        release_event: dict[str, object] | None = None,
    ) -> dict[str, Decimal | str]:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            if release_event is not None:
                cur.execute(
                    """
                    SELECT application_status, cash_released, shares_released
                    FROM quant.paper_reservation_release_events
                    WHERE release_event_key=%s
                    """,
                    (str(release_event["release_event_key"]),),
                )
                applied = cur.fetchone()
                if applied is not None:
                    conn.commit()
                    return {
                        "status": "ALREADY_APPLIED",
                        "application_status": str(applied["application_status"]),
                        "reserved_cash": Decimal("0"),
                        "reserved_shares": Decimal("0"),
                        "cash_released": Decimal(str(applied["cash_released"])),
                        "shares_released": Decimal(str(applied["shares_released"])),
                    }
                cur.execute(
                    """
                    SELECT intent_id, strategy_id, client_order_id, status,
                           order_state
                    FROM quant.paper_live_order_intents
                    WHERE intent_id=%s
                    FOR UPDATE
                    """,
                    (int(intent_id),),
                )
                intent_row = cur.fetchone()
                if intent_row is None:
                    conn.commit()
                    return {
                        "status": "MISSING_INTENT",
                        "reserved_cash": Decimal("0"),
                        "reserved_shares": Decimal("0"),
                    }
            else:
                intent_row = None
            cur.execute(
                """
                SELECT *
                FROM quant.paper_order_reservations
                WHERE intent_id=%s
                FOR UPDATE
                """,
                (int(intent_id),),
            )
            row = cur.fetchone()
            if row is None:
                conn.commit()
                return {
                    "status": "MISSING",
                    "reserved_cash": Decimal("0"),
                    "reserved_shares": Decimal("0"),
                }
            current_cash = Decimal(str(row["reserved_cash"]))
            current_shares = Decimal(str(row["reserved_shares"]))
            if str(row["status"]) != "ACTIVE":
                if release_event is not None:
                    self._terminalize_order_for_release_event(
                        cur,
                        intent_row,
                        release_event,
                    )
                    cur.execute(
                        """
                        INSERT INTO quant.paper_reservation_release_events (
                            release_event_key, command_id, intent_id, reason,
                            event_ts_ns, application_status
                        ) VALUES (%s,%s,%s,%s,%s,'ALREADY_RELEASED')
                        ON CONFLICT (release_event_key) DO NOTHING
                        """,
                        (
                            str(release_event["release_event_key"]),
                            str(release_event["command_id"]),
                            int(intent_id),
                            str(release_event["reason"]),
                            int(release_event["event_ts_ns"]),
                        ),
                    )
                conn.commit()
                return {
                    "status": str(row["status"]),
                    "application_status": "ALREADY_RELEASED",
                    "reserved_cash": current_cash,
                    "reserved_shares": current_shares,
                }
            desired_cash = min(current_cash, max(Decimal("0"), desired_cash))
            desired_shares = min(current_shares, max(Decimal("0"), desired_shares))
            cash_release = current_cash - desired_cash
            shares_release = current_shares - desired_shares
            if cash_release:
                cur.execute(
                    """
                    UPDATE quant.paper_accounts
                    SET cash_reserved=GREATEST(0, cash_reserved - %s),
                        updated_at=clock_timestamp()
                    WHERE strategy_id=%s
                    """,
                    (cash_release, str(row["strategy_id"])),
                )
            if shares_release:
                cur.execute(
                    """
                    UPDATE quant.paper_positions
                    SET reserved_quantity=GREATEST(0, reserved_quantity - %s),
                        updated_at=clock_timestamp()
                    WHERE strategy_id=%s AND asset_id=%s
                    """,
                    (shares_release, str(row["strategy_id"]), str(row["asset_id"])),
                )
            released = desired_cash == 0 and desired_shares == 0
            cur.execute(
                """
                UPDATE quant.paper_order_reservations
                SET reserved_cash=%s, reserved_shares=%s,
                    status=%s, release_reason=%s,
                    released_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                    updated_at=clock_timestamp()
                WHERE intent_id=%s
                """,
                (
                    desired_cash,
                    desired_shares,
                    "RELEASED" if released else "ACTIVE",
                    release_reason if released else None,
                    released,
                    int(intent_id),
                ),
            )
            if release_event is not None:
                self._terminalize_order_for_release_event(
                    cur,
                    intent_row,
                    release_event,
                )
                cur.execute(
                    """
                    INSERT INTO quant.paper_reservation_release_events (
                        release_event_key, command_id, intent_id, reason,
                        event_ts_ns, application_status, cash_released,
                        shares_released
                    ) VALUES (%s,%s,%s,%s,%s,'APPLIED',%s,%s)
                    ON CONFLICT (release_event_key) DO NOTHING
                    """,
                    (
                        str(release_event["release_event_key"]),
                        str(release_event["command_id"]),
                        int(intent_id),
                        str(release_event["reason"]),
                        int(release_event["event_ts_ns"]),
                        cash_release,
                        shares_release,
                    ),
                )
            conn.commit()
        result: dict[str, Decimal | str] = {
            "status": "RELEASED" if released else "ACTIVE",
            "reserved_cash": desired_cash,
            "reserved_shares": desired_shares,
        }
        if release_event is not None:
            result.update(
                {
                    "application_status": "APPLIED",
                    "cash_released": cash_release,
                    "shares_released": shares_release,
                }
            )
        return result

    @staticmethod
    def _terminalize_order_for_release_event(
        cur: Any,
        intent_row: Any,
        release_event: dict[str, object],
    ) -> bool:
        if intent_row is None or str(intent_row["status"]) not in {
            "QUEUED",
            "PROCESSING",
            "WORKING",
        }:
            return False
        intent_id = int(intent_row["intent_id"])
        event_ts_ns = int(release_event["event_ts_ns"])
        reason = str(release_event["reason"])
        cur.execute(
            """
            UPDATE quant.paper_live_order_intents
            SET status='CANCELED', order_state='CANCELED',
                cancel_ack_ts=to_timestamp(%s::numeric / 1000000000),
                completed_at=to_timestamp(%s::numeric / 1000000000),
                last_error=%s, updated_at=clock_timestamp()
            WHERE intent_id=%s
              AND status IN ('QUEUED','PROCESSING','WORKING')
            """,
            (event_ts_ns, event_ts_ns, reason[:1000], intent_id),
        )
        cur.execute(
            """
            INSERT INTO quant.paper_order_events (
                idempotency_key, intent_id, strategy_id, client_order_id,
                event_type, from_state, to_state, reason, payload, event_ts
            ) VALUES (
                %s,%s,%s,%s,'HEARTBEAT_AUTO_CANCEL',%s,'CANCELED',%s,
                %s::jsonb,to_timestamp(%s::numeric / 1000000000)
            ) ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                f"paper-order:{intent_id}:command-release:"
                f"{release_event['release_event_key']}",
                intent_id,
                str(intent_row["strategy_id"]),
                str(intent_row["client_order_id"]),
                str(intent_row["order_state"]),
                reason,
                json.dumps(
                    {
                        "command_id": str(release_event["command_id"]),
                        "release_event_key": str(release_event["release_event_key"]),
                    }
                ),
                event_ts_ns,
            ),
        )
        return True

    def append(self, result: PaperExecutionResult) -> None:
        intent = result.intent
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            insert_paper_audit(cur, result)
            cur.execute(
                """
                INSERT INTO quant.paper_portfolio_applied_results (
                    audit_key, strategy_id, client_order_id, status, filled_size
                ) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (audit_key) DO NOTHING
                RETURNING audit_key
                """,
                (
                    result.audit_key,
                    intent.strategy_id,
                    intent.client_order_id,
                    result.status,
                    result.filled_size,
                ),
            )
            if cur.fetchone() is None:
                conn.commit()
                return
            if not result.fills:
                conn.commit()
                return

            _persist_execution_finality(
                cur,
                result,
                outcome=self.synthetic_finality_outcome,
            )
            if self.synthetic_finality_outcome == "FAILED_REVERSED":
                conn.commit()
                return
            state = self._lock_portfolio(cur, result)
            if state.position_size > 0:
                CompleteSetLotStore.ensure_position_coverage(
                    cur,
                    strategy_id=intent.strategy_id,
                    market_id=intent.market_id,
                    condition_id=intent.condition_id,
                    asset_id=intent.asset_id,
                    quantity=state.position_size,
                    cost_basis=state.cost_basis,
                    observed_at=result.arrival_ts,
                )
            mutation = apply_execution_result(state, result)
            for entry in mutation.entries:
                fill = result.fills[entry.fill_index]
                fill_ref = f"fill:{result.audit_key}:{entry.fill_index}"
                if intent.side == "BUY":
                    CompleteSetLotStore.create_lot(
                        cur,
                        strategy_id=intent.strategy_id,
                        market_id=intent.market_id,
                        condition_id=intent.condition_id,
                        provenance=CompleteSetProvenance.SEPARATE_MARKET_BUYS,
                        created_by_type="PAPER_FILL",
                        source_ref=fill_ref,
                        legs={
                            intent.asset_id: CompleteSetLotLegInput(
                                quantity=fill.size,
                                cost_basis=fill.price * fill.size + fill.fee,
                            )
                        },
                        created_at=result.arrival_ts,
                        metadata={
                            "audit_key": result.audit_key,
                            "fill_index": entry.fill_index,
                            "side": intent.side,
                        },
                    )
                    CompleteSetLotStore.pair_separate_market_buys(
                        cur,
                        strategy_id=intent.strategy_id,
                        market_id=intent.market_id,
                        condition_id=intent.condition_id,
                        paired_at=result.arrival_ts,
                    )
                else:
                    CompleteSetLotStore.consume_assets(
                        cur,
                        consumption_id=f"paper-fill-consumption:{result.audit_key}:"
                        f"{entry.fill_index}",
                        strategy_id=intent.strategy_id,
                        market_id=intent.market_id,
                        condition_id=intent.condition_id,
                        consumer_type="PAPER_SELL",
                        consumer_ref=fill_ref,
                        quantities={intent.asset_id: fill.size},
                        expected_basis_by_asset={
                            intent.asset_id: entry.cash_delta
                            - entry.realized_pnl_delta
                        },
                        cash_delta=entry.cash_delta,
                        realized_pnl_delta=entry.realized_pnl_delta,
                        consumed_at=result.arrival_ts,
                        metadata={
                            "audit_key": result.audit_key,
                            "fill_index": entry.fill_index,
                            "side": intent.side,
                        },
                    )
                evidence_id = fill.settlement_evidence_id or (
                    f"paper-l2:{result.audit_key}:{entry.fill_index}"
                )
                if fill.settlement_match_type == CtfSettlementMatchType.UNKNOWN.value:
                    settlement_audit = unknown_ctf_settlement_audit(
                        evidence_id=evidence_id,
                        evidence_source=fill.settlement_evidence_source,
                        evidence={
                            "arrival_checkpoint_id": result.arrival_checkpoint_id,
                            "reason": "paper fill has no counterparty order evidence",
                        },
                    )
                    conservation_status = settlement_audit.conservation.status
                    conservation_hash = settlement_audit.conservation.conservation_hash
                else:
                    try:
                        CtfSettlementMatchType(fill.settlement_match_type)
                    except ValueError as exc:
                        raise PaperLedgerError(
                            "unsupported fill settlement match type:"
                            f"{fill.settlement_match_type}"
                        ) from exc
                    if not fill.settlement_conservation_hash:
                        raise PaperLedgerError(
                            "classified CTF fill requires a conservation hash"
                        )
                    settlement_audit = None
                    conservation_status = fill.settlement_conservation_status
                    conservation_hash = fill.settlement_conservation_hash
                cur.execute(
                    """
                    INSERT INTO quant.paper_fills (
                        audit_key, fill_index, strategy_id, client_order_id,
                        market_id, condition_id, asset_id, side, price, size,
                        notional, fee, arrival_ts, arrival_checkpoint_id,
                        settlement_match_type,settlement_evidence_id,
                        settlement_evidence_source,settlement_conservation_status,
                        settlement_conservation_hash
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s
                    )
                    ON CONFLICT (audit_key, fill_index) DO NOTHING
                    """,
                    (
                        result.audit_key,
                        entry.fill_index,
                        intent.strategy_id,
                        intent.client_order_id,
                        intent.market_id,
                        intent.condition_id,
                        intent.asset_id,
                        intent.side,
                        fill.price,
                        fill.size,
                        fill.price * fill.size,
                        fill.fee,
                        result.arrival_ts,
                        result.arrival_checkpoint_id,
                        fill.settlement_match_type,
                        evidence_id,
                        fill.settlement_evidence_source,
                        conservation_status,
                        conservation_hash,
                    ),
                )
                if fill.fee_charge_id is not None:
                    if fill.fee != fill.platform_fee + fill.builder_fee:
                        raise PaperLedgerError(
                            "fill fee does not equal platform plus builder fee"
                        )
                    cur.execute(
                        """
                        INSERT INTO quant.paper_fill_fee_charges (
                            fee_charge_id,audit_key,fill_index,strategy_id,fill_id,
                            asset_id,condition_id,liquidity_role,price,shares,
                            platform_fee_rate,platform_fee_exponent,platform_fee,
                            builder_code,builder_fee_rate_bps,builder_fee,total_fee,
                            rounding_policy,fee_schedule_id,economics_regime_id,source
                        ) VALUES (
                            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            %s,%s,%s,%s,%s
                        )
                        ON CONFLICT (fee_charge_id) DO NOTHING
                        """,
                        (
                            fill.fee_charge_id,
                            result.audit_key,
                            entry.fill_index,
                            intent.strategy_id,
                            f"{result.audit_key}:{entry.fill_index}",
                            intent.asset_id,
                            intent.condition_id,
                            "MAKER" if intent.post_only else "TAKER",
                            fill.price,
                            fill.size,
                            fill.platform_fee_rate,
                            fill.platform_fee_exponent,
                            fill.platform_fee,
                            intent.builder_code,
                            fill.builder_fee_rate_bps,
                            fill.builder_fee,
                            fill.fee,
                            fill.rounding_policy,
                            fill.fee_schedule_id,
                            fill.economics_regime_id,
                            fill.fee_source,
                        ),
                    )
                if settlement_audit is not None:
                    self._insert_ctf_settlement_audit(
                        cur,
                        audit_key=result.audit_key,
                        fill_index=entry.fill_index,
                        audit=settlement_audit,
                        observed_at=result.arrival_ts,
                    )
                cur.execute(
                    """
                    INSERT INTO quant.paper_ledger_entries (
                        idempotency_key, strategy_id, audit_key, client_order_id,
                        event_type, market_id, condition_id, asset_id, event_ts,
                        price, shares_delta, cash_delta, fee, realized_pnl_delta,
                        cash_after, position_after, cost_basis_after, metadata
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (
                        f"fill:{result.audit_key}:{entry.fill_index}",
                        intent.strategy_id,
                        result.audit_key,
                        intent.client_order_id,
                        entry.event_type,
                        intent.market_id,
                        intent.condition_id,
                        intent.asset_id,
                        result.arrival_ts,
                        entry.price,
                        entry.shares_delta,
                        entry.cash_delta,
                        entry.fee,
                        entry.realized_pnl_delta,
                        entry.cash_after,
                        entry.position_after,
                        entry.cost_basis_after,
                        json.dumps(
                            {
                                "arrival_checkpoint_id": result.arrival_checkpoint_id,
                                "coverage_grade": result.coverage_grade,
                                "model_version": result.model_version,
                                "settlement_match_type": fill.settlement_match_type,
                                "settlement_evidence_id": evidence_id,
                                "settlement_conservation_status": conservation_status,
                                "settlement_conservation_hash": conservation_hash,
                            }
                        ),
                    ),
                )

            self._persist_state(cur, result, mutation)
            if self.transaction_fault_hook is not None:
                self.transaction_fault_hook("before_commit", result)
            conn.commit()

    def reconcile_fill_ctf_settlement(
        self,
        *,
        audit_key: str,
        fill_index: int,
        audit: CtfSettlementAudit,
        observed_at: datetime | None = None,
    ) -> dict[str, str]:
        """Attach stronger counterparty/on-chain evidence to a persisted paper fill."""

        at = observed_at or datetime.now(timezone.utc)
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT settlement_match_type,settlement_evidence_id,
                       settlement_conservation_hash
                FROM quant.paper_fills
                WHERE audit_key=%s AND fill_index=%s FOR UPDATE
                """,
                (audit_key, int(fill_index)),
            )
            current = cur.fetchone()
            if current is None:
                raise PaperLedgerError("unknown paper fill for CTF reconciliation")
            current_type = CtfSettlementMatchType(
                str(current["settlement_match_type"])
            )
            next_type = audit.settlement_match_type
            if (
                current_type is not CtfSettlementMatchType.UNKNOWN
                and current_type is not next_type
            ):
                raise PaperLedgerError(
                    "conflicting definitive CTF settlement classifications"
                )
            self._insert_ctf_settlement_audit(
                cur,
                audit_key=audit_key,
                fill_index=int(fill_index),
                audit=audit,
                observed_at=at,
            )
            cur.execute(
                """
                UPDATE quant.paper_fills
                SET settlement_match_type=%s,
                    settlement_evidence_id=%s,
                    settlement_evidence_source=%s,
                    settlement_conservation_status=%s,
                    settlement_conservation_hash=%s
                WHERE audit_key=%s AND fill_index=%s
                """,
                (
                    next_type.value,
                    audit.evidence_id,
                    audit.evidence_source,
                    audit.conservation.status,
                    audit.conservation.conservation_hash,
                    audit_key,
                    int(fill_index),
                ),
            )
            CompleteSetLotStore.annotate_fill_settlement(
                cur,
                audit_key=audit_key,
                fill_index=int(fill_index),
                settlement_match_type=next_type.value,
            )
            conn.commit()
        return {
            "audit_key": audit_key,
            "fill_index": str(int(fill_index)),
            "settlement_match_type": next_type.value,
            "conservation_status": audit.conservation.status,
            "conservation_hash": audit.conservation.conservation_hash,
        }

    def reconcile_official_fill_fee(
        self,
        *,
        audit_key: str,
        official_total_fee: Decimal,
        evidence_id: str,
        evidence_sha256: str,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Apply an authoritative on-chain fee without rewriting the paper fill."""

        official_fee = Decimal(official_total_fee)
        if official_fee < 0:
            raise ValueError("official fill fee cannot be negative")
        if not str(evidence_id).strip() or len(str(evidence_sha256).strip()) != 64:
            raise ValueError("official fee evidence identity and sha256 are required")
        at = observed_at or datetime.now(timezone.utc)
        if at.tzinfo is None:
            raise ValueError("official fee observed_at must be timezone-aware")

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT charge.fill_index,charge.strategy_id,charge.asset_id,
                       charge.condition_id,charge.total_fee,
                       charge.reconciliation_status,charge.official_total_fee,
                       charge.official_evidence_id,
                       charge.official_evidence_sha256,
                       fill.market_id,fill.side,fill.size
                FROM quant.paper_fill_fee_charges charge
                JOIN quant.paper_fills fill
                  ON fill.audit_key=charge.audit_key
                 AND fill.fill_index=charge.fill_index
                WHERE charge.audit_key=%s
                ORDER BY charge.fill_index
                FOR UPDATE OF charge,fill
                """,
                (str(audit_key),),
            )
            rows = [dict(row) for row in cur.fetchall()]
            if not rows:
                raise PaperLedgerError("paper fee charge not found for reconciliation")
            identity = {
                (
                    str(row["strategy_id"]),
                    str(row["asset_id"]),
                    str(row["condition_id"]),
                    str(row["market_id"]),
                    str(row["side"]).upper(),
                )
                for row in rows
            }
            if len(identity) != 1:
                raise PaperLedgerError("paper fee charges span multiple order identities")
            strategy_id, asset_id, condition_id, market_id, side = identity.pop()
            modeled_fees = [Decimal(str(row["total_fee"])) for row in rows]
            modeled_total = sum(modeled_fees, Decimal(0))
            statuses = {str(row["reconciliation_status"]) for row in rows}
            if statuses != {"UNRECONCILED"}:
                if not statuses <= {"FINALITY_EXACT", "FINALITY_CORRECTED"}:
                    raise PaperLedgerError("paper fee charges have mixed reconciliation state")
                replay_total = sum(
                    (Decimal(str(row["official_total_fee"])) for row in rows),
                    Decimal(0),
                )
                if (
                    replay_total != official_fee
                    or any(
                        str(row.get("official_evidence_id") or "") != str(evidence_id)
                        or str(row.get("official_evidence_sha256") or "")
                        != str(evidence_sha256)
                        for row in rows
                    )
                ):
                    raise PaperLedgerError("conflicting official fee finality evidence")
                return {
                    "status": next(iter(statuses)),
                    "audit_key": str(audit_key),
                    "modeled_total_fee": format(modeled_total, "f"),
                    "official_total_fee": format(official_fee, "f"),
                    "cash_correction": format(modeled_total - official_fee, "f"),
                    "evidence_id": str(evidence_id),
                    "evidence_sha256": str(evidence_sha256),
                    "replayed": True,
                }

            official_allocations = _allocate_official_fee(
                official_fee,
                modeled_fees,
            )
            cash_corrections = [
                modeled - official
                for modeled, official in zip(
                    modeled_fees, official_allocations, strict=True
                )
            ]
            cash_correction = sum(cash_corrections, Decimal(0))
            status = "FINALITY_EXACT" if cash_correction == 0 else "FINALITY_CORRECTED"

            cur.execute(
                """
                SELECT account.cash_balance,
                       position.quantity,position.cost_basis,position.realized_pnl
                FROM quant.paper_accounts account
                JOIN quant.paper_positions position
                  ON position.strategy_id=account.strategy_id
                WHERE account.strategy_id=%s AND position.asset_id=%s
                FOR UPDATE OF account,position
                """,
                (strategy_id, asset_id),
            )
            state = cur.fetchone()
            if state is None:
                raise PaperLedgerError("paper portfolio missing for fee reconciliation")
            position_quantity = Decimal(str(state["quantity"]))
            cost_basis = Decimal(str(state["cost_basis"]))
            realized_delta = Decimal(0)
            cost_basis_delta = Decimal(0)

            if cash_correction and side == "BUY":
                for row, modeled, official in zip(
                    rows, modeled_fees, official_allocations, strict=True
                ):
                    fill_index = int(row["fill_index"])
                    cur.execute(
                        """
                        SELECT lot.lot_id,lot.paired,leg.original_quantity,
                               leg.remaining_quantity,leg.original_cost_basis,
                               leg.remaining_cost_basis
                        FROM quant.simulator_complete_set_lots lot
                        JOIN quant.simulator_complete_set_lot_legs leg
                          ON leg.lot_id=lot.lot_id AND leg.asset_id=%s
                        WHERE lot.strategy_id=%s
                          AND lot.created_by_type='PAPER_FILL'
                          AND lot.source_ref=%s
                        FOR UPDATE OF lot,leg
                        """,
                        (asset_id, strategy_id, f"fill:{audit_key}:{fill_index}"),
                    )
                    lot = cur.fetchone()
                    if (
                        lot is None
                        or bool(lot["paired"])
                        or Decimal(str(lot["remaining_quantity"]))
                        != Decimal(str(lot["original_quantity"]))
                    ):
                        raise PaperLedgerError(
                            "BUY fee finality must be reconciled before lot consumption"
                        )
                    basis_delta = official - modeled
                    if Decimal(str(lot["remaining_cost_basis"])) + basis_delta < 0:
                        raise PaperLedgerError("official fee would create negative lot basis")
                    cur.execute(
                        """
                        UPDATE quant.simulator_complete_set_lot_legs
                        SET original_cost_basis=original_cost_basis + %s,
                            remaining_cost_basis=remaining_cost_basis + %s,
                            updated_at=clock_timestamp()
                        WHERE lot_id=%s AND asset_id=%s
                        """,
                        (basis_delta, basis_delta, lot["lot_id"], asset_id),
                    )
                    cur.execute(
                        """
                        UPDATE quant.simulator_complete_set_lots
                        SET original_joint_cost_basis=original_joint_cost_basis + %s,
                            remaining_joint_cost_basis=remaining_joint_cost_basis + %s,
                            metadata=metadata || %s::jsonb,
                            updated_at=clock_timestamp()
                        WHERE lot_id=%s
                        """,
                        (
                            basis_delta,
                            basis_delta,
                            json.dumps(
                                {
                                    "fee_finality_evidence_id": str(evidence_id),
                                    "fee_finality_evidence_sha256": str(
                                        evidence_sha256
                                    ),
                                },
                                sort_keys=True,
                            ),
                            lot["lot_id"],
                        ),
                    )
                    cost_basis_delta += basis_delta
            elif cash_correction and side == "SELL":
                realized_delta = cash_correction
                for row, correction in zip(rows, cash_corrections, strict=True):
                    cur.execute(
                        """
                        UPDATE quant.simulator_complete_set_consumptions
                        SET cash_delta=cash_delta + %s,
                            realized_pnl_delta=realized_pnl_delta + %s,
                            metadata=metadata || %s::jsonb,
                            updated_at=clock_timestamp()
                        WHERE consumption_id=%s
                        """,
                        (
                            correction,
                            correction,
                            json.dumps(
                                {
                                    "fee_finality_evidence_id": str(evidence_id),
                                    "fee_finality_evidence_sha256": str(
                                        evidence_sha256
                                    ),
                                },
                                sort_keys=True,
                            ),
                            f"paper-fill-consumption:{audit_key}:{int(row['fill_index'])}",
                        ),
                    )
                    if cur.rowcount != 1:
                        raise PaperLedgerError(
                            "paper SELL consumption missing for fee reconciliation"
                        )
            elif side not in {"BUY", "SELL"}:
                raise PaperLedgerError(f"unsupported paper fill side: {side}")

            next_cash = Decimal(str(state["cash_balance"])) + cash_correction
            next_cost_basis = cost_basis + cost_basis_delta
            next_realized = Decimal(str(state["realized_pnl"])) + realized_delta
            if next_cost_basis < 0:
                raise PaperLedgerError("official fee would create negative position basis")
            if cash_correction:
                cur.execute(
                    """
                    INSERT INTO quant.paper_ledger_entries (
                        idempotency_key,strategy_id,audit_key,event_type,market_id,
                        condition_id,asset_id,event_ts,shares_delta,cash_delta,fee,
                        realized_pnl_delta,cash_after,position_after,cost_basis_after,
                        metadata
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING entry_id
                    """,
                    (
                        f"fee-finality:{audit_key}",
                        strategy_id,
                        str(audit_key),
                        f"{side}_FEE_FINALITY_ADJUSTMENT",
                        market_id,
                        condition_id,
                        asset_id,
                        at.astimezone(timezone.utc),
                        cash_correction,
                        official_fee - modeled_total,
                        realized_delta,
                        next_cash,
                        position_quantity,
                        next_cost_basis,
                        json.dumps(
                            {
                                "modeled_total_fee": format(modeled_total, "f"),
                                "official_total_fee": format(official_fee, "f"),
                                "evidence_id": str(evidence_id),
                                "evidence_sha256": str(evidence_sha256),
                            },
                            sort_keys=True,
                        ),
                    ),
                )
                if cur.fetchone() is None:
                    raise PaperLedgerError("official fee correction already exists")
                cur.execute(
                    """
                    UPDATE quant.paper_accounts
                    SET cash_balance=%s,realized_pnl=realized_pnl + %s,
                        updated_at=clock_timestamp()
                    WHERE strategy_id=%s
                    """,
                    (next_cash, realized_delta, strategy_id),
                )
                cur.execute(
                    """
                    UPDATE quant.paper_positions
                    SET cost_basis=%s,realized_pnl=%s,updated_at=clock_timestamp()
                    WHERE strategy_id=%s AND asset_id=%s
                    """,
                    (next_cost_basis, next_realized, strategy_id, asset_id),
                )

            for row, allocation in zip(rows, official_allocations, strict=True):
                cur.execute(
                    """
                    UPDATE quant.paper_fill_fee_charges
                    SET reconciliation_status=%s,official_total_fee=%s,
                        official_evidence_id=%s,official_evidence_sha256=%s,
                        reconciled_at=%s
                    WHERE audit_key=%s AND fill_index=%s
                    """,
                    (
                        status,
                        allocation,
                        str(evidence_id),
                        str(evidence_sha256),
                        at.astimezone(timezone.utc),
                        str(audit_key),
                        int(row["fill_index"]),
                    ),
                )
            conn.commit()
        return {
            "status": status,
            "audit_key": str(audit_key),
            "modeled_total_fee": format(modeled_total, "f"),
            "official_total_fee": format(official_fee, "f"),
            "cash_correction": format(cash_correction, "f"),
            "realized_pnl_correction": format(realized_delta, "f"),
            "cost_basis_correction": format(cost_basis_delta, "f"),
            "evidence_id": str(evidence_id),
            "evidence_sha256": str(evidence_sha256),
            "replayed": False,
        }

    @staticmethod
    def _insert_ctf_settlement_audit(
        cur: Any,
        *,
        audit_key: str,
        fill_index: int,
        audit: CtfSettlementAudit,
        observed_at: datetime,
    ) -> None:
        payload = {
            **dict(audit.evidence),
            "conservation_reason": audit.conservation.reason,
            "expected": dict(audit.conservation.expected),
            "observed": dict(audit.conservation.observed),
        }
        cur.execute(
            """
            INSERT INTO quant.paper_fill_ctf_settlement_audits (
                audit_key,fill_index,evidence_id,settlement_match_type,
                evidence_source,conservation_status,conservation_hash,
                evidence,observed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (audit_key,fill_index,evidence_id) DO NOTHING
            RETURNING evidence_id
            """,
            (
                audit_key,
                int(fill_index),
                audit.evidence_id,
                audit.settlement_match_type.value,
                audit.evidence_source,
                audit.conservation.status,
                audit.conservation.conservation_hash,
                json.dumps(payload, sort_keys=True, default=str),
                observed_at,
            ),
        )
        if cur.fetchone() is not None:
            return
        cur.execute(
            """
            SELECT settlement_match_type,evidence_source,conservation_status,
                   conservation_hash
            FROM quant.paper_fill_ctf_settlement_audits
            WHERE audit_key=%s AND fill_index=%s AND evidence_id=%s
            """,
            (audit_key, int(fill_index), audit.evidence_id),
        )
        existing = cur.fetchone()
        expected = (
            audit.settlement_match_type.value,
            audit.evidence_source,
            audit.conservation.status,
            audit.conservation.conservation_hash,
        )
        actual = (
            str(existing["settlement_match_type"]),
            str(existing["evidence_source"]),
            str(existing["conservation_status"]),
            str(existing["conservation_hash"]),
        )
        if actual != expected:
            raise PaperLedgerError("CTF settlement evidence id collision")

    def settle_resolved_positions(
        self,
        *,
        limit: int = 100,
        strategy_id: str | None = None,
        require_redeemed: bool = True,
    ) -> int:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (p.strategy_id, p.asset_id)
                       p.strategy_id, p.asset_id, p.market_id, p.condition_id,
                       p.reserved_quantity,
                       m.winning_asset_id, m.resolution_source, m.resolved_time,
                       COALESCE(sp.payout_per_share,
                           CASE WHEN p.asset_id=m.winning_asset_id THEN 1 ELSE 0 END
                       ) AS payout_per_share,
                       sp.truth_hash
                FROM quant.paper_positions p
                JOIN quant.paper_execution_market_catalog m
                  ON m.asset_id=p.asset_id
                LEFT JOIN LATERAL (
                    SELECT payout_per_share, truth_hash
                    FROM quant.market_settlement_payouts payout
                    WHERE payout.condition_id=p.condition_id
                      AND payout.asset_id=p.asset_id
                    ORDER BY oracle_finalized_at DESC, created_at DESC
                    LIMIT 1
                ) sp ON TRUE
                LEFT JOIN quant.market_resolution_states rs
                  ON rs.condition_id=p.condition_id
                WHERE p.quantity > 0
                  AND (%s::text IS NULL OR p.strategy_id=%s)
                  AND m.resolved=TRUE
                  AND m.resolution_status='RESOLVED'
                  AND m.winning_asset_id IS NOT NULL
                  AND (
                    (%s::boolean=TRUE AND rs.phase='REDEEMED')
                    OR (
                      %s::boolean=FALSE
                      AND (
                        rs.condition_id IS NULL
                        OR rs.phase IN ('FINALIZED','REDEEMABLE','REDEEMED')
                      )
                    )
                  )
                ORDER BY p.strategy_id, p.asset_id, m.synced_at DESC
                LIMIT %s
                """,
                (
                    str(strategy_id) if strategy_id else None,
                    str(strategy_id) if strategy_id else None,
                    bool(require_redeemed),
                    bool(require_redeemed),
                    max(1, int(limit)),
                ),
            )
            candidates = [dict(row) for row in cur.fetchall()]
            changed = 0
            for candidate in candidates:
                strategy_id = str(candidate["strategy_id"])
                asset_id = str(candidate["asset_id"])
                self._ensure_account(cur, strategy_id)
                cur.execute(
                    """
                    SELECT a.cash_balance, a.realized_pnl AS account_realized_pnl,
                           p.quantity, p.cost_basis, p.realized_pnl
                    FROM quant.paper_accounts a
                    JOIN quant.paper_positions p
                      ON p.strategy_id=a.strategy_id
                    WHERE a.strategy_id=%s AND p.asset_id=%s
                    FOR UPDATE OF a, p
                    """,
                    (strategy_id, asset_id),
                )
                row = cur.fetchone()
                if row is None or Decimal(str(row["quantity"])) <= 0:
                    continue
                if Decimal(str(candidate["reserved_quantity"])) > 0:
                    continue
                resolved_at = candidate.get("resolved_time")
                settlement_key = _settlement_key(
                    strategy_id,
                    asset_id,
                    str(candidate["winning_asset_id"]),
                    resolved_at,
                )
                cur.execute(
                    """
                    INSERT INTO quant.paper_settlements (
                        settlement_key, strategy_id, market_id, condition_id,
                        asset_id, winning_asset_id, quantity, payout_per_share,
                        cash_delta, realized_pnl_delta, resolution_source, resolved_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,%s)
                    ON CONFLICT (settlement_key) DO NOTHING
                    RETURNING settlement_key
                    """,
                    (
                        settlement_key,
                        strategy_id,
                        str(candidate["market_id"]),
                        str(candidate["condition_id"]),
                        asset_id,
                        str(candidate["winning_asset_id"]),
                        row["quantity"],
                        Decimal(str(candidate["payout_per_share"])),
                        candidate.get("resolution_source"),
                        resolved_at,
                    ),
                )
                if cur.fetchone() is None:
                    continue
                state = PaperPortfolioState(
                    cash_balance=Decimal(str(row["cash_balance"])),
                    position_size=Decimal(str(row["quantity"])),
                    cost_basis=Decimal(str(row["cost_basis"])),
                    realized_pnl=Decimal(str(row["realized_pnl"])),
                )
                after, payout, realized_delta = apply_settlement(
                    state,
                    payout_per_share=Decimal(str(candidate["payout_per_share"])),
                )
                CompleteSetLotStore.ensure_position_coverage(
                    cur,
                    strategy_id=strategy_id,
                    market_id=str(candidate["market_id"]),
                    condition_id=str(candidate["condition_id"]),
                    asset_id=asset_id,
                    quantity=state.position_size,
                    cost_basis=state.cost_basis,
                    observed_at=resolved_at or datetime.now(timezone.utc),
                )
                CompleteSetLotStore.consume_assets(
                    cur,
                    consumption_id=f"paper-settlement-consumption:{settlement_key}",
                    strategy_id=strategy_id,
                    market_id=str(candidate["market_id"]),
                    condition_id=str(candidate["condition_id"]),
                    consumer_type="SETTLEMENT",
                    consumer_ref=settlement_key,
                    quantities={asset_id: state.position_size},
                    expected_basis_by_asset={asset_id: state.cost_basis},
                    cash_delta=payout,
                    realized_pnl_delta=realized_delta,
                    consumed_at=resolved_at or datetime.now(timezone.utc),
                    metadata={
                        "winning_asset_id": str(candidate["winning_asset_id"]),
                        "payout_per_share": str(candidate["payout_per_share"]),
                    },
                )
                cur.execute(
                    """
                    UPDATE quant.paper_settlements
                    SET cash_delta=%s, realized_pnl_delta=%s
                    WHERE settlement_key=%s
                    """,
                    (payout, realized_delta, settlement_key),
                )
                cur.execute(
                    """
                    UPDATE quant.paper_accounts
                    SET cash_balance=%s, realized_pnl=realized_pnl + %s,
                        updated_at=clock_timestamp()
                    WHERE strategy_id=%s
                    """,
                    (after.cash_balance, realized_delta, strategy_id),
                )
                cur.execute(
                    """
                    UPDATE quant.paper_positions
                    SET quantity=0, cost_basis=0, realized_pnl=%s,
                        settled_at=clock_timestamp(), updated_at=clock_timestamp()
                    WHERE strategy_id=%s AND asset_id=%s
                    """,
                    (after.realized_pnl, strategy_id, asset_id),
                )
                cur.execute(
                    """
                    INSERT INTO quant.paper_ledger_entries (
                        idempotency_key, strategy_id, event_type, market_id,
                        condition_id, asset_id, event_ts, price, shares_delta,
                        cash_delta, realized_pnl_delta, cash_after, position_after,
                        cost_basis_after, metadata
                    ) VALUES (%s,%s,'SETTLEMENT',%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s::jsonb)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (
                        settlement_key,
                        strategy_id,
                        str(candidate["market_id"]),
                        str(candidate["condition_id"]),
                        asset_id,
                        resolved_at or datetime.now().astimezone(),
                        Decimal(str(candidate["payout_per_share"])),
                        -state.position_size,
                        payout,
                        realized_delta,
                        after.cash_balance,
                        json.dumps(
                            {
                                "winning_asset_id": str(candidate["winning_asset_id"]),
                                "resolution_source": candidate.get("resolution_source"),
                                "payout_per_share": str(candidate["payout_per_share"]),
                                "truth_hash": candidate.get("truth_hash"),
                            }
                        ),
                    ),
                )
                changed += 1
            conn.commit()
        return changed

    def merge_complete_set(
        self,
        *,
        merge_id: str,
        strategy_id: str,
        market_id: str,
        condition_id: str,
        yes_asset_id: str,
        no_asset_id: str,
        quantity: Decimal,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Idempotently merge equal paper YES/NO positions into paper cash."""

        if not merge_id or not strategy_id or not market_id or not condition_id:
            raise ValueError("merge identifiers must be non-empty")
        if not yes_asset_id or not no_asset_id or yes_asset_id == no_asset_id:
            raise ValueError("merge requires distinct YES and NO asset ids")
        if quantity <= 0:
            raise ValueError("merge quantity must be positive")

        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            self._ensure_account(cur, strategy_id)
            cur.execute(
                """
                SELECT cash_balance
                FROM quant.paper_accounts
                WHERE strategy_id=%s
                FOR UPDATE
                """,
                (str(strategy_id),),
            )
            account = cur.fetchone()
            assert account is not None
            cur.execute(
                """
                SELECT asset_id, quantity, reserved_quantity, cost_basis,
                       realized_pnl
                FROM quant.paper_positions
                WHERE strategy_id=%s AND asset_id IN (%s,%s)
                ORDER BY asset_id
                FOR UPDATE
                """,
                (str(strategy_id), str(yes_asset_id), str(no_asset_id)),
            )
            positions = {str(row["asset_id"]): row for row in cur.fetchall()}
            if yes_asset_id not in positions or no_asset_id not in positions:
                raise PaperLedgerError("complete_set_positions_not_found")

            cur.execute(
                """
                SELECT merge_id, quantity, cash_delta, realized_pnl_delta
                FROM quant.paper_complete_set_merges
                WHERE merge_id=%s
                """,
                (str(merge_id),),
            )
            existing = cur.fetchone()
            if existing is not None:
                conn.commit()
                return {
                    "status": "ALREADY_APPLIED",
                    "merge_id": str(existing["merge_id"]),
                    "quantity": Decimal(str(existing["quantity"])),
                    "cash_delta": Decimal(str(existing["cash_delta"])),
                    "realized_pnl_delta": Decimal(str(existing["realized_pnl_delta"])),
                }

            def position_state(asset_id: str) -> PaperPositionState:
                row = positions[asset_id]
                return PaperPositionState(
                    quantity=Decimal(str(row["quantity"])),
                    cost_basis=Decimal(str(row["cost_basis"])),
                    realized_pnl=Decimal(str(row["realized_pnl"])),
                )

            mutation = apply_complete_set_merge(
                cash_balance=Decimal(str(account["cash_balance"])),
                yes=position_state(yes_asset_id),
                no=position_state(no_asset_id),
                quantity=quantity,
            )
            for asset_id in (yes_asset_id, no_asset_id):
                row = positions[asset_id]
                available = Decimal(str(row["quantity"])) - Decimal(
                    str(row["reserved_quantity"])
                )
                if available < quantity:
                    raise PaperLedgerError(
                        "insufficient_unreserved_complete_set_position_at_merge"
                    )
                CompleteSetLotStore.ensure_position_coverage(
                    cur,
                    strategy_id=strategy_id,
                    market_id=market_id,
                    condition_id=condition_id,
                    asset_id=asset_id,
                    quantity=Decimal(str(row["quantity"])),
                    cost_basis=Decimal(str(row["cost_basis"])),
                    observed_at=datetime.now(timezone.utc),
                )
            merge_metadata = {
                "merge_id": merge_id,
                "yes_asset_id": yes_asset_id,
                "no_asset_id": no_asset_id,
                **(metadata or {}),
            }
            cur.execute(
                """
                INSERT INTO quant.paper_complete_set_merges (
                    merge_id, strategy_id, market_id, condition_id,
                    yes_asset_id, no_asset_id, quantity, cash_delta,
                    realized_pnl_delta, metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    str(merge_id),
                    str(strategy_id),
                    str(market_id),
                    str(condition_id),
                    str(yes_asset_id),
                    str(no_asset_id),
                    mutation.quantity,
                    mutation.payout,
                    mutation.realized_pnl_delta,
                    json.dumps(merge_metadata),
                ),
            )
            merge_event_ts = datetime.now(timezone.utc)
            CompleteSetLotStore.consume_assets(
                cur,
                consumption_id=f"complete-set-merge-consumption:{merge_id}",
                strategy_id=strategy_id,
                market_id=market_id,
                condition_id=condition_id,
                consumer_type="COMPLETE_SET_MERGE",
                consumer_ref=merge_id,
                quantities={
                    yes_asset_id: quantity,
                    no_asset_id: quantity,
                },
                expected_basis_by_asset={
                    yes_asset_id: Decimal(str(positions[yes_asset_id]["cost_basis"]))
                    - mutation.yes_after.cost_basis,
                    no_asset_id: Decimal(str(positions[no_asset_id]["cost_basis"]))
                    - mutation.no_after.cost_basis,
                },
                cash_delta=mutation.payout,
                realized_pnl_delta=mutation.realized_pnl_delta,
                consumed_at=merge_event_ts,
                metadata=merge_metadata,
            )
            cur.execute(
                """
                UPDATE quant.paper_accounts
                SET cash_balance=%s, realized_pnl=realized_pnl + %s,
                    updated_at=clock_timestamp()
                WHERE strategy_id=%s
                """,
                (mutation.cash_after, mutation.realized_pnl_delta, str(strategy_id)),
            )
            for asset_id, after in (
                (yes_asset_id, mutation.yes_after),
                (no_asset_id, mutation.no_after),
            ):
                cur.execute(
                    """
                    UPDATE quant.paper_positions
                    SET quantity=%s, cost_basis=%s, realized_pnl=%s,
                        updated_at=clock_timestamp()
                    WHERE strategy_id=%s AND asset_id=%s
                    """,
                    (
                        after.quantity,
                        after.cost_basis,
                        after.realized_pnl,
                        str(strategy_id),
                        str(asset_id),
                    ),
                )

            event_ts = merge_event_ts
            cash_before = Decimal(str(account["cash_balance"]))
            entries = (
                (
                    "MERGE_YES",
                    yes_asset_id,
                    mutation.yes_after,
                    mutation.yes_realized_pnl_delta,
                    Decimal("0"),
                    cash_before,
                ),
                (
                    "MERGE_NO",
                    no_asset_id,
                    mutation.no_after,
                    mutation.no_realized_pnl_delta,
                    mutation.payout,
                    mutation.cash_after,
                ),
            )
            for (
                event_type,
                asset_id,
                after,
                realized_delta,
                cash_delta,
                cash_after,
            ) in entries:
                cur.execute(
                    """
                    INSERT INTO quant.paper_ledger_entries (
                        idempotency_key, strategy_id, event_type, market_id,
                        condition_id, asset_id, event_ts, shares_delta,
                        cash_delta, realized_pnl_delta, cash_after, position_after,
                        cost_basis_after, metadata
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (
                        f"{merge_id}:{event_type}",
                        str(strategy_id),
                        event_type,
                        str(market_id),
                        str(condition_id),
                        str(asset_id),
                        event_ts,
                        -quantity,
                        cash_delta,
                        realized_delta,
                        cash_after,
                        after.quantity,
                        after.cost_basis,
                        json.dumps(merge_metadata),
                    ),
                )
            conn.commit()
            return {
                "status": "MERGED",
                "merge_id": merge_id,
                "quantity": mutation.quantity,
                "cash_delta": mutation.payout,
                "cash_after": mutation.cash_after,
                "realized_pnl_delta": mutation.realized_pnl_delta,
            }

    def build_daily_accounting_snapshot(
        self,
        *,
        strategy_id: str | None = None,
        accounting_date: date | None = None,
        tolerance: Decimal = Decimal("0.00000001"),
    ) -> list[dict[str, Any]]:
        """Reconcile durable balances, ledger deltas, and active reservations."""

        snapshot_date = accounting_date or datetime.now(timezone.utc).date()
        predicate = "WHERE strategy_id=%s" if strategy_id else ""
        params = (str(strategy_id),) if strategy_id else ()
        results: list[dict[str, Any]] = []
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT strategy_id, initial_cash, cash_balance, cash_reserved
                FROM quant.paper_accounts
                {predicate}
                ORDER BY strategy_id
                """,
                params,
            )
            accounts = [dict(row) for row in cur.fetchall()]
            cash_delta_breakdowns = load_account_cash_delta_breakdown(
                cur,
                strategy_ids=[str(account["strategy_id"]) for account in accounts],
            )
            cur.execute(
                """
                SELECT to_regclass(
                    'quant.simulator_position_operation_reservations'
                ) IS NOT NULL AS available
                """
            )
            operation_reservations_available = bool(cur.fetchone()["available"])
            for account in accounts:
                current_strategy = str(account["strategy_id"])
                cash_delta_breakdown = cash_delta_breakdowns.get(current_strategy, {})
                ledger_cash_delta = cash_delta_breakdown.get(PAPER_LEDGER, Decimal(0))
                total_accounted_cash_delta = cash_delta_breakdown.get(TOTAL, Decimal(0))
                initial_cash = Decimal(str(account["initial_cash"]))
                actual_cash = Decimal(str(account["cash_balance"]))
                cash_reserved = Decimal(str(account["cash_reserved"]))
                expected_cash = initial_cash + total_accounted_cash_delta
                cash_difference = actual_cash - expected_cash
                cur.execute(
                    """
                    WITH expected AS (
                        SELECT asset_id, sum(shares_delta) AS quantity
                        FROM quant.paper_ledger_entries
                        WHERE strategy_id=%s
                        GROUP BY asset_id
                    ), actual AS (
                        SELECT asset_id, quantity
                        FROM quant.paper_positions
                        WHERE strategy_id=%s
                    ), compared AS (
                        SELECT COALESCE(e.asset_id, p.asset_id) AS asset_id,
                               COALESCE(e.quantity, 0) AS expected_quantity,
                               COALESCE(p.quantity, 0) AS actual_quantity
                        FROM expected e
                        FULL OUTER JOIN actual p ON p.asset_id=e.asset_id
                        WHERE e.asset_id IS NOT NULL OR p.asset_id IS NOT NULL
                    )
                    SELECT count(*) AS position_count,
                           count(*) FILTER (
                               WHERE abs(expected_quantity - actual_quantity) > %s
                           ) AS mismatch_count,
                           COALESCE(
                               jsonb_agg(
                                   jsonb_build_object(
                                       'asset_id', asset_id,
                                       'expected', expected_quantity,
                                       'actual', actual_quantity
                                   )
                               ) FILTER (
                                   WHERE abs(expected_quantity - actual_quantity) > %s
                               ),
                               '[]'::jsonb
                           ) AS mismatches
                    FROM compared
                    """,
                    (current_strategy, current_strategy, tolerance, tolerance),
                )
                positions = dict(cur.fetchone())
                operation_token_source = (
                    """
                        UNION ALL
                        SELECT asset_id,
                               reserved_quantity AS reserved_shares
                        FROM quant.simulator_position_operation_token_reservations
                        WHERE strategy_id=%s AND status='ACTIVE'
                    """
                    if operation_reservations_available
                    else ""
                )
                reservation_params: tuple[Any, ...] = (
                    (current_strategy, current_strategy)
                    if operation_reservations_available
                    else (current_strategy,)
                )
                cur.execute(
                    f"""
                    WITH active_rows AS (
                        SELECT asset_id,reserved_shares
                        FROM quant.paper_order_reservations
                        WHERE strategy_id=%s AND status='ACTIVE'
                        {operation_token_source}
                    ), active AS (
                        SELECT asset_id,sum(reserved_shares) AS reserved_shares
                        FROM active_rows GROUP BY asset_id
                    ), actual AS (
                        SELECT asset_id, reserved_quantity
                        FROM quant.paper_positions
                        WHERE strategy_id=%s
                    ), compared AS (
                        SELECT COALESCE(a.asset_id, p.asset_id) AS asset_id,
                               COALESCE(a.reserved_shares, 0) AS expected_reserved,
                               COALESCE(p.reserved_quantity, 0) AS actual_reserved
                        FROM active a
                        FULL OUTER JOIN actual p ON p.asset_id=a.asset_id
                        WHERE COALESCE(a.reserved_shares, 0) <> 0
                           OR COALESCE(p.reserved_quantity, 0) <> 0
                    )
                    SELECT count(*) FILTER (
                               WHERE abs(expected_reserved - actual_reserved) > %s
                           ) AS mismatch_count,
                           COALESCE(
                               jsonb_agg(
                                   jsonb_build_object(
                                       'asset_id', asset_id,
                                       'expected', expected_reserved,
                                       'actual', actual_reserved
                                   )
                               ) FILTER (
                                   WHERE abs(expected_reserved - actual_reserved) > %s
                               ),
                               '[]'::jsonb
                           ) AS mismatches
                    FROM compared
                    """,
                    (*reservation_params, current_strategy, tolerance, tolerance),
                )
                reserved_positions = dict(cur.fetchone())
                operation_cash_source = (
                    """
                        UNION ALL
                        SELECT reserved_cash
                        FROM quant.simulator_position_operation_reservations
                        WHERE strategy_id=%s AND status='ACTIVE'
                    """
                    if operation_reservations_available
                    else ""
                )
                cash_params = (
                    (current_strategy, current_strategy)
                    if operation_reservations_available
                    else (current_strategy,)
                )
                cur.execute(
                    f"""
                    SELECT COALESCE(sum(reserved_cash), 0) AS reserved_cash
                    FROM (
                        SELECT reserved_cash
                        FROM quant.paper_order_reservations
                        WHERE strategy_id=%s AND status='ACTIVE'
                        {operation_cash_source}
                    ) reservations
                    """,
                    cash_params,
                )
                active_reserved_cash = Decimal(str(cur.fetchone()["reserved_cash"]))
                position_mismatch_count = int(positions["mismatch_count"] or 0)
                reserved_position_mismatch_count = int(
                    reserved_positions["mismatch_count"] or 0
                )
                passed = (
                    abs(cash_difference) <= tolerance
                    and abs(cash_reserved - active_reserved_cash) <= tolerance
                    and position_mismatch_count == 0
                    and reserved_position_mismatch_count == 0
                )
                details = {
                    "cash_delta_sources": cash_delta_breakdown,
                    "total_accounted_cash_delta": total_accounted_cash_delta,
                    "position_mismatches": positions["mismatches"],
                    "reserved_position_mismatches": reserved_positions["mismatches"],
                    "reserved_cash_difference": cash_reserved - active_reserved_cash,
                    "tolerance": tolerance,
                }
                cur.execute(
                    """
                    INSERT INTO quant.paper_daily_accounting_snapshots (
                        accounting_date, strategy_id, initial_cash, ledger_cash_delta,
                        expected_cash, actual_cash, cash_reserved, active_reserved_cash,
                        position_count, position_mismatch_count,
                        reserved_position_mismatch_count, cash_difference, passed, details
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (accounting_date, strategy_id) DO UPDATE SET
                        initial_cash=EXCLUDED.initial_cash,
                        ledger_cash_delta=EXCLUDED.ledger_cash_delta,
                        expected_cash=EXCLUDED.expected_cash,
                        actual_cash=EXCLUDED.actual_cash,
                        cash_reserved=EXCLUDED.cash_reserved,
                        active_reserved_cash=EXCLUDED.active_reserved_cash,
                        position_count=EXCLUDED.position_count,
                        position_mismatch_count=EXCLUDED.position_mismatch_count,
                        reserved_position_mismatch_count=EXCLUDED.reserved_position_mismatch_count,
                        cash_difference=EXCLUDED.cash_difference,
                        passed=EXCLUDED.passed,
                        details=EXCLUDED.details,
                        generated_at=clock_timestamp()
                    """,
                    (
                        snapshot_date,
                        current_strategy,
                        initial_cash,
                        ledger_cash_delta,
                        expected_cash,
                        actual_cash,
                        cash_reserved,
                        active_reserved_cash,
                        int(positions["position_count"] or 0),
                        position_mismatch_count,
                        reserved_position_mismatch_count,
                        cash_difference,
                        passed,
                        json.dumps(details, default=str),
                    ),
                )
                results.append(
                    {
                        "accounting_date": snapshot_date.isoformat(),
                        "strategy_id": current_strategy,
                        "expected_cash": expected_cash,
                        "actual_cash": actual_cash,
                        "cash_delta_sources": cash_delta_breakdown,
                        "cash_reserved": cash_reserved,
                        "active_reserved_cash": active_reserved_cash,
                        "position_mismatch_count": position_mismatch_count,
                        "reserved_position_mismatch_count": reserved_position_mismatch_count,
                        "cash_difference": cash_difference,
                        "passed": passed,
                        "details": details,
                    }
                )
            conn.commit()
        return results

    def summary(self, *, strategy_id: str | None = None) -> dict[str, Any]:
        predicate = "WHERE a.strategy_id=%s" if strategy_id else ""
        params = (str(strategy_id),) if strategy_id else ()
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT count(*) AS accounts,
                       COALESCE(sum(a.initial_cash), 0) AS initial_cash,
                       COALESCE(sum(a.cash_balance), 0) AS cash_balance,
                       COALESCE(sum(a.cash_reserved), 0) AS cash_reserved,
                       COALESCE(sum(a.cash_balance - a.cash_reserved), 0) AS cash_available,
                       COALESCE(sum(a.realized_pnl), 0) AS realized_pnl
                FROM quant.paper_accounts a {predicate}
                """,
                params,
            )
            account = dict(cur.fetchone() or {})
            position_predicate = "WHERE strategy_id=%s" if strategy_id else ""
            cur.execute(
                f"""
                SELECT count(*) FILTER (WHERE quantity > 0) AS open_positions,
                       COALESCE(sum(quantity) FILTER (WHERE quantity > 0), 0) AS open_shares,
                       COALESCE(sum(reserved_quantity), 0) AS reserved_shares,
                       COALESCE(sum(cost_basis) FILTER (WHERE quantity > 0), 0) AS open_cost_basis
                FROM quant.paper_positions {position_predicate}
                """,
                params,
            )
            positions = dict(cur.fetchone() or {})
            cur.execute(
                f"""
                SELECT count(*) AS ledger_entries,
                       COALESCE(sum(cash_delta), 0) AS ledger_cash_delta,
                       count(*) FILTER (WHERE event_type='SETTLEMENT') AS settlements,
                       count(*) FILTER (WHERE event_type='MERGE_NO') AS complete_set_merges
                FROM quant.paper_ledger_entries
                {("WHERE strategy_id=%s" if strategy_id else "")}
                """,
                params,
            )
            ledger = dict(cur.fetchone() or {})
            cur.execute(
                """
                SELECT
                       strategy_id, observed_at, equity, conservative_equity,
                       unrealized_pnl, conservative_unrealized_pnl,
                       total_pnl, conservative_total_pnl, gross_exposure,
                       drawdown, drawdown_pct, open_positions,
                       unmarkable_positions, nav_complete
                FROM quant.paper_portfolio_nav_current
                WHERE (%s::text IS NULL OR strategy_id=%s)
                ORDER BY strategy_id
                """,
                (
                    str(strategy_id) if strategy_id else None,
                    str(strategy_id) if strategy_id else None,
                ),
            )
            latest_nav = [dict(row) for row in cur.fetchall()]
        return {
            "account": account,
            "positions": positions,
            "ledger": ledger,
            "latest_nav": latest_nav,
        }

    def _ensure_account(self, cur: Any, strategy_id: str) -> None:
        cur.execute(
            """
            INSERT INTO quant.paper_accounts (strategy_id, initial_cash, cash_balance)
            VALUES (%s,%s,%s)
            ON CONFLICT (strategy_id) DO NOTHING
            """,
            (str(strategy_id), self.initial_cash, self.initial_cash),
        )

    def _lock_portfolio(
        self, cur: Any, result: PaperExecutionResult
    ) -> PaperPortfolioState:
        intent = result.intent
        self._ensure_account(cur, intent.strategy_id)
        cur.execute(
            """
            INSERT INTO quant.paper_positions (
                strategy_id, asset_id, market_id, condition_id
            ) VALUES (%s,%s,%s,%s)
            ON CONFLICT (strategy_id, asset_id) DO UPDATE SET
                market_id=EXCLUDED.market_id,
                condition_id=EXCLUDED.condition_id,
                updated_at=clock_timestamp()
            """,
            (
                intent.strategy_id,
                intent.asset_id,
                intent.market_id,
                intent.condition_id,
            ),
        )
        cur.execute(
            """
            SELECT a.cash_balance, a.realized_pnl AS account_realized_pnl,
                   p.quantity, p.cost_basis, p.realized_pnl
            FROM quant.paper_accounts a
            JOIN quant.paper_positions p ON p.strategy_id=a.strategy_id
            WHERE a.strategy_id=%s AND p.asset_id=%s
            FOR UPDATE OF a, p
            """,
            (intent.strategy_id, intent.asset_id),
        )
        row = cur.fetchone()
        assert row is not None
        return PaperPortfolioState(
            cash_balance=Decimal(str(row["cash_balance"])),
            position_size=Decimal(str(row["quantity"])),
            cost_basis=Decimal(str(row["cost_basis"])),
            realized_pnl=Decimal(str(row["realized_pnl"])),
        )

    def _persist_state(
        self, cur: Any, result: PaperExecutionResult, mutation: PaperPortfolioMutation
    ) -> None:
        intent = result.intent
        state = mutation.after
        cur.execute(
            """
            UPDATE quant.paper_accounts
            SET cash_balance=%s, realized_pnl=realized_pnl + %s,
                updated_at=clock_timestamp()
            WHERE strategy_id=%s
            """,
            (state.cash_balance, mutation.realized_pnl_delta, intent.strategy_id),
        )
        cur.execute(
            """
            UPDATE quant.paper_positions
            SET quantity=%s, cost_basis=%s, realized_pnl=%s,
                settled_at=NULL, updated_at=clock_timestamp()
            WHERE strategy_id=%s AND asset_id=%s
            """,
            (
                state.position_size,
                state.cost_basis,
                state.realized_pnl,
                intent.strategy_id,
                intent.asset_id,
            ),
        )


def _allocate_official_fee(
    official_total: Decimal,
    modeled_fees: list[Decimal],
) -> list[Decimal]:
    """Allocate an order-level six-decimal chain fee without changing its total."""

    if not modeled_fees:
        raise ValueError("modeled fee allocation requires at least one fill")
    if len(modeled_fees) == 1:
        return [official_total]
    weight_total = sum(modeled_fees, Decimal(0))
    if weight_total == 0:
        return [Decimal(0)] * (len(modeled_fees) - 1) + [official_total]
    allocations: list[Decimal] = []
    remaining = official_total
    for modeled in modeled_fees[:-1]:
        allocation = (official_total * modeled / weight_total).quantize(
            Decimal("0.000001"),
            rounding=ROUND_DOWN,
        )
        allocations.append(allocation)
        remaining -= allocation
    allocations.append(remaining)
    return allocations


def _persist_execution_finality(
    cur: Any,
    result: PaperExecutionResult,
    *,
    outcome: str = "CONFIRMED",
) -> None:
    if outcome not in {"CONFIRMED", "FAILED_REVERSED"}:
        raise ValueError("unsupported paper execution finality outcome")
    model = SettlementFinalityModel()
    intent = result.intent
    for fill_index, fill in enumerate(result.fills):
        trade_id = f"paper:{result.audit_key}:{fill_index}"
        trade = ProvisionalTrade(
            trade_id=trade_id,
            asset_id=intent.asset_id,
            side=intent.side,
            size=fill.size,
            price=fill.price,
            fee=fill.fee,
            state="MATCHED",
            matched_at=result.arrival_ts,
        )
        pending, provisional_journal = model.provisional(trade)
        if outcome == "CONFIRMED":
            finalized, final_journal = model.confirm(
                pending,
                confirmed_at=result.arrival_ts,
            )
        else:
            finalized, final_journal = model.fail_and_reverse(
                pending,
                failed_at=result.arrival_ts,
            )
        metadata = {
            "audit_key": result.audit_key,
            "fill_index": fill_index,
            "client_order_id": intent.client_order_id,
            "market_id": intent.market_id,
            "condition_id": intent.condition_id,
            "side": intent.side,
            "price": fill.price,
            "size": fill.size,
            "fee": fill.fee,
            "simulation_confirmation_mode": (
                "IMMEDIATE" if outcome == "CONFIRMED" else "FAULT_INJECTION"
            ),
        }
        cur.execute(
            """
            INSERT INTO quant.paper_execution_finality (
                trade_id, strategy_id, asset_id, state,
                provisional_size, confirmed_size, matched_at,
                finalized_at, metadata
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT (trade_id) DO UPDATE SET
                state=EXCLUDED.state,
                provisional_size=EXCLUDED.provisional_size,
                confirmed_size=EXCLUDED.confirmed_size,
                finalized_at=EXCLUDED.finalized_at,
                metadata=EXCLUDED.metadata,
                updated_at=clock_timestamp()
            """,
            (
                trade_id,
                intent.strategy_id,
                intent.asset_id,
                finalized.state,
                pending.size,
                pending.size if outcome == "CONFIRMED" else Decimal("0"),
                result.arrival_ts,
                finalized.finalized_at,
                json.dumps(_json_value(metadata)),
            ),
        )
        _persist_finality_event(
            cur,
            event_key=f"{trade_id}:matched-provisional",
            trade_id=trade_id,
            strategy_id=intent.strategy_id,
            state="MATCHED_PROVISIONAL",
            event_type="PROVISIONAL_MATCH",
            event_ts=result.arrival_ts,
            journal_key=None,
            metadata=metadata,
        )
        _persist_finality_journal(
            cur,
            strategy_id=intent.strategy_id,
            event_ts=result.arrival_ts,
            journal=provisional_journal,
            metadata=metadata,
        )
        _persist_finality_event(
            cur,
            event_key=f"{trade_id}:settlement-pending",
            trade_id=trade_id,
            strategy_id=intent.strategy_id,
            state="SETTLEMENT_PENDING",
            event_type=provisional_journal.event_type,
            event_ts=result.arrival_ts,
            journal_key=provisional_journal.journal_key,
            metadata=metadata,
        )
        _persist_finality_journal(
            cur,
            strategy_id=intent.strategy_id,
            event_ts=result.arrival_ts,
            journal=final_journal,
            metadata=metadata,
        )
        _persist_finality_event(
            cur,
            event_key=(
                f"{trade_id}:confirmed"
                if outcome == "CONFIRMED"
                else f"{trade_id}:failed-reversed"
            ),
            trade_id=trade_id,
            strategy_id=intent.strategy_id,
            state=outcome,
            event_type=final_journal.event_type,
            event_ts=result.arrival_ts,
            journal_key=final_journal.journal_key,
            metadata=metadata,
        )


def _persist_finality_event(
    cur: Any,
    *,
    event_key: str,
    trade_id: str,
    strategy_id: str,
    state: str,
    event_type: str,
    event_ts: datetime,
    journal_key: str | None,
    metadata: dict[str, Any],
) -> None:
    cur.execute(
        """
        INSERT INTO quant.paper_execution_finality_events (
            event_key, trade_id, strategy_id, state, event_type,
            event_ts, journal_key, metadata
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (event_key) DO NOTHING
        """,
        (
            event_key,
            trade_id,
            strategy_id,
            state,
            event_type,
            event_ts,
            journal_key,
            json.dumps(_json_value(metadata)),
        ),
    )


def _persist_finality_journal(
    cur: Any,
    *,
    strategy_id: str,
    event_ts: datetime,
    journal: FinalityJournal,
    metadata: dict[str, Any],
) -> None:
    changes = (
        ("CASH_AVAILABLE", journal.cash_delta),
        ("TOKEN_POSITION", journal.inventory_value_delta),
        ("TRADE_RECEIVABLE_PENDING", journal.receivable_delta),
        ("FEE_EXPENSE", journal.fee_delta),
    )
    lines = [
        (
            journal.journal_key,
            index,
            journal.event_type,
            strategy_id,
            account,
            amount if amount > 0 else Decimal("0"),
            -amount if amount < 0 else Decimal("0"),
            event_ts,
            json.dumps(
                _json_value(
                    {
                        **metadata,
                        "shares_delta": journal.shares_delta,
                        "reason": journal.reason,
                    }
                )
            ),
        )
        for index, (account, amount) in enumerate(changes)
        if amount != 0
    ]
    debit = sum((line[5] for line in lines), Decimal("0"))
    credit = sum((line[6] for line in lines), Decimal("0"))
    if not lines or debit != credit:
        raise PaperLedgerError(
            f"unbalanced finality journal {journal.journal_key}: {debit} != {credit}"
        )
    cur.executemany(
        """
        INSERT INTO quant.paper_journal_lines (
            journal_id, line_index, event_type, strategy_id,
            account_code, debit, credit, event_ts, metadata
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (journal_id, line_index) DO NOTHING
        """,
        lines,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _settlement_key(
    strategy_id: str,
    asset_id: str,
    winning_asset_id: str,
    resolved_at: datetime | None,
) -> str:
    raw = "|".join(
        (strategy_id, asset_id, winning_asset_id, str(resolved_at or "unknown"))
    )
    return "settlement:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _nav_history_due(
    previous: dict[str, Any] | None,
    *,
    observed_at: datetime,
    interval_seconds: float,
    realized_pnl: Decimal,
    open_positions: int,
    unmarkable_positions: int,
    nav_complete: bool,
) -> bool:
    if previous is None:
        return True
    previous_at = previous.get("observed_at")
    if isinstance(previous_at, datetime):
        elapsed = (observed_at - previous_at).total_seconds()
        if elapsed >= max(1.0, float(interval_seconds)):
            return True
    return any(
        (
            Decimal(str(previous.get("realized_pnl") or 0)) != realized_pnl,
            int(previous.get("open_positions") or 0) != open_positions,
            int(previous.get("unmarkable_positions") or 0) != unmarkable_positions,
            bool(previous.get("nav_complete")) != nav_complete,
        )
    )
