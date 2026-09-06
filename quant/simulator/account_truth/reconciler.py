"""As-of paper-account reconstruction and official truth reconciliation."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .mismatch_classifier import (
    categorical_mismatch,
    decimal_mismatch,
    informational_mismatch,
)
from .models import (
    AccountTruthGateStatus,
    AccountTruthMismatch,
    AccountTruthReport,
    MismatchType,
    OfficialAccountBundle,
    PaperAccountSnapshot,
    PaperPosition,
)

PROVISIONAL_FINALITY_STATES = frozenset({"MATCHED_PROVISIONAL", "RETRYING", "UNKNOWN"})
PNL_POSITION_FIELDS = frozenset(
    {
        "size",
        "avg_price",
        "initial_value",
        "gross_initial_value",
        "entry_fees_usdc",
        "current_value",
        "cash_pnl",
        "realized_pnl",
    }
)
PNL_ACCOUNT_FIELDS = frozenset(
    {"cash_balance", "equity", "realized_pnl", "total_pnl"}
)
PNL_CLOSED_POSITION_FIELDS = frozenset({"realized_pnl"})
CLEAN_V2_CONTRACT_CHECKS = frozenset(
    {
        "baseline_after_cutover",
        "baseline_asset_isolation",
        "paper_zero_delta_baseline",
        "cohort_asset_scope_bound",
        "cohort_economic_change_present",
        "no_out_of_scope_asset_changes",
    }
)


@dataclass
class _PositionState:
    asset_id: str
    condition_id: str = ""
    quantity: Decimal = Decimal(0)
    cost_basis: Decimal = Decimal(0)
    entry_fees: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)


class PaperAccountSnapshotLoader:
    """Rebuild paper state from append-only ledger and confirmed economics."""

    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory

    def load(
        self, *, strategy_ids: Sequence[str], as_of: datetime
    ) -> PaperAccountSnapshot:
        strategies = tuple(
            dict.fromkeys(str(item) for item in strategy_ids if str(item))
        )
        if not strategies:
            raise ValueError("paper account truth requires at least one strategy_id")
        if as_of.tzinfo is None:
            raise ValueError("paper account truth as_of must be timezone-aware")
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT strategy_id,initial_cash
                FROM quant.paper_accounts WHERE strategy_id=ANY(%s)
                ORDER BY strategy_id
                """,
                (list(strategies),),
            )
            accounts = [dict(row) for row in cur.fetchall()]
            found = {str(row["strategy_id"]) for row in accounts}
            missing = set(strategies) - found
            if missing:
                raise ValueError(
                    "paper strategy account not found: " + ",".join(sorted(missing))
                )
            cur.execute(
                """
                SELECT entry_id,strategy_id,asset_id,condition_id,event_type,event_ts,
                       shares_delta,cash_delta,fee,realized_pnl_delta,
                       position_after,cost_basis_after
                FROM quant.paper_ledger_entries
                WHERE strategy_id=ANY(%s) AND event_ts <= %s
                ORDER BY event_ts,entry_id
                """,
                (list(strategies), as_of),
            )
            ledger = [dict(row) for row in cur.fetchall()]
            cashflow_delta = self._confirmed_cashflows(cur, strategies, as_of)
            reward_delta = self._received_rewards(cur, strategies, as_of)
            provisional_count = self._provisional_fills(cur, strategies, as_of)
            marks = self._marks(cur, strategies, as_of)
            unmodeled = self._unmodeled_cashflows(cur, strategies, as_of)
            registry = self._registry_states(
                cur, {str(row["asset_id"]) for row in ledger}
            )

        states: dict[tuple[str, str], _PositionState] = {}
        ledger_cash_delta = Decimal(0)
        total_realized = Decimal(0)
        last_entry_id = 0
        for row in ledger:
            strategy_id = str(row["strategy_id"])
            asset_id = str(row["asset_id"])
            key = (strategy_id, asset_id)
            state = states.setdefault(key, _PositionState(asset_id=asset_id))
            state.condition_id = str(row.get("condition_id") or state.condition_id)
            before_quantity = state.quantity
            shares_delta = _decimal(row.get("shares_delta"))
            fee = abs(_decimal(row.get("fee")))
            event_type = str(row.get("event_type") or "").upper()
            if event_type == "BUY" and shares_delta > 0:
                state.entry_fees += fee
            elif event_type == "BUY_FEE_FINALITY_ADJUSTMENT":
                state.entry_fees += _decimal(row.get("fee"))
            elif event_type == "SELL" and shares_delta < 0 and before_quantity > 0:
                sold = min(before_quantity, abs(shares_delta))
                state.entry_fees -= state.entry_fees * sold / before_quantity
            state.quantity = _decimal(row.get("position_after"))
            state.cost_basis = _decimal(row.get("cost_basis_after"))
            state.realized_pnl += _decimal(row.get("realized_pnl_delta"))
            if state.quantity <= 0:
                state.entry_fees = Decimal(0)
            ledger_cash_delta += _decimal(row.get("cash_delta"))
            total_realized += _decimal(row.get("realized_pnl_delta"))
            last_entry_id = max(last_entry_id, int(row.get("entry_id") or 0))

        aggregated: dict[str, _PositionState] = {}
        for state in states.values():
            target = aggregated.setdefault(
                state.asset_id,
                _PositionState(
                    asset_id=state.asset_id, condition_id=state.condition_id
                ),
            )
            if (
                target.condition_id
                and state.condition_id
                and target.condition_id != state.condition_id
            ):
                raise RuntimeError(
                    f"paper asset maps to conflicting conditions: {state.asset_id}"
                )
            target.condition_id = target.condition_id or state.condition_id
            target.quantity += state.quantity
            target.cost_basis += state.cost_basis
            target.entry_fees += state.entry_fees
            target.realized_pnl += state.realized_pnl

        positive_assets_by_condition: dict[str, set[str]] = defaultdict(set)
        for asset_id, state in aggregated.items():
            if state.quantity > 0 and state.condition_id:
                positive_assets_by_condition[state.condition_id].add(asset_id)
        positions: dict[str, PaperPosition] = {}
        for asset_id, state in sorted(aggregated.items()):
            if (
                state.quantity == 0
                and state.cost_basis == 0
                and state.realized_pnl == 0
            ):
                continue
            mark = marks.get(asset_id)
            registry_row = registry.get(asset_id)
            token_count = (
                int(registry_row.get("token_count") or 0) if registry_row else 0
            )
            positions[asset_id] = PaperPosition(
                asset_id=asset_id,
                condition_id=state.condition_id,
                quantity=state.quantity,
                cost_basis=state.cost_basis,
                entry_fees=state.entry_fees,
                realized_pnl=state.realized_pnl,
                current_value=(state.quantity * mark if mark is not None else None),
                mark_price=mark,
                redeemable=(
                    bool(registry_row.get("resolved")) if registry_row else None
                ),
                mergeable=(
                    len(positive_assets_by_condition[state.condition_id]) >= 2
                    if registry_row and token_count == 2
                    else None
                ),
            )
        initial_cash = sum(
            (_decimal(row["initial_cash"]) for row in accounts), Decimal(0)
        )
        cash_balance = initial_cash + ledger_cash_delta + cashflow_delta + reward_delta
        marked_values = [
            row.current_value
            for row in positions.values()
            if row.current_value is not None
        ]
        nav = (
            cash_balance + sum(marked_values, Decimal(0))
            if len(marked_values) == len(positions)
            else None
        )
        checkpoint_payload = {
            "strategies": strategies,
            "as_of": as_of.astimezone(timezone.utc).isoformat(),
            "last_entry_id": last_entry_id,
            "ledger_rows": len(ledger),
            "provisional_fill_count": provisional_count,
        }
        checkpoint = hashlib.sha256(
            json.dumps(checkpoint_payload, sort_keys=True).encode()
        ).hexdigest()
        return PaperAccountSnapshot(
            strategy_ids=strategies,
            as_of=as_of.astimezone(timezone.utc),
            initial_cash=initial_cash,
            cash_balance=cash_balance,
            realized_pnl=total_realized,
            positions=positions,
            nav=nav,
            unmodeled_cashflows=tuple(unmodeled),
            provisional_fill_count=provisional_count,
            ledger_checkpoint=checkpoint,
        )

    def _confirmed_cashflows(
        self, cur: Any, strategies: tuple[str, ...], as_of: datetime
    ) -> Decimal:
        if not _table_exists(cur, "quant.paper_account_cashflow_operations"):
            return Decimal(0)
        cur.execute(
            """
            SELECT COALESCE(sum(cash_delta),0) AS amount
            FROM quant.paper_account_cashflow_operations
            WHERE strategy_id=ANY(%s) AND state='CONFIRMED' AND effective_ts <= %s
            """,
            (list(strategies), as_of),
        )
        return _decimal(cur.fetchone()["amount"])

    def _received_rewards(
        self, cur: Any, strategies: tuple[str, ...], as_of: datetime
    ) -> Decimal:
        if not _table_exists(cur, "quant.paper_reward_payouts"):
            return Decimal(0)
        cur.execute(
            """
            SELECT COALESCE(sum(amount),0) AS amount
            FROM quant.paper_reward_payouts
            WHERE strategy_id=ANY(%s) AND status='RECEIVED'
              AND COALESCE(received_at,effective_ts) <= %s
            """,
            (list(strategies), as_of),
        )
        received = _decimal(cur.fetchone()["amount"])
        if not _table_exists(cur, "quant.paper_reward_clawbacks"):
            return received
        cur.execute(
            """
            SELECT COALESCE(sum(amount),0) AS amount
            FROM quant.paper_reward_clawbacks
            WHERE strategy_id=ANY(%s) AND status='RECEIVED' AND effective_ts <= %s
            """,
            (list(strategies), as_of),
        )
        return received - _decimal(cur.fetchone()["amount"])

    def _provisional_fills(
        self, cur: Any, strategies: tuple[str, ...], as_of: datetime
    ) -> int:
        if not _table_exists(cur, "quant.simulator_fill_finality_trades"):
            return 0
        cur.execute(
            """
            SELECT count(*) AS n FROM quant.simulator_fill_finality_trades
            WHERE strategy_id=ANY(%s) AND matched_at <= %s AND state=ANY(%s)
            """,
            (list(strategies), as_of, list(PROVISIONAL_FINALITY_STATES)),
        )
        return int(cur.fetchone()["n"] or 0)

    def _marks(
        self, cur: Any, strategies: tuple[str, ...], as_of: datetime
    ) -> dict[str, Decimal]:
        if not _table_exists(cur, "quant.paper_position_marks"):
            return {}
        cur.execute(
            """
            SELECT asset_id,
                   COALESCE(liquidation_mark,conservative_mark,research_mark) AS mark
            FROM quant.paper_position_marks
            WHERE strategy_id=ANY(%s) AND observed_at <= %s
            ORDER BY observed_at DESC
            """,
            (list(strategies), as_of),
        )
        marks: dict[str, Decimal] = {}
        for row in cur.fetchall():
            asset = str(row["asset_id"])
            if asset not in marks and row.get("mark") is not None:
                marks[asset] = _decimal(row["mark"])
        return marks

    def _unmodeled_cashflows(
        self, cur: Any, strategies: tuple[str, ...], as_of: datetime
    ) -> list[Mapping[str, Any]]:
        if not _table_exists(cur, "quant.paper_account_return_reports"):
            return []
        cur.execute(
            """
            SELECT strategy_id,report->'unmodeled_cashflows' AS rows
            FROM quant.paper_account_return_reports
            WHERE strategy_id=ANY(%s) AND as_of <= %s
            ORDER BY strategy_id,as_of DESC
            """,
            (list(strategies), as_of),
        )
        found: dict[str, list[Mapping[str, Any]]] = {}
        for row in cur.fetchall():
            strategy = str(row["strategy_id"])
            if strategy not in found:
                found[strategy] = list(row.get("rows") or [])
        return [item for rows in found.values() for item in rows]

    def _registry_states(
        self, cur: Any, asset_ids: set[str]
    ) -> dict[str, Mapping[str, Any]]:
        if not asset_ids or not _table_exists(
            cur, "quant.paper_market_registry_tokens"
        ):
            return {}
        cur.execute(
            """
            SELECT asset_id,condition_id,resolved,token_count,market_state
            FROM quant.paper_market_registry_tokens
            WHERE asset_id=ANY(%s)
            """,
            (list(asset_ids),),
        )
        return {str(row["asset_id"]): dict(row) for row in cur.fetchall()}


class AccountTruthReconciler:
    def __init__(
        self,
        *,
        quantity_tolerance: Decimal = Decimal("0.000001"),
        money_tolerance: Decimal = Decimal("0.00001"),
        price_tolerance: Decimal = Decimal("0.000001"),
    ) -> None:
        self.quantity_tolerance = abs(Decimal(quantity_tolerance))
        self.money_tolerance = abs(Decimal(money_tolerance))
        self.price_tolerance = abs(Decimal(price_tolerance))

    def reconcile(
        self,
        *,
        official: OfficialAccountBundle,
        paper: PaperAccountSnapshot | None,
        comparison_scope: str = "WHOLE_ACCOUNT",
        generated_at: datetime | None = None,
    ) -> AccountTruthReport:
        now = generated_at or datetime.now(timezone.utc)
        mismatches = self._official_source_checks(official)
        if not mismatches:
            source_status = "PASS"
        elif all(item.retryable for item in mismatches):
            source_status = "PASS_WITH_TIMING_LAG"
        else:
            source_status = "SOURCE_CONFLICT"
        if paper is not None:
            mismatches.extend(self._paper_checks(official, paper))
        material = [item for item in mismatches if not item.retryable]
        retryable = [item for item in mismatches if item.retryable]
        if paper is None:
            status = AccountTruthGateStatus.INSUFFICIENT_EVIDENCE
        elif material:
            status = AccountTruthGateStatus.FAIL_ACCOUNT_TRUTH
        elif retryable:
            status = AccountTruthGateStatus.PASS_WITH_TIMING_LAG
        else:
            status = AccountTruthGateStatus.PASS
        comparison_rows = (
            tuple(self._paper_comparison_rows(official, paper)) if paper else ()
        )
        summary = {
            "schema_version": "official-account-truth-report-v1",
            "official_position_count": len(official.positions),
            "official_closed_position_count": len(official.closed_positions),
            "accounting_position_count": len(official.accounting.positions),
            "paper_position_count": (
                sum(
                    row.quantity > self.quantity_tolerance
                    and not row.truth_surface_omitted
                    for row in paper.positions.values()
                )
                if paper
                else None
            ),
            "paper_ledger_checkpoint": paper.ledger_checkpoint if paper else None,
            "paper_provisional_fill_count": paper.provisional_fill_count
            if paper
            else None,
            "mismatch_count": len(mismatches),
            "material_mismatch_count": len(material),
            "retryable_mismatch_count": len(retryable),
            "mismatch_type_counts": _counts(
                item.mismatch_type.value for item in mismatches
            ),
            "official_source_status": source_status,
            "comparison_scope": comparison_scope,
            "comparison_item_count": len(comparison_rows),
            "official_cash_balance": format(
                official.accounting.equity.cash_balance, "f"
            ),
            "official_positions_value": format(
                official.accounting.equity.positions_value, "f"
            ),
            "official_equity": format(official.accounting.equity.equity, "f"),
            "paper_cash_balance": format(paper.cash_balance, "f") if paper else None,
            "paper_nav": format(paper.nav, "f")
            if paper and paper.nav is not None
            else None,
            "paper_realized_pnl": (format(paper.realized_pnl, "f") if paper else None),
            "paper_unmodeled_cashflow_count": (
                len(paper.unmodeled_cashflows) if paper else None
            ),
            "paper_snapshot_source": paper.snapshot_source if paper else None,
            "paper_source_completeness": (
                dict(paper.source_completeness) if paper else None
            ),
        }
        summary["pnl_truth_contract"] = _pnl_truth_contract(
            comparison_rows=comparison_rows,
            account_truth_status=status,
            comparison_scope=comparison_scope,
            strategy_ids=paper.strategy_ids if paper else (),
            official_position_count=len(official.positions),
            official_closed_position_count=len(official.closed_positions),
            provisional_fill_count=paper.provisional_fill_count if paper else None,
            unmodeled_cashflow_count=(
                len(paper.unmodeled_cashflows) if paper else None
            ),
            snapshot_source=paper.snapshot_source if paper else None,
        )
        stable_payload = {
            "official_run_id": official.run_id,
            "strategy_ids": paper.strategy_ids if paper else (),
            "as_of": official.source_as_of.isoformat(),
            "comparison_scope": comparison_scope,
            "status": status.value,
            "summary": summary,
            "mismatches": [_mismatch_payload(item) for item in mismatches],
        }
        content_hash = hashlib.sha256(
            json.dumps(stable_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return AccountTruthReport(
            reconciliation_id=f"account-truth:{content_hash[:32]}",
            official_run_id=official.run_id,
            account_address=official.account_address,
            strategy_ids=paper.strategy_ids if paper else (),
            as_of=official.source_as_of,
            generated_at=now.astimezone(timezone.utc),
            status=status,
            official_source_status=source_status,
            comparison_scope=comparison_scope,
            mismatches=tuple(sorted(mismatches, key=lambda item: item.mismatch_id)),
            summary=summary,
            content_sha256=content_hash,
            comparison_rows=comparison_rows,
        )

    def reconcile_delta(
        self,
        *,
        official: OfficialAccountBundle,
        baseline: Mapping[str, Any],
        paper_before: PaperAccountSnapshot,
        paper_after: PaperAccountSnapshot,
        generated_at: datetime | None = None,
        clean_cohort_asset_ids: Sequence[str] = (),
        clean_cohort_cutover_at: datetime | None = None,
    ) -> AccountTruthReport:
        """Compare wallet and paper changes after an immutable official baseline."""

        baseline_payload = baseline.get("baseline_payload")
        if not isinstance(baseline_payload, Mapping):
            raise TypeError("account truth baseline payload is missing")
        baseline_as_of = _timestamp(baseline.get("source_as_of"))
        if paper_before.as_of != baseline_as_of:
            raise ValueError(
                "paper baseline snapshot is not aligned to official baseline"
            )
        if paper_before.strategy_ids != paper_after.strategy_ids:
            raise ValueError("paper delta snapshots use different strategy scopes")
        clean_assets = frozenset(
            str(item).strip() for item in clean_cohort_asset_ids if str(item).strip()
        )
        clean_scope = bool(clean_assets)
        comparison_scope = (
            "CLEAN_V2_COHORT_DELTA" if clean_scope else "CALIBRATION_DELTA"
        )
        if clean_scope and clean_cohort_cutover_at is None:
            raise ValueError("clean V2 cohort requires an explicit venue cutover")
        if clean_scope and baseline_as_of < _timestamp(clean_cohort_cutover_at):
            raise ValueError("clean V2 cohort baseline predates the venue cutover")
        now = generated_at or datetime.now(timezone.utc)
        source_mismatches = self._official_source_checks(official)
        mismatches = (
            [
                item
                for item in source_mismatches
                if item.comparison_key in clean_assets
                or item.comparison_key == "equity.csv"
            ]
            if clean_scope
            else source_mismatches
        )
        baseline_positions = baseline_payload.get("positions")
        if not isinstance(baseline_positions, Mapping):
            raise TypeError("account truth baseline positions are invalid")
        if clean_scope:
            contaminated = clean_assets & set(str(item) for item in baseline_positions)
            if contaminated:
                raise ValueError(
                    "clean V2 cohort asset existed before baseline: "
                    + ",".join(sorted(contaminated))
                )
            dirty_paper_positions = {
                asset_id
                for asset_id, row in paper_before.positions.items()
                if any(
                    abs(value) > self.money_tolerance
                    for value in (
                        row.quantity,
                        row.cost_basis,
                        row.entry_fees,
                        row.realized_pnl,
                    )
                )
            }
            if (
                dirty_paper_positions
                or abs(paper_before.cash_balance - paper_before.initial_cash)
                > self.money_tolerance
                or abs(paper_before.realized_pnl) > self.money_tolerance
                or paper_before.provisional_fill_count
                or paper_before.unmodeled_cashflows
            ):
                raise ValueError(
                    "clean V2 cohort paper strategy is not at a zero-delta baseline"
                )
        current_positions = _official_delta_positions(official)
        accounting_marks = {
            row.asset_id: row.current_price for row in official.accounting.positions
        }
        before_paper = dict(paper_before.positions)
        after_paper = dict(paper_after.positions)
        assets = sorted(
            set(baseline_positions)
            | set(current_positions)
            | set(before_paper)
            | set(after_paper)
        )
        compared_assets = 0
        out_of_scope_changed_assets: list[str] = []
        clean_open_asset_count = 0
        clean_closed_asset_count = 0
        official_clean_reported_realized_delta = Decimal(0)
        paper_clean_realized_delta = Decimal(0)
        official_clean_marked_value = Decimal(0)
        paper_clean_marked_value = Decimal(0)
        comparison_rows: list[Mapping[str, Any]] = []
        for asset_id in assets:
            baseline_row = _mapping(baseline_positions.get(asset_id))
            current_row = _mapping(current_positions.get(asset_id))
            paper_start = before_paper.get(asset_id)
            paper_end = after_paper.get(asset_id)
            official_deltas = {
                "size": _decimal(current_row.get("size"))
                - _decimal(baseline_row.get("size")),
                "gross_initial_value": _decimal(current_row.get("gross_initial_value"))
                - _decimal(baseline_row.get("gross_initial_value")),
                "entry_fees_usdc": _decimal(current_row.get("entry_fees_usdc"))
                - _decimal(baseline_row.get("entry_fees_usdc")),
                "realized_pnl": _decimal(current_row.get("total_realized_pnl"))
                - _decimal(baseline_row.get("open_realized_pnl"))
                - _decimal(baseline_row.get("closed_realized_pnl")),
            }
            paper_deltas = {
                "size": _paper_value(paper_end, "quantity")
                - _paper_value(paper_start, "quantity"),
                "gross_initial_value": _paper_value(paper_end, "cost_basis")
                - _paper_value(paper_start, "cost_basis"),
                "entry_fees_usdc": _paper_value(paper_end, "entry_fees")
                - _paper_value(paper_start, "entry_fees"),
                "realized_pnl": _paper_value(paper_end, "realized_pnl")
                - _paper_value(paper_start, "realized_pnl"),
            }
            has_quantity_change = any(
                abs(values["size"]) > self.quantity_tolerance
                for values in (official_deltas, paper_deltas)
            )
            has_money_change = any(
                abs(values[field_name]) > self.money_tolerance
                for values in (official_deltas, paper_deltas)
                for field_name in (
                    "gross_initial_value",
                    "entry_fees_usdc",
                    "realized_pnl",
                )
            )
            if not has_quantity_change and not has_money_change:
                continue
            if clean_scope and asset_id not in clean_assets:
                out_of_scope_changed_assets.append(asset_id)
                mismatches.append(
                    informational_mismatch(
                        mismatch_type=MismatchType.UNMODELED_CASHFLOW,
                        comparison_type="CLEAN_V2_COHORT_ISOLATION",
                        comparison_key=asset_id,
                        field_name="asset_scope",
                        reason=(
                            "wallet or paper asset changed outside the immutable clean "
                            "cohort scope"
                        ),
                        retryable=False,
                        evidence={
                            "baseline_id": baseline.get("baseline_id"),
                            "clean_cohort_asset_ids": sorted(clean_assets),
                        },
                    )
                )
                comparison_rows.append(
                    _categorical_comparison_row(
                        comparison_type="CLEAN_V2_COHORT_ISOLATION",
                        comparison_key=asset_id,
                        field_name="asset_scope",
                        official_value="OUT_OF_SCOPE_CHANGE",
                        paper_value="REJECTED",
                    )
                )
                continue
            compared_assets += 1
            comparison_type = "OFFICIAL_VS_PAPER_DELTA"
            field_values = {
                field_name: (official_deltas[field_name], paper_deltas[field_name])
                for field_name in (
                    "size",
                    "gross_initial_value",
                    "entry_fees_usdc",
                    "realized_pnl",
                )
            }
            if clean_scope:
                comparison_type = "OFFICIAL_VS_PAPER_COHORT_POSITION"
                official_size = official_deltas["size"]
                paper_size = paper_deltas["size"]
                official_mark = accounting_marks.get(asset_id)
                if official_size > self.quantity_tolerance and official_mark is None:
                    mismatches.append(
                        informational_mismatch(
                            mismatch_type=MismatchType.SOURCE_CONFLICT,
                            comparison_type="OFFICIAL_SOURCE",
                            comparison_key=asset_id,
                            field_name="accounting_mark",
                            reason=(
                                "clean cohort position has no mark in the official "
                                "Accounting Snapshot"
                            ),
                            retryable=False,
                            evidence={"baseline_id": baseline.get("baseline_id")},
                        )
                    )
                mark = official_mark or _decimal(current_row.get("current_price"))
                official_initial_value = _decimal(current_row.get("initial_value"))
                paper_initial_value = _paper_value(
                    paper_end, "fee_exclusive_basis"
                )
                official_current_value = official_size * mark
                paper_current_value = paper_size * mark
                field_values.update(
                    {
                        "avg_price": (
                            _decimal(current_row.get("avg_price"))
                            if official_size > self.quantity_tolerance
                            else Decimal(0),
                            _paper_value(paper_end, "average_price")
                            if paper_size > self.quantity_tolerance
                            else Decimal(0),
                        ),
                        "initial_value": (
                            official_initial_value,
                            paper_initial_value,
                        ),
                        "current_value": (
                            official_current_value,
                            paper_current_value,
                        ),
                        "cash_pnl": (
                            official_current_value - official_initial_value,
                            paper_current_value - paper_initial_value,
                        ),
                    }
                )
                official_clean_reported_realized_delta += official_deltas[
                    "realized_pnl"
                ]
                paper_clean_realized_delta += paper_deltas["realized_pnl"]
                official_clean_marked_value += official_current_value
                paper_clean_marked_value += paper_current_value
                if official_size > self.quantity_tolerance:
                    clean_open_asset_count += 1
                elif has_money_change:
                    clean_closed_asset_count += 1
                    reported_tolerance = max(
                        self.money_tolerance,
                        _decimal_source_unit(official_deltas["realized_pnl"]),
                    )
                    comparison_rows.append(
                        _decimal_comparison_row(
                            comparison_type="OFFICIAL_VS_PAPER_CLOSED_POSITION",
                            comparison_key=asset_id,
                            field_name="realized_pnl",
                            official_value=official_deltas["realized_pnl"],
                            paper_value=paper_deltas["realized_pnl"],
                            tolerance=reported_tolerance,
                            evidence={
                                "baseline_id": baseline.get("baseline_id"),
                                "normalized_zero_baseline": True,
                                "comparison_semantics": (
                                    "OFFICIAL_REPORTED_DISPLAY_PRECISION"
                                ),
                                "official_source_unit": format(
                                    reported_tolerance, "f"
                                ),
                            },
                        )
                    )
                    item = decimal_mismatch(
                        comparison_type="OFFICIAL_VS_PAPER_CLOSED_POSITION",
                        comparison_key=asset_id,
                        field_name="realized_pnl",
                        official_value=official_deltas["realized_pnl"],
                        paper_value=paper_deltas["realized_pnl"],
                        tolerance=reported_tolerance,
                        reason=(
                            "paper realized PnL differs beyond the official "
                            "closed-position source precision"
                        ),
                        evidence={
                            "baseline_id": baseline.get("baseline_id"),
                            "normalized_zero_baseline": True,
                            "comparison_semantics": (
                                "OFFICIAL_REPORTED_DISPLAY_PRECISION"
                            ),
                        },
                    )
                    if item:
                        mismatches.append(item)
                    field_values.pop("realized_pnl", None)
            for field_name, (official_value, paper_value) in field_values.items():
                tolerance = (
                    self.quantity_tolerance
                    if field_name == "size"
                    else self.price_tolerance
                    if field_name == "avg_price"
                    else self.money_tolerance
                )
                comparison_rows.append(
                    _decimal_comparison_row(
                        comparison_type=comparison_type,
                        comparison_key=asset_id,
                        field_name=field_name,
                        official_value=official_value,
                        paper_value=paper_value,
                        tolerance=tolerance,
                        evidence={
                            "baseline_id": baseline.get("baseline_id"),
                            "baseline_as_of": baseline_as_of.isoformat(),
                            "normalized_zero_baseline": clean_scope,
                            "valuation_mark_source": (
                                "OFFICIAL_ACCOUNTING_SNAPSHOT"
                                if clean_scope
                                else None
                            ),
                        },
                    )
                )
                item = decimal_mismatch(
                    comparison_type=comparison_type,
                    comparison_key=asset_id,
                    field_name=field_name,
                    official_value=official_value,
                    paper_value=paper_value,
                    tolerance=tolerance,
                    reason="paper change since baseline differs from official wallet change",
                    evidence={
                        "baseline_id": baseline.get("baseline_id"),
                        "baseline_as_of": baseline_as_of.isoformat(),
                        "normalized_zero_baseline": clean_scope,
                    },
                )
                if item:
                    mismatches.append(item)
        official_cash_delta = official.accounting.equity.cash_balance - _decimal(
            baseline_payload.get("cash_balance")
        )
        paper_cash_delta = paper_after.cash_balance - paper_before.cash_balance
        account_values = {"cash_balance": (official_cash_delta, paper_cash_delta)}
        if clean_scope:
            official_total_pnl_delta = (
                official_cash_delta + official_clean_marked_value
            )
            paper_total_pnl_delta = paper_cash_delta + paper_clean_marked_value
            all_clean_positions_closed = (
                compared_assets > 0 and clean_open_asset_count == 0
            )
            official_clean_economic_realized_delta = (
                official_total_pnl_delta
                if all_clean_positions_closed
                else official_clean_reported_realized_delta
            )
            official_clean_realized_source = (
                "ACCOUNTING_CASH_DELTA_PLUS_MARKED_VALUE"
                if all_clean_positions_closed
                else "POSITIONS_AND_CLOSED_POSITIONS_REPORTED"
            )
            account_values.update(
                {
                    "equity": (official_total_pnl_delta, paper_total_pnl_delta),
                    "realized_pnl": (
                        official_clean_economic_realized_delta,
                        paper_clean_realized_delta,
                    ),
                    "total_pnl": (
                        official_total_pnl_delta,
                        paper_total_pnl_delta,
                    ),
                }
            )
        account_comparison_type = (
            "OFFICIAL_VS_PAPER_COHORT_ACCOUNT"
            if clean_scope
            else "OFFICIAL_VS_PAPER_DELTA"
        )
        for field_name, (official_value, paper_value) in account_values.items():
            comparison_rows.append(
                _decimal_comparison_row(
                    comparison_type=account_comparison_type,
                    comparison_key="account",
                    field_name=field_name,
                    official_value=official_value,
                    paper_value=paper_value,
                    tolerance=self.money_tolerance,
                    evidence={
                        "baseline_id": baseline.get("baseline_id"),
                        "baseline_as_of": baseline_as_of.isoformat(),
                        "normalized_zero_baseline": clean_scope,
                        "valuation_mark_source": (
                            "OFFICIAL_ACCOUNTING_SNAPSHOT" if clean_scope else None
                        ),
                    },
                )
            )
            item = decimal_mismatch(
                comparison_type=account_comparison_type,
                comparison_key="account",
                field_name=field_name,
                official_value=official_value,
                paper_value=paper_value,
                tolerance=self.money_tolerance,
                reason=(
                    "paper normalized cohort account change differs from official "
                    "wallet change"
                    if clean_scope
                    else "paper cash change since baseline differs from official wallet cash change"
                ),
                evidence={
                    "baseline_id": baseline.get("baseline_id"),
                    "baseline_as_of": baseline_as_of.isoformat(),
                    "normalized_zero_baseline": clean_scope,
                },
            )
            if item:
                mismatches.append(item)
        comparison_rows.append(
            _decimal_comparison_row(
                comparison_type="PAPER_FINALITY",
                comparison_key="account",
                field_name="provisional_fill_count",
                official_value=Decimal(0),
                paper_value=Decimal(paper_after.provisional_fill_count),
                tolerance=Decimal(0),
            )
        )
        if paper_after.provisional_fill_count:
            mismatches.append(
                informational_mismatch(
                    mismatch_type=MismatchType.FINALITY_MISMATCH,
                    comparison_type="PAPER_FINALITY",
                    comparison_key="account",
                    field_name="provisional_fill_count",
                    reason="paper delta still contains non-final fill evidence",
                    retryable=True,
                    evidence={"count": paper_after.provisional_fill_count},
                )
            )
        for index, row in enumerate(paper_after.unmodeled_cashflows):
            mismatches.append(
                informational_mismatch(
                    mismatch_type=MismatchType.UNMODELED_CASHFLOW,
                    comparison_type="PAPER_CASHFLOW",
                    comparison_key=str(row.get("event_id") or index),
                    field_name="event_type",
                    reason="paper delta contains an unmodeled cashflow",
                    retryable=False,
                    evidence=row,
                )
            )
        source_items = [
            row for row in mismatches if row.comparison_type == "OFFICIAL_SOURCE"
        ]
        if not source_items:
            source_status = "PASS"
        elif all(item.retryable for item in source_items):
            source_status = "PASS_WITH_TIMING_LAG"
        else:
            source_status = "SOURCE_CONFLICT"
        material = [item for item in mismatches if not item.retryable]
        retryable = [item for item in mismatches if item.retryable]
        if material:
            status = AccountTruthGateStatus.FAIL_ACCOUNT_TRUTH
        elif retryable:
            status = AccountTruthGateStatus.PASS_WITH_TIMING_LAG
        else:
            status = AccountTruthGateStatus.PASS
        summary = {
            "schema_version": "official-account-truth-report-v1",
            "baseline_id": baseline.get("baseline_id"),
            "baseline_as_of": baseline_as_of.isoformat(),
            "official_position_count": len(official.positions),
            "official_closed_position_count": len(official.closed_positions),
            "clean_open_position_count": (
                clean_open_asset_count if clean_scope else None
            ),
            "clean_closed_position_count": (
                clean_closed_asset_count if clean_scope else None
            ),
            "paper_position_count": len(paper_after.positions),
            "paper_ledger_checkpoint": paper_after.ledger_checkpoint,
            "paper_provisional_fill_count": paper_after.provisional_fill_count,
            "compared_delta_asset_count": compared_assets,
            "official_cash_delta": format(official_cash_delta, "f"),
            "paper_cash_delta": format(paper_cash_delta, "f"),
            "mismatch_count": len(mismatches),
            "material_mismatch_count": len(material),
            "retryable_mismatch_count": len(retryable),
            "mismatch_type_counts": _counts(
                item.mismatch_type.value for item in mismatches
            ),
            "official_source_status": source_status,
            "comparison_scope": comparison_scope,
            "comparison_item_count": len(comparison_rows),
            "official_cash_balance": format(
                official.accounting.equity.cash_balance, "f"
            ),
            "official_positions_value": format(
                official.accounting.equity.positions_value, "f"
            ),
            "official_equity": format(official.accounting.equity.equity, "f"),
            "paper_cash_balance": format(paper_after.cash_balance, "f"),
            "paper_nav": (
                format(paper_after.nav, "f") if paper_after.nav is not None else None
            ),
            "paper_realized_pnl": format(paper_after.realized_pnl, "f"),
            "paper_unmodeled_cashflow_count": len(paper_after.unmodeled_cashflows),
            "paper_snapshot_source": paper_after.snapshot_source,
            "paper_source_completeness": dict(paper_after.source_completeness),
            "clean_cohort_asset_ids": sorted(clean_assets),
            "out_of_scope_changed_assets": sorted(out_of_scope_changed_assets),
            "normalized_zero_baseline": clean_scope,
            "official_cohort_marked_value": (
                format(official_clean_marked_value, "f") if clean_scope else None
            ),
            "paper_cohort_marked_value": (
                format(paper_clean_marked_value, "f") if clean_scope else None
            ),
            "official_cohort_realized_pnl_delta": (
                format(official_clean_economic_realized_delta, "f")
                if clean_scope
                else None
            ),
            "official_cohort_reported_realized_pnl_delta": (
                format(official_clean_reported_realized_delta, "f")
                if clean_scope
                else None
            ),
            "official_cohort_realized_pnl_source": (
                official_clean_realized_source if clean_scope else None
            ),
            "paper_cohort_realized_pnl_delta": (
                format(paper_clean_realized_delta, "f") if clean_scope else None
            ),
        }
        summary["pnl_truth_contract"] = _pnl_truth_contract(
            comparison_rows=tuple(comparison_rows),
            account_truth_status=status,
            comparison_scope=comparison_scope,
            strategy_ids=paper_after.strategy_ids,
            official_position_count=(
                clean_open_asset_count if clean_scope else len(official.positions)
            ),
            official_closed_position_count=(
                clean_closed_asset_count
                if clean_scope
                else len(official.closed_positions)
            ),
            provisional_fill_count=paper_after.provisional_fill_count,
            unmodeled_cashflow_count=len(paper_after.unmodeled_cashflows),
            snapshot_source=paper_after.snapshot_source,
            clean_cohort_checks=(
                {
                    "baseline_after_cutover": True,
                    "baseline_asset_isolation": True,
                    "paper_zero_delta_baseline": True,
                    "cohort_asset_scope_bound": bool(clean_assets),
                    "cohort_economic_change_present": compared_assets > 0,
                    "no_out_of_scope_asset_changes": not out_of_scope_changed_assets,
                }
                if clean_scope
                else None
            ),
        )
        stable_payload = {
            "official_run_id": official.run_id,
            "baseline_id": baseline.get("baseline_id"),
            "strategy_ids": paper_after.strategy_ids,
            "as_of": official.source_as_of.isoformat(),
            "status": status.value,
            "summary": summary,
            "mismatches": [_mismatch_payload(item) for item in mismatches],
        }
        content_hash = hashlib.sha256(
            json.dumps(stable_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return AccountTruthReport(
            reconciliation_id=f"account-truth:{content_hash[:32]}",
            official_run_id=official.run_id,
            account_address=official.account_address,
            strategy_ids=paper_after.strategy_ids,
            as_of=official.source_as_of,
            generated_at=now.astimezone(timezone.utc),
            status=status,
            official_source_status=source_status,
            comparison_scope=comparison_scope,
            mismatches=tuple(sorted(mismatches, key=lambda row: row.mismatch_id)),
            summary=summary,
            content_sha256=content_hash,
            comparison_rows=tuple(comparison_rows),
        )

    def with_mismatches(
        self,
        *,
        report: AccountTruthReport,
        mismatches: Sequence[AccountTruthMismatch],
    ) -> AccountTruthReport:
        """Rebuild hashes and gate status after persisted convergence classification."""

        ordered = tuple(sorted(mismatches, key=lambda row: row.mismatch_id))
        source_items = [
            row for row in ordered if row.comparison_type == "OFFICIAL_SOURCE"
        ]
        if not source_items:
            source_status = "PASS"
        elif all(item.retryable for item in source_items):
            source_status = "PASS_WITH_TIMING_LAG"
        else:
            source_status = "SOURCE_CONFLICT"
        material = [item for item in ordered if not item.retryable]
        retryable = [item for item in ordered if item.retryable]
        if report.comparison_scope == "OFFICIAL_ONLY":
            status = AccountTruthGateStatus.INSUFFICIENT_EVIDENCE
        elif material:
            status = AccountTruthGateStatus.FAIL_ACCOUNT_TRUTH
        elif retryable:
            status = AccountTruthGateStatus.PASS_WITH_TIMING_LAG
        else:
            status = AccountTruthGateStatus.PASS
        summary = dict(report.summary)
        summary.update(
            {
                "mismatch_count": len(ordered),
                "material_mismatch_count": len(material),
                "retryable_mismatch_count": len(retryable),
                "mismatch_type_counts": _counts(
                    item.mismatch_type.value for item in ordered
                ),
                "official_source_status": source_status,
            }
        )
        clean_scope = report.comparison_scope == "CLEAN_V2_COHORT_DELTA"
        prior_contract = _mapping(summary.get("pnl_truth_contract"))
        prior_checks = _mapping(prior_contract.get("checks"))
        summary["pnl_truth_contract"] = _pnl_truth_contract(
            comparison_rows=report.comparison_rows,
            account_truth_status=status,
            comparison_scope=report.comparison_scope,
            strategy_ids=report.strategy_ids,
            official_position_count=int(
                (
                    summary.get("clean_open_position_count")
                    if clean_scope
                    else summary.get("official_position_count")
                )
                or 0
            ),
            official_closed_position_count=int(
                (
                    summary.get("clean_closed_position_count")
                    if clean_scope
                    else summary.get("official_closed_position_count")
                )
                or 0
            ),
            provisional_fill_count=summary.get("paper_provisional_fill_count"),
            unmodeled_cashflow_count=summary.get(
                "paper_unmodeled_cashflow_count"
            ),
            snapshot_source=summary.get("paper_snapshot_source", "PAPER_LEDGER"),
            clean_cohort_checks=(
                {
                    key: bool(prior_checks.get(key))
                    for key in CLEAN_V2_CONTRACT_CHECKS
                }
                if clean_scope
                else None
            ),
        )
        stable_payload = {
            "official_run_id": report.official_run_id,
            "strategy_ids": report.strategy_ids,
            "as_of": report.as_of.isoformat(),
            "comparison_scope": report.comparison_scope,
            "status": status.value,
            "summary": summary,
            "mismatches": [_mismatch_payload(item) for item in ordered],
        }
        content_hash = hashlib.sha256(
            json.dumps(stable_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return AccountTruthReport(
            reconciliation_id=f"account-truth:{content_hash[:32]}",
            official_run_id=report.official_run_id,
            account_address=report.account_address,
            strategy_ids=report.strategy_ids,
            as_of=report.as_of,
            generated_at=report.generated_at,
            status=status,
            official_source_status=source_status,
            comparison_scope=report.comparison_scope,
            mismatches=ordered,
            summary=summary,
            content_sha256=content_hash,
            comparison_rows=report.comparison_rows,
        )

    def _official_source_checks(
        self, official: OfficialAccountBundle
    ) -> list[AccountTruthMismatch]:
        mismatches: list[AccountTruthMismatch] = []
        data_positions = {row.asset_id: row for row in official.positions}
        csv_positions = {row.asset_id: row for row in official.accounting.positions}
        for asset_id in sorted(set(data_positions) | set(csv_positions)):
            data = data_positions.get(asset_id)
            csv_row = csv_positions.get(asset_id)
            item = categorical_mismatch(
                mismatch_type=MismatchType.SOURCE_CONFLICT,
                comparison_type="OFFICIAL_SOURCE",
                comparison_key=asset_id,
                field_name="asset_presence",
                official_value=data is not None,
                paper_value=csv_row is not None,
                reason="Data API /positions and accounting positions.csv disagree",
            )
            if item:
                mismatches.append(item)
                continue
            assert data is not None and csv_row is not None
            for field_name, data_value, csv_value, tolerance in (
                ("size", data.size, csv_row.size, self.quantity_tolerance),
                (
                    "current_price",
                    data.current_price,
                    csv_row.current_price,
                    self.price_tolerance,
                ),
                (
                    "current_value",
                    data.current_value,
                    csv_row.current_value,
                    max(self.money_tolerance, Decimal("0.0001")),
                ),
            ):
                sampled_value = field_name in {"current_price", "current_value"}
                item = decimal_mismatch(
                    mismatch_type=(
                        MismatchType.TIMING_LAG
                        if sampled_value
                        else MismatchType.SOURCE_CONFLICT
                    ),
                    comparison_type="OFFICIAL_SOURCE",
                    comparison_key=asset_id,
                    field_name=field_name,
                    official_value=data_value,
                    paper_value=csv_value,
                    tolerance=tolerance,
                    reason=(
                        "official sampled mark/value differs across non-atomic endpoints"
                        if sampled_value
                        else "official /positions and accounting CSV values disagree"
                    ),
                    retryable=sampled_value,
                )
                if item:
                    mismatches.append(item)
        accounting = official.accounting
        positions_value = sum(
            (row.current_value for row in accounting.positions), Decimal(0)
        )
        for field_name, expected, actual in (
            ("positions_value", accounting.equity.positions_value, positions_value),
            (
                "equity",
                accounting.equity.equity,
                accounting.equity.cash_balance + accounting.equity.positions_value,
            ),
        ):
            item = decimal_mismatch(
                mismatch_type=MismatchType.SOURCE_CONFLICT,
                comparison_type="OFFICIAL_SOURCE",
                comparison_key="equity.csv",
                field_name=field_name,
                official_value=expected,
                paper_value=actual,
                tolerance=self.money_tolerance,
                reason="official accounting snapshot violates its own value identity",
            )
            if item:
                mismatches.append(item)
        return mismatches

    def _paper_checks(
        self, official: OfficialAccountBundle, paper: PaperAccountSnapshot
    ) -> list[AccountTruthMismatch]:
        mismatches: list[AccountTruthMismatch] = []
        official_positions = {row.asset_id: row for row in official.positions}
        # Closed positions remain in the append-only Paper snapshot so their
        # realized PnL is preserved. They are not open-position presence rows.
        paper_positions = {
            asset_id: row
            for asset_id, row in paper.positions.items()
            if row.quantity > self.quantity_tolerance
            and not row.truth_surface_omitted
        }
        for asset_id in sorted(set(official_positions) | set(paper_positions)):
            truth = official_positions.get(asset_id)
            simulated = paper_positions.get(asset_id)
            item = categorical_mismatch(
                mismatch_type=MismatchType.POSITION_MISMATCH,
                comparison_type="OFFICIAL_VS_PAPER_POSITION",
                comparison_key=asset_id,
                field_name="asset_presence",
                official_value=truth is not None,
                paper_value=simulated is not None,
                reason="official and paper open-position universes disagree",
            )
            if item:
                mismatches.append(item)
                continue
            assert truth is not None and simulated is not None
            comparisons = (
                ("size", truth.size, simulated.quantity, self.quantity_tolerance),
                (
                    "avg_price",
                    truth.avg_price,
                    simulated.average_price,
                    self.price_tolerance,
                ),
                (
                    "initial_value",
                    truth.initial_value,
                    simulated.fee_exclusive_basis,
                    self.money_tolerance,
                ),
                (
                    "realized_pnl",
                    truth.realized_pnl,
                    simulated.realized_pnl,
                    self.money_tolerance,
                ),
                (
                    "current_value",
                    truth.current_value,
                    simulated.quantity * truth.current_price,
                    self.money_tolerance,
                ),
                (
                    "cash_pnl",
                    truth.cash_pnl,
                    simulated.quantity * truth.current_price
                    - simulated.fee_exclusive_basis,
                    self.money_tolerance,
                ),
            )
            if truth.gross_initial_value is not None:
                comparisons += (
                    (
                        "gross_initial_value",
                        truth.gross_initial_value,
                        simulated.cost_basis,
                        self.money_tolerance,
                    ),
                )
            if truth.entry_fees_usdc is not None:
                comparisons += (
                    (
                        "entry_fees_usdc",
                        truth.entry_fees_usdc,
                        simulated.entry_fees,
                        self.money_tolerance,
                    ),
                )
            for field_name, official_value, paper_value, tolerance in comparisons:
                item = decimal_mismatch(
                    comparison_type="OFFICIAL_VS_PAPER_POSITION",
                    comparison_key=asset_id,
                    field_name=field_name,
                    official_value=official_value,
                    paper_value=paper_value,
                    tolerance=tolerance,
                    reason="paper as-of position differs from official account truth",
                    evidence={
                        "condition_id": truth.condition_id,
                        "paper_condition_id": simulated.condition_id,
                    },
                )
                if item:
                    mismatches.append(item)
            item = categorical_mismatch(
                mismatch_type=MismatchType.POSITION_MISMATCH,
                comparison_type="OFFICIAL_VS_PAPER_POSITION",
                comparison_key=asset_id,
                field_name="condition_id",
                official_value=truth.condition_id.lower(),
                paper_value=simulated.condition_id.lower(),
                reason="paper asset is associated with a different condition",
            )
            if item:
                mismatches.append(item)
            for field_name, official_value, paper_value in (
                ("redeemable", truth.redeemable, simulated.redeemable),
                ("mergeable", truth.mergeable, simulated.mergeable),
            ):
                if paper_value is None:
                    continue
                item = categorical_mismatch(
                    mismatch_type=MismatchType.POSITION_MISMATCH,
                    comparison_type="OFFICIAL_VS_PAPER_POSITION",
                    comparison_key=asset_id,
                    field_name=field_name,
                    official_value=official_value,
                    paper_value=paper_value,
                    reason=(
                        "paper lifecycle eligibility differs from official position state"
                    ),
                )
                if item:
                    mismatches.append(item)
        closed_realized = _official_closed_realized_by_asset(official)
        for asset_id, official_value in sorted(closed_realized.items()):
            simulated = paper.positions.get(asset_id)
            paper_value = simulated.realized_pnl if simulated is not None else Decimal(0)
            item = decimal_mismatch(
                comparison_type="OFFICIAL_VS_PAPER_CLOSED_POSITION",
                comparison_key=asset_id,
                field_name="realized_pnl",
                official_value=official_value,
                paper_value=paper_value,
                tolerance=self.money_tolerance,
                reason="paper closed-position realized PnL differs from official history",
                evidence={
                    "paper_state_present": simulated is not None,
                    "also_current_position": asset_id in official_positions,
                },
            )
            if item:
                mismatches.append(item)
        accounting_positions = {
            row.asset_id: row for row in official.accounting.positions
        }
        official_marked_paper_value = sum(
            (
                row.quantity * accounting_positions[asset].current_price
                for asset, row in paper_positions.items()
                if asset in accounting_positions
            ),
            Decimal(0),
        )
        official_realized_pnl = _official_total_realized_pnl(official)
        official_total_pnl = official_realized_pnl + sum(
            (row.cash_pnl for row in official.positions), Decimal(0)
        )
        paper_unrealized_pnl = sum(
            (
                row.quantity * official_positions[asset].current_price
                - row.fee_exclusive_basis
                for asset, row in paper_positions.items()
                if asset in official_positions
            ),
            Decimal(0),
        )
        for field_name, official_value, paper_value in (
            (
                "cash_balance",
                official.accounting.equity.cash_balance,
                paper.cash_balance,
            ),
            (
                "equity",
                official.accounting.equity.equity,
                paper.cash_balance + official_marked_paper_value,
            ),
            ("realized_pnl", official_realized_pnl, paper.realized_pnl),
            (
                "total_pnl",
                official_total_pnl,
                paper.realized_pnl + paper_unrealized_pnl,
            ),
        ):
            item = decimal_mismatch(
                mismatch_type=(
                    MismatchType.UNREALIZED_PNL_MISMATCH
                    if field_name == "total_pnl"
                    else None
                ),
                comparison_type="OFFICIAL_VS_PAPER_ACCOUNT",
                comparison_key="account",
                field_name=field_name,
                official_value=official_value,
                paper_value=paper_value,
                tolerance=self.money_tolerance,
                reason="paper as-of account total differs from official accounting snapshot",
            )
            if item:
                mismatches.append(item)
        if paper.provisional_fill_count:
            mismatches.append(
                informational_mismatch(
                    mismatch_type=MismatchType.FINALITY_MISMATCH,
                    comparison_type="PAPER_FINALITY",
                    comparison_key="account",
                    field_name="provisional_fill_count",
                    reason="paper account still contains non-final fill evidence",
                    retryable=True,
                    evidence={"count": paper.provisional_fill_count},
                )
            )
        for index, row in enumerate(paper.unmodeled_cashflows):
            mismatches.append(
                informational_mismatch(
                    mismatch_type=MismatchType.UNMODELED_CASHFLOW,
                    comparison_type="PAPER_CASHFLOW",
                    comparison_key=str(row.get("event_id") or index),
                    field_name="event_type",
                    reason="paper account return contains an unmodeled cashflow",
                    retryable=False,
                    evidence=row,
                )
            )
        return mismatches

    def _paper_comparison_rows(
        self, official: OfficialAccountBundle, paper: PaperAccountSnapshot
    ) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        official_positions = {row.asset_id: row for row in official.positions}
        paper_positions = {
            asset_id: row
            for asset_id, row in paper.positions.items()
            if row.quantity > self.quantity_tolerance
            and not row.truth_surface_omitted
        }
        for asset_id in sorted(set(official_positions) | set(paper_positions)):
            truth = official_positions.get(asset_id)
            simulated = paper_positions.get(asset_id)
            rows.append(
                _categorical_comparison_row(
                    comparison_type="OFFICIAL_VS_PAPER_POSITION",
                    comparison_key=asset_id,
                    field_name="asset_presence",
                    official_value=truth is not None,
                    paper_value=simulated is not None,
                )
            )
            if truth is None or simulated is None:
                continue
            evidence = {
                "condition_id": truth.condition_id,
                "paper_condition_id": simulated.condition_id,
            }
            comparisons = (
                ("size", truth.size, simulated.quantity, self.quantity_tolerance),
                (
                    "avg_price",
                    truth.avg_price,
                    simulated.average_price,
                    self.price_tolerance,
                ),
                (
                    "initial_value",
                    truth.initial_value,
                    simulated.fee_exclusive_basis,
                    self.money_tolerance,
                ),
                (
                    "realized_pnl",
                    truth.realized_pnl,
                    simulated.realized_pnl,
                    self.money_tolerance,
                ),
                (
                    "current_value",
                    truth.current_value,
                    simulated.quantity * truth.current_price,
                    self.money_tolerance,
                ),
                (
                    "cash_pnl",
                    truth.cash_pnl,
                    simulated.quantity * truth.current_price
                    - simulated.fee_exclusive_basis,
                    self.money_tolerance,
                ),
            )
            if truth.gross_initial_value is not None:
                comparisons += (
                    (
                        "gross_initial_value",
                        truth.gross_initial_value,
                        simulated.cost_basis,
                        self.money_tolerance,
                    ),
                )
            if truth.entry_fees_usdc is not None:
                comparisons += (
                    (
                        "entry_fees_usdc",
                        truth.entry_fees_usdc,
                        simulated.entry_fees,
                        self.money_tolerance,
                    ),
                )
            rows.extend(
                _decimal_comparison_row(
                    comparison_type="OFFICIAL_VS_PAPER_POSITION",
                    comparison_key=asset_id,
                    field_name=field_name,
                    official_value=official_value,
                    paper_value=paper_value,
                    tolerance=tolerance,
                    evidence=evidence,
                )
                for field_name, official_value, paper_value, tolerance in comparisons
            )
            rows.append(
                _categorical_comparison_row(
                    comparison_type="OFFICIAL_VS_PAPER_POSITION",
                    comparison_key=asset_id,
                    field_name="condition_id",
                    official_value=truth.condition_id.lower(),
                    paper_value=simulated.condition_id.lower(),
                )
            )
            for field_name, official_value, paper_value in (
                ("redeemable", truth.redeemable, simulated.redeemable),
                ("mergeable", truth.mergeable, simulated.mergeable),
            ):
                if paper_value is not None:
                    rows.append(
                        _categorical_comparison_row(
                            comparison_type="OFFICIAL_VS_PAPER_POSITION",
                            comparison_key=asset_id,
                            field_name=field_name,
                            official_value=official_value,
                            paper_value=paper_value,
                        )
                    )
        closed_realized = _official_closed_realized_by_asset(official)
        for asset_id, official_value in sorted(closed_realized.items()):
            simulated = paper.positions.get(asset_id)
            rows.append(
                _decimal_comparison_row(
                    comparison_type="OFFICIAL_VS_PAPER_CLOSED_POSITION",
                    comparison_key=asset_id,
                    field_name="realized_pnl",
                    official_value=official_value,
                    paper_value=(
                        simulated.realized_pnl
                        if simulated is not None
                        else Decimal(0)
                    ),
                    tolerance=self.money_tolerance,
                    evidence={
                        "paper_state_present": simulated is not None,
                        "also_current_position": asset_id in official_positions,
                    },
                )
            )
        accounting_positions = {
            row.asset_id: row for row in official.accounting.positions
        }
        official_marked_paper_value = sum(
            (
                row.quantity * accounting_positions[asset].current_price
                for asset, row in paper_positions.items()
                if asset in accounting_positions
            ),
            Decimal(0),
        )
        official_realized_pnl = _official_total_realized_pnl(official)
        official_total_pnl = official_realized_pnl + sum(
            (row.cash_pnl for row in official.positions), Decimal(0)
        )
        paper_unrealized_pnl = sum(
            (
                row.quantity * official_positions[asset].current_price
                - row.fee_exclusive_basis
                for asset, row in paper_positions.items()
                if asset in official_positions
            ),
            Decimal(0),
        )
        rows.extend(
            _decimal_comparison_row(
                comparison_type="OFFICIAL_VS_PAPER_ACCOUNT",
                comparison_key="account",
                field_name=field_name,
                official_value=official_value,
                paper_value=paper_value,
                tolerance=self.money_tolerance,
            )
            for field_name, official_value, paper_value in (
                (
                    "cash_balance",
                    official.accounting.equity.cash_balance,
                    paper.cash_balance,
                ),
                (
                    "equity",
                    official.accounting.equity.equity,
                    paper.cash_balance + official_marked_paper_value,
                ),
                ("realized_pnl", official_realized_pnl, paper.realized_pnl),
                (
                    "total_pnl",
                    official_total_pnl,
                    paper.realized_pnl + paper_unrealized_pnl,
                ),
            )
        )
        rows.append(
            _decimal_comparison_row(
                comparison_type="PAPER_FINALITY",
                comparison_key="account",
                field_name="provisional_fill_count",
                official_value=Decimal(0),
                paper_value=Decimal(paper.provisional_fill_count),
                tolerance=Decimal(0),
            )
        )
        return rows


def _official_closed_realized_by_asset(
    official: OfficialAccountBundle,
) -> dict[str, Decimal]:
    realized: dict[str, Decimal] = defaultdict(Decimal)
    for row in official.closed_positions:
        realized[row.asset_id] += row.realized_pnl
    return dict(realized)


def _official_total_realized_pnl(official: OfficialAccountBundle) -> Decimal:
    """Combine current and closed Data API surfaces without exact duplicates.

    A residual position can appear in both endpoints with the same cumulative
    realized PnL. Summing that exact overlap double counts one economic result.
    Distinct values remain additive because they may describe separate cycles.
    """

    current = {row.asset_id: row.realized_pnl for row in official.positions}
    total = sum(current.values(), Decimal(0))
    for asset_id, value in _official_closed_realized_by_asset(official).items():
        if asset_id in current and value == current[asset_id]:
            continue
        total += value
    return total


def _table_exists(cur: Any, qualified_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) AS name", (qualified_name,))
    return cur.fetchone()["name"] is not None


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal(0)
    return Decimal(str(value))


def _decimal_source_unit(value: Decimal) -> Decimal:
    """Return the least significant unit explicitly exposed by a non-zero value."""

    decimal = Decimal(value)
    exponent = decimal.as_tuple().exponent
    return Decimal(1).scaleb(exponent) if decimal and exponent < 0 else Decimal(0)


def _pnl_truth_contract(
    *,
    comparison_rows: Sequence[Mapping[str, Any]],
    account_truth_status: AccountTruthGateStatus,
    comparison_scope: str,
    strategy_ids: Sequence[str],
    official_position_count: int,
    official_closed_position_count: int,
    provisional_fill_count: int | None,
    unmodeled_cashflow_count: int | None,
    snapshot_source: str | None = "PAPER_LEDGER",
    clean_cohort_checks: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Describe exactly which official PnL claims this comparison can support."""

    position_fields = {
        str(row.get("field_name") or "")
        for row in comparison_rows
        if row.get("comparison_type")
        in {
            "OFFICIAL_VS_PAPER_POSITION",
            "OFFICIAL_VS_PAPER_COHORT_POSITION",
        }
    }
    account_fields = {
        str(row.get("field_name") or "")
        for row in comparison_rows
        if row.get("comparison_type")
        in {
            "OFFICIAL_VS_PAPER_ACCOUNT",
            "OFFICIAL_VS_PAPER_COHORT_ACCOUNT",
        }
    }
    closed_position_fields = {
        str(row.get("field_name") or "")
        for row in comparison_rows
        if row.get("comparison_type") == "OFFICIAL_VS_PAPER_CLOSED_POSITION"
    }
    finality_fields = {
        str(row.get("field_name") or "")
        for row in comparison_rows
        if row.get("comparison_type") == "PAPER_FINALITY"
    }
    clean_scope = comparison_scope == "CLEAN_V2_COHORT_DELTA"
    required_position_fields = (
        PNL_POSITION_FIELDS - PNL_CLOSED_POSITION_FIELDS
        if clean_scope and official_position_count == 0
        else PNL_POSITION_FIELDS
    )
    checks = {
        "account_truth_exact": account_truth_status is AccountTruthGateStatus.PASS,
        (
            "clean_v2_cohort_scope" if clean_scope else "whole_account_scope"
        ): (
            clean_scope if clean_scope else comparison_scope == "WHOLE_ACCOUNT"
        ),
        "strategy_binding_present": bool(tuple(strategy_ids)),
        "open_position_evidence_present": (
            True if clean_scope and official_position_count == 0 else official_position_count > 0
        ),
        "closed_position_fields_complete": (
            official_closed_position_count == 0
            or PNL_CLOSED_POSITION_FIELDS <= closed_position_fields
        ),
        "dual_cost_and_position_fields_complete": (
            required_position_fields <= position_fields
        ),
        "account_total_fields_complete": PNL_ACCOUNT_FIELDS <= account_fields,
        "finality_field_present": "provisional_fill_count" in finality_fields,
        "provisional_fill_count_zero": provisional_fill_count == 0,
        "unmodeled_cashflow_count_zero": unmodeled_cashflow_count == 0,
        "authoritative_paper_ledger_source": snapshot_source == "PAPER_LEDGER",
    }
    if clean_scope:
        checks.update(dict(clean_cohort_checks or {}))
    reference_checks = {
        key: value
        for key, value in checks.items()
        if key != "authoritative_paper_ledger_source"
    }
    if all(checks.values()):
        status = "PASS"
    elif snapshot_source == "CHAIN_ACTIVITY_MIRROR" and all(
        reference_checks.values()
    ):
        status = "REFERENCE_REPLAY_PASS"
    elif comparison_scope == "CALIBRATION_DELTA" and comparison_rows:
        status = "PARTIAL_OPERATION_DELTA"
    elif account_truth_status is AccountTruthGateStatus.FAIL_ACCOUNT_TRUTH:
        status = "FAIL"
    else:
        status = "INSUFFICIENT_EVIDENCE"
    return {
        "schema_version": "official-pnl-truth-contract-v1",
        "status": status,
        "snapshot_source": snapshot_source,
        "checks": checks,
        "position_fields_compared": sorted(position_fields),
        "closed_position_fields_compared": sorted(closed_position_fields),
        "account_fields_compared": sorted(account_fields),
        "finality_fields_compared": sorted(finality_fields),
        "missing_position_fields": sorted(required_position_fields - position_fields),
        "missing_closed_position_fields": sorted(
            PNL_CLOSED_POSITION_FIELDS - closed_position_fields
            if official_closed_position_count
            else ()
        ),
        "missing_account_fields": sorted(PNL_ACCOUNT_FIELDS - account_fields),
        "claim": (
            "OFFICIAL_POST_V2_COHORT_PNL_CURVE_EQUIVALENCE"
            if status == "PASS" and clean_scope
            else "OFFICIAL_AS_OF_PNL_CURVE_EQUIVALENCE"
            if status == "PASS"
            else "INDEPENDENT_CHAIN_ACCOUNTING_REPLAY"
            if status == "REFERENCE_REPLAY_PASS"
            else "OPERATION_DELTA_ONLY"
            if status == "PARTIAL_OPERATION_DELTA"
            else "NOT_ESTABLISHED"
        ),
    }


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _paper_value(position: PaperPosition | None, field_name: str) -> Decimal:
    return _decimal(getattr(position, field_name, 0) if position is not None else 0)


def _decimal_comparison_row(
    *,
    comparison_type: str,
    comparison_key: str,
    field_name: str,
    official_value: Decimal,
    paper_value: Decimal,
    tolerance: Decimal,
    evidence: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    delta = paper_value - official_value
    return {
        "comparison_type": comparison_type,
        "comparison_key": comparison_key,
        "field_name": field_name,
        "official_value": format(official_value, "f"),
        "paper_value": format(paper_value, "f"),
        "delta": format(delta, "f"),
        "tolerance": format(abs(tolerance), "f"),
        "status": "MATCH" if abs(delta) <= abs(tolerance) else "MISMATCH",
        "evidence": dict(evidence or {}),
    }


def _categorical_comparison_row(
    *,
    comparison_type: str,
    comparison_key: str,
    field_name: str,
    official_value: Any,
    paper_value: Any,
) -> Mapping[str, Any]:
    return {
        "comparison_type": comparison_type,
        "comparison_key": comparison_key,
        "field_name": field_name,
        "official_value": None if official_value is None else str(official_value),
        "paper_value": None if paper_value is None else str(paper_value),
        "delta": None,
        "tolerance": "0",
        "status": "MATCH" if official_value == paper_value else "MISMATCH",
        "evidence": {},
    }


def _official_delta_positions(
    official: OfficialAccountBundle,
) -> dict[str, Mapping[str, Any]]:
    closed_realized: dict[str, Decimal] = defaultdict(Decimal)
    for row in official.closed_positions:
        closed_realized[row.asset_id] += row.realized_pnl
    result: dict[str, Mapping[str, Any]] = {}
    for row in official.positions:
        closed_value = closed_realized.pop(row.asset_id, Decimal(0))
        if closed_value == row.realized_pnl:
            closed_value = Decimal(0)
        result[row.asset_id] = {
            "condition_id": row.condition_id,
            "size": row.size,
            "avg_price": row.avg_price,
            "initial_value": row.initial_value,
            "gross_initial_value": row.gross_initial_value,
            "entry_fees_usdc": row.entry_fees_usdc,
            "current_price": row.current_price,
            "current_value": row.current_value,
            "cash_pnl": row.cash_pnl,
            "total_realized_pnl": row.realized_pnl + closed_value,
            "redeemable": row.redeemable,
            "mergeable": row.mergeable,
        }
    for asset_id, realized in closed_realized.items():
        result[asset_id] = {
            "condition_id": "",
            "size": Decimal(0),
            "avg_price": Decimal(0),
            "initial_value": Decimal(0),
            "gross_initial_value": Decimal(0),
            "entry_fees_usdc": Decimal(0),
            "current_price": Decimal(0),
            "current_value": Decimal(0),
            "cash_pnl": Decimal(0),
            "total_realized_pnl": realized,
            "redeemable": False,
            "mergeable": False,
        }
    return result


def _counts(values: Any) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    for value in values:
        result[str(value)] += 1
    return dict(sorted(result.items()))


def _mismatch_payload(item: AccountTruthMismatch) -> dict[str, Any]:
    return {
        "mismatch_id": item.mismatch_id,
        "mismatch_type": item.mismatch_type.value,
        "comparison_type": item.comparison_type,
        "comparison_key": item.comparison_key,
        "field_name": item.field_name,
        "official_value": item.official_value,
        "paper_value": item.paper_value,
        "delta": item.delta,
        "tolerance": item.tolerance,
        "reason": item.reason,
        "severity": item.severity,
        "retryable": item.retryable,
        "evidence": dict(item.evidence),
    }
