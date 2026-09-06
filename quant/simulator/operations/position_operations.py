"""Allowance/nonce/finality machine and confirmed-only operation balances."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from .domain import PositionOperationIntent, PositionOperationState


class NonceReservationBook:
    def __init__(self) -> None:
        self._reserved: dict[tuple[str, int], str] = {}

    def reserve(self, *, account_id: str, nonce: int, event_id: str) -> None:
        key = (str(account_id), int(nonce))
        current = self._reserved.get(key)
        if current is not None and current != event_id:
            raise ValueError("nonce already reserved by another operation")
        self._reserved[key] = str(event_id)

    def release(self, *, account_id: str, nonce: int, event_id: str) -> None:
        key = (str(account_id), int(nonce))
        if self._reserved.get(key) == str(event_id):
            del self._reserved[key]


@dataclass(frozen=True)
class OperationRecord:
    intent: PositionOperationIntent
    state: PositionOperationState = PositionOperationState.CREATED
    nonce: int | None = None
    transaction_hash: str | None = None
    reason: str | None = None


class ConfirmedOperationLedger:
    """Balances change only once an operation reaches confirmed finality."""

    def __init__(self) -> None:
        self.collateral_by_account: dict[str, Decimal] = {}
        self.token_by_account_asset: dict[tuple[str, str], Decimal] = {}
        self._applied_events: set[str] = set()

    def apply_confirmed(self, intent: PositionOperationIntent) -> None:
        if intent.event_id in self._applied_events:
            return
        next_tokens: dict[tuple[str, str], Decimal] = {}
        for asset_id, delta in intent.token_deltas.items():
            key = (intent.account_id, asset_id)
            next_value = self.token_by_account_asset.get(key, Decimal(0)) + Decimal(
                delta
            )
            if next_value < 0:
                raise ValueError(
                    "confirmed operation would create negative token balance"
                )
            next_tokens[key] = next_value
        self.collateral_by_account[intent.account_id] = (
            self.collateral_by_account.get(intent.account_id, Decimal(0))
            + intent.collateral_delta
        )
        self.token_by_account_asset.update(next_tokens)
        self._applied_events.add(intent.event_id)

    def token_balance(self, *, account_id: str, asset_id: str) -> Decimal:
        return self.token_by_account_asset.get((account_id, asset_id), Decimal(0))


class PositionOperationMachine:
    def __init__(
        self,
        intent: PositionOperationIntent,
        *,
        nonces: NonceReservationBook,
        ledger: ConfirmedOperationLedger,
    ) -> None:
        self.record = OperationRecord(intent)
        self._nonces = nonces
        self._ledger = ledger
        self._events: set[str] = set()

    def allowance_checked(self, *, event_id: str, approved: bool) -> OperationRecord:
        if self._seen(event_id):
            return self.record
        self._require(PositionOperationState.CREATED)
        self._record_event(event_id)
        self.record = replace(
            self.record,
            state=PositionOperationState.ALLOWANCE_CHECKED
            if approved
            else PositionOperationState.FAILED,
            reason=None if approved else "allowance_missing",
        )
        return self.record

    def reserve_nonce(self, *, event_id: str, nonce: int) -> OperationRecord:
        if self._seen(event_id):
            return self.record
        self._require(PositionOperationState.ALLOWANCE_CHECKED)
        self._record_event(event_id)
        self._nonces.reserve(
            account_id=self.record.intent.account_id,
            nonce=nonce,
            event_id=self.record.intent.event_id,
        )
        self.record = replace(
            self.record, state=PositionOperationState.NONCE_RESERVED, nonce=int(nonce)
        )
        return self.record

    def submit(self, *, event_id: str, transaction_hash: str) -> OperationRecord:
        if self._seen(event_id):
            return self.record
        self._require(PositionOperationState.NONCE_RESERVED)
        self._record_event(event_id)
        self.record = replace(
            self.record,
            state=PositionOperationState.SUBMITTED,
            transaction_hash=str(transaction_hash),
        )
        return self.record

    def mined(self, *, event_id: str) -> OperationRecord:
        if self._seen(event_id):
            return self.record
        self._require(PositionOperationState.SUBMITTED)
        self._record_event(event_id)
        self.record = replace(self.record, state=PositionOperationState.MINED)
        return self.record

    def confirm(self, *, event_id: str) -> OperationRecord:
        if self._seen(event_id):
            return self.record
        self._require(PositionOperationState.MINED)
        self._record_event(event_id)
        self._ledger.apply_confirmed(self.record.intent)
        self.record = replace(self.record, state=PositionOperationState.CONFIRMED)
        self._release_nonce()
        return self.record

    def fail(self, *, event_id: str, reason: str) -> OperationRecord:
        if self._seen(event_id):
            return self.record
        if self.record.state in {
            PositionOperationState.CONFIRMED,
            PositionOperationState.RECONCILED,
        }:
            raise ValueError("confirmed operation cannot fail")
        self._record_event(event_id)
        self.record = replace(
            self.record, state=PositionOperationState.FAILED, reason=str(reason)
        )
        self._release_nonce()
        return self.record

    def reconcile(self, *, event_id: str) -> OperationRecord:
        if self._seen(event_id):
            return self.record
        if self.record.state not in {
            PositionOperationState.CONFIRMED,
            PositionOperationState.FAILED,
        }:
            raise ValueError("only terminal operations can reconcile")
        self._record_event(event_id)
        self.record = replace(self.record, state=PositionOperationState.RECONCILED)
        return self.record

    def _require(self, state: PositionOperationState) -> None:
        if self.record.state is not state:
            raise ValueError(
                f"invalid operation state {self.record.state.value}; expected {state.value}"
            )

    def _seen(self, event_id: str) -> bool:
        return str(event_id) in self._events

    def _record_event(self, event_id: str) -> None:
        key = str(event_id)
        self._events.add(key)

    def _release_nonce(self) -> None:
        if self.record.nonce is not None:
            self._nonces.release(
                account_id=self.record.intent.account_id,
                nonce=self.record.nonce,
                event_id=self.record.intent.event_id,
            )
