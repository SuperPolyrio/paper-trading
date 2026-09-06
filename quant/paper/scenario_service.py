"""Tenant-scoped scenario research kept separate from executable paper PnL."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from quant.simulator.kernel.deterministic_id import stable_hash

from .tenant_platform import (
    PaperPermission,
    PostgresTenantPlatformStore,
    TenantPrincipal,
)


class ScenarioServiceError(RuntimeError):
    pass


class ScenarioValidationError(ScenarioServiceError):
    pass


class ScenarioNotFound(ScenarioServiceError):
    pass


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        selected = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ScenarioValidationError(f"{field} must be numeric") from exc
    if not selected.is_finite():
        raise ScenarioValidationError(f"{field} must be finite")
    return selected


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def normalize_scenario_inputs(payload: Mapping[str, Any]) -> dict[str, Any]:
    payout_by_asset = {
        str(asset_id): _decimal(value, field=f"payout_by_asset.{asset_id}")
        for asset_id, value in dict(payload.get("payout_by_asset") or {}).items()
    }
    price_shock_by_asset = {
        str(asset_id): _decimal(value, field=f"price_shock_by_asset.{asset_id}")
        for asset_id, value in dict(payload.get("price_shock_by_asset") or {}).items()
    }
    if any(value < 0 or value > 1 for value in payout_by_asset.values()):
        raise ScenarioValidationError("resolution payouts must be between 0 and 1")
    if any(value < -1 or value > 1 for value in price_shock_by_asset.values()):
        raise ScenarioValidationError("price shocks must be between -1 and 1")
    haircut = _decimal(payload.get("book_depth_haircut", 0), field="book_depth_haircut")
    if haircut < 0 or haircut > 1:
        raise ScenarioValidationError("book_depth_haircut must be between 0 and 1")
    fee_multiplier = _decimal(payload.get("fee_multiplier", 1), field="fee_multiplier")
    if fee_multiplier < 0 or fee_multiplier > 100:
        raise ScenarioValidationError("fee_multiplier must be between 0 and 100")
    latency_ms = _decimal(payload.get("latency_shock_ms", 0), field="latency_shock_ms")
    dispute_hours = _decimal(
        payload.get("dispute_duration_hours", 0), field="dispute_duration_hours"
    )
    if latency_ms < 0 or dispute_hours < 0:
        raise ScenarioValidationError("latency and dispute duration cannot be negative")
    return _json_value(
        {
            "payout_by_asset": payout_by_asset,
            "price_shock_by_asset": price_shock_by_asset,
            "fee_multiplier": fee_multiplier,
            "latency_shock_ms": latency_ms,
            "book_depth_haircut": haircut,
            "market_closure_assets": sorted(
                {str(value) for value in payload.get("market_closure_assets") or []}
            ),
            "feed_outage_assets": sorted(
                {str(value) for value in payload.get("feed_outage_assets") or []}
            ),
            "dispute_duration_hours": dispute_hours,
        }
    )


def evaluate_scenario(
    *,
    cash_balance: Decimal,
    positions: Sequence[Mapping[str, Any]],
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply explicit shocks without inventing marks for unavailable liquidity."""

    normalized = normalize_scenario_inputs(inputs)
    payouts = dict(normalized["payout_by_asset"])
    price_shocks = dict(normalized["price_shock_by_asset"])
    closure = set(normalized["market_closure_assets"])
    outage = set(normalized["feed_outage_assets"])
    haircut = Decimal(str(normalized["book_depth_haircut"]))
    fee_multiplier = Decimal(str(normalized["fee_multiplier"]))
    baseline_value = Decimal(0)
    scenario_value = Decimal(0)
    baseline_complete = True
    scenario_complete = True
    unmarked_assets: list[str] = []
    rows: list[dict[str, Any]] = []
    for position in positions:
        asset_id = str(position["asset_id"])
        quantity = Decimal(str(position.get("quantity") or 0))
        liquidation_mark = position.get("liquidation_mark")
        baseline_mark = (
            None if liquidation_mark in (None, "") else Decimal(str(liquidation_mark))
        )
        if baseline_mark is None:
            baseline_complete = False
        else:
            baseline_value += quantity * baseline_mark
        source = "LIQUIDATION_MARK"
        scenario_mark: Decimal | None = baseline_mark
        executable_quantity = quantity * (Decimal(1) - haircut)
        if asset_id in payouts:
            scenario_mark = Decimal(str(payouts[asset_id]))
            executable_quantity = quantity
            source = "RESOLUTION_PAYOUT"
        elif asset_id in closure:
            scenario_mark = None
            source = "MARKET_CLOSURE_UNMARKED"
        elif asset_id in outage:
            scenario_mark = None
            source = "FEED_OUTAGE_UNMARKED"
        elif scenario_mark is not None:
            scenario_mark = min(
                Decimal(1),
                max(
                    Decimal(0),
                    scenario_mark + Decimal(str(price_shocks.get(asset_id, 0))),
                ),
            )
            source = "SHOCKED_LIQUIDATION_MARK"
        residual = quantity - executable_quantity
        if scenario_mark is None or residual > 0:
            scenario_complete = False
            unmarked_assets.append(asset_id)
        if scenario_mark is not None:
            scenario_value += executable_quantity * scenario_mark
        rows.append(
            {
                "asset_id": asset_id,
                "quantity": format(quantity, "f"),
                "baseline_mark": None
                if baseline_mark is None
                else format(baseline_mark, "f"),
                "scenario_mark": None
                if scenario_mark is None
                else format(scenario_mark, "f"),
                "executable_quantity": format(executable_quantity, "f"),
                "unmarked_quantity": format(residual, "f"),
                "valuation_source": source,
            }
        )
    incremental_fee_drag = sum(
        (Decimal(str(row.get("cost_basis") or 0)) for row in positions), Decimal(0)
    ) * max(Decimal(0), fee_multiplier - Decimal(1))
    baseline_nav = cash_balance + baseline_value if baseline_complete else None
    scenario_nav = (
        cash_balance + scenario_value - incremental_fee_drag
        if scenario_complete
        else None
    )
    result = {
        "classification": "SCENARIO_NOT_EXECUTION",
        "writes_to_paper_ledger": False,
        "baseline_liquidation_nav": (
            None if baseline_nav is None else format(baseline_nav, "f")
        ),
        "scenario_nav": None if scenario_nav is None else format(scenario_nav, "f"),
        "scenario_pnl_delta": (
            None
            if baseline_nav is None or scenario_nav is None
            else format(scenario_nav - baseline_nav, "f")
        ),
        "incremental_fee_drag": format(incremental_fee_drag, "f"),
        "scenario_complete": scenario_complete,
        "unmarked_assets": sorted(set(unmarked_assets)),
        "positions": rows,
        "assumptions": normalized,
        "limitations": [
            "NO_ENDOGENOUS_MARKET_REACTION",
            "LATENCY_SHOCK_REPORTED_NOT_PRICE_INFERRED",
            "UNAVAILABLE_LIQUIDITY_REMAINS_UNMARKED",
        ],
    }
    result["result_hash"] = stable_hash(result)
    return result


class PostgresScenarioService:
    def __init__(self, tenant_store: PostgresTenantPlatformStore) -> None:
        self.tenant_store = tenant_store

    def create_run(
        self,
        principal: TenantPrincipal,
        *,
        account_id: UUID,
        strategy_id: UUID | None,
        name: str,
        inputs: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        normalized = normalize_scenario_inputs(inputs)
        if not str(name).strip():
            raise ScenarioValidationError("scenario name is required")
        with self.tenant_store._transaction(
            principal, PaperPermission.STRATEGY_MANAGE
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT account.ledger_strategy_id,ledger.cash_balance,
                       strategy.strategy_id
                FROM quant.paper_account_registry account
                JOIN quant.paper_accounts ledger
                  ON ledger.strategy_id=account.ledger_strategy_id
                JOIN quant.paper_strategies strategy
                  ON strategy.tenant_id=account.tenant_id
                 AND strategy.account_id=account.account_id
                WHERE account.tenant_id=%s AND account.account_id=%s
                  AND (%s::uuid IS NULL OR strategy.strategy_id=%s)
                ORDER BY (strategy.idempotency_key='default') DESC,strategy.created_at
                LIMIT 1
                """,
                (principal.tenant_id, account_id, strategy_id, strategy_id),
            )
            ownership = cur.fetchone()
            if ownership is None:
                raise ScenarioNotFound("paper account/strategy was not found")
            cur.execute(
                """
                SELECT position.*,mark.liquidation_mark,mark.mark_quality,
                       mark.observed_at AS mark_observed_at
                FROM quant.paper_positions position
                LEFT JOIN quant.paper_position_marks mark
                  ON mark.strategy_id=position.strategy_id
                 AND mark.asset_id=position.asset_id
                WHERE position.strategy_id=%s AND position.quantity<>0
                ORDER BY position.asset_id
                """,
                (ownership["ledger_strategy_id"],),
            )
            positions = [dict(row) for row in cur.fetchall()]
            result = evaluate_scenario(
                cash_balance=Decimal(str(ownership["cash_balance"])),
                positions=positions,
                inputs=normalized,
            )
            run_id = uuid4()
            input_hash = stable_hash(normalized)
            cur.execute(
                """
                INSERT INTO quant.paper_scenario_runs (
                    scenario_run_id,tenant_id,account_id,strategy_id,name,
                    inputs,input_hash,result,result_hash,idempotency_key,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT (tenant_id,idempotency_key) DO NOTHING
                RETURNING scenario_run_id
                """,
                (
                    run_id,
                    principal.tenant_id,
                    account_id,
                    ownership["strategy_id"],
                    str(name).strip(),
                    json.dumps(normalized, sort_keys=True),
                    input_hash,
                    json.dumps(result, sort_keys=True),
                    result["result_hash"],
                    str(idempotency_key),
                    principal.subject_user_id,
                ),
            )
            inserted = cur.fetchone()
            if inserted is None:
                cur.execute(
                    """SELECT scenario_run_id FROM quant.paper_scenario_runs
                       WHERE tenant_id=%s AND idempotency_key=%s""",
                    (principal.tenant_id, str(idempotency_key)),
                )
                run_id = UUID(str(cur.fetchone()["scenario_run_id"]))
            if inserted is not None:
                self.tenant_store._append_audit(
                    cur,
                    principal,
                    event_type="SCENARIO_RUN_CREATED",
                    resource_type="SCENARIO_RUN",
                    resource_id=str(run_id),
                    reason="explicit_non_execution_research",
                    payload={
                        "input_hash": input_hash,
                        "result_hash": result["result_hash"],
                    },
                )
            conn.commit()
        return self.get_run(principal, run_id)

    def get_run(self, principal: TenantPrincipal, run_id: UUID) -> dict[str, Any]:
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                "SELECT * FROM quant.paper_scenario_runs WHERE tenant_id=%s AND scenario_run_id=%s",
                (principal.tenant_id, run_id),
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise ScenarioNotFound("scenario run was not found")
        return _json_value(dict(row))

    def list_runs(
        self, principal: TenantPrincipal, *, account_id: UUID, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT * FROM quant.paper_scenario_runs
                WHERE tenant_id=%s AND account_id=%s
                ORDER BY created_at DESC,scenario_run_id DESC LIMIT %s
                """,
                (principal.tenant_id, account_id, int(limit)),
            )
            rows = [_json_value(dict(row)) for row in cur.fetchall()]
            conn.commit()
        return rows
