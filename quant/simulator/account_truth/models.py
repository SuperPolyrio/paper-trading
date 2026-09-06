"""Typed Decimal models for official and paper account truth."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class MismatchType(str, Enum):
    MATCH = "MATCH"
    PENDING_CONVERGENCE = "PENDING_CONVERGENCE"
    TIMING_LAG = "TIMING_LAG"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    COST_BASIS_MISMATCH = "COST_BASIS_MISMATCH"
    FEE_MISMATCH = "FEE_MISMATCH"
    REALIZED_PNL_MISMATCH = "REALIZED_PNL_MISMATCH"
    UNREALIZED_PNL_MISMATCH = "UNREALIZED_PNL_MISMATCH"
    EQUITY_MISMATCH = "EQUITY_MISMATCH"
    CASH_MISMATCH = "CASH_MISMATCH"
    FINALITY_MISMATCH = "FINALITY_MISMATCH"
    UNMODELED_CASHFLOW = "UNMODELED_CASHFLOW"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    SCOPE_BASELINE_MISSING = "SCOPE_BASELINE_MISSING"
    UNCLASSIFIED = "UNCLASSIFIED"


class AccountTruthGateStatus(str, Enum):
    PASS = "PASS"
    PASS_WITH_TIMING_LAG = "PASS_WITH_TIMING_LAG"
    FAIL_ACCOUNT_TRUTH = "FAIL_ACCOUNT_TRUTH"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class OfficialPosition:
    account_address: str
    asset_id: str
    condition_id: str
    size: Decimal
    avg_price: Decimal
    initial_value: Decimal
    gross_initial_value: Decimal | None
    entry_fees_usdc: Decimal | None
    current_value: Decimal
    cash_pnl: Decimal
    realized_pnl: Decimal
    current_price: Decimal
    total_bought: Decimal
    redeemable: bool
    mergeable: bool
    title: str = ""
    slug: str = ""
    outcome: str = ""
    outcome_index: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClosedPosition:
    account_address: str
    asset_id: str
    condition_id: str
    avg_price: Decimal
    total_bought: Decimal
    realized_pnl: Decimal
    current_price: Decimal
    closed_at: datetime | None
    title: str = ""
    slug: str = ""
    outcome: str = ""
    outcome_index: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountingPosition:
    condition_id: str
    asset_id: str
    size: Decimal
    current_price: Decimal
    valuation_time: datetime

    @property
    def current_value(self) -> Decimal:
        return self.size * self.current_price


@dataclass(frozen=True)
class AccountingEquity:
    cash_balance: Decimal
    positions_value: Decimal
    equity: Decimal
    valuation_time: datetime


@dataclass(frozen=True)
class ParsedAccountingSnapshot:
    positions: tuple[AccountingPosition, ...]
    equity: AccountingEquity
    positions_csv_sha256: str
    equity_csv_sha256: str
    zip_sha256: str

    @property
    def source_as_of(self) -> datetime:
        return self.equity.valuation_time


@dataclass(frozen=True)
class OfficialAccountBundle:
    run_id: str
    account_address: str
    observed_at: datetime
    source_as_of: datetime
    positions: tuple[OfficialPosition, ...]
    closed_positions: tuple[ClosedPosition, ...]
    accounting: ParsedAccountingSnapshot
    fetch_manifest: Mapping[str, Any]


@dataclass(frozen=True)
class PaperPosition:
    asset_id: str
    condition_id: str
    quantity: Decimal
    cost_basis: Decimal
    entry_fees: Decimal
    realized_pnl: Decimal
    current_value: Decimal | None
    mark_price: Decimal | None
    provisional_quantity: Decimal = Decimal(0)
    redeemable: bool | None = None
    mergeable: bool | None = None
    truth_surface_omitted: bool = False

    @property
    def fee_exclusive_basis(self) -> Decimal:
        return self.cost_basis - self.entry_fees

    @property
    def average_price(self) -> Decimal:
        if self.quantity <= 0:
            return Decimal(0)
        return self.fee_exclusive_basis / self.quantity


@dataclass(frozen=True)
class PaperAccountSnapshot:
    strategy_ids: tuple[str, ...]
    as_of: datetime
    initial_cash: Decimal
    cash_balance: Decimal
    realized_pnl: Decimal
    positions: Mapping[str, PaperPosition]
    nav: Decimal | None
    unmodeled_cashflows: tuple[Mapping[str, Any], ...] = ()
    provisional_fill_count: int = 0
    ledger_checkpoint: str = ""
    snapshot_source: str = "PAPER_LEDGER"
    source_completeness: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountTruthMismatch:
    mismatch_id: str
    mismatch_type: MismatchType
    comparison_type: str
    comparison_key: str
    field_name: str
    official_value: str | None
    paper_value: str | None
    delta: str | None
    tolerance: str
    reason: str
    severity: str
    retryable: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountTruthReport:
    reconciliation_id: str
    official_run_id: str
    account_address: str
    strategy_ids: tuple[str, ...]
    as_of: datetime
    generated_at: datetime
    status: AccountTruthGateStatus
    official_source_status: str
    comparison_scope: str
    mismatches: tuple[AccountTruthMismatch, ...]
    summary: Mapping[str, Any]
    content_sha256: str
    comparison_rows: tuple[Mapping[str, Any], ...] = ()
