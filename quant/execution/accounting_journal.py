"""Independent balanced journal and materialized-state rebuild verifier."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

ACCOUNTS = {
    "CASH_AVAILABLE",
    "CASH_RESERVED",
    "TOKEN_POSITION",
    "TOKEN_RESERVED",
    "TRADE_RECEIVABLE_PENDING",
    "FEE_EXPENSE",
    "REBATE_RECEIVABLE",
    "REALIZED_PNL",
    "SETTLEMENT_RECEIVABLE",
}


@dataclass(frozen=True)
class JournalLine:
    account: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.account not in ACCOUNTS:
            raise ValueError(f"unknown account {self.account}")
        if self.debit < 0 or self.credit < 0 or (self.debit and self.credit):
            raise ValueError("journal line must have one non-negative debit or credit")


@dataclass(frozen=True)
class JournalGroup:
    journal_id: str
    event_type: str
    lines: tuple[JournalLine, ...]

    @property
    def debit_total(self) -> Decimal:
        return sum((line.debit for line in self.lines), Decimal("0"))

    @property
    def credit_total(self) -> Decimal:
        return sum((line.credit for line in self.lines), Decimal("0"))

    def assert_balanced(self) -> None:
        if not self.lines or self.debit_total != self.credit_total:
            raise ValueError(
                f"unbalanced journal {self.journal_id}: {self.debit_total} != {self.credit_total}"
            )


class IndependentJournal:
    def __init__(self) -> None:
        self._groups: dict[str, JournalGroup] = {}

    def append(self, group: JournalGroup) -> None:
        group.assert_balanced()
        existing = self._groups.get(group.journal_id)
        if existing is not None and existing != group:
            raise ValueError("idempotency key reused for different journal")
        self._groups[group.journal_id] = group

    def rebuild(self) -> dict[str, Decimal]:
        balances = {account: Decimal("0") for account in ACCOUNTS}
        for key in sorted(self._groups):
            for line in self._groups[key].lines:
                balances[line.account] += line.debit - line.credit
        return balances

    def verify_materialized(
        self,
        expected: dict[str, Decimal],
    ) -> dict[str, object]:
        rebuilt = self.rebuild()
        mismatches = {
            account: {
                "rebuilt": format(rebuilt.get(account, Decimal("0")), "f"),
                "materialized": format(value, "f"),
            }
            for account, value in expected.items()
            if rebuilt.get(account, Decimal("0")) != value
        }
        return {
            "status": "PASS" if not mismatches else "FAIL",
            "mismatches": mismatches,
        }


def transfer(
    journal_id: str,
    event_type: str,
    *,
    debit_account: str,
    credit_account: str,
    amount: Decimal,
) -> JournalGroup:
    if amount < 0:
        raise ValueError("transfer amount must be non-negative")
    return JournalGroup(
        journal_id,
        event_type,
        (
            JournalLine(debit_account, debit=amount),
            JournalLine(credit_account, credit=amount),
        ),
    )


def verify_groups(groups: Iterable[JournalGroup]) -> dict[str, object]:
    errors = []
    for group in groups:
        try:
            group.assert_balanced()
        except ValueError as exc:
            errors.append(str(exc))
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}
