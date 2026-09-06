import json
from datetime import datetime, timezone
from pathlib import Path

from quant.calibration.calibration_domain import payload_hash
from quant.maker.historical_order_recovery import HistoricalMakerOrderRecovery

ORDER_ID = "0x" + "ab" * 32
ASSET_ID = "123"
CONDITION_ID = "0x" + "cd" * 32
MAKER = "0x" + "12" * 20


class FakeAdapter:
    def get_authenticated_trades(self, **_kwargs):
        return [
            {
                "id": "trade-1",
                "trader_side": "MAKER",
                "market": CONDITION_ID,
                "asset_id": ASSET_ID,
                "match_time": "1787712120",
                "maker_address": MAKER,
                "maker_orders": [
                    {
                        "order_id": ORDER_ID,
                        "maker_address": MAKER,
                        "asset_id": ASSET_ID,
                        "matched_amount": "5000000",
                    }
                ],
            }
        ]

    def get_order_reconciliation_snapshot(self, **_kwargs):
        return {
            "order": {
                "id": ORDER_ID,
                "status": "MATCHED",
                "original_size": "5",
            },
            "trades": [
                {
                    "id": "trade-1",
                    "status": "TRADE_STATUS_CONFIRMED",
                    "market": CONDITION_ID,
                    "asset_id": ASSET_ID,
                    "size": "5000000",
                    "price": "0.4",
                    "match_time": "1787712120",
                    "transaction_hash": "0x" + "ef" * 32,
                    "maker_orders": [
                        {
                            "order_id": ORDER_ID,
                            "maker_address": MAKER,
                            "asset_id": ASSET_ID,
                            "matched_amount": "5000000",
                            "price": "0.4",
                            "outcome": "YES",
                        }
                    ],
                }
            ],
            "open_orders": [],
            "order_lookup_error": None,
        }


class FakeOnchain:
    def reconcile(self, **kwargs):
        return {
            "status": "CONFIRMED_MATCH",
            "matched_size": str(kwargs["expected_matched_size"]),
            "receipt_truth": {"status": "CONFIRMED_SUCCESS", "complete": True},
        }


class RejectedAdapter(FakeAdapter):
    def get_order_reconciliation_snapshot(self, **_kwargs):
        return {
            "order": {
                "id": ORDER_ID,
                "status": "REJECTED",
                "original_size": "5",
                "size_matched": "0",
            },
            "trades": [],
            "open_orders": [],
            "order_lookup_error": None,
        }


def test_historical_recovery_separates_truth_from_prospective_calibration(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    frozen_at = "2026-08-26T00:00:00+00:00"
    prediction = {
        "schema_version": "maker_prediction_snapshot_v1",
        "frozen_at": frozen_at,
        "book_checkpoint_id": "checkpoint-1",
    }
    (evidence_dir / "probe.json").write_text(
        json.dumps(
            {
                "order_id": ORDER_ID,
                "asset_id": ASSET_ID,
                "condition_id": CONDITION_ID,
                "submitted_at": "2026-08-26T00:00:01+00:00",
                "prediction_snapshot": prediction,
                "prediction_snapshot_hash": payload_hash(
                    prediction, prefix="maker-prediction-"
                ),
            }
        ),
        encoding="utf-8",
    )
    recovery = HistoricalMakerOrderRecovery(
        adapter=FakeAdapter(),
        maker_address=MAKER,
        output_dir=tmp_path / "output",
        onchain_truth_reconciler=FakeOnchain(),
        local_evidence_roots=(evidence_dir,),
    )

    report = recovery.recover(
        after=datetime(2026, 8, 25, tzinfo=timezone.utc),
        before=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )
    row = json.loads(
        Path(report["manifest"][0]["path"]).read_text(encoding="utf-8")
    )

    assert report["outcome_counts"] == {"FULL": 1}
    assert row["historical_truth_eligible"] is True
    assert row["prospective_calibration_eligible"] is True
    assert row["venue_acceptance_observed"] is True
    assert row["finality_label"]["label"] == "CONFIRMED"
    assert report["exchange_submit_called"] is False
    assert report["exchange_cancel_called"] is False


def test_historical_fill_without_frozen_prediction_is_not_calibration(
    tmp_path: Path,
) -> None:
    recovery = HistoricalMakerOrderRecovery(
        adapter=FakeAdapter(),
        maker_address=MAKER,
        output_dir=tmp_path / "output",
        onchain_truth_reconciler=FakeOnchain(),
    )

    report = recovery.recover(
        after=datetime(2026, 8, 25, tzinfo=timezone.utc),
        before=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )
    row = json.loads(
        Path(report["manifest"][0]["path"]).read_text(encoding="utf-8")
    )

    assert row["historical_truth_eligible"] is True
    assert row["prospective_calibration_eligible"] is False


def test_rejected_order_is_not_mislabeled_as_historical_no_fill(
    tmp_path: Path,
) -> None:
    recovery = HistoricalMakerOrderRecovery(
        adapter=RejectedAdapter(),
        maker_address=MAKER,
        output_dir=tmp_path / "output",
        onchain_truth_reconciler=FakeOnchain(),
    )

    report = recovery.recover(
        after=datetime(2026, 8, 25, tzinfo=timezone.utc),
        before=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )
    row = json.loads(
        Path(report["manifest"][0]["path"]).read_text(encoding="utf-8")
    )

    assert row["actual_outcome"] == "NO_FILL"
    assert row["venue_acceptance_observed"] is False
    assert row["historical_truth_eligible"] is False
    assert row["prospective_calibration_eligible"] is False
