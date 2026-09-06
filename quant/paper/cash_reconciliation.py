"""Authoritative cash-delta sources used by paper-account reconciliation."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

PAPER_LEDGER = "PAPER_LEDGER"
ACCOUNT_CASHFLOW = "ACCOUNT_CASHFLOW"
REWARD_PAYOUT = "REWARD_PAYOUT"
REWARD_CLAWBACK = "REWARD_CLAWBACK"
COMBO_ACCOUNTING = "COMBO_ACCOUNTING"
TOTAL = "TOTAL"

_SOURCE_QUERIES = (
    (
        PAPER_LEDGER,
        "quant.paper_ledger_entries",
        "SELECT strategy_id,cash_delta FROM quant.paper_ledger_entries",
    ),
    (
        ACCOUNT_CASHFLOW,
        "quant.paper_account_cashflow_operations",
        """
        SELECT strategy_id,cash_delta
        FROM quant.paper_account_cashflow_operations
        WHERE state='CONFIRMED' AND cash_applied=TRUE
        """,
    ),
    (
        REWARD_PAYOUT,
        "quant.paper_reward_payouts",
        """
        SELECT strategy_id,amount AS cash_delta
        FROM quant.paper_reward_payouts
        WHERE status='RECEIVED'
        """,
    ),
    (
        REWARD_CLAWBACK,
        "quant.paper_reward_clawbacks",
        """
        SELECT strategy_id,-amount AS cash_delta
        FROM quant.paper_reward_clawbacks
        WHERE status='RECEIVED'
        """,
    ),
    (
        COMBO_ACCOUNTING,
        "quant.simulator_combo_accounting_events",
        """
        SELECT strategy_id,cash_delta
        FROM quant.simulator_combo_accounting_events
        """,
    ),
)


def load_account_cash_delta_breakdown(
    cur: Any,
    *,
    strategy_ids: Sequence[str] | None = None,
) -> dict[str, dict[str, Decimal]]:
    """Return every persisted source that has actually changed account cash.

    Optional subsystems use separate immutable journals. Reconciliation must
    include those journals without double-counting their derived economic-event
    rows.
    """

    selected = (
        tuple(dict.fromkeys(str(item) for item in strategy_ids if str(item)))
        if strategy_ids is not None
        else None
    )
    if selected == ():
        return {}

    available: list[tuple[str, str]] = []
    for source, relation, query in _SOURCE_QUERIES:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS available", (relation,))
        if bool(cur.fetchone()["available"]):
            available.append((source, query))
    if not available:
        return {}

    union = " UNION ALL ".join(
        f"SELECT strategy_id,%s::text AS source,cash_delta FROM ({query}) rows"
        for _source, query in available
    )
    params: list[Any] = [source for source, _query in available]
    predicate = ""
    if selected is not None:
        predicate = "WHERE strategy_id=ANY(%s::text[])"
        params.append(list(selected))
    cur.execute(
        f"""
        SELECT strategy_id,source,COALESCE(sum(cash_delta),0) AS cash_delta
        FROM ({union}) cash_events
        {predicate}
        GROUP BY strategy_id,source
        ORDER BY strategy_id,source
        """,
        tuple(params),
    )

    result: dict[str, dict[str, Decimal]] = {}
    for row in cur.fetchall():
        strategy_id = str(row["strategy_id"])
        source = str(row["source"])
        result.setdefault(strategy_id, {})[source] = Decimal(row["cash_delta"])
    for breakdown in result.values():
        breakdown[TOTAL] = sum(breakdown.values(), Decimal(0))
    return result
