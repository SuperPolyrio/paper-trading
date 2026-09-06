from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from eth_abi import encode as abi_encode

from quant.simulator.account_truth import (
    AccountingEquity,
    AccountingPosition,
    AccountTruthGateStatus,
    AccountTruthReconciler,
    ChainActivityMirrorError,
    ClosedPosition,
    OfficialAccountBundle,
    OfficialActivity,
    OfficialPosition,
    ParsedAccountingSnapshot,
    ReceiptEvidence,
    analyze_chain_account_differences,
    decode_wallet_transfers,
    replay_chain_activity,
)
from quant.simulator.account_truth.chain_activity_mirror import (
    CTF_CONTRACT,
    ERC1155_TRANSFER_BATCH_TOPIC,
    ERC1155_TRANSFER_SINGLE_TOPIC,
    ERC20_TRANSFER_TOPIC,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
WALLET = "0x1111111111111111111111111111111111111111"
VENUE = "0x2222222222222222222222222222222222222222"
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
CONDITION = "0x" + "33" * 32


def test_decode_wallet_transfers_handles_erc20_single_and_batch() -> None:
    tx_hash = _tx(1)
    payload = _receipt(
        tx_hash,
        1,
        [
            _erc20(PUSD, VENUE, WALLET, Decimal("3.25")),
            _single(VENUE, WALLET, "7", Decimal("2")),
            _batch(WALLET, VENUE, {"7": Decimal("0.5"), "8": Decimal("1")}),
        ],
    )

    result = decode_wallet_transfers(payload, account_address=WALLET)

    assert result.cash_delta == Decimal("3.25")
    assert result.token_deltas == {
        "7": Decimal("1.5"),
        "8": Decimal("-1"),
    }


def test_real_trade_replay_matches_dual_basis_and_account_pnl() -> None:
    deposit = _activity(1, "DEPOSIT")
    buy = _activity(2, "TRADE", asset_id="7", side="BUY", price="0.4")
    sell = _activity(3, "TRADE", asset_id="7", side="SELL", price="0.6")
    receipts = {
        deposit.transaction_hash: _evidence(
            deposit.transaction_hash,
            1,
            [_erc20(PUSD, VENUE, WALLET, Decimal("10"))],
        ),
        buy.transaction_hash: _evidence(
            buy.transaction_hash,
            2,
            [
                _erc20(PUSD, WALLET, VENUE, Decimal("0.82")),
                _single(VENUE, WALLET, "7", Decimal("2")),
            ],
        ),
        sell.transaction_hash: _evidence(
            sell.transaction_hash,
            3,
            [
                _erc20(PUSD, VENUE, WALLET, Decimal("0.59")),
                _single(WALLET, VENUE, "7", Decimal("1")),
            ],
        ),
    }
    official = _official(
        cash="9.77",
        positions=(
            _official_position(
                asset_id="7",
                size="1",
                avg_price="0.4",
                initial_value="0.4",
                gross_initial_value="0.41",
                entry_fees="0.01",
                current_price="0.5",
                current_value="0.5",
                cash_pnl="0.1",
                realized_pnl="0.18",
                total_bought="2",
            ),
        ),
    )

    mirror = replay_chain_activity(
        official=official,
        activities=(deposit, buy, sell),
        receipts=receipts,
        condition_ids={"7": CONDITION},
    )
    report = AccountTruthReconciler().reconcile(
        official=official,
        paper=mirror.snapshot,
        comparison_scope="WHOLE_ACCOUNT",
    )

    position = mirror.snapshot.positions["7"]
    assert mirror.summary["asset_replay_gate"] == "PASS"
    assert position.quantity == Decimal("1")
    assert position.fee_exclusive_basis == Decimal("0.4")
    assert position.entry_fees == Decimal("0.01")
    assert position.realized_pnl == Decimal("0.18")
    assert mirror.snapshot.cash_balance == Decimal("9.77")
    assert report.status is AccountTruthGateStatus.PASS
    assert report.summary["pnl_truth_contract"]["status"] == "REFERENCE_REPLAY_PASS"
    assert (
        report.summary["pnl_truth_contract"]["claim"]
        == "INDEPENDENT_CHAIN_ACCOUNTING_REPLAY"
    )


def test_redeem_realizes_remaining_gross_basis_without_open_position() -> None:
    deposit = _activity(1, "DEPOSIT")
    buy = _activity(2, "TRADE", asset_id="7", side="BUY", price="0.4")
    redeem = _activity(3, "REDEEM")
    receipts = {
        deposit.transaction_hash: _evidence(
            deposit.transaction_hash,
            1,
            [_erc20(PUSD, VENUE, WALLET, Decimal("10"))],
        ),
        buy.transaction_hash: _evidence(
            buy.transaction_hash,
            2,
            [
                _erc20(PUSD, WALLET, VENUE, Decimal("0.82")),
                _single(VENUE, WALLET, "7", Decimal("2")),
            ],
        ),
        redeem.transaction_hash: _evidence(
            redeem.transaction_hash,
            3,
            [
                _erc20(PUSD, VENUE, WALLET, Decimal("2")),
                _single(WALLET, VENUE, "7", Decimal("2")),
            ],
        ),
    }
    closed = ClosedPosition(
        account_address=WALLET,
        asset_id="7",
        condition_id=CONDITION,
        avg_price=Decimal("0.4"),
        total_bought=Decimal("2"),
        realized_pnl=Decimal("1.18"),
        current_price=Decimal("1"),
        closed_at=NOW,
    )
    official = _official(cash="11.18", positions=(), closed=(closed,))

    mirror = replay_chain_activity(
        official=official,
        activities=(deposit, buy, redeem),
        receipts=receipts,
        condition_ids={"7": CONDITION},
    )

    assert mirror.snapshot.cash_balance == Decimal("11.18")
    assert mirror.snapshot.realized_pnl == Decimal("1.18")
    assert mirror.snapshot.positions["7"].quantity == 0
    assert mirror.snapshot.positions["7"].realized_pnl == Decimal("1.18")
    assert mirror.raw_positions["7"]["realized_pnl"] == "1.18"
    report = AccountTruthReconciler().reconcile(
        official=official,
        paper=mirror.snapshot,
        comparison_scope="WHOLE_ACCOUNT",
    )
    assert report.status is AccountTruthGateStatus.PASS
    assert any(
        row["comparison_type"] == "OFFICIAL_VS_PAPER_CLOSED_POSITION"
        and row["status"] == "MATCH"
        for row in report.comparison_rows
    )


def test_split_merge_and_conversion_preserve_gross_basis() -> None:
    deposit = _activity(1, "DEPOSIT")
    split = _activity(2, "SPLIT")
    merge = _activity(3, "MERGE")
    buy = _activity(4, "TRADE", asset_id="9", side="BUY", price="0.8")
    conversion = _activity(5, "CONVERSION")
    receipts = {
        deposit.transaction_hash: _evidence(
            deposit.transaction_hash,
            1,
            [_erc20(PUSD, VENUE, WALLET, Decimal("2"))],
        ),
        split.transaction_hash: _evidence(
            split.transaction_hash,
            2,
            [
                _erc20(PUSD, WALLET, VENUE, Decimal("1")),
                _batch(VENUE, WALLET, {"7": Decimal("1"), "8": Decimal("1")}),
            ],
        ),
        merge.transaction_hash: _evidence(
            merge.transaction_hash,
            3,
            [
                _erc20(PUSD, VENUE, WALLET, Decimal("1")),
                _batch(WALLET, VENUE, {"7": Decimal("1"), "8": Decimal("1")}),
            ],
        ),
        buy.transaction_hash: _evidence(
            buy.transaction_hash,
            4,
            [
                _erc20(PUSD, WALLET, VENUE, Decimal("0.82")),
                _single(VENUE, WALLET, "9", Decimal("1")),
            ],
        ),
        conversion.transaction_hash: _evidence(
            conversion.transaction_hash,
            5,
            [
                _single(WALLET, VENUE, "9", Decimal("1")),
                _batch(VENUE, WALLET, {"10": Decimal("1"), "11": Decimal("1")}),
            ],
        ),
    }
    official = _official(
        cash="1.18",
        positions=(
            _official_position(
                asset_id="10",
                size="1",
                avg_price="0.4",
                initial_value="0.4",
                gross_initial_value="0.41",
                entry_fees="0.01",
                current_price="0.4",
                current_value="0.4",
                cash_pnl="0",
                realized_pnl="0",
                total_bought="1",
            ),
            _official_position(
                asset_id="11",
                size="1",
                avg_price="0.4",
                initial_value="0.4",
                gross_initial_value="0.41",
                entry_fees="0.01",
                current_price="0.4",
                current_value="0.4",
                cash_pnl="0",
                realized_pnl="0",
                total_bought="1",
            ),
        ),
    )

    mirror = replay_chain_activity(
        official=official,
        activities=(deposit, split, merge, buy, conversion),
        receipts=receipts,
        condition_ids={str(i): CONDITION for i in range(7, 12)},
    )

    assert mirror.snapshot.realized_pnl == 0
    assert mirror.summary["economic_replay_gate"] == "PASS"
    assert mirror.summary["economics"]["conservation_delta"] == "0.00"
    assert mirror.snapshot.positions["10"].cost_basis == Decimal("0.41")
    assert mirror.snapshot.positions["11"].cost_basis == Decimal("0.41")
    assert "7" not in mirror.raw_positions
    assert "8" not in mirror.raw_positions


def test_multiple_activity_rows_for_one_receipt_fail_closed() -> None:
    first = _activity(1, "DEPOSIT")
    second = OfficialActivity(
        **{**first.__dict__, "source_event_id": "duplicate-source-event"}
    )
    official = _official(cash="1", positions=())
    evidence = _evidence(
        first.transaction_hash,
        1,
        [_erc20(PUSD, VENUE, WALLET, Decimal("1"))],
    )

    with pytest.raises(ChainActivityMirrorError, match="maps to 2"):
        replay_chain_activity(
            official=official,
            activities=(first, second),
            receipts={first.transaction_hash: evidence},
        )


def test_difference_analysis_attributes_legacy_without_waiving_gate() -> None:
    deposit = replace(
        _activity(1, "DEPOSIT"),
        event_ts=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    buy = replace(
        _activity(2, "TRADE", asset_id="7", side="BUY", price="0.4"),
        event_ts=datetime(2026, 4, 20, 0, 1, tzinfo=timezone.utc),
    )
    receipts = {
        deposit.transaction_hash: _evidence(
            deposit.transaction_hash,
            1,
            [_erc20(PUSD, VENUE, WALLET, Decimal("10"))],
        ),
        buy.transaction_hash: _evidence(
            buy.transaction_hash,
            2,
            [
                _erc20(PUSD, WALLET, VENUE, Decimal("0.82")),
                _single(VENUE, WALLET, "7", Decimal("2")),
            ],
        ),
    }
    official = _official(
        cash="9.18",
        positions=(
            _official_position(
                asset_id="7",
                size="2",
                avg_price="0.5",
                initial_value="1",
                gross_initial_value="1",
                entry_fees="0",
                current_price="0.5",
                current_value="1",
                cash_pnl="0",
                realized_pnl="0",
                total_bought="2",
            ),
        ),
    )
    mirror = replay_chain_activity(
        official=official,
        activities=(deposit, buy),
        receipts=receipts,
        condition_ids={"7": CONDITION},
    )
    report = AccountTruthReconciler().reconcile(
        official=official,
        paper=mirror.snapshot,
        comparison_scope="WHOLE_ACCOUNT",
    )

    analysis = analyze_chain_account_differences(
        official=official,
        result=mirror,
        account_truth=report,
    )

    assert report.status is AccountTruthGateStatus.FAIL_ACCOUNT_TRUTH
    assert analysis["semantic_attribution_gate"] == "PASS"
    assert analysis["summary"]["unexplained_material_mismatch_count"] == 0
    assert {
        row["category"]
        for row in analysis["rows"]
        if row["comparison_key"] == "7"
    } == {"LEGACY_PRE_V2_DATA_API_ACCOUNTING"}


def _activity(
    index: int,
    activity_type: str,
    *,
    asset_id: str = "",
    side: str = "",
    price: str = "0",
) -> OfficialActivity:
    payload = {
        "type": activity_type,
        "asset": asset_id,
        "conditionId": CONDITION,
        "side": side,
        "price": price,
    }
    return OfficialActivity(
        source_event_id=f"source-{index}",
        account_address=WALLET,
        activity_type=activity_type,
        event_ts=NOW,
        condition_id=CONDITION,
        asset_id=asset_id,
        transaction_hash=_tx(index),
        raw_payload_hash=hashlib.sha256(str(payload).encode()).hexdigest(),
        raw_payload=payload,
    )


def _official(
    *,
    cash: str,
    positions: tuple[OfficialPosition, ...],
    closed: tuple[ClosedPosition, ...] = (),
) -> OfficialAccountBundle:
    accounting_positions = tuple(
        AccountingPosition(
            condition_id=row.condition_id,
            asset_id=row.asset_id,
            size=row.size,
            current_price=row.current_price,
            valuation_time=NOW,
        )
        for row in positions
    )
    positions_value = sum(
        (row.current_value for row in accounting_positions), Decimal(0)
    )
    return OfficialAccountBundle(
        run_id="official-test-run",
        account_address=WALLET,
        observed_at=NOW,
        source_as_of=NOW,
        positions=positions,
        closed_positions=closed,
        accounting=ParsedAccountingSnapshot(
            positions=accounting_positions,
            equity=AccountingEquity(
                cash_balance=Decimal(cash),
                positions_value=positions_value,
                equity=Decimal(cash) + positions_value,
                valuation_time=NOW,
            ),
            positions_csv_sha256="a" * 64,
            equity_csv_sha256="b" * 64,
            zip_sha256="c" * 64,
        ),
        fetch_manifest={},
    )


def _official_position(
    *,
    asset_id: str,
    size: str,
    avg_price: str,
    initial_value: str,
    gross_initial_value: str,
    entry_fees: str,
    current_price: str,
    current_value: str,
    cash_pnl: str,
    realized_pnl: str,
    total_bought: str,
) -> OfficialPosition:
    return OfficialPosition(
        account_address=WALLET,
        asset_id=asset_id,
        condition_id=CONDITION,
        size=Decimal(size),
        avg_price=Decimal(avg_price),
        initial_value=Decimal(initial_value),
        gross_initial_value=Decimal(gross_initial_value),
        entry_fees_usdc=Decimal(entry_fees),
        current_value=Decimal(current_value),
        cash_pnl=Decimal(cash_pnl),
        realized_pnl=Decimal(realized_pnl),
        current_price=Decimal(current_price),
        total_bought=Decimal(total_bought),
        redeemable=False,
        mergeable=False,
    )


def _evidence(
    transaction_hash: str,
    block: int,
    logs: list[dict[str, object]],
) -> ReceiptEvidence:
    payload = _receipt(transaction_hash, block, logs)
    return ReceiptEvidence(
        transaction_hash=transaction_hash,
        payload=payload,
        content_sha256=hashlib.sha256(str(payload).encode()).hexdigest(),
        artifact_path=f"/{transaction_hash}.json",
        rpc_source="fixture",
    )


def _receipt(
    transaction_hash: str,
    block: int,
    logs: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "status": "0x1",
            "transactionHash": transaction_hash,
            "blockNumber": hex(block),
            "transactionIndex": "0x0",
            "logs": logs,
        },
    }


def _erc20(
    contract: str,
    sender: str,
    receiver: str,
    amount: Decimal,
) -> dict[str, object]:
    return {
        "address": contract,
        "topics": [ERC20_TRANSFER_TOPIC, _address_topic(sender), _address_topic(receiver)],
        "data": _word(_units(amount)),
    }


def _single(
    sender: str,
    receiver: str,
    token_id: str,
    amount: Decimal,
) -> dict[str, object]:
    return {
        "address": CTF_CONTRACT,
        "topics": [
            ERC1155_TRANSFER_SINGLE_TOPIC,
            _address_topic(VENUE),
            _address_topic(sender),
            _address_topic(receiver),
        ],
        "data": "0x" + f"{int(token_id):064x}" + f"{_units(amount):064x}",
    }


def _batch(
    sender: str,
    receiver: str,
    amounts: dict[str, Decimal],
) -> dict[str, object]:
    encoded = abi_encode(
        ["uint256[]", "uint256[]"],
        ([int(key) for key in amounts], [_units(value) for value in amounts.values()]),
    )
    return {
        "address": CTF_CONTRACT,
        "topics": [
            ERC1155_TRANSFER_BATCH_TOPIC,
            _address_topic(VENUE),
            _address_topic(sender),
            _address_topic(receiver),
        ],
        "data": "0x" + encoded.hex(),
    }


def _address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def _word(value: int) -> str:
    return "0x" + f"{value:064x}"


def _units(value: Decimal) -> int:
    return int(value * Decimal(1_000_000))


def _tx(index: int) -> str:
    return "0x" + f"{index:064x}"
