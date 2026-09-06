"""Paper execution and lifecycle-reconciliation adapter for fill finality."""

from __future__ import annotations

from datetime import datetime, timezone

from .finality_model import FillFragment, FinalityTrade
from .finality_store import PostgresFillFinalityStore


class DurablePaperFillFinality:
    def __init__(self, store: PostgresFillFinalityStore) -> None:
        self.store = store

    def record_result(
        self,
        result,
        *,
        paper_intent_id: int | None = None,
    ) -> tuple[FinalityTrade, ...]:
        trades: list[FinalityTrade] = []
        intent = result.intent
        for fill_index, fill in enumerate(result.fills):
            trade_id = self.trade_id(result.audit_key, fill_index)
            trades.append(
                self.store.record_match(
                    FillFragment(
                        trade_id=trade_id,
                        account_id=str(intent.strategy_id),
                        strategy_id=str(intent.strategy_id),
                        asset_id=str(intent.asset_id),
                        side=str(intent.side),
                        size=fill.size,
                        price=fill.price,
                        fee=fill.fee,
                        matched_at=result.arrival_ts,
                    ),
                    event_id=f"{trade_id}:matched-provisional",
                    audit_key=result.audit_key,
                    fill_index=fill_index,
                    paper_intent_id=paper_intent_id,
                )
            )
        return tuple(trades)

    def reconcile(
        self,
        trade_id: str,
        *,
        outcome: str,
        event_id: str,
        event_ts: datetime | None = None,
        reason: str = "lifecycle_reconciliation",
    ) -> FinalityTrade:
        observed_at = event_ts or datetime.now(timezone.utc)
        selected = str(outcome).upper()
        if selected == "RETRYING":
            return self.store.mark_retrying(
                trade_id,
                event_id=event_id,
                event_ts=observed_at,
            )
        if selected == "CONFIRMED":
            return self.store.confirm(
                trade_id,
                event_id=event_id,
                event_ts=observed_at,
            )
        if selected in {"FAILED", "VOIDED", "FAILED_REVERSED"}:
            return self.store.fail_and_void(
                trade_id,
                event_id=event_id,
                event_ts=observed_at,
                reason=reason,
            )
        raise ValueError(f"unsupported finality reconciliation outcome: {outcome}")

    def reconcile_result(
        self,
        result,
        *,
        outcome: str = "CONFIRMED",
        reason: str = "paper_order_lifecycle_and_ledger_confirmed",
    ) -> tuple[FinalityTrade, ...]:
        return tuple(
            self.reconcile(
                self.trade_id(result.audit_key, fill_index),
                outcome=outcome,
                event_id=(
                    f"{self.trade_id(result.audit_key, fill_index)}:"
                    f"auto-{str(outcome).lower()}"
                ),
                event_ts=result.arrival_ts,
                reason=reason,
            )
            for fill_index, _fill in enumerate(result.fills)
        )

    def reconcile_pending_from_lifecycle(self, *, limit: int = 1000) -> int:
        reconciled = 0
        for candidate in self.store.pending_lifecycle_reconciliation(limit=limit):
            self.reconcile(
                candidate.trade_id,
                outcome=candidate.outcome,
                event_id=(
                    f"{candidate.trade_id}:lifecycle:"
                    f"{candidate.outcome.lower()}"
                ),
                event_ts=candidate.event_ts,
                reason=candidate.reason,
            )
            reconciled += 1
        return reconciled

    @staticmethod
    def trade_id(audit_key: str, fill_index: int) -> str:
        return f"paper:{audit_key}:{int(fill_index)}"
