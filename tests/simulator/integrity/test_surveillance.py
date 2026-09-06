from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant.simulator.integrity.models import (
    IntegrityFindingType,
    SurveillanceObservation,
)
from quant.simulator.integrity.service import IntegritySurveillanceService

NOW = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self) -> None:
        self.evidence: list[object] = []

    def record_evidence(self, evidence: object, *, policy_version: str) -> object:
        self.evidence.append(evidence)
        return {"status": "OPEN", "policy_version": policy_version, "evidence": evidence}


def _observation(identifier: str, event_type: str, **overrides: object) -> SurveillanceObservation:
    values = {
        "observation_id": identifier,
        "event_type": event_type,
        "account_id": "account-1",
        "beneficial_owner_id": "owner-1",
        "condition_id": "condition-1",
        "asset_id": "asset-1",
        "side": "BUY",
        "price": Decimal("0.5"),
        "size": Decimal(1),
        "event_ts": NOW,
    }
    values.update(overrides)
    return SurveillanceObservation(**values)


def test_same_beneficial_owner_trade_opens_self_dealing_case() -> None:
    store = Store()
    service = IntegritySurveillanceService(store=store, policy_version="policy-1")

    cases = service.observe(
        _observation(
            "trade-1",
            "TRADE",
            counterparty_account_id="account-2",
            counterparty_owner_id="owner-1",
            trade_id="trade-1",
        )
    )

    assert len(cases) == 1
    assert store.evidence[0].finding_type is IntegrityFindingType.SELF_DEALING
    assert cases[0]["status"] == "OPEN"


def test_spoofing_heuristic_opens_review_case_but_does_not_auto_convict() -> None:
    store = Store()
    service = IntegritySurveillanceService(
        store=store,
        policy_version="policy-1",
        spoof_min_orders=4,
        spoof_cancel_ratio=Decimal("0.75"),
    )
    for index in range(4):
        service.observe(
            _observation(
                f"open-{index}",
                "ORDER_OPEN",
                event_ts=NOW + timedelta(seconds=index),
                metadata={"near_touch": True},
            )
        )
    for index in range(3):
        service.observe(
            _observation(
                f"cancel-{index}",
                "ORDER_CANCEL",
                event_ts=NOW + timedelta(seconds=5 + index),
                metadata={"near_touch": True},
            )
        )

    finding = store.evidence[-1]
    assert finding.finding_type is IntegrityFindingType.SPOOFING_LAYERING
    assert finding.payload["requires_human_review"] is True


def test_confidential_information_requires_manual_evidence() -> None:
    store = Store()
    service = IntegritySurveillanceService(store=store, policy_version="policy-1")
    observation = _observation("manual-1", "TRADE")

    result = service.manual_policy_signal(
        observation,
        finding_type=IntegrityFindingType.CONFIDENTIAL_INFORMATION,
        reason_codes=("declared_duty_of_confidence",),
        evidence_payload={"source": "human_compliance_review"},
    )

    assert result["status"] == "OPEN"
    assert store.evidence[0].payload["source"] == "human_compliance_review"
