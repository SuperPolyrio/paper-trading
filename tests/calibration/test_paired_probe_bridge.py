from copy import deepcopy

import pytest

from quant.calibration.paired_probe_bridge import (
    sync_calibration_probe_to_paired,
)


PAIR_ID = "pp-bridge-test"


class FakeStore:
    def __init__(self, pair: dict) -> None:
        self.pair = deepcopy(pair)
        self.writes: list[dict] = []

    def load_paired_probe(self, probe_id: str):
        return deepcopy(self.pair) if probe_id == PAIR_ID else None

    def upsert_paired_probe(self, row: dict):
        self.pair = deepcopy(row)
        self.writes.append(deepcopy(row))
        return deepcopy(row)


def paired_probe() -> dict:
    return {
        "probe_id": PAIR_ID,
        "strategy_id": "test",
        "client_order_id": "paper-client-1",
        "paper_intent_id": 7,
        "mode": "no-submit",
        "status": "SHADOW_READY",
        "market_id": "market-1",
        "condition_id": "condition-1",
        "asset_id": "asset-1",
        "decision_ts": "2026-08-17T03:49:11Z",
        "paper_prediction": {
            "status": "FILLED",
            "queue_status": "COMPLETED",
            "audit_key": "audit-1",
            "filled_size": "5",
            "avg_fill_price": "0.19",
            "total_fee": "0.03",
        },
        "live_lifecycle": {
            "state": "NOT_SUBMITTED",
            "network_submit_called_by_probe": False,
        },
        "orderfilled_ex_self": {
            "state": "VERIFIED",
            "reason": "no self contamination is possible in no-submit mode",
        },
        "bucket_context": {"tick_size": "0.01"},
        "audit": {
            "exchange_submit_called": False,
            "order_submitter_present": False,
        },
    }


def calibration_probe() -> dict:
    return {
        "probe_id": "calprobe-1",
        "run_id": "calrun-1",
        "paired_probe_id": PAIR_ID,
        "probe_state": "CALIBRATABLE",
        "exchange_submit_called": True,
        "market_id": "market-1",
        "condition_id": "condition-1",
        "asset_id": "asset-1",
        "updated_at": "2026-08-17T03:51:01Z",
        "prediction": {"audit_key": "audit-1"},
        "signed_order_audit": {
            "maker": "0xMaker",
            "signer": "0xSigner",
            "order_hash": "0xOrder",
        },
        "lifecycle": {
            "state": "CONFIRMED",
            "order_id": "0xOrder",
            "transaction_hashes": ["0xTx"],
            "rest_order": {"maker_address": "0xMaker"},
        },
        "reconciliation": {
            "actual_class": "FULL",
            "actual_fee": "0.03",
            "order": {
                "order_id": "0xOrder",
                "first_match_at": "2026-08-17T03:50:48Z",
                "actual_matched_size": "5",
                "actual_avg_price": "0.19",
            },
        },
        "timestamps": {
            "http_response_completed_ts": "2026-08-17T03:50:49Z",
        },
    }


def test_live_calibration_promotes_shadow_pair_to_record_only() -> None:
    store = FakeStore(paired_probe())

    synced = sync_calibration_probe_to_paired(calibration_probe(), store=store)

    assert synced["mode"] == "record-only"
    assert synced["status"] == "PENDING_ORDERFILLED_EX_SELF"
    assert synced["live_lifecycle"]["state"] == "TERMINAL"
    assert synced["live_lifecycle"]["terminal_status"] == "FILLED"
    assert synced["live_lifecycle"]["live_fill_size"] == "5"
    assert synced["live_lifecycle"]["live_fill_price"] == "0.19"
    assert synced["audit"]["exchange_submit_called"] is False
    assert synced["audit"]["order_submitter_present"] is False
    assert synced["audit"]["external_live"]["exchange_submit_called"] is True
    assert synced["orderfilled_ex_self"]["state"] == "PENDING"


def test_repeated_sync_preserves_verified_delayed_evidence() -> None:
    store = FakeStore(paired_probe())
    first = sync_calibration_probe_to_paired(calibration_probe(), store=store)
    first["orderfilled_ex_self"] = {
        "state": "VERIFIED",
        "events": [],
        "source_coverage_complete": True,
        "transaction_confirmation_complete": True,
        "used_for_actual_live_outcome": False,
    }
    first["status"] = "CALIBRATION_READY"
    store.pair = first

    second = sync_calibration_probe_to_paired(calibration_probe(), store=store)

    assert second["status"] == "CALIBRATION_READY"
    assert second["orderfilled_ex_self"]["state"] == "VERIFIED"
    assert second["audit"]["external_live"]["own_order_hashes"] == [
        "0xorder"
    ]


def test_repeated_sync_resets_legacy_verified_evidence_without_watermark() -> None:
    store = FakeStore(paired_probe())
    first = sync_calibration_probe_to_paired(calibration_probe(), store=store)
    first["orderfilled_ex_self"] = {
        "state": "VERIFIED",
        "events": [],
        "used_for_actual_live_outcome": False,
    }
    first["status"] = "CALIBRATION_READY"
    store.pair = first

    second = sync_calibration_probe_to_paired(calibration_probe(), store=store)

    assert second["status"] == "PENDING_ORDERFILLED_EX_SELF"
    assert second["orderfilled_ex_self"]["state"] == "PENDING"
    assert second["orderfilled_ex_self"]["source_coverage_complete"] is False


def test_repeated_sync_preserves_confirmed_transaction_while_source_lags() -> None:
    store = FakeStore(paired_probe())
    first = sync_calibration_probe_to_paired(calibration_probe(), store=store)
    first["orderfilled_ex_self"] = {
        "state": "SOURCE_LAG",
        "source_watermark": "2026-08-16T00:42:36Z",
        "source_coverage_complete": False,
        "transaction_confirmation_complete": True,
        "observed_transaction_hashes": ["tx"],
        "excluded_self_events": [{"tx_hash": "tx"}],
    }
    first["status"] = "PENDING_ORDERFILLED_EX_SELF"
    store.pair = first

    second = sync_calibration_probe_to_paired(calibration_probe(), store=store)

    assert second["status"] == "PENDING_ORDERFILLED_EX_SELF"
    assert second["orderfilled_ex_self"]["state"] == "SOURCE_LAG"
    assert second["orderfilled_ex_self"]["transaction_confirmation_complete"] is True
    assert second["orderfilled_ex_self"]["excluded_self_events"] == [
        {"tx_hash": "tx"}
    ]


def test_identity_mismatch_is_rejected_before_persistence() -> None:
    store = FakeStore(paired_probe())
    row = calibration_probe()
    row["asset_id"] = "wrong-asset"

    with pytest.raises(ValueError, match="identity mismatch"):
        sync_calibration_probe_to_paired(row, store=store)

    assert store.writes == []


def test_non_submitted_calibration_cannot_be_promoted() -> None:
    store = FakeStore(paired_probe())
    row = calibration_probe()
    row["exchange_submit_called"] = False

    with pytest.raises(ValueError, match="did not cross"):
        sync_calibration_probe_to_paired(row, store=store)

    assert store.writes == []


def test_terminal_http_rejection_without_calibratable_truth_is_excluded() -> None:
    store = FakeStore(paired_probe())
    row = calibration_probe()
    row["probe_state"] = "HTTP_REJECTED"
    row["reconciliation"] = {}
    row["lifecycle"] = {"state": "HTTP_REJECTED", "order_id": "0xOrder"}

    synced = sync_calibration_probe_to_paired(row, store=store)

    assert synced["live_lifecycle"]["terminal_status"] == "REJECTED"
    assert synced["status"] == "LIVE_NOT_CALIBRATABLE"
    assert synced["audit"]["external_live"]["probe_state"] == "HTTP_REJECTED"
