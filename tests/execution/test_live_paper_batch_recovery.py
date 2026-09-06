from decimal import Decimal

from quant.paper.live_shadow_store import (
    _audit_has_working_remainder,
    _recovered_order_state,
    _result_payload_from_audit,
)


def _audit(**overrides):
    value = {
        "audit_key": "audit-one",
        "status": "FILLED",
        "reason": "arrival_book_walk_complete",
        "intent": {
            "order_type": "FOK",
            "amount_unit": "SHARES",
            "size": "5",
        },
        "filled_size": Decimal("5"),
        "remaining_size": Decimal("0"),
        "fills": [{"price": "0.5", "size": "5", "fee": "0"}],
    }
    value.update(overrides)
    return value


def test_recovered_terminal_audit_is_not_a_working_remainder() -> None:
    audit = _audit()

    assert _audit_has_working_remainder(audit) is False
    assert _recovered_order_state(audit) == "CONFIRMED"


def test_recovered_working_remainder_requires_reconciliation() -> None:
    audit = _audit(
        status="PARTIAL",
        remaining_size=Decimal("2"),
        intent={"order_type": "GTC", "amount_unit": "SHARES", "size": "5"},
    )

    assert _audit_has_working_remainder(audit) is True


def test_recovered_audit_payload_preserves_fill_accounting() -> None:
    payload = _result_payload_from_audit(_audit())

    assert payload["audit_key"] == "audit-one"
    assert payload["filled_notional"] == "2.5"
    assert payload["remaining_amount"] == "0"
    assert payload["worker_recovered_from_durable_audit"] is True


def test_recovered_quote_payload_uses_notional_for_remaining_amount() -> None:
    payload = _result_payload_from_audit(
        _audit(
            intent={"order_type": "FOK", "amount_unit": "QUOTE", "size": "3"},
            filled_size=Decimal("5"),
            remaining_size=Decimal("0"),
        )
    )

    assert payload["filled_notional"] == "2.5"
    assert payload["remaining_amount"] == "0.5"
