from contextlib import contextmanager

from quant.paper.live_shadow_store import LiveShadowStore
from quant.paper.paper_ledger import PostgresPaperLedgerSink


class _Cursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = 0
        self._result: list[dict[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql: str, params=()) -> None:
        self.calls.append((sql, tuple(params)))
        if "pg_try_advisory_xact_lock" in sql:
            self._result = [{"acquired": True}]
        elif "SELECT w.asset_id" in sql:
            self._result = []
        elif "SELECT r.asset_id" in sql:
            self._result = [{"asset_id": "new-live-token"}]
        elif (
            "UPDATE quant.paper_execution_market_catalog" in sql
            and "_official_probe_gate" in sql
        ):
            self._result = [
                {
                    "asset_id": "new-live-token",
                    "condition_id": "condition-1",
                    "market_state": "LIVE",
                    "execution_eligible": True,
                    "current_tick_size": "0.01",
                    "min_order_size": "5",
                    "source_updated_at": "now",
                }
            ]
        elif (
            "INSERT INTO quant.paper_live_watchlist" in sql
            or "UPDATE quant.paper_live_watchlist" in sql
        ):
            self.rowcount = 1
            self._result = []

    def fetchone(self):
        return self._result[0]

    def fetchall(self):
        return list(self._result)


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_seed_watchlist_rotates_out_non_live_seed_assets() -> None:
    cursor = _Cursor()
    connection = _Connection(cursor)

    @contextmanager
    def connection_factory(*, readonly: bool):
        assert readonly is False
        yield connection

    store = LiveShadowStore(connection_factory=connection_factory)

    assert store.seed_watchlist(limit=1) == 2
    existing_seed_sql = next(
        sql for sql, _params in cursor.calls if "SELECT w.asset_id" in sql
    )
    assert "r.market_state='LIVE'" in existing_seed_sql
    assert "r.execution_eligible=TRUE" in existing_seed_sql
    insert_call = next(
        params
        for sql, params in cursor.calls
        if "INSERT INTO quant.paper_live_watchlist" in sql
    )
    insert_sql = next(
        sql
        for sql, _params in cursor.calls
        if "INSERT INTO quant.paper_live_watchlist" in sql
    )
    assert insert_call[1] == ["new-live-token"]
    assert "quant.paper_live_watchlist.enabled=FALSE" in insert_sql
    assert connection.commits == 1


def test_load_watch_targets_excludes_non_live_history_without_exposure() -> None:
    cursor = _Cursor()
    connection = _Connection(cursor)

    @contextmanager
    def connection_factory(*, readonly: bool):
        assert readonly is True
        yield connection

    store = LiveShadowStore(connection_factory=connection_factory)

    assert store.load_watch_targets(limit=32) == []
    target_sql = next(
        sql for sql, _params in cursor.calls if "SELECT w.asset_id" in sql
    )
    assert "r.market_state='LIVE'" in target_sql
    assert "r.execution_eligible, FALSE)=TRUE" in target_sql
    assert "'active_intent', 'open_position'" in target_sql
    assert "'live_probe_candidate'" in target_sql
    assert "WHEN w.reason='intent_asset' THEN 0" not in target_sql
    assert "refresh.refresh_requested_at::text AS refresh_nonce" in target_sql


def test_live_probe_pin_preserves_owner_but_updates_watch_reason() -> None:
    cursor = _Cursor()
    connection = _Connection(cursor)

    @contextmanager
    def connection_factory(*, readonly: bool):
        assert readonly is False
        yield connection

    store = LiveShadowStore(connection_factory=connection_factory)

    assert store.ensure_calibration_watch_batch(
        ["new-live-token"],
        strategy_id="taker-calibration-live",
        reason="live_probe_candidate",
    ) == 1
    sql = next(
        sql
        for sql, _params in cursor.calls
        if "INSERT INTO quant.paper_live_watchlist" in sql
    )
    assert "EXCLUDED.reason='live_probe_candidate'" in sql
    assert "strategy_id=CASE" in sql


def test_official_probe_snapshot_refreshes_only_exact_catalog_gate() -> None:
    cursor = _Cursor()
    connection = _Connection(cursor)

    @contextmanager
    def connection_factory(*, readonly: bool):
        assert readonly is False
        yield connection

    store = LiveShadowStore(connection_factory=connection_factory)
    result = store.refresh_official_probe_market_gate(
        asset_id="new-live-token",
        condition_id="condition-1",
        snapshot={
            "market_state": "LIVE",
            "execution_eligible": True,
            "tick_size": "0.01",
            "min_order_size": "5",
            "end_date": "2027-01-01T00:00:00+00:00",
            "observed_at": "2026-09-01T16:00:00+00:00",
        },
    )

    sql, params = next(
        (sql, params)
        for sql, params in cursor.calls
        if "UPDATE quant.paper_execution_market_catalog" in sql
    )
    assert "WHERE asset_id=%s AND condition_id=%s" in sql
    assert params[-2:] == ("new-live-token", "condition-1")
    assert result["execution_eligible"] is True
    assert len(result["evidence"]["payload_hash"]) == 64


def test_synchronize_calibration_watch_batch_retires_only_owned_candidates() -> None:
    cursor = _Cursor()
    connection = _Connection(cursor)

    @contextmanager
    def connection_factory(*, readonly: bool):
        assert readonly is False
        yield connection

    store = LiveShadowStore(connection_factory=connection_factory)

    result = store.synchronize_calibration_watch_batch(
        ["new-live-token"],
        strategy_id="maker-calibration",
        reason="maker_calibration_candidate",
    )

    assert result == {"upserted": 1, "disabled": 1}
    retire_sql, retire_params = next(
        (sql, params)
        for sql, params in cursor.calls
        if "UPDATE quant.paper_live_watchlist w" in sql
    )
    assert "w.strategy_id LIKE %s" in retire_sql
    assert "w.reason=%s" in retire_sql
    assert "paper_live_order_intents" in retire_sql
    assert "paper_positions" in retire_sql
    assert "paper_calibration_pnl_positions" in retire_sql
    assert retire_params == (
        "maker-calibration%",
        "maker_calibration_candidate",
        ["new-live-token"],
    )
    assert connection.commits == 1


def test_ledger_can_start_without_repeating_schema_initialization() -> None:
    def unexpected_connection_factory(*, readonly: bool):
        raise AssertionError(f"unexpected database connection readonly={readonly}")

    sink = PostgresPaperLedgerSink(
        connection_factory=unexpected_connection_factory,
        ensure_schema=False,
    )

    assert sink.initial_cash > 0
