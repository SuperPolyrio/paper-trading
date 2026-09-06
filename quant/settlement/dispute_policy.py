"""Versioned official dispute windows, dynamic bond snapshots and reconciliation."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from quant.core.db import postgres_connection
from quant.simulator.admission.domain import stable_hash
from quant.simulator.economics.account_programs import (
    DisputeBond,
    DisputeOutcome,
)


class DisputeState(str, Enum):
    PROPOSED = "PROPOSED"
    CHALLENGE_OPEN = "CHALLENGE_OPEN"
    UNCHALLENGED_FINAL = "UNCHALLENGED_FINAL"
    DISPUTED = "DISPUTED"
    DISCUSSION = "DISCUSSION"
    VOTING = "VOTING"
    FINAL = "FINAL"


class UmaOutcome(str, Enum):
    PROPOSER_WINS = "PROPOSER_WINS"
    DISPUTER_WINS = "DISPUTER_WINS"
    TOO_EARLY = "TOO_EARLY"
    UNKNOWN_50_50 = "UNKNOWN_50_50"


@dataclass(frozen=True)
class DisputeRuleSnapshot:
    version: str
    source_url: str
    source_payload_hash: str
    observed_at: datetime
    challenge_window: timedelta = timedelta(hours=2)
    discussion_min: timedelta = timedelta(hours=24)
    discussion_max: timedelta = timedelta(hours=48)
    vote_window_estimate: timedelta = timedelta(hours=48)

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("dispute rule timestamp must be timezone-aware")
        if len(self.source_payload_hash) != 64:
            raise ValueError("dispute rule source hash must be SHA256")
        if not timedelta(0) < self.discussion_min <= self.discussion_max:
            raise ValueError("invalid dispute discussion window")

    @property
    def snapshot_id(self) -> str:
        return stable_hash(
            {
                "version": self.version,
                "source_url": self.source_url,
                "source_payload_hash": self.source_payload_hash,
                "observed_at": self.observed_at,
                "challenge_seconds": self.challenge_window.total_seconds(),
                "discussion_min_seconds": self.discussion_min.total_seconds(),
                "discussion_max_seconds": self.discussion_max.total_seconds(),
                "vote_seconds": self.vote_window_estimate.total_seconds(),
            },
            prefix="dispute-rule-",
        )


@dataclass(frozen=True)
class DisputeCase:
    case_id: str
    condition_id: str
    proposal_id: str
    proposer: str
    proposed_outcome: str
    proposer_bond: Decimal
    currency: str
    proposed_at: datetime
    challenge_deadline: datetime
    rule_snapshot_id: str
    state: DisputeState
    disputer: str | None = None
    disputer_bond: Decimal | None = None
    disputed_at: datetime | None = None
    discussion_not_before: datetime | None = None
    discussion_not_after: datetime | None = None
    final_outcome: UmaOutcome | None = None
    transaction_hash: str | None = None


class AccountDisputeProgram(Protocol):
    def post_dispute(self, dispute: DisputeBond) -> Any: ...

    def resolve_dispute(
        self,
        dispute: DisputeBond,
        *,
        outcome: DisputeOutcome,
        resolved_at: datetime,
        transaction_hash: str,
        bounty_amount: Decimal = Decimal(0),
    ) -> tuple[Any, ...]: ...


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_dispute_rule_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        version TEXT NOT NULL,
        source_url TEXT NOT NULL,
        source_payload_hash TEXT NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        challenge_window_seconds BIGINT NOT NULL,
        discussion_min_seconds BIGINT NOT NULL,
        discussion_max_seconds BIGINT NOT NULL,
        vote_window_seconds BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_dispute_cases (
        case_id TEXT PRIMARY KEY,
        condition_id TEXT NOT NULL,
        proposal_id TEXT NOT NULL UNIQUE,
        proposer TEXT NOT NULL,
        proposed_outcome TEXT NOT NULL,
        proposer_bond NUMERIC NOT NULL CHECK (proposer_bond > 0),
        currency TEXT NOT NULL,
        proposed_at TIMESTAMPTZ NOT NULL,
        challenge_deadline TIMESTAMPTZ NOT NULL,
        rule_snapshot_id TEXT NOT NULL,
        state TEXT NOT NULL,
        disputer TEXT,
        disputer_bond NUMERIC,
        disputed_at TIMESTAMPTZ,
        discussion_not_before TIMESTAMPTZ,
        discussion_not_after TIMESTAMPTZ,
        final_outcome TEXT,
        transaction_hash TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quant.simulator_dispute_case_events (
        event_id TEXT PRIMARY KEY,
        case_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        event_ts TIMESTAMPTZ NOT NULL,
        payload_hash TEXT NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
    """,
)


class PostgresDisputePolicyStore:
    def __init__(
        self,
        connection_factory: Callable[..., AbstractContextManager[Any]] = (
            postgres_connection
        ),
    ) -> None:
        self.connection_factory = connection_factory

    def ensure_schema(self) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)

    def record_rules(self, rules: DisputeRuleSnapshot) -> None:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.simulator_dispute_rule_snapshots (
                    snapshot_id,version,source_url,source_payload_hash,observed_at,
                    challenge_window_seconds,discussion_min_seconds,
                    discussion_max_seconds,vote_window_seconds
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (snapshot_id) DO NOTHING
                """,
                (
                    rules.snapshot_id,
                    rules.version,
                    rules.source_url,
                    rules.source_payload_hash,
                    rules.observed_at,
                    int(rules.challenge_window.total_seconds()),
                    int(rules.discussion_min.total_seconds()),
                    int(rules.discussion_max.total_seconds()),
                    int(rules.vote_window_estimate.total_seconds()),
                ),
            )

    def create(self, dispute: DisputeCase, *, event_id: str) -> DisputeCase:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quant.simulator_dispute_cases (
                    case_id,condition_id,proposal_id,proposer,proposed_outcome,
                    proposer_bond,currency,proposed_at,challenge_deadline,
                    rule_snapshot_id,state
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (case_id) DO NOTHING
                """,
                (
                    dispute.case_id,
                    dispute.condition_id,
                    dispute.proposal_id,
                    dispute.proposer,
                    dispute.proposed_outcome,
                    dispute.proposer_bond,
                    dispute.currency,
                    dispute.proposed_at,
                    dispute.challenge_deadline,
                    dispute.rule_snapshot_id,
                    dispute.state.value,
                ),
            )
            self._event(
                cur,
                event_id=event_id,
                case_id=dispute.case_id,
                event_type="PROPOSAL_OBSERVED",
                from_state=None,
                to_state=dispute.state,
                event_ts=dispute.proposed_at,
                payload={
                    "proposal_id": dispute.proposal_id,
                    "proposer_bond": str(dispute.proposer_bond),
                    "challenge_deadline": dispute.challenge_deadline.isoformat(),
                    "rule_snapshot_id": dispute.rule_snapshot_id,
                },
            )
        return self.get(dispute.case_id)

    def transition(
        self,
        dispute: DisputeCase,
        *,
        from_state: DisputeState,
        event_id: str,
        event_type: str,
        event_ts: datetime,
        payload: Mapping[str, Any],
    ) -> DisputeCase:
        with self.connection_factory(readonly=False) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM quant.simulator_dispute_cases WHERE case_id=%s FOR UPDATE",
                (dispute.case_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown dispute case: {dispute.case_id}")
            if str(row["state"]) != from_state.value:
                existing = self._event_row(cur, event_id)
                if existing is not None and str(existing["to_state"]) == dispute.state.value:
                    return self.get(dispute.case_id)
                raise ValueError("dispute case state changed before transition")
            cur.execute(
                """
                UPDATE quant.simulator_dispute_cases
                SET state=%s,disputer=%s,disputer_bond=%s,disputed_at=%s,
                    discussion_not_before=%s,discussion_not_after=%s,
                    final_outcome=%s,transaction_hash=%s,updated_at=clock_timestamp()
                WHERE case_id=%s
                """,
                (
                    dispute.state.value,
                    dispute.disputer,
                    dispute.disputer_bond,
                    dispute.disputed_at,
                    dispute.discussion_not_before,
                    dispute.discussion_not_after,
                    dispute.final_outcome.value if dispute.final_outcome else None,
                    dispute.transaction_hash,
                    dispute.case_id,
                ),
            )
            self._event(
                cur,
                event_id=event_id,
                case_id=dispute.case_id,
                event_type=event_type,
                from_state=from_state,
                to_state=dispute.state,
                event_ts=event_ts,
                payload=payload,
            )
        return self.get(dispute.case_id)

    def get(self, case_id: str) -> DisputeCase:
        with self.connection_factory(readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM quant.simulator_dispute_cases WHERE case_id=%s",
                (case_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"unknown dispute case: {case_id}")
        return DisputeCase(
            case_id=str(row["case_id"]),
            condition_id=str(row["condition_id"]),
            proposal_id=str(row["proposal_id"]),
            proposer=str(row["proposer"]),
            proposed_outcome=str(row["proposed_outcome"]),
            proposer_bond=Decimal(row["proposer_bond"]),
            currency=str(row["currency"]),
            proposed_at=row["proposed_at"],
            challenge_deadline=row["challenge_deadline"],
            rule_snapshot_id=str(row["rule_snapshot_id"]),
            state=DisputeState(str(row["state"])),
            disputer=row["disputer"],
            disputer_bond=(
                Decimal(row["disputer_bond"])
                if row["disputer_bond"] is not None
                else None
            ),
            disputed_at=row["disputed_at"],
            discussion_not_before=row["discussion_not_before"],
            discussion_not_after=row["discussion_not_after"],
            final_outcome=(
                UmaOutcome(str(row["final_outcome"])) if row["final_outcome"] else None
            ),
            transaction_hash=row["transaction_hash"],
        )

    def _event(
        self,
        cur: Any,
        *,
        event_id: str,
        case_id: str,
        event_type: str,
        from_state: DisputeState | None,
        to_state: DisputeState,
        event_ts: datetime,
        payload: Mapping[str, Any],
    ) -> None:
        digest = stable_hash(payload)
        cur.execute(
            """
            INSERT INTO quant.simulator_dispute_case_events (
                event_id,case_id,event_type,from_state,to_state,event_ts,
                payload_hash,payload
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                event_id,
                case_id,
                event_type,
                from_state.value if from_state else None,
                to_state.value,
                event_ts,
                digest,
                json.dumps(payload),
            ),
        )

    @staticmethod
    def _event_row(cur: Any, event_id: str) -> Any:
        cur.execute(
            "SELECT to_state FROM quant.simulator_dispute_case_events WHERE event_id=%s",
            (event_id,),
        )
        return cur.fetchone()


class DisputeLifecycleService:
    def __init__(
        self,
        *,
        rules: DisputeRuleSnapshot,
        store: PostgresDisputePolicyStore,
        account_program: AccountDisputeProgram | None = None,
    ) -> None:
        self.rules = rules
        self.store = store
        self.account_program = account_program

    def observe_proposal(
        self,
        *,
        condition_id: str,
        proposal_id: str,
        proposer: str,
        proposed_outcome: str,
        proposer_bond: Decimal,
        currency: str,
        proposed_at: datetime,
        source_event_id: str,
    ) -> DisputeCase:
        bond = Decimal(proposer_bond)
        if bond <= 0:
            raise ValueError("official proposer bond snapshot must be positive")
        self.store.record_rules(self.rules)
        case_id = stable_hash(
            {"condition_id": condition_id, "proposal_id": proposal_id},
            prefix="dispute-",
        )
        dispute = DisputeCase(
            case_id=case_id,
            condition_id=condition_id,
            proposal_id=proposal_id,
            proposer=proposer,
            proposed_outcome=proposed_outcome,
            proposer_bond=bond,
            currency=currency,
            proposed_at=proposed_at,
            challenge_deadline=proposed_at + self.rules.challenge_window,
            rule_snapshot_id=self.rules.snapshot_id,
            state=DisputeState.CHALLENGE_OPEN,
        )
        return self.store.create(dispute, event_id=source_event_id)

    def challenge(
        self,
        *,
        case_id: str,
        account_id: str,
        strategy_id: str,
        disputer: str,
        challenge_bond: Decimal,
        challenged_at: datetime,
        source_event_id: str,
    ) -> DisputeCase:
        current = self.store.get(case_id)
        if current.state is not DisputeState.CHALLENGE_OPEN:
            raise ValueError("proposal is not open for challenge")
        if challenged_at > current.challenge_deadline:
            raise ValueError("official two-hour challenge deadline elapsed")
        bond = Decimal(challenge_bond)
        if bond != current.proposer_bond:
            raise ValueError("challenge bond must equal the observed proposer bond")
        account_bond = DisputeBond(
            dispute_id=current.case_id,
            account_id=account_id,
            strategy_id=strategy_id,
            condition_id=current.condition_id,
            bond_amount=bond,
            currency=current.currency,
            posted_at=challenged_at,
            source_event_id=source_event_id,
        )
        if self.account_program is not None:
            self.account_program.post_dispute(account_bond)
        after = DisputeCase(
            **{
                **current.__dict__,
                "state": DisputeState.DISCUSSION,
                "disputer": disputer,
                "disputer_bond": bond,
                "disputed_at": challenged_at,
                "discussion_not_before": challenged_at + self.rules.discussion_min,
                "discussion_not_after": challenged_at + self.rules.discussion_max,
            }
        )
        return self.store.transition(
            after,
            from_state=current.state,
            event_id=source_event_id,
            event_type="PROPOSAL_CHALLENGED",
            event_ts=challenged_at,
            payload={
                "disputer": disputer,
                "challenge_bond": str(bond),
                "discussion_not_before": after.discussion_not_before.isoformat(),
                "discussion_not_after": after.discussion_not_after.isoformat(),
            },
        )

    def finalize_unchallenged(
        self, *, case_id: str, observed_at: datetime, source_event_id: str
    ) -> DisputeCase:
        current = self.store.get(case_id)
        if current.state is not DisputeState.CHALLENGE_OPEN:
            raise ValueError("proposal is not awaiting challenge finality")
        if observed_at < current.challenge_deadline:
            raise ValueError("challenge window has not elapsed")
        after = DisputeCase(
            **{
                **current.__dict__,
                "state": DisputeState.UNCHALLENGED_FINAL,
                "final_outcome": UmaOutcome.PROPOSER_WINS,
            }
        )
        return self.store.transition(
            after,
            from_state=current.state,
            event_id=source_event_id,
            event_type="UNCHALLENGED_FINALIZED",
            event_ts=observed_at,
            payload={"proposal_id": current.proposal_id},
        )

    def reconcile_uma_outcome(
        self,
        *,
        case_id: str,
        account_id: str,
        strategy_id: str,
        outcome: UmaOutcome,
        resolved_at: datetime,
        transaction_hash: str,
        source_event_id: str,
    ) -> DisputeCase:
        current = self.store.get(case_id)
        if current.state not in {DisputeState.DISCUSSION, DisputeState.VOTING}:
            raise ValueError("disputed case is not awaiting UMA finality")
        if not transaction_hash:
            raise ValueError("UMA finality requires a transaction hash")
        if current.disputer_bond is None or current.disputed_at is None:
            raise ValueError("dispute case has no challenger bond snapshot")
        account_bond = DisputeBond(
            dispute_id=current.case_id,
            account_id=account_id,
            strategy_id=strategy_id,
            condition_id=current.condition_id,
            bond_amount=current.disputer_bond,
            currency=current.currency,
            posted_at=current.disputed_at,
            source_event_id=source_event_id,
        )
        won = outcome in {
            UmaOutcome.DISPUTER_WINS,
            UmaOutcome.TOO_EARLY,
            UmaOutcome.UNKNOWN_50_50,
        }
        bounty = current.proposer_bond / Decimal(2) if won else Decimal(0)
        if self.account_program is not None:
            self.account_program.resolve_dispute(
                account_bond,
                outcome=DisputeOutcome.WON if won else DisputeOutcome.LOST,
                resolved_at=resolved_at,
                transaction_hash=transaction_hash,
                bounty_amount=bounty,
            )
        after = DisputeCase(
            **{
                **current.__dict__,
                "state": DisputeState.FINAL,
                "final_outcome": outcome,
                "transaction_hash": transaction_hash,
            }
        )
        return self.store.transition(
            after,
            from_state=current.state,
            event_id=source_event_id,
            event_type="UMA_OUTCOME_CONFIRMED",
            event_ts=resolved_at,
            payload={
                "outcome": outcome.value,
                "transaction_hash": transaction_hash,
                "challenger_won": won,
                "bounty": str(bounty),
            },
        )
