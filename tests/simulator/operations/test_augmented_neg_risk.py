from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant.simulator.operations import (
    AugmentedNegRiskEvent,
    AugmentedOutcome,
    AugmentedOutcomeKind,
    ConfirmedOperationLedger,
    NonceReservationBook,
    PositionOperationIntent,
    PositionOperationMachine,
    PositionOperationType,
    build_augmented_conversion_intent,
    build_conversion_matrix,
    clarify_placeholder,
    is_augmented_neg_risk_event,
    normalize_augmented_event_payload,
    reconcile_augmented_conversion,
)

NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)


def _event() -> AugmentedNegRiskEvent:
    return AugmentedNegRiskEvent(
        event_key="election:event",
        version=1,
        effective_ts=NOW,
        source="OFFICIAL_NEG_RISK_ADAPTER",
        source_event_id="event-v1",
        raw_payload_hash="hash-v1",
        outcomes=(
            AugmentedOutcome(
                "alice", "Alice", "condition-a", "a-yes", "a-no", AugmentedOutcomeKind.NAMED
            ),
            AugmentedOutcome(
                "slot-2",
                "Placeholder 2",
                "condition-b",
                "b-yes",
                "b-no",
                AugmentedOutcomeKind.PLACEHOLDER,
                tradeable=False,
            ),
            AugmentedOutcome(
                "other", "Other", "condition-c", "c-yes", "c-no", AugmentedOutcomeKind.OTHER,
                tradeable=False,
            ),
        ),
    )


def test_conversion_matrix_includes_placeholder_for_economic_conservation() -> None:
    matrix = build_conversion_matrix(_event(), "alice")

    assert matrix.token_deltas_per_unit == {
        "a-no": Decimal("-1"),
        "b-yes": Decimal("1"),
        "c-yes": Decimal("1"),
    }
    assert set(matrix.conservation_by_winner.values()) == {Decimal(0)}
    assert "b-yes" not in _event().tradeable_assets()


def test_placeholder_clarification_is_a_new_immutable_tradeable_version() -> None:
    first = _event()
    second = clarify_placeholder(
        first,
        placeholder_outcome_id="slot-2",
        label="Bob",
        effective_ts=NOW + timedelta(hours=1),
        source_event_id="event-v2",
        raw_payload_hash="hash-v2",
    )

    assert first.outcome("slot-2").kind is AugmentedOutcomeKind.PLACEHOLDER
    assert second.version == 2
    assert second.outcome("slot-2").kind is AugmentedOutcomeKind.NAMED
    assert second.outcome("slot-2").tradeable is True


def test_augmented_conversion_uses_atomic_position_operation_and_failure_rolls_back() -> None:
    intent = build_augmented_conversion_intent(
        _event(),
        source_outcome_id="alice",
        amount=Decimal("2"),
        account_id="account",
        strategy_id="strategy",
        decision_ts=NOW,
    )
    assert intent.token_deltas == {
        "a-no": Decimal("-2.000000"),
        "b-yes": Decimal("2.000000"),
        "c-yes": Decimal("2.000000"),
    }
    ledger = ConfirmedOperationLedger()
    ledger.token_by_account_asset[("account", "a-no")] = Decimal("2")
    machine = PositionOperationMachine(
        intent, nonces=NonceReservationBook(), ledger=ledger
    )
    machine.allowance_checked(event_id="allow", approved=True)
    machine.reserve_nonce(event_id="nonce", nonce=1)
    machine.fail(event_id="failed", reason="chain_revert")

    assert ledger.token_balance(account_id="account", asset_id="a-no") == Decimal("2")
    assert ledger.token_balance(account_id="account", asset_id="b-yes") == 0


def test_split_convert_redeem_chain_and_official_reconciliation() -> None:
    ledger = ConfirmedOperationLedger()
    nonces = NonceReservationBook()
    split = PositionOperationMachine(
        PositionOperationIntent(
            event_id="split",
            operation_type=PositionOperationType.SPLIT,
            account_id="account",
            strategy_id="strategy",
            condition_id="condition-a",
            amount=Decimal("1"),
            decision_ts=NOW,
            collateral_delta=Decimal("-1"),
            token_deltas={"a-yes": Decimal("1"), "a-no": Decimal("1")},
        ),
        nonces=nonces,
        ledger=ledger,
    )
    for machine, nonce in ((split, 1),):
        machine.allowance_checked(event_id=f"allow:{nonce}", approved=True)
        machine.reserve_nonce(event_id=f"nonce:{nonce}", nonce=nonce)
        machine.submit(event_id=f"submit:{nonce}", transaction_hash=f"0x{nonce}")
        machine.mined(event_id=f"mined:{nonce}")
        machine.confirm(event_id=f"confirm:{nonce}")
    convert_intent = build_augmented_conversion_intent(
        _event(),
        source_outcome_id="alice",
        amount=Decimal("1"),
        account_id="account",
        strategy_id="strategy",
        decision_ts=NOW,
        operation_id="convert",
    )
    convert = PositionOperationMachine(convert_intent, nonces=nonces, ledger=ledger)
    convert.allowance_checked(event_id="allow:2", approved=True)
    convert.reserve_nonce(event_id="nonce:2", nonce=2)
    convert.submit(event_id="submit:2", transaction_hash="0x2")
    convert.mined(event_id="mined:2")
    convert.confirm(event_id="confirm:2")
    redeem = PositionOperationMachine(
        PositionOperationIntent(
            event_id="redeem",
            operation_type=PositionOperationType.REDEEM,
            account_id="account",
            strategy_id="strategy",
            condition_id="condition-b",
            amount=Decimal("1"),
            decision_ts=NOW,
            collateral_delta=Decimal("1"),
            token_deltas={"b-yes": Decimal("-1")},
        ),
        nonces=nonces,
        ledger=ledger,
    )
    redeem.allowance_checked(event_id="allow:3", approved=True)
    redeem.reserve_nonce(event_id="nonce:3", nonce=3)
    redeem.submit(event_id="submit:3", transaction_hash="0x3")
    redeem.mined(event_id="mined:3")
    redeem.confirm(event_id="confirm:3")

    reconciliation = reconcile_augmented_conversion(
        operation_id="convert",
        expected_token_deltas=convert_intent.token_deltas,
        observed_token_deltas=convert_intent.token_deltas,
        source_event_id="conversion-log:1",
        raw_payload_hash="raw",
        reconciled_at=NOW,
        transaction_hash="0x2",
    )
    assert reconciliation.status == "PASS"
    assert reconciliation.amount_delta == 0
    assert ledger.collateral_by_account["account"] == 0
    assert ledger.token_balance(account_id="account", asset_id="b-yes") == 0


def test_placeholder_cannot_be_marked_tradeable_and_missing_chain_truth_is_not_pass() -> None:
    with pytest.raises(ValueError, match="only named"):
        AugmentedOutcome(
            "slot", "Placeholder", "condition", "yes", "no", AugmentedOutcomeKind.PLACEHOLDER
        )
    reconciliation = reconcile_augmented_conversion(
        operation_id="convert",
        expected_token_deltas={"a-no": Decimal("-1")},
        observed_token_deltas=None,
        source_event_id="missing",
        raw_payload_hash="raw",
        reconciled_at=NOW,
    )
    assert reconciliation.status == "NO_OFFICIAL_EVIDENCE"


def test_gamma_augmented_event_adapter_identifies_named_placeholder_and_other() -> None:
    payload = {
        "id": "event-1",
        "slug": "candidate-event",
        "enableNegRisk": True,
        "negRiskAugmented": True,
        "markets": [
            {
                "id": "market-a",
                "conditionId": "condition-a",
                "groupItemTitle": "Alice",
                "clobTokenIds": '["a-yes","a-no"]',
                "outcomes": '["Yes","No"]',
                "active": True,
                "acceptingOrders": True,
            },
            {
                "id": "market-b",
                "conditionId": "condition-b",
                "groupItemTitle": "Trade Indexer Placeholder 2",
                "slug": "trade-indexer-placeholder-2",
                "clobTokenIds": '["b-yes","b-no"]',
                "outcomes": '["Yes","No"]',
            },
            {
                "id": "market-other",
                "conditionId": "condition-other",
                "groupItemTitle": "Other",
                "negRiskOther": True,
                "clobTokenIds": '["o-yes","o-no"]',
                "outcomes": '["Yes","No"]',
            },
        ],
    }

    event = normalize_augmented_event_payload(
        payload, version=1, effective_ts=NOW
    )

    assert is_augmented_neg_risk_event(payload) is True
    assert event.outcome("market-a").tradeable is True
    assert event.outcome("market-b").kind is AugmentedOutcomeKind.PLACEHOLDER
    assert event.outcome("market-b").tradeable is False
    assert event.outcome("market-other").kind is AugmentedOutcomeKind.OTHER
    assert event.outcome("market-other").tradeable is False
