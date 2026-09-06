from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant.settlement.dispute_policy import (
    DisputeCase,
    DisputeLifecycleService,
    DisputeRuleSnapshot,
    DisputeState,
    UmaOutcome,
)

NOW = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self) -> None:
        self.cases: dict[str, DisputeCase] = {}
        self.rules: list[DisputeRuleSnapshot] = []

    def record_rules(self, rules: DisputeRuleSnapshot) -> None:
        self.rules.append(rules)

    def create(self, dispute: DisputeCase, *, event_id: str) -> DisputeCase:
        self.cases.setdefault(dispute.case_id, dispute)
        return self.cases[dispute.case_id]

    def get(self, case_id: str) -> DisputeCase:
        return self.cases[case_id]

    def transition(
        self,
        dispute: DisputeCase,
        *,
        from_state: DisputeState,
        event_id: str,
        event_type: str,
        event_ts: datetime,
        payload: dict,
    ) -> DisputeCase:
        assert self.cases[dispute.case_id].state is from_state
        self.cases[dispute.case_id] = dispute
        return dispute


class AccountProgram:
    def __init__(self) -> None:
        self.posted: list[object] = []
        self.resolved: list[dict] = []

    def post_dispute(self, dispute: object) -> None:
        self.posted.append(dispute)

    def resolve_dispute(self, dispute: object, **kwargs: object) -> tuple[()]:
        self.resolved.append({"dispute": dispute, **kwargs})
        return ()


def _service() -> tuple[DisputeLifecycleService, Store, AccountProgram]:
    rules = DisputeRuleSnapshot(
        version="official-help-2026-08-18",
        source_url="https://help.polymarket.com/en/articles/13364551-how-are-markets-disputed",
        source_payload_hash=hashlib.sha256(b"official dispute snapshot").hexdigest(),
        observed_at=NOW,
    )
    store = Store()
    program = AccountProgram()
    return (
        DisputeLifecycleService(rules=rules, store=store, account_program=program),
        store,
        program,
    )


def _proposal(service: DisputeLifecycleService) -> DisputeCase:
    return service.observe_proposal(
        condition_id="condition-1",
        proposal_id="proposal-1",
        proposer="0xproposer",
        proposed_outcome="YES",
        proposer_bond=Decimal("913.25"),
        currency="pUSD",
        proposed_at=NOW,
        source_event_id="proposal-event",
    )


def test_dynamic_bond_and_official_windows_are_snapshotted() -> None:
    service, _, program = _service()
    proposal = _proposal(service)

    challenged = service.challenge(
        case_id=proposal.case_id,
        account_id="account-1",
        strategy_id="strategy-1",
        disputer="0xdisputer",
        challenge_bond=Decimal("913.25"),
        challenged_at=NOW + timedelta(hours=1, minutes=59),
        source_event_id="challenge-event",
    )

    assert proposal.challenge_deadline == NOW + timedelta(hours=2)
    assert challenged.discussion_not_before == challenged.disputed_at + timedelta(hours=24)
    assert challenged.discussion_not_after == challenged.disputed_at + timedelta(hours=48)
    assert program.posted[0].bond_amount == Decimal("913.25")


def test_late_or_wrong_bond_challenge_is_rejected() -> None:
    service, _, _ = _service()
    proposal = _proposal(service)
    with pytest.raises(ValueError, match="must equal"):
        service.challenge(
            case_id=proposal.case_id,
            account_id="account-1",
            strategy_id="strategy-1",
            disputer="0xdisputer",
            challenge_bond=Decimal(750),
            challenged_at=NOW + timedelta(hours=1),
            source_event_id="wrong-bond",
        )
    with pytest.raises(ValueError, match="two-hour"):
        service.challenge(
            case_id=proposal.case_id,
            account_id="account-1",
            strategy_id="strategy-1",
            disputer="0xdisputer",
            challenge_bond=Decimal("913.25"),
            challenged_at=NOW + timedelta(hours=2, microseconds=1),
            source_event_id="late",
        )


def test_confirmed_uma_outcome_drives_bond_return_and_half_bounty() -> None:
    service, _, program = _service()
    proposal = _proposal(service)
    challenged = service.challenge(
        case_id=proposal.case_id,
        account_id="account-1",
        strategy_id="strategy-1",
        disputer="0xdisputer",
        challenge_bond=Decimal("913.25"),
        challenged_at=NOW + timedelta(hours=1),
        source_event_id="challenge",
    )

    final = service.reconcile_uma_outcome(
        case_id=challenged.case_id,
        account_id="account-1",
        strategy_id="strategy-1",
        outcome=UmaOutcome.TOO_EARLY,
        resolved_at=NOW + timedelta(days=4),
        transaction_hash="0xuma",
        source_event_id="uma-final",
    )

    assert final.state is DisputeState.FINAL
    assert program.resolved[0]["bounty_amount"] == Decimal("456.625")
