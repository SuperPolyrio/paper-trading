"""Tenant-scoped deterministic replay sessions and strategy reports.

The first product replay source is deliberately narrow: already persisted paper
order lifecycle events.  Inputs are frozen when the session is created, so
pause/resume/fork never query a moving order history.  This module does not
rematch L2, submit orders, or call an exchange.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any
from uuid import UUID, uuid4

from quant.simulator.kernel.deterministic_id import stable_hash
from quant.simulator.kernel.event import SimEvent
from quant.simulator.kernel.replay_driver import replay_events
from quant.simulator.paper_episode import lifecycle_rows_to_sim_events

from .tenant_platform import (
    PaperPermission,
    PostgresTenantPlatformStore,
    TenantPrincipal,
)

REPLAY_SCHEMA_VERSION = "paper-replay-session-v1"
ARTIFACT_SCHEMA_VERSION = "paper-replay-artifact-v1"
REPORT_SCHEMA_VERSION = "paper-strategy-report-v1"
MAX_REPLAY_EVENTS = 10_000
MAX_REPLAY_RANGE_DAYS = 366
ACTIVE_REPLAY_STATES = ("CREATED", "RUNNING", "PAUSED")


class ReplayServiceError(RuntimeError):
    pass


class ReplayValidationError(ReplayServiceError):
    pass


class ReplayNotFound(ReplayServiceError):
    pass


class ReplayStateConflict(ReplayServiceError):
    pass


class ReplayCapacityExceeded(ReplayServiceError):
    pass


def parse_replay_timestamp(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        selected = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            selected = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ReplayValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if selected.tzinfo is None:
        raise ReplayValidationError(f"{field} must include a timezone")
    return selected.astimezone(timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _decimal(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _ratio(numerator: Decimal, denominator: Decimal) -> str | None:
    if denominator == 0:
        return None
    return format(numerator / denominator, "f")


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / Decimal(len(values)) if values else None


def _sample_deviation(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    average = _mean(values)
    assert average is not None
    variance = sum(((value - average) ** 2 for value in values), Decimal(0)) / Decimal(
        len(values) - 1
    )
    return variance.sqrt()


def _risk_adjusted_returns(nav_rows: Sequence[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    returns: list[Decimal] = []
    for previous, current in pairwise(nav_rows):
        previous_equity = _decimal(previous.get("equity"))
        current_equity = _decimal(current.get("equity"))
        if previous_equity > 0:
            returns.append((current_equity - previous_equity) / previous_equity)
    average = _mean(returns)
    deviation = _sample_deviation(returns)
    sharpe = average / deviation if average is not None and deviation not in (None, 0) else None
    downside = [min(value, Decimal(0)) for value in returns]
    downside_deviation = (
        (sum((value**2 for value in downside), Decimal(0)) / Decimal(len(downside))).sqrt()
        if downside
        else None
    )
    sortino = (
        average / downside_deviation
        if average is not None and downside_deviation not in (None, 0)
        else None
    )
    return (
        None if sharpe is None else format(sharpe, "f"),
        None if sortino is None else format(sortino, "f"),
    )


def _attribution(
    ledger_rows: Sequence[Mapping[str, Any]], dimension: str
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"realized_pnl": Decimal(0), "fees": Decimal(0), "event_count": 0}
    )
    for row in ledger_rows:
        key = str(row.get(dimension) or "unknown")
        grouped[key]["realized_pnl"] += _decimal(row.get("realized_pnl_delta"))
        grouped[key]["fees"] += _decimal(row.get("fee"))
        grouped[key]["event_count"] += 1
    return [
        {
            dimension: key,
            "realized_pnl": format(Decimal(str(values["realized_pnl"])), "f"),
            "fees": format(Decimal(str(values["fees"])), "f"),
            "event_count": int(values["event_count"]),
        }
        for key, values in sorted(grouped.items())
    ]


def build_strategy_report(
    report_input: Mapping[str, Any], *, benchmark: str
) -> dict[str, Any]:
    """Build one deterministic report only from frozen JSON-compatible inputs."""

    nav_rows = [dict(row) for row in report_input.get("nav") or []]
    tca_rows = [dict(row) for row in report_input.get("tca") or []]
    fill_rows = [dict(row) for row in report_input.get("fills") or []]
    ledger_rows = [dict(row) for row in report_input.get("ledger") or []]
    lifecycle = [dict(row) for row in report_input.get("lifecycle") or []]
    nav_rows.sort(key=lambda row: str(row.get("observed_at") or ""))

    first = nav_rows[0] if nav_rows else {}
    last = nav_rows[-1] if nav_rows else {}
    start_equity = _decimal(first.get("equity")) if first else None
    end_equity = _decimal(last.get("equity")) if last else None
    start_conservative = _decimal(first.get("conservative_equity")) if first else None
    end_conservative = _decimal(last.get("conservative_equity")) if last else None
    strategy_change = (
        end_equity - start_equity
        if start_equity is not None and end_equity is not None
        else None
    )
    conservative_change = (
        end_conservative - start_conservative
        if start_conservative is not None and end_conservative is not None
        else None
    )
    strategy_return = (
        _ratio(strategy_change, start_equity)
        if strategy_change is not None and start_equity is not None
        else None
    )
    conservative_return = (
        _ratio(conservative_change, start_conservative)
        if conservative_change is not None and start_conservative is not None
        else None
    )
    benchmark_return = "0" if benchmark == "CASH" else conservative_return
    excess_return = (
        format(_decimal(strategy_return) - _decimal(benchmark_return), "f")
        if strategy_return is not None and benchmark_return is not None
        else None
    )

    max_drawdown = max((_decimal(row.get("drawdown")) for row in nav_rows), default=Decimal(0))
    max_drawdown_pct = max(
        (_decimal(row.get("drawdown_pct")) for row in nav_rows),
        default=Decimal(0),
    )
    max_gross_exposure = max(
        (_decimal(row.get("gross_exposure")) for row in nav_rows),
        default=Decimal(0),
    )
    incomplete_nav = sum(not bool(row.get("nav_complete")) for row in nav_rows)
    total_fee = sum((_decimal(row.get("fee_cost")) for row in tca_rows), Decimal(0))
    total_delay_cost = sum((_decimal(row.get("delay_cost")) for row in tca_rows), Decimal(0))
    total_shortfall = sum(
        (_decimal(row.get("implementation_shortfall")) for row in tca_rows),
        Decimal(0),
    )
    filled_notional = sum((_decimal(row.get("notional")) for row in fill_rows), Decimal(0))
    requested_size = sum((_decimal(row.get("requested_size")) for row in tca_rows), Decimal(0))
    filled_size = sum((_decimal(row.get("filled_size")) for row in tca_rows), Decimal(0))
    average_equity = _mean(
        [_decimal(row.get("equity")) for row in nav_rows if row.get("equity") not in (None, "")]
    )
    sharpe, sortino = _risk_adjusted_returns(nav_rows)
    capacity = Counter(str(row.get("capacity_status") or "UNASSESSED") for row in tca_rows)
    fidelity = Counter(str(row.get("fidelity_level") or "UNASSESSED") for row in tca_rows)
    lifecycle_states = Counter(str(row.get("to_state") or "UNKNOWN") for row in lifecycle)
    completed_orders = sum(
        count
        for state, count in lifecycle_states.items()
        if state in {"CONFIRMED", "COMPLETED", "FILLED", "MATCHED_PROVISIONAL"}
    )
    rejected_orders = sum(
        count for state, count in lifecycle_states.items() if state in {"REJECTED", "FAILED"}
    )
    capacity_rejected = sum(
        count for status, count in capacity.items() if status == "CAPACITY_EXCEEDED"
    )
    assessed_tca = sum(
        1
        for row in tca_rows
        if str(row.get("fidelity_level") or "UNASSESSED") != "UNASSESSED"
        and str(row.get("capacity_status") or "UNASSESSED") != "UNASSESSED"
    )
    confirmed_nav = None
    if last:
        valuation_views = dict((last.get("metadata") or {}).get("valuation_views") or {})
        confirmed_nav = valuation_views.get("confirmed_nav")
    confirmed_pnl = (
        format(_decimal(confirmed_nav) - start_equity, "f")
        if confirmed_nav not in (None, "") and start_equity is not None
        else None
    )

    performance = {
        "start_equity": None if start_equity is None else format(start_equity, "f"),
        "end_equity": None if end_equity is None else format(end_equity, "f"),
        "absolute_return": None if strategy_change is None else format(strategy_change, "f"),
        "return_pct": strategy_return,
        "start_conservative_equity": (
            None if start_conservative is None else format(start_conservative, "f")
        ),
        "end_conservative_equity": (
            None if end_conservative is None else format(end_conservative, "f")
        ),
        "conservative_return_pct": conservative_return,
        "net_pnl": None if strategy_change is None else format(strategy_change, "f"),
        "confirmed_pnl": confirmed_pnl,
        "sharpe": sharpe,
        "sortino": sortino,
        "turnover": _ratio(filled_notional, average_equity or Decimal(0)),
        "fill_ratio": _ratio(filled_size, requested_size),
        "capacity_rejected_ratio": _ratio(Decimal(capacity_rejected), Decimal(len(tca_rows))),
        "fee_drag": _ratio(total_fee, average_equity or Decimal(0)),
        "latency_slippage": format(total_delay_cost, "f"),
        "realized_pnl": last.get("realized_pnl") if last else None,
        "unrealized_pnl": last.get("unrealized_pnl") if last else None,
        "fill_count": len(fill_rows),
        "filled_notional": format(filled_notional, "f"),
        "fee_cost": format(total_fee, "f"),
        "implementation_shortfall": format(total_shortfall, "f"),
    }
    risk = {
        "max_drawdown": format(max_drawdown, "f"),
        "max_drawdown_pct": format(max_drawdown_pct, "f"),
        "max_gross_exposure": format(max_gross_exposure, "f"),
        "nav_snapshot_count": len(nav_rows),
        "incomplete_nav_snapshots": incomplete_nav,
        "completed_order_events": completed_orders,
        "rejected_order_events": rejected_orders,
        "capacity_status_counts": dict(sorted(capacity.items())),
        "fidelity_counts": dict(sorted(fidelity.items())),
        "confidence_coverage": _ratio(Decimal(assessed_tca), Decimal(len(tca_rows))),
    }
    comparison = {
        "benchmark": benchmark,
        "strategy_return_pct": strategy_return,
        "benchmark_return_pct": benchmark_return,
        "excess_return_pct": excess_return,
        "status": (
            "OUTPERFORMED"
            if excess_return is not None and _decimal(excess_return) > 0
            else "UNDERPERFORMED"
            if excess_return is not None and _decimal(excess_return) < 0
            else "MATCHED"
            if excess_return is not None
            else "UNAVAILABLE"
        ),
    }
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "performance": performance,
        "risk": risk,
        "benchmark_comparison": comparison,
        "data_quality": {
            "nav_complete": bool(nav_rows) and incomplete_nav == 0,
            "has_nav_history": len(nav_rows) >= 2,
            "has_tca": bool(tca_rows),
            "has_fills": bool(fill_rows),
            "has_ledger_attribution": bool(ledger_rows),
            "confidence_coverage": _ratio(Decimal(assessed_tca), Decimal(len(tca_rows))),
        },
        "attribution": {
            "basis": "REALIZED_PNL_LEDGER_EVENTS",
            "market": _attribution(ledger_rows, "market_id"),
            "event": _attribution(ledger_rows, "event_id"),
            "category": _attribution(ledger_rows, "category"),
        },
        "limitations": [
            "RECORDED_PAPER_LIFECYCLE_NOT_L2_REMATCH",
            "NO_COUNTERFACTUAL_MARKET_IMPACT",
            "REPORT_USES_FROZEN_PERSISTED_NAV_TCA_AND_FILL_EVIDENCE",
        ],
    }
    report["report_hash"] = stable_hash(report)
    return report


class PostgresReplayService:
    def __init__(self, tenant_store: PostgresTenantPlatformStore) -> None:
        self.tenant_store = tenant_store

    def create_session(
        self,
        principal: TenantPrincipal,
        *,
        account_id: UUID,
        strategy_id: UUID | None,
        name: str,
        start_ts: datetime,
        end_ts: datetime,
        speed: Decimal,
        seed: int,
        strategy_version: str,
        execution_model: str,
        benchmark: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not str(name).strip():
            raise ReplayValidationError("replay name is required")
        if end_ts < start_ts:
            raise ReplayValidationError("end_ts must be at or after start_ts")
        if (end_ts - start_ts).days > MAX_REPLAY_RANGE_DAYS:
            raise ReplayValidationError("replay range exceeds 366 days")
        if Decimal(speed) <= 0 or Decimal(speed) > Decimal(1000):
            raise ReplayValidationError("speed must be greater than 0 and at most 1000")
        if not -(2**63) <= int(seed) < 2**63:
            raise ReplayValidationError("seed must fit a signed 64-bit integer")
        selected_benchmark = str(benchmark).strip().upper()
        if selected_benchmark not in {"CASH", "CONSERVATIVE_NAV"}:
            raise ReplayValidationError("benchmark must be CASH or CONSERVATIVE_NAV")
        if not str(strategy_version).strip() or not str(execution_model).strip():
            raise ReplayValidationError("strategy_version and execution_model are required")

        with self.tenant_store._transaction(
            principal, PaperPermission.STRATEGY_MANAGE
        ) as (conn, cur, _):
            account = self._select_account_strategy(cur, principal, account_id, strategy_id)
            self._assert_capacity(cur, principal)
            lifecycle_rows = self._load_lifecycle_rows(
                cur,
                ledger_strategy_id=str(account["ledger_strategy_id"]),
                start_ts=start_ts,
                end_ts=end_ts,
            )
            events = self._build_events(lifecycle_rows)
            if not events:
                raise ReplayValidationError(
                    "no persisted paper lifecycle events exist in the selected range"
                )
            if len(events) > MAX_REPLAY_EVENTS:
                raise ReplayValidationError(
                    f"replay contains more than {MAX_REPLAY_EVENTS} events"
                )
            report_input = self._load_report_input(
                cur,
                ledger_strategy_id=str(account["ledger_strategy_id"]),
                start_ts=start_ts,
                end_ts=end_ts,
                lifecycle_rows=lifecycle_rows,
            )
            initial_state = self._load_initial_state(
                cur,
                ledger_strategy_id=str(account["ledger_strategy_id"]),
                start_ts=start_ts,
            )
            event_dicts = [event.to_dict() for event in events]
            data_hash = stable_hash(event_dicts)
            data_snapshot = {
                "source_mode": "RECORDED_PAPER_LIFECYCLE",
                "account_id": str(account_id),
                "strategy_id": str(account["strategy_id"]),
                "start_ts": start_ts.isoformat(),
                "end_ts": end_ts.isoformat(),
                "event_count": len(event_dicts),
                "event_hash": data_hash,
                "nav_snapshot_count": len(report_input["nav"]),
                "tca_count": len(report_input["tca"]),
                "fill_count": len(report_input["fills"]),
                "ledger_count": len(report_input["ledger"]),
                "frozen": True,
            }
            config = {
                "data_hash": data_hash,
                "start_ts": start_ts.isoformat(),
                "end_ts": end_ts.isoformat(),
                "speed": format(Decimal(speed), "f"),
                "seed": int(seed),
                "strategy_version": str(strategy_version).strip(),
                "execution_model": str(execution_model).strip(),
                "benchmark": selected_benchmark,
                "account_initial_state": initial_state,
            }
            config_hash = stable_hash(config)
            session_id = uuid4()
            cur.execute(
                """
                INSERT INTO quant.paper_replay_sessions (
                    replay_session_id,tenant_id,account_id,strategy_id,name,status,
                    source_mode,data_snapshot,data_hash,start_ts,end_ts,speed,seed,
                    strategy_version,account_initial_state,execution_model,benchmark,
                    config_hash,event_count,report_input,idempotency_key,created_by
                ) VALUES (
                    %s,%s,%s,%s,%s,'CREATED','RECORDED_PAPER_LIFECYCLE',%s::jsonb,
                    %s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s
                )
                """,
                (
                    session_id,
                    principal.tenant_id,
                    account_id,
                    account["strategy_id"],
                    str(name).strip(),
                    json.dumps(data_snapshot, sort_keys=True),
                    data_hash,
                    start_ts,
                    end_ts,
                    Decimal(speed),
                    int(seed),
                    str(strategy_version).strip(),
                    json.dumps(initial_state, sort_keys=True),
                    str(execution_model).strip(),
                    selected_benchmark,
                    config_hash,
                    len(event_dicts),
                    json.dumps(report_input, sort_keys=True),
                    str(idempotency_key),
                    principal.subject_user_id,
                ),
            )
            cur.executemany(
                """
                INSERT INTO quant.paper_replay_events (
                    tenant_id,replay_session_id,event_index,event_id,event_type,
                    event_ts_ns,event_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                [
                    (
                        principal.tenant_id,
                        session_id,
                        index,
                        event["event_id"],
                        event["event_type"],
                        event["event_ts_ns"],
                        json.dumps(event, sort_keys=True),
                    )
                    for index, event in enumerate(event_dicts)
                ],
            )
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="REPLAY_SESSION_CREATED",
                resource_type="REPLAY_SESSION",
                resource_id=str(session_id),
                reason="recorded_paper_lifecycle_snapshot",
                payload={"data_hash": data_hash, "event_count": len(event_dicts)},
            )
            conn.commit()
        return self.get_session(principal, session_id)

    def list_sessions(
        self,
        principal: TenantPrincipal,
        *,
        limit: int,
        cursor_created_at: datetime | None = None,
        cursor_session_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            cur.execute(
                """
                SELECT replay_session_id,account_id,strategy_id,
                       parent_replay_session_id,forked_from_event_index,name,status,
                       source_mode,data_snapshot,data_hash,start_ts,end_ts,speed,seed,
                       strategy_version,execution_model,benchmark,config_hash,event_count,
                       cursor_event_index,cursor_ts_ns,state_version,artifact_hash,
                       last_error,created_at,updated_at,started_at,completed_at
                FROM quant.paper_replay_sessions
                WHERE tenant_id=%s AND (
                    %s::timestamptz IS NULL
                    OR (created_at,replay_session_id)<(%s::timestamptz,%s::uuid)
                )
                ORDER BY created_at DESC,replay_session_id DESC
                LIMIT %s
                """,
                (
                    principal.tenant_id,
                    cursor_created_at,
                    cursor_created_at,
                    cursor_session_id,
                    limit,
                ),
            )
            rows = [_json_value(dict(row)) for row in cur.fetchall()]
            conn.commit()
        return rows

    def get_session(
        self, principal: TenantPrincipal, replay_session_id: UUID
    ) -> dict[str, Any]:
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            row = self._select_session(cur, principal, replay_session_id)
            conn.commit()
        return _json_value(dict(row))

    def list_events(
        self,
        principal: TenantPrincipal,
        replay_session_id: UUID,
        *,
        start_index: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self.tenant_store._transaction(
            principal, PaperPermission.ACCOUNT_READ
        ) as (conn, cur, _):
            self._select_session(cur, principal, replay_session_id)
            cur.execute(
                """
                SELECT event_index,event_id,event_type,event_ts_ns,event_json
                FROM quant.paper_replay_events
                WHERE tenant_id=%s AND replay_session_id=%s AND event_index>=%s
                ORDER BY event_index LIMIT %s
                """,
                (principal.tenant_id, replay_session_id, start_index, limit),
            )
            rows = [_json_value(dict(row)) for row in cur.fetchall()]
            conn.commit()
        return rows

    def pause_session(
        self, principal: TenantPrincipal, replay_session_id: UUID
    ) -> dict[str, Any]:
        with self.tenant_store._transaction(
            principal, PaperPermission.STRATEGY_MANAGE
        ) as (conn, cur, _):
            row = self._select_session(cur, principal, replay_session_id, lock=True)
            status = str(row["status"])
            if status == "PAUSED":
                conn.commit()
                return _json_value(dict(row))
            if status not in {"CREATED", "RUNNING"}:
                raise ReplayStateConflict(f"cannot pause replay in {status} state")
            cur.execute(
                """
                UPDATE quant.paper_replay_sessions
                SET status='PAUSED',state_version=state_version+1,
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND replay_session_id=%s
                """,
                (principal.tenant_id, replay_session_id),
            )
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="REPLAY_SESSION_PAUSED",
                resource_type="REPLAY_SESSION",
                resource_id=str(replay_session_id),
                reason="user_request",
                payload={"cursor_event_index": int(row["cursor_event_index"])},
            )
            conn.commit()
        return self.get_session(principal, replay_session_id)

    def resume_session(
        self,
        principal: TenantPrincipal,
        replay_session_id: UUID,
        *,
        max_events: int,
    ) -> dict[str, Any]:
        if max_events < 1 or max_events > 5_000:
            raise ReplayValidationError("max_events must be between 1 and 5000")
        with self.tenant_store._transaction(
            principal, PaperPermission.STRATEGY_MANAGE
        ) as (conn, cur, _):
            row = self._select_session(cur, principal, replay_session_id, lock=True)
            status = str(row["status"])
            if status == "COMPLETED":
                conn.commit()
                return _json_value(dict(row))
            if status not in {"CREATED", "PAUSED", "RUNNING"}:
                raise ReplayStateConflict(f"cannot resume replay in {status} state")
            cursor = int(row["cursor_event_index"])
            event_count = int(row["event_count"])
            next_cursor = min(event_count, cursor + int(max_events))
            cur.execute(
                """
                SELECT event_json FROM quant.paper_replay_events
                WHERE tenant_id=%s AND replay_session_id=%s AND event_index<%s
                ORDER BY event_index
                """,
                (principal.tenant_id, replay_session_id, next_cursor),
            )
            events = tuple(SimEvent.from_dict(dict(item["event_json"])) for item in cur.fetchall())
            result = replay_events(events)
            completed = next_cursor == event_count
            artifact = None
            artifact_hash = None
            if completed:
                report = build_strategy_report(
                    dict(row["report_input"] or {}), benchmark=str(row["benchmark"])
                )
                artifact = self._build_artifact(row, result=result, report=report)
                artifact_hash = stable_hash(artifact)
            cursor_ts_ns = events[-1].event_ts_ns if events else None
            cur.execute(
                """
                UPDATE quant.paper_replay_sessions
                SET status=%s,cursor_event_index=%s,cursor_ts_ns=%s,
                    replay_result=%s::jsonb,artifact=%s::jsonb,artifact_hash=%s,
                    state_version=state_version+1,last_error=NULL,
                    started_at=COALESCE(started_at,clock_timestamp()),
                    completed_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND replay_session_id=%s
                """,
                (
                    "COMPLETED" if completed else "RUNNING",
                    next_cursor,
                    cursor_ts_ns,
                    json.dumps(result, sort_keys=True),
                    None if artifact is None else json.dumps(artifact, sort_keys=True),
                    artifact_hash,
                    completed,
                    principal.tenant_id,
                    replay_session_id,
                ),
            )
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="REPLAY_SESSION_COMPLETED" if completed else "REPLAY_SESSION_ADVANCED",
                resource_type="REPLAY_SESSION",
                resource_id=str(replay_session_id),
                reason="deterministic_scheduler",
                payload={
                    "cursor_before": cursor,
                    "cursor_after": next_cursor,
                    "journal_hash": result["journal_hash"],
                    "artifact_hash": artifact_hash,
                },
            )
            conn.commit()
        return self.get_session(principal, replay_session_id)

    def fork_session(
        self,
        principal: TenantPrincipal,
        replay_session_id: UUID,
        *,
        name: str,
        speed: Decimal | None,
        seed: int | None,
        execution_model: str | None,
        benchmark: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not str(name).strip():
            raise ReplayValidationError("fork name is required")
        with self.tenant_store._transaction(
            principal, PaperPermission.STRATEGY_MANAGE
        ) as (conn, cur, _):
            parent = self._select_session(cur, principal, replay_session_id, lock=True)
            self._assert_capacity(cur, principal)
            selected_speed = Decimal(speed) if speed is not None else Decimal(parent["speed"])
            selected_seed = int(seed) if seed is not None else int(parent["seed"])
            selected_model = str(execution_model or parent["execution_model"]).strip()
            selected_benchmark = str(benchmark or parent["benchmark"]).strip().upper()
            if selected_speed <= 0 or selected_speed > Decimal(1000):
                raise ReplayValidationError("speed must be greater than 0 and at most 1000")
            if selected_benchmark not in {"CASH", "CONSERVATIVE_NAV"}:
                raise ReplayValidationError("benchmark must be CASH or CONSERVATIVE_NAV")
            config = {
                "data_hash": str(parent["data_hash"]),
                "start_ts": parent["start_ts"].isoformat(),
                "end_ts": parent["end_ts"].isoformat(),
                "speed": format(selected_speed, "f"),
                "seed": selected_seed,
                "strategy_version": str(parent["strategy_version"]),
                "execution_model": selected_model,
                "benchmark": selected_benchmark,
                "account_initial_state": _json_value(parent["account_initial_state"]),
            }
            child_id = uuid4()
            cur.execute(
                """
                INSERT INTO quant.paper_replay_sessions (
                    replay_session_id,tenant_id,account_id,strategy_id,
                    parent_replay_session_id,forked_from_event_index,name,status,
                    source_mode,data_snapshot,data_hash,start_ts,end_ts,speed,seed,
                    strategy_version,account_initial_state,execution_model,benchmark,
                    config_hash,event_count,report_input,idempotency_key,created_by
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,'CREATED',%s,%s::jsonb,%s,%s,%s,%s,%s,
                    %s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s
                )
                """,
                (
                    child_id,
                    principal.tenant_id,
                    parent["account_id"],
                    parent["strategy_id"],
                    replay_session_id,
                    int(parent["cursor_event_index"]),
                    str(name).strip(),
                    parent["source_mode"],
                    json.dumps(_json_value(parent["data_snapshot"]), sort_keys=True),
                    parent["data_hash"],
                    parent["start_ts"],
                    parent["end_ts"],
                    selected_speed,
                    selected_seed,
                    parent["strategy_version"],
                    json.dumps(_json_value(parent["account_initial_state"]), sort_keys=True),
                    selected_model,
                    selected_benchmark,
                    stable_hash(config),
                    parent["event_count"],
                    json.dumps(_json_value(parent["report_input"]), sort_keys=True),
                    str(idempotency_key),
                    principal.subject_user_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO quant.paper_replay_events (
                    tenant_id,replay_session_id,event_index,event_id,event_type,
                    event_ts_ns,event_json
                )
                SELECT tenant_id,%s,event_index,event_id,event_type,event_ts_ns,event_json
                FROM quant.paper_replay_events
                WHERE tenant_id=%s AND replay_session_id=%s
                ORDER BY event_index
                """,
                (child_id, principal.tenant_id, replay_session_id),
            )
            self.tenant_store._append_audit(
                cur,
                principal,
                event_type="REPLAY_SESSION_FORKED",
                resource_type="REPLAY_SESSION",
                resource_id=str(child_id),
                reason="restart_from_frozen_parent_snapshot",
                payload={
                    "parent_replay_session_id": str(replay_session_id),
                    "parent_cursor_event_index": int(parent["cursor_event_index"]),
                },
            )
            conn.commit()
        return self.get_session(principal, child_id)

    def get_report(
        self, principal: TenantPrincipal, replay_session_id: UUID
    ) -> dict[str, Any]:
        session = self.get_session(principal, replay_session_id)
        if session["status"] != "COMPLETED" or not session.get("artifact"):
            raise ReplayStateConflict("strategy report is available only after replay completion")
        artifact = dict(session["artifact"])
        return {
            "replay_session_id": str(replay_session_id),
            "artifact_hash": session["artifact_hash"],
            "report": artifact["strategy_report"],
            "replay": artifact["replay"],
            "limitations": artifact["limitations"],
        }

    @staticmethod
    def _select_account_strategy(
        cur: Any,
        principal: TenantPrincipal,
        account_id: UUID,
        strategy_id: UUID | None,
    ) -> Mapping[str, Any]:
        cur.execute(
            """
            SELECT account.account_id,account.ledger_strategy_id,strategy.strategy_id
            FROM quant.paper_account_registry account
            JOIN quant.paper_strategies strategy
              ON strategy.tenant_id=account.tenant_id
             AND strategy.account_id=account.account_id
            WHERE account.tenant_id=%s AND account.account_id=%s
              AND (%s::uuid IS NULL OR strategy.strategy_id=%s)
              AND account.status='ACTIVE' AND strategy.status IN ('ACTIVE','PAUSED')
            ORDER BY (strategy.idempotency_key='default') DESC,strategy.created_at
            LIMIT 1
            """,
            (principal.tenant_id, account_id, strategy_id, strategy_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ReplayNotFound("paper account/strategy was not found")
        return row

    @staticmethod
    def _select_session(
        cur: Any,
        principal: TenantPrincipal,
        replay_session_id: UUID,
        *,
        lock: bool = False,
    ) -> Mapping[str, Any]:
        cur.execute(
            f"""
            SELECT * FROM quant.paper_replay_sessions
            WHERE tenant_id=%s AND replay_session_id=%s
            {'FOR UPDATE' if lock else ''}
            """,
            (principal.tenant_id, replay_session_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ReplayNotFound("replay session was not found")
        return row

    @staticmethod
    def _assert_capacity(cur: Any, principal: TenantPrincipal) -> None:
        cur.execute(
            """
            SELECT hard_limit FROM quant.paper_quotas
            WHERE tenant_id=%s AND metric='TENANT_REPLAY_CONCURRENCY'
              AND subject_type='TENANT' AND enabled=TRUE
              AND (subject_id IS NULL OR subject_id=%s)
            ORDER BY (subject_id IS NOT NULL) DESC LIMIT 1
            """,
            (principal.tenant_id, str(principal.tenant_id)),
        )
        quota = cur.fetchone()
        hard_limit = int(Decimal(quota["hard_limit"])) if quota else 4
        cur.execute(
            """
            SELECT count(*) AS active FROM quant.paper_replay_sessions
            WHERE tenant_id=%s AND status=ANY(%s)
            """,
            (principal.tenant_id, list(ACTIVE_REPLAY_STATES)),
        )
        active = int(cur.fetchone()["active"])
        if active >= hard_limit:
            raise ReplayCapacityExceeded(
                f"tenant replay concurrency limit reached ({active}/{hard_limit})"
            )

    @staticmethod
    def _load_lifecycle_rows(
        cur: Any,
        *,
        ledger_strategy_id: str,
        start_ts: datetime,
        end_ts: datetime,
    ) -> list[dict[str, Any]]:
        cur.execute(
            """
            SELECT intent.intent_id,intent.result_audit_key,intent.strategy_id,
                   intent.client_order_id,intent.asset_id,intent.status,
                   intent.last_error AS result_reason,event.idempotency_key,event.event_type,
                   event.from_state,event.to_state,event.reason,event.checkpoint_id,
                   event.payload,event.event_ts
            FROM quant.paper_live_order_intents intent
            JOIN quant.paper_order_events event ON event.intent_id=intent.intent_id
            WHERE intent.strategy_id=%s AND event.event_ts>=%s AND event.event_ts<=%s
            ORDER BY event.event_ts,event.idempotency_key
            LIMIT %s
            """,
            (ledger_strategy_id, start_ts, end_ts, MAX_REPLAY_EVENTS + 1),
        )
        return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def _build_events(rows: Sequence[Mapping[str, Any]]) -> tuple[SimEvent, ...]:
        grouped: dict[int, list[Mapping[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(int(row["intent_id"]), []).append(row)
        events: list[SimEvent] = []
        for intent_id, selected in grouped.items():
            first = selected[0]
            audit = {
                "audit_key": str(first.get("result_audit_key") or f"intent:{intent_id}"),
                "strategy_id": str(first["strategy_id"]),
                "client_order_id": str(first["client_order_id"]),
                "asset_id": str(first["asset_id"]),
                "status": str(first["status"]),
                "reason": str(first.get("result_reason") or "recorded_lifecycle"),
                "intent": {},
                "fidelity": {},
            }
            events.extend(lifecycle_rows_to_sim_events(audit, selected))
        return tuple(sorted(events, key=lambda event: event.sort_key))

    @staticmethod
    def _load_initial_state(
        cur: Any, *, ledger_strategy_id: str, start_ts: datetime
    ) -> dict[str, Any]:
        cur.execute(
            """
            SELECT observed_at,initial_cash,cash_balance,cash_reserved,
                   realized_pnl,equity,conservative_equity,gross_exposure,
                   open_positions,unmarkable_positions,nav_complete
            FROM quant.paper_portfolio_nav_snapshots
            WHERE strategy_id=%s AND observed_at<=%s
            ORDER BY observed_at DESC,nav_id DESC LIMIT 1
            """,
            (ledger_strategy_id, start_ts),
        )
        row = cur.fetchone()
        if row is None:
            return {
                "source": "UNAVAILABLE",
                "observed_at": None,
                "nav_complete": False,
                "reason": "NO_CAUSAL_NAV_SNAPSHOT_AT_OR_BEFORE_START",
            }
        return {"source": "NAV_SNAPSHOT", **_json_value(dict(row))}

    @staticmethod
    def _load_report_input(
        cur: Any,
        *,
        ledger_strategy_id: str,
        start_ts: datetime,
        end_ts: datetime,
        lifecycle_rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        cur.execute(
            """
            SELECT observed_at,equity,conservative_equity,realized_pnl,
                   unrealized_pnl,gross_exposure,drawdown,drawdown_pct,
                   nav_complete,metadata
            FROM quant.paper_portfolio_nav_snapshots
            WHERE strategy_id=%s AND observed_at>=%s AND observed_at<=%s
            ORDER BY observed_at,nav_id LIMIT 10000
            """,
            (ledger_strategy_id, start_ts, end_ts),
        )
        nav = [_json_value(dict(row)) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT updated_at,order_id,fee_cost,delay_cost,
                   implementation_shortfall,requested_size,filled_size,
                   capacity_status,fidelity_level,status
            FROM quant.execution_tca
            WHERE strategy_id=%s AND updated_at>=%s AND updated_at<=%s
            ORDER BY updated_at,order_id LIMIT 10000
            """,
            (ledger_strategy_id, start_ts, end_ts),
        )
        tca = [_json_value(dict(row)) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT fill.created_at,fill.audit_key,fill.fill_index,fill.notional,
                   fill.fee,fill.side,fill.price,fill.size,fill.market_id,
                   fill.condition_id,fill.asset_id,catalog.event_id,catalog.category
            FROM quant.paper_fills fill
            LEFT JOIN quant.paper_execution_market_catalog catalog
              ON catalog.asset_id=fill.asset_id
            WHERE fill.strategy_id=%s AND fill.created_at>=%s AND fill.created_at<=%s
            ORDER BY fill.created_at,fill.audit_key,fill.fill_index LIMIT 10000
            """,
            (ledger_strategy_id, start_ts, end_ts),
        )
        fills = [_json_value(dict(row)) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT ledger.event_ts,ledger.event_type,ledger.market_id,
                   ledger.condition_id,ledger.asset_id,ledger.realized_pnl_delta,
                   ledger.fee,catalog.event_id,catalog.category
            FROM quant.paper_ledger_entries ledger
            LEFT JOIN quant.paper_execution_market_catalog catalog
              ON catalog.asset_id=ledger.asset_id
            WHERE ledger.strategy_id=%s AND ledger.event_ts>=%s AND ledger.event_ts<=%s
            ORDER BY ledger.event_ts,ledger.entry_id LIMIT 10000
            """,
            (ledger_strategy_id, start_ts, end_ts),
        )
        ledger = [_json_value(dict(row)) for row in cur.fetchall()]
        lifecycle = [
            {
                "intent_id": int(row["intent_id"]),
                "event_type": str(row["event_type"]),
                "from_state": row.get("from_state"),
                "to_state": row.get("to_state"),
                "event_ts": _json_value(row["event_ts"]),
            }
            for row in lifecycle_rows
        ]
        return {
            "nav": nav,
            "tca": tca,
            "fills": fills,
            "ledger": ledger,
            "lifecycle": lifecycle,
        }

    @staticmethod
    def _build_artifact(
        session: Mapping[str, Any], *, result: Mapping[str, Any], report: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "source_mode": str(session["source_mode"]),
            "data_hash": str(session["data_hash"]),
            "config_hash": str(session["config_hash"]),
            "configuration": {
                "start_ts": _json_value(session["start_ts"]),
                "end_ts": _json_value(session["end_ts"]),
                "speed": format(Decimal(session["speed"]), "f"),
                "seed": int(session["seed"]),
                "strategy_version": str(session["strategy_version"]),
                "execution_model": str(session["execution_model"]),
                "benchmark": str(session["benchmark"]),
                "account_initial_state": _json_value(session["account_initial_state"]),
            },
            "replay": _json_value(result),
            "strategy_report": _json_value(report),
            "limitations": [
                "RECORDED_PAPER_LIFECYCLE_NOT_L2_REMATCH",
                "NO_LIVE_OR_EXCHANGE_SUBMISSION",
                "NO_COUNTERFACTUAL_MARKET_IMPACT",
            ],
            "live_submission_performed": False,
        }
