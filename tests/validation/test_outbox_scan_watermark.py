from quant.market.repository import (
    MarketRegistryRepository,
    RegistryOutboxPublishSummary,
)


class _Cursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...] | None]] = []
        self._fetch_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None) -> None:
        self.executions.append((sql, params))

    def fetchall(self):
        self._fetch_count += 1
        return []

    def fetchone(self):
        if self.executions and "pg_try_advisory_xact_lock" in self.executions[-1][0]:
            return {"acquired": True}
        return {"outbox_id": 123}


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()

    def cursor(self):
        return self.cursor_instance


def test_incremental_outbox_scan_uses_watermark_and_advances_to_table_max() -> None:
    connection = _Connection()
    repository = MarketRegistryRepository(connection)

    summary = repository.publish_pending_outbox(limit=10, after_outbox_id=42)

    lock_sql, lock_params = connection.cursor_instance.executions[0]
    assert "pg_try_advisory_xact_lock(914020250708)" in lock_sql
    assert lock_params is None
    scan_sql, scan_params = next(
        (sql, params)
        for sql, params in connection.cursor_instance.executions
        if "FROM quant.paper_registry_outbox o" in sql
    )
    assert "AND o.outbox_id > %s" in scan_sql
    assert scan_params == (42, 10)
    assert summary.scan_watermark == 123


def test_outbox_summary_exposes_scan_watermark() -> None:
    summary = RegistryOutboxPublishSummary(scan_watermark=456)

    assert summary.as_meta()["scan_watermark"] == 456
    assert summary.as_meta()["lock_deferred"] is False


def test_outbox_publish_defers_without_scanning_when_registry_writer_is_busy() -> None:
    connection = _Connection()

    def busy_fetchone():
        return {"acquired": False}

    connection.cursor_instance.fetchone = busy_fetchone
    summary = MarketRegistryRepository(connection).publish_pending_outbox(
        limit=10,
        after_outbox_id=42,
    )

    assert summary.lock_deferred is True
    assert len(connection.cursor_instance.executions) == 1
