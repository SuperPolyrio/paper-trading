from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

from quant.paper.live_shadow_store import LiveShadowStore


class _Cursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


def test_maker_trade_event_insert_is_idempotent():
    cursor = _Cursor(rowcount=1)
    connection = _Connection(cursor)

    @contextmanager
    def connection_factory(*, readonly: bool):
        assert readonly is False
        yield connection

    inserted = LiveShadowStore(connection_factory).persist_maker_trade_event(
        event_id="maker-trade:asset-1:hash",
        worker_id="worker-a",
        asset_id="asset-1",
        price=Decimal("0.42"),
        size=Decimal("3"),
        aggressor_side="SELL",
        event_ts=datetime(2026, 8, 16, tzinfo=timezone.utc),
        transaction_hash="0xtx",
    )

    sql, params = cursor.calls[0]
    assert "ON CONFLICT (event_id) DO NOTHING" in sql
    assert params[0:3] == (
        "maker-trade:asset-1:hash",
        "worker-a",
        "asset-1",
    )
    assert inserted is True
    assert connection.commits == 1


def test_maker_trade_event_retention_is_bounded_in_batches():
    cursor = _Cursor(rowcount=17)
    connection = _Connection(cursor)

    @contextmanager
    def connection_factory(*, readonly: bool):
        assert readonly is False
        yield connection

    cutoff = datetime(2026, 8, 9, tzinfo=timezone.utc)
    deleted = LiveShadowStore(connection_factory).prune_maker_trade_events(
        before=cutoff,
        limit=500,
    )

    sql, params = cursor.calls[0]
    assert "WHERE event_ts < %s" in sql
    assert "LIMIT %s" in sql
    assert params == (cutoff, 500)
    assert deleted == 17
    assert connection.commits == 1
