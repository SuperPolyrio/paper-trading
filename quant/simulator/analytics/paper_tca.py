"""Build an auditable immediate TCA artifact from one paper order result."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .tca import TcaInput, TcaReport, build_tca


@dataclass(frozen=True)
class PaperTcaArtifact:
    order_id: str
    strategy_id: str
    intent_id: int
    audit_key: str
    side: str
    requested_size: Decimal
    filled_size: Decimal
    decision_price: Decimal | None
    arrival_price: Decimal | None
    fill_vwap: Decimal | None
    report: TcaReport | None
    status: str
    reasons: tuple[str, ...]
    capacity_status: str
    fidelity_level: str
    decision_checkpoint_id: str | None
    arrival_checkpoint_id: str | None
    venue_regime_id: str
    venue_regime_source_hash: str | None
    model_version: str = "paper-immediate-tca-v1"

    def as_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


def build_paper_tca_artifact(
    *,
    intent_id: int,
    result: Any,
    decision_checkpoint: Any | None,
    arrival_checkpoint: Any | None,
    risk_decision: Any,
) -> PaperTcaArtifact:
    side = str(result.intent.side).upper()
    requested_size = _requested_shares(result)
    decision_price = _reference_price(decision_checkpoint, side)
    arrival_price = _reference_price(arrival_checkpoint, side)
    reasons: list[str] = []
    if decision_price is None:
        reasons.append("decision_reference_unavailable")
    if arrival_price is None:
        reasons.append("arrival_reference_unavailable")
    report = None
    if decision_price is not None and arrival_price is not None:
        report = build_tca(
            TcaInput(
                order_id=str(result.intent.client_order_id),
                side=side,
                requested_size=requested_size,
                filled_size=Decimal(result.filled_size),
                decision_price=decision_price,
                arrival_price=arrival_price,
                executed_vwap=(
                    Decimal(result.avg_fill_price)
                    if result.avg_fill_price is not None
                    else None
                ),
                fee=Decimal(result.total_fee),
                opportunity_reference_price=None,
                markouts=None,
            )
        )
    fidelity = dict(result.fidelity or {})
    capacity = dict(risk_decision.capacity or {})
    return PaperTcaArtifact(
        order_id=str(result.intent.client_order_id),
        strategy_id=str(result.intent.strategy_id),
        intent_id=int(intent_id),
        audit_key=str(result.audit_key),
        side=side,
        requested_size=requested_size,
        filled_size=Decimal(result.filled_size),
        decision_price=decision_price,
        arrival_price=arrival_price,
        fill_vwap=(
            Decimal(result.avg_fill_price)
            if result.avg_fill_price is not None
            else None
        ),
        report=report,
        status="IMMEDIATE_COMPLETE" if report is not None else "UNAVAILABLE",
        reasons=tuple(reasons),
        capacity_status=str(capacity.get("status") or "NOT_EVALUATED"),
        fidelity_level=str(fidelity.get("fidelity_level") or "UNASSESSED"),
        decision_checkpoint_id=getattr(decision_checkpoint, "checkpoint_id", None),
        arrival_checkpoint_id=getattr(arrival_checkpoint, "checkpoint_id", None),
        venue_regime_id=str(
            getattr(result.intent, "venue_regime_id", None) or "UNBOUND"
        ),
        venue_regime_source_hash=getattr(
            result.intent, "venue_regime_source_hash", None
        ),
    )


def _requested_shares(result: Any) -> Decimal:
    if str(result.amount_unit).upper() == "QUOTE":
        return max(
            Decimal(0),
            Decimal(result.requested_amount) / Decimal(result.intent.limit_price),
        )
    return max(Decimal(0), Decimal(result.requested_amount))


def _reference_price(checkpoint: Any | None, side: str) -> Decimal | None:
    if checkpoint is None:
        return None
    levels = checkpoint.asks if side == "BUY" else checkpoint.bids
    prices = [Decimal(level.price) for level in levels]
    if not prices:
        return None
    return min(prices) if side == "BUY" else max(prices)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
