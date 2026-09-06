"""Point-in-time PostgreSQL inputs for the central paper event-risk gate."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .admission import EventRiskAdmissionInput
from .event_exposure import EventRiskPosition
from .outcome_scenarios import OutcomeScenario


def load_event_risk_input(
    cur: Any,
    intent: Any,
    *,
    candidate_event_id: str,
    candidate_category: str,
) -> EventRiskAdmissionInput:
    cur.execute(
        """
        SELECT p.asset_id,p.condition_id,p.quantity,p.cost_basis,
               COALESCE(catalog.event_id,p.condition_id) AS event_id,
               COALESCE(NULLIF(catalog.category,''),'unknown') AS category,
               COALESCE(catalog.enable_neg_risk,FALSE) AS enable_neg_risk,
               marks.liquidation_mark,marks.mark_quality,marks.mark_age_ms,
               marks.exit_levels,
               resolution.phase AS resolution_phase
        FROM quant.paper_positions p
        LEFT JOIN quant.paper_execution_market_catalog catalog
          ON catalog.asset_id=p.asset_id
        LEFT JOIN quant.paper_position_marks marks
          ON marks.strategy_id=p.strategy_id AND marks.asset_id=p.asset_id
        LEFT JOIN quant.market_resolution_states resolution
          ON resolution.condition_id=p.condition_id
        WHERE p.strategy_id=%s AND p.quantity>0
        ORDER BY p.condition_id,p.asset_id
        """,
        (str(intent.strategy_id),),
    )
    raw_positions = [dict(row) for row in cur.fetchall()]
    positions = tuple(_position(row, str(intent.strategy_id)) for row in raw_positions)
    conditions = {str(intent.condition_id)} | {
        str(row["condition_id"]) for row in raw_positions
    }
    cur.execute(
        """
        SELECT DISTINCT catalog.asset_id,catalog.condition_id,
               catalog.outcome_name,catalog.outcome_index,
               COALESCE(catalog.event_id,catalog.condition_id) AS event_id,
               COALESCE(NULLIF(catalog.category,''),'unknown') AS category,
               COALESCE(catalog.enable_neg_risk,FALSE) AS enable_neg_risk
        FROM quant.paper_execution_market_catalog catalog
        WHERE catalog.condition_id=ANY(%s::text[])
        ORDER BY event_id,catalog.condition_id,catalog.outcome_index,catalog.asset_id
        """,
        (sorted(conditions),),
    )
    token_rows = [dict(row) for row in cur.fetchall()]
    neg_risk_events = sorted(
        {str(row["event_id"]) for row in token_rows if bool(row.get("enable_neg_risk"))}
    )
    if neg_risk_events:
        cur.execute(
            """
            SELECT DISTINCT catalog.asset_id,catalog.condition_id,
                   catalog.outcome_name,catalog.outcome_index,
                   catalog.event_id,
                   COALESCE(NULLIF(catalog.category,''),'unknown') AS category,
                   TRUE AS enable_neg_risk
            FROM quant.paper_execution_market_catalog catalog
            WHERE catalog.event_id=ANY(%s::text[])
              AND COALESCE(catalog.enable_neg_risk,FALSE)=TRUE
            ORDER BY event_id,catalog.condition_id,
                     catalog.outcome_index,catalog.asset_id
            """,
            (neg_risk_events,),
        )
        combined = {
            (str(row["condition_id"]), str(row["asset_id"])): row for row in token_rows
        }
        combined.update(
            {
                (str(row["condition_id"]), str(row["asset_id"])): dict(row)
                for row in cur.fetchall()
            }
        )
        token_rows = list(combined.values())
    scenarios, reasons = _build_scenarios(token_rows)
    covered_assets = {
        str(asset_id) for scenario in scenarios for asset_id in scenario.payout_by_asset
    }
    required_assets = {str(intent.asset_id)} | {
        position.asset_id for position in positions
    }
    missing_assets = sorted(required_assets - covered_assets)
    if missing_assets:
        reasons.append("missing_payout_asset:" + ",".join(missing_assets))
    if not scenarios:
        reasons.append("missing_payout_scenarios")
    return EventRiskAdmissionInput(
        positions=positions,
        scenarios=tuple(scenarios),
        status="READY" if not reasons else "UNAVAILABLE",
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _position(row: dict[str, Any], strategy_id: str) -> EventRiskPosition:
    quantity = Decimal(str(row["quantity"]))
    liquidation_value = _walk_persisted_exit_levels(
        quantity,
        row.get("exit_levels"),
    )
    resolution_phase = str(row.get("resolution_phase") or "").upper()
    locked = (
        Decimal(str(row["cost_basis"]))
        if resolution_phase and resolution_phase != "REDEEMED"
        else Decimal(0)
    )
    return EventRiskPosition(
        event_id=str(row["event_id"]),
        category=str(row.get("category") or "unknown"),
        strategy_id=strategy_id,
        asset_id=str(row["asset_id"]),
        quantity=quantity,
        cost_basis=Decimal(str(row["cost_basis"])),
        current_liquidation_value=liquidation_value,
        dispute_locked_capital=locked,
        condition_id=str(row["condition_id"]),
    )


def _walk_persisted_exit_levels(
    quantity: Decimal,
    payload: Any,
) -> Decimal | None:
    from .liquidity_risk import ExitLevel, walk_exit_book

    if not isinstance(payload, list) or not payload:
        return None
    levels = tuple(
        ExitLevel(Decimal(str(row["price"])), Decimal(str(row["size"])))
        for row in payload
        if isinstance(row, dict) and "price" in row and "size" in row
    )
    if not levels:
        return None
    result = walk_exit_book(quantity, levels)
    return result.executable_value if result.unliquidated_quantity == 0 else None


def _build_scenarios(
    token_rows: list[dict[str, Any]],
) -> tuple[list[OutcomeScenario], list[str]]:
    by_event: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    event_neg_risk: dict[str, bool] = {}
    for row in token_rows:
        event_id = str(row["event_id"])
        condition_id = str(row["condition_id"])
        by_event[event_id][condition_id].append(row)
        event_neg_risk[event_id] = event_neg_risk.get(event_id, False) or bool(
            row.get("enable_neg_risk")
        )

    scenarios: list[OutcomeScenario] = []
    reasons: list[str] = []
    for event_id, condition_rows in sorted(by_event.items()):
        normalized: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for condition_id, rows in sorted(condition_rows.items()):
            unique = {str(row["asset_id"]): row for row in rows}
            ordered = sorted(
                unique.values(),
                key=lambda row: (
                    int(row["outcome_index"])
                    if row.get("outcome_index") is not None
                    else 99,
                    str(row["asset_id"]),
                ),
            )
            if len(ordered) != 2:
                reasons.append(f"condition_token_count:{condition_id}:{len(ordered)}")
                continue
            normalized[condition_id] = (ordered[0], ordered[1])

        if len(normalized) != len(condition_rows):
            continue
        if event_neg_risk.get(event_id, False):
            for winning_condition in sorted(normalized):
                payouts: dict[str, Decimal] = {}
                for condition_id, (yes_token, no_token) in normalized.items():
                    payouts[str(yes_token["asset_id"])] = Decimal(
                        1 if condition_id == winning_condition else 0
                    )
                    payouts[str(no_token["asset_id"])] = Decimal(
                        0 if condition_id == winning_condition else 1
                    )
                scenarios.append(
                    OutcomeScenario(
                        scenario_id=f"{event_id}:winner:{winning_condition}",
                        event_id=event_id,
                        payout_by_asset=payouts,
                    )
                )
            scenarios.append(
                OutcomeScenario(
                    scenario_id=f"{event_id}:other",
                    event_id=event_id,
                    payout_by_asset={
                        str(token["asset_id"]): Decimal(index == 1)
                        for pair in normalized.values()
                        for index, token in enumerate(pair)
                    },
                )
            )
            continue

        for condition_id, pair in sorted(normalized.items()):
            for winner_index, winner in enumerate(pair):
                scenarios.append(
                    OutcomeScenario(
                        scenario_id=(
                            f"{event_id}:{condition_id}:winner:{winner['asset_id']}"
                        ),
                        event_id=event_id,
                        payout_by_asset={
                            str(token["asset_id"]): Decimal(index == winner_index)
                            for index, token in enumerate(pair)
                        },
                        condition_id=condition_id,
                    )
                )
    return scenarios, reasons
