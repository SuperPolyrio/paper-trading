from decimal import Decimal

from quant.paper.cash_reconciliation import (
    ACCOUNT_CASHFLOW,
    COMBO_ACCOUNTING,
    PAPER_LEDGER,
    REWARD_CLAWBACK,
    REWARD_PAYOUT,
    TOTAL,
    load_account_cash_delta_breakdown,
)


class _Cursor:
    def __init__(self) -> None:
        self._one = None
        self.query = ""
        self.parameters = ()

    def execute(self, query, parameters) -> None:
        self.query = str(query)
        self.parameters = parameters
        if "to_regclass" in self.query:
            self._one = {"available": True}

    def fetchone(self):
        return self._one

    def fetchall(self):
        return [
            {"strategy_id": "s1", "source": PAPER_LEDGER, "cash_delta": "-1.2"},
            {"strategy_id": "s1", "source": ACCOUNT_CASHFLOW, "cash_delta": "10"},
            {"strategy_id": "s1", "source": REWARD_PAYOUT, "cash_delta": "0.4"},
            {"strategy_id": "s1", "source": REWARD_CLAWBACK, "cash_delta": "-0.1"},
            {"strategy_id": "s1", "source": COMBO_ACCOUNTING, "cash_delta": "0.2"},
        ]


def test_cash_reconciliation_combines_each_authoritative_source_once() -> None:
    cursor = _Cursor()

    result = load_account_cash_delta_breakdown(cursor, strategy_ids=["s1", "s1"])

    assert result["s1"] == {
        PAPER_LEDGER: Decimal("-1.2"),
        ACCOUNT_CASHFLOW: Decimal("10"),
        REWARD_PAYOUT: Decimal("0.4"),
        REWARD_CLAWBACK: Decimal("-0.1"),
        COMBO_ACCOUNTING: Decimal("0.2"),
        TOTAL: Decimal("9.3"),
    }
    assert "cash_applied=TRUE" in cursor.query
    assert "status='RECEIVED'" in cursor.query
    assert cursor.parameters[-1] == ["s1"]
