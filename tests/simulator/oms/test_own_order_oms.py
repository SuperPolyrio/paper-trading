from decimal import Decimal

from quant.simulator.oms import (
    AttributedFill,
    CancelRequestStatus,
    EXTERNAL_STRATEGY_ID,
    ExternalOrderImporter,
    ExternalOrderSnapshot,
    InternalCrossPolicy,
    OmsAdmissionStatus,
    OmsOrderState,
    OwnOrder,
    OwnOrderBook,
    PositionAssignment,
    PositionAssignmentBook,
    SelfTradePolicy,
    StrategySubledger,
)


def _order(name: str, *, strategy: str, side: str, price: str, sequence: int) -> OwnOrder:
    return OwnOrder(
        order_id=f"order:{name}",
        account_id="account:one",
        strategy_id=strategy,
        asset_id="asset:one",
        side=side,
        price=Decimal(price),
        original_size=Decimal("10"),
        remaining_size=Decimal("10"),
        created_sequence=sequence,
    )


def test_default_policy_rejects_cross_strategy_self_trade_without_synthetic_fill() -> None:
    book = OwnOrderBook()
    policy = InternalCrossPolicy()
    resting = _order("resting", strategy="strategy:a", side="SELL", price="0.5", sequence=1)
    incoming = _order("incoming", strategy="strategy:b", side="BUY", price="0.5", sequence=2)

    assert policy.admit(book, resting).status is OmsAdmissionStatus.ACCEPTED
    decision = policy.admit(book, incoming)

    assert decision.status is OmsAdmissionStatus.REJECTED_SELF_TRADE
    assert book.get(incoming.order_id) is None
    assert book.get(resting.order_id).remaining_size == Decimal("10")


def test_cancel_oldest_defers_new_order_until_cancel_ack_and_does_not_repeat_cancel() -> None:
    book = OwnOrderBook()
    policy = InternalCrossPolicy(SelfTradePolicy.CANCEL_OLDEST)
    resting = _order("resting", strategy="strategy:a", side="SELL", price="0.5", sequence=1)
    incoming = _order("incoming", strategy="strategy:b", side="BUY", price="0.5", sequence=2)
    policy.admit(book, resting)

    decision = policy.admit(book, incoming)

    assert decision.status is OmsAdmissionStatus.DEFERRED_CANCEL_PENDING
    assert book.get(resting.order_id).state is OmsOrderState.PENDING_CANCEL
    assert book.get(incoming.order_id) is None
    assert book.request_cancel(resting.order_id, event_id="other-cancel") is CancelRequestStatus.ALREADY_PENDING
    book.acknowledge_cancel(resting.order_id)
    assert policy.admit(book, incoming).status is OmsAdmissionStatus.ACCEPTED


def test_closing_one_strategy_only_marks_its_own_orders_pending_cancel() -> None:
    book = OwnOrderBook()
    first = _order("first", strategy="strategy:a", side="BUY", price="0.4", sequence=1)
    second = _order("second", strategy="strategy:b", side="BUY", price="0.4", sequence=2)
    book.submit(first)
    book.submit(second)

    selected = book.close_strategy(account_id="account:one", strategy_id="strategy:a", event_prefix="shutdown")

    assert selected == (first.order_id,)
    assert book.get(first.order_id).state is OmsOrderState.PENDING_CANCEL
    assert book.get(second.order_id).state is OmsOrderState.WORKING


def test_strategy_attribution_sums_to_shared_account_including_external_bucket() -> None:
    assignments = PositionAssignmentBook()
    ledger = StrategySubledger(assignments)
    for order_id, strategy in (("order:a", "strategy:a"), ("order:b", "strategy:b")):
        ledger.register_order(PositionAssignment(order_id, "account:one", strategy, "asset:one"))
    assignments.assign_external(order_id="external:venue-1", account_id="account:one", asset_id="asset:one")

    ledger.apply_fill(AttributedFill("fill:a", "order:a", "account:one", "asset:one", "BUY", Decimal("0.5"), Decimal("5")))
    ledger.apply_fill(AttributedFill("fill:b", "order:b", "account:one", "asset:one", "BUY", Decimal("0.6"), Decimal("3")))
    ledger.apply_fill(AttributedFill("fill:external", "external:venue-1", "account:one", "asset:one", "BUY", Decimal("0.4"), Decimal("2")))

    assert ledger.position(account_id="account:one", strategy_id=EXTERNAL_STRATEGY_ID, asset_id="asset:one").quantity == Decimal("2")
    assert ledger.account_position(account_id="account:one", asset_id="asset:one") == Decimal("10")
    assert ledger.reconciles_account_position(account_id="account:one", asset_id="asset:one", account_quantity=Decimal("10"))


def test_external_order_import_is_idempotent_and_owned_by_external_bucket() -> None:
    book = OwnOrderBook()
    assignments = PositionAssignmentBook()
    importer = ExternalOrderImporter(assignments)
    snapshot = ExternalOrderSnapshot(
        venue_order_id="venue-1",
        account_id="account:one",
        asset_id="asset:one",
        side="SELL",
        price=Decimal("0.7"),
        original_size=Decimal("4"),
        remaining_size=Decimal("4"),
        observed_sequence=9,
    )

    first = importer.import_snapshot(book, snapshot)
    second = importer.import_snapshot(book, snapshot)

    assert first == second
    assert first.strategy_id == EXTERNAL_STRATEGY_ID
    assert assignments.get(first.order_id).strategy_id == EXTERNAL_STRATEGY_ID
