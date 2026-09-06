from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant.paper import paired_probe
from quant.paper.paired_probe import (
    NO_SUBMIT,
    PROBE_BOOK_MAX_AGE_SECONDS,
    RECORD_ONLY,
    OrderFilledEvidenceClient,
    _depth_walk_limit_price,
    _select_candidate,
    attach_live_lifecycle,
    attach_orderfilled_evidence,
    build_ex_self_evidence,
    build_paired_probe_report,
    run_paired_probe,
)

NOW = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self) -> None:
        self.submissions = []
        self.persisted = []
        self.intent = None

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        self.intent = {
            "intent_id": 7,
            "status": "COMPLETED",
            "decision_ts": kwargs["decision_ts"],
            "result_audit_key": "audit-7",
            "result": {
                "status": "FILLED",
                "reason": "arrival_book_walk_complete",
                "arrival_ts": kwargs["decision_ts"] + timedelta(milliseconds=100),
                "decision_checkpoint_id": "decision-cp",
                "arrival_checkpoint_id": "arrival-cp",
                "coverage_grade": "A",
                "book_age_ms": 100,
                "filled_size": kwargs["size"],
                "remaining_size": "0",
                "avg_fill_price": kwargs["limit_price"],
                "total_fee": "0",
                "slippage": "0",
                "model_version": "paper_taker_l2_v1",
                "config_hash": "config-1",
            },
        }
        return 7

    def load_intent(self, intent_id):
        assert intent_id == 7
        return self.intent

    def load_book_checkpoint(self, checkpoint_id):
        assert checkpoint_id == "arrival-cp"
        return {
            "checkpoint_id": checkpoint_id,
            "bids": [["0.49", "25"]],
            "asks": [["0.50", "20"], ["0.51", "30"]],
            "source_connection_id": "paper-route-1",
        }

    def upsert_paired_probe(self, row):
        self.persisted.append(row)
        return row


class FakeEvidence:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def fetch_window(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.rows)


def candidate() -> dict:
    return {
        "asset_id": "asset-1",
        "market_id": "market-1",
        "condition_id": "condition-1",
        "market_slug": "demo-market",
        "outcome_name": "YES",
        "tick_size": "0.001",
        "min_order_size": "1",
        "best_bid": "0.49",
        "best_ask": "0.50",
        "coverage_grade": "A",
        "connection_id": "primary-connection",
        "shard_id": 3,
        "last_receive_ts": NOW,
        "redundant_feed_match": True,
        "raw_metadata": {"category": "sports", "endDate": "2026-07-21T08:00:00Z"},
    }


def test_candidate_selection_uses_the_worker_book_freshness_window(monkeypatch) -> None:
    calls = []

    def load(_store, **kwargs):
        calls.append(kwargs)
        return [candidate()]

    monkeypatch.setattr(paired_probe, "load_live_probe_candidates", load)

    assert _select_candidate(FakeStore(), 0.1)["asset_id"] == "asset-1"
    assert calls == [
        {"limit": 20, "max_age_seconds": PROBE_BOOK_MAX_AGE_SECONDS}
    ]


def test_depth_walk_limit_uses_the_last_required_ask_level() -> None:
    selected = {
        "best_bid": "0.020",
        "best_ask": "0.021",
        "asks": [["0.021", "25"], ["0.022", "100"]],
    }

    assert _depth_walk_limit_price(
        selected,
        side="BUY",
        amount=Decimal("1.10"),
        amount_unit="QUOTE",
    ) == Decimal("0.022")


def test_depth_walk_limit_uses_the_last_required_bid_level() -> None:
    selected = {
        "best_bid": "0.979",
        "best_ask": "0.980",
        "bids": [["0.979", "2"], ["0.978", "10"]],
    }

    assert _depth_walk_limit_price(
        selected,
        side="SELL",
        amount=Decimal(5),
        amount_unit="SHARES",
    ) == Decimal("0.978")


def test_no_submit_probe_runs_a_without_an_exchange_transport() -> None:
    store = FakeStore()
    evidence = FakeEvidence()

    probe = run_paired_probe(
        store=store,
        mode=NO_SUBMIT,
        candidate=candidate(),
        evidence_client=evidence,
        now=NOW,
    )

    assert len(store.submissions) == 1
    assert store.submissions[0]["time_in_force"] == "FAK"
    assert probe["status"] == "SHADOW_READY"
    assert probe["paper_prediction"]["status"] == "FILLED"
    assert probe["live_lifecycle"]["state"] == "NOT_SUBMITTED"
    assert probe["audit"]["exchange_submit_called"] is False
    assert probe["audit"]["order_submitter_present"] is False
    assert probe["bucket_context"]["coverage_grade"] == "A"
    assert probe["bucket_context"]["visible_opposite_depth"] == Decimal(20)
    assert probe["bucket_context"]["order_size_depth_ratio"] == Decimal("0.05")
    assert evidence.calls[0]["asset_id"] == "asset-1"


def test_record_only_probe_waits_for_external_lifecycle() -> None:
    probe = run_paired_probe(
        store=FakeStore(),
        mode=RECORD_ONLY,
        candidate=candidate(),
        evidence_client=None,
        now=NOW,
    )

    assert probe["status"] == "PENDING_LIVE"
    assert probe["live_lifecycle"]["state"] == "AWAITING_IMPORT"
    assert probe["live_lifecycle"]["network_submit_called_by_probe"] is False


def test_sell_probe_seeds_the_real_cost_basis_before_prediction(monkeypatch) -> None:
    seeded = {}

    class CaptureLedger:
        def __init__(self, connection_factory, *, ensure_schema):
            assert connection_factory == "paper-db"
            assert ensure_schema is False

        def seed_calibration_position(self, **kwargs):
            seeded.update(kwargs)

    monkeypatch.setattr(paired_probe, "PostgresPaperLedgerSink", CaptureLedger)
    store = FakeStore()
    store.connection_factory = "paper-db"

    probe = run_paired_probe(
        store=store,
        mode=RECORD_ONLY,
        candidate=candidate(),
        side="SELL",
        amount="2",
        amount_unit="SHARES",
        paper_position_seed="3.125",
        paper_position_cost_basis="1.00000",
        evidence_client=None,
        now=NOW,
    )

    assert seeded["quantity"] == Decimal("3.125")
    assert seeded["cost_basis"] == Decimal("1.00000")
    assert probe["audit"]["paper_position_seed"] == "3.125"
    assert probe["audit"]["paper_position_cost_basis"] == "1.00000"


def test_live_lifecycle_and_orderfilled_ex_self_share_one_probe_id() -> None:
    probe = run_paired_probe(
        store=FakeStore(),
        mode=RECORD_ONLY,
        candidate=candidate(),
        evidence_client=None,
        now=NOW,
    )
    live = attach_live_lifecycle(
        probe,
        [
            {
                "client_order_id": probe["client_order_id"],
                "order_hash": "0xownorder",
                "asset_id": "asset-1",
                "status": "FILLED",
                "timestamp": "2026-07-20T08:00:00.250Z",
                "filled_size": "1",
                "avg_fill_price": "0.501",
            }
        ],
    )

    assert live["live_lifecycle"]["state"] == "TERMINAL"
    assert live["live_lifecycle"]["terminal_status"] == "FILLED"
    assert live["status"] == "PENDING_ORDERFILLED_EX_SELF"

    reconciled = attach_orderfilled_evidence(
        live,
        [
            {
                "tx_hash": "0xtx-self",
                "log_index": 1,
                "order_hash": "0xownorder",
                "maker": "0xmine",
                "taker": "0xother",
                "asset_id": "asset-1",
                "price": "0.501",
                "size": "1",
            },
            {
                "tx_hash": "0xtx-external",
                "log_index": 2,
                "order_hash": "0xexternal",
                "maker": "0xexternal-maker",
                "taker": "0xexternal-taker",
                "asset_id": "asset-1",
                "price": "0.502",
                "size": "2",
            },
        ],
        own_addresses=["0xMine"],
        window_end=NOW + timedelta(seconds=30),
        source_watermark=NOW + timedelta(minutes=1),
    )

    assert reconciled["probe_id"] == probe["probe_id"]
    assert reconciled["orderfilled_ex_self"]["excluded_self_count"] == 1
    assert reconciled["orderfilled_ex_self"]["ex_self_count"] == 1
    assert reconciled["orderfilled_ex_self"]["used_for_actual_live_outcome"] is False
    assert reconciled["status"] == "CALIBRATION_READY"


def test_lifecycle_correlation_mismatch_is_invalid() -> None:
    probe = run_paired_probe(
        store=FakeStore(),
        mode=RECORD_ONLY,
        candidate=candidate(),
        evidence_client=None,
        now=NOW,
    )

    invalid = attach_live_lifecycle(
        probe,
        [{"client_order_id": "wrong", "asset_id": "asset-1", "status": "FILLED"}],
    )

    assert invalid["status"] == "INVALID"
    assert invalid["live_lifecycle"]["state"] == "INVALID"
    assert "event_correlation_mismatch" in invalid["live_lifecycle"]["errors"]


def test_orderfilled_filter_deduplicates_and_requires_self_identity_for_live() -> None:
    rows = [
        {"tx_hash": "0x1", "log_index": 1, "order_hash": "0xa", "maker": "0xme", "size": "3"},
        {"tx_hash": "0x1", "log_index": 1, "order_hash": "0xa", "maker": "0xme", "size": "3"},
    ]
    unverified = build_ex_self_evidence(rows, mode=RECORD_ONLY)
    verified = build_ex_self_evidence(
        rows,
        mode=RECORD_ONLY,
        own_addresses=["0xME"],
        window_end=NOW + timedelta(seconds=30),
        source_watermark=NOW + timedelta(minutes=1),
    )

    assert unverified["state"] == "UNVERIFIED_SELF_IDENTITY"
    assert unverified["duplicate_count"] == 1
    assert verified["state"] == "VERIFIED"
    assert verified["excluded_self_count"] == 1
    assert verified["ex_self_count"] == 0


def test_orderfilled_filter_normalizes_optional_hex_prefixes() -> None:
    tx_hash = "ab" * 32
    evidence = build_ex_self_evidence(
        [{
            "tx_hash": tx_hash,
            "log_index": 1,
            "order_hash": "deadbeef",
            "maker": "1234",
            "size": "1",
        }],
        mode=RECORD_ONLY,
        own_addresses=["0x1234"],
        own_order_hashes=["0xdeadbeef"],
        window_end=NOW + timedelta(seconds=30),
        source_watermark=NOW + timedelta(minutes=1),
        expected_transaction_hashes=["0x" + tx_hash],
    )

    assert evidence["state"] == "VERIFIED"
    assert evidence["excluded_self_count"] == 1
    assert evidence["ex_self_count"] == 0
    assert evidence["transaction_confirmation_complete"] is True
    assert evidence["missing_transaction_hashes"] == []


def test_orderfilled_filter_stays_pending_until_timestamp_source_catches_up() -> None:
    evidence = build_ex_self_evidence(
        [],
        mode=RECORD_ONLY,
        own_addresses=["0xmine"],
        window_end=NOW + timedelta(minutes=5),
        source_watermark=NOW + timedelta(minutes=4),
    )

    assert evidence["state"] == "SOURCE_LAG"
    assert evidence["source_coverage_complete"] is False


def test_report_never_promotes_no_submit_to_calibration_pass() -> None:
    shadow = run_paired_probe(
        store=FakeStore(),
        mode=NO_SUBMIT,
        candidate=candidate(),
        evidence_client=None,
        now=NOW,
    )
    shadow_report = build_paired_probe_report([shadow])

    assert shadow_report["status"] == "SHADOW_READY"
    assert shadow_report["calibratable_count"] == 0
    assert shadow_report["safety"]["exchange_submit_calls_by_this_module"] == 0


def test_report_exposes_pending_delayed_evidence_without_hiding_ready_metrics() -> None:
    ready = run_paired_probe(
        store=FakeStore(),
        mode=RECORD_ONLY,
        candidate=candidate(),
        evidence_client=None,
        now=NOW,
    )
    ready = attach_live_lifecycle(
        ready,
        [{
            "client_order_id": ready["client_order_id"],
            "asset_id": "asset-1",
            "status": "FILLED",
            "timestamp": "2026-07-20T08:00:00.250Z",
            "filled_size": "1",
            "avg_fill_price": "0.50",
        }],
    )
    pending = dict(ready)
    ready = attach_orderfilled_evidence(
        ready,
        [],
        own_addresses=["0xmine"],
        window_end=NOW + timedelta(seconds=30),
        source_watermark=NOW + timedelta(minutes=1),
    )

    report = build_paired_probe_report([ready, pending])

    assert report["status"] == "PASS_WITH_PENDING"
    assert report["calibratable_count"] == 1
    assert report["pending_count"] == 1


def test_calibration_report_contains_all_required_buckets() -> None:
    probe = run_paired_probe(
        store=FakeStore(),
        mode=RECORD_ONLY,
        candidate=candidate(),
        evidence_client=None,
        now=NOW,
    )
    live = attach_live_lifecycle(
        probe,
        [{
            "client_order_id": probe["client_order_id"],
            "order_hash": "0xownorder",
            "asset_id": "asset-1",
            "status": "FILLED",
            "timestamp": "2026-07-20T08:00:00.250Z",
            "filled_size": "1",
            "avg_fill_price": "0.501",
        }],
    )
    ready = attach_orderfilled_evidence(
        live,
        [],
        own_addresses=["0xmine"],
        window_end=NOW + timedelta(seconds=30),
        source_watermark=NOW + timedelta(minutes=1),
    )
    report = build_paired_probe_report([ready])

    assert report["status"] == "PASS"
    assert report["metrics"]["classification_accuracy_pct"] == "100.00"
    assert report["metrics"]["avg_price_error_ticks"] == "1.0000"
    assert set(report["buckets"]) == {
        "category", "source_category", "outcome_name", "price_bucket", "spread_bucket", "depth_bucket",
        "activity_bucket", "time_to_resolution_bucket", "coverage_grade",
        "connection", "shard", "size_depth_ratio_bucket",
    }
    assert report["buckets"]["category"]["sports"]["sample_count"] == 1


class CaptureClickHouse:
    class Settings:
        orderfilled_table = "orderfilled_fact"

    settings = Settings()

    def __init__(self) -> None:
        self.sql = ""
        self.timeout = None

    def query_json_rows(self, sql, *, timeout_seconds=None):
        self.sql = sql
        self.timeout = timeout_seconds
        return []


class WatermarkedClickHouse(CaptureClickHouse):
    def query_json_rows(self, sql, *, timeout_seconds=None):
        self.sql = sql
        self.timeout = timeout_seconds
        if "FROM block_timestamps" in sql and "ORDER BY block_number DESC" in sql:
            return [{"block_number": "123", "block_time": "2026-07-20T08:01:00Z"}]
        return []


class TransactionClickHouse(CaptureClickHouse):
    def query_json_rows(self, sql, *, timeout_seconds=None):
        self.sql = sql
        self.timeout = timeout_seconds
        return [{
            "tx_hash": "ab" * 32,
            "log_index": 1,
            "order_hash": "cd" * 32,
            "maker": "12" * 20,
        }]


class ActiveAssetClickHouse(CaptureClickHouse):
    def query_json_rows(self, sql, *, timeout_seconds=None):
        self.sql = sql
        self.timeout = timeout_seconds
        return [
            {
                "asset_key": format(12345, "064x"),
                "trade_count": "7",
                "trade_volume": "42.5",
            }
        ]


def test_orderfilled_adapter_uses_bounded_token_time_query() -> None:
    client = CaptureClickHouse()
    rows = OrderFilledEvidenceClient(client).fetch_window(
        asset_id="asset-1",
        start=NOW,
        end=NOW + timedelta(seconds=30),
    )

    assert rows == []
    assert "PREWHERE f.token_id='asset-1'" in client.sql
    assert "block_timestamps" in client.sql
    assert "2026-07-20 08:00:00.000" in client.sql
    assert "LIMIT 2000" in client.sql
    assert client.timeout == 60


def test_orderfilled_adapter_exposes_timestamp_join_watermark() -> None:
    result = OrderFilledEvidenceClient(WatermarkedClickHouse()).coverage_watermark()

    assert result["block_number"] == 123
    assert result["block_time"] == NOW + timedelta(minutes=1)


def test_orderfilled_adapter_fetches_prior_activity_for_pit_selection() -> None:
    client = ActiveAssetClickHouse()
    result = OrderFilledEvidenceClient(client).fetch_active_assets(
        start=NOW - timedelta(hours=1),
        end=NOW,
    )

    assert result == {
        "12345": {"trade_count": 7, "trade_volume": Decimal("42.5")}
    }
    assert "GROUP BY f.token_id" in client.sql
    assert "ORDER BY trade_count DESC" in client.sql
    assert "2026-07-20 07:00:00.000" in client.sql
    assert "2026-07-20 08:00:00.000" in client.sql


def test_orderfilled_adapter_fetches_known_transaction_without_time_join() -> None:
    client = TransactionClickHouse()
    rows = OrderFilledEvidenceClient(client).fetch_transactions(["0x" + "ab" * 32])

    assert len(rows) == 1
    assert "PREWHERE f.tx_hash IN ('" + "ab" * 32 + "')" in client.sql
    assert "block_timestamps" not in client.sql


def test_orderfilled_adapter_summarizes_compatible_maker_volume() -> None:
    client = CaptureClickHouse()
    result = OrderFilledEvidenceClient(client).summarize_compatible_maker_volume(
        asset_id="asset-1",
        maker_side="BUY",
        limit_price=Decimal("0.40"),
        start=NOW,
        end=NOW + timedelta(minutes=15),
    )

    assert result["compatible_trade_volume"] == "0"
    assert "f.side_code=2" in client.sql
    assert "f.price <= toDecimal64('0.40', 10)" in client.sql
    assert "LIMIT" not in client.sql


def test_orderfilled_adapter_normalizes_decimal_clob_asset_id() -> None:
    client = CaptureClickHouse()
    OrderFilledEvidenceClient(client).fetch_window(
        asset_id="12345",
        start=NOW,
        end=NOW + timedelta(seconds=30),
    )

    assert "PREWHERE f.token_id='" + format(12345, "064x") + "'" in client.sql
