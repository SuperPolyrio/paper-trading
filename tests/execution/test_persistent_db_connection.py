from types import SimpleNamespace

import pytest

from quant.core import db


class _Cursor:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql) -> None:
        self.connection.statements.append(str(sql))


class _Connection:
    def __init__(self) -> None:
        self.closed = False
        self.broken = False
        self.commits = 0
        self.rollbacks = 0
        self.statements: list[str] = []

    def cursor(self):
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _settings() -> db.PostgresSettings:
    return db.PostgresSettings(
        host="localhost",
        port=5432,
        user="user",
        password="password",
        database="database",
        search_path="",
    )


def test_thread_local_factory_reuses_connection_and_closes_read_transaction(
    monkeypatch,
) -> None:
    connections: list[_Connection] = []

    def connect(**_kwargs):
        connection = _Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(db, "psycopg", SimpleNamespace(connect=connect))
    factory = db.ThreadLocalPostgresConnectionFactory(_settings())

    with factory(readonly=True):
        pass
    with factory(readonly=False):
        pass

    assert len(connections) == 1
    assert connections[0].commits == 2  # session setup plus the write transaction
    assert connections[0].rollbacks == 1
    assert "SET TRANSACTION READ ONLY" in connections[0].statements


def test_thread_local_factory_discards_broken_connection(monkeypatch) -> None:
    connections: list[_Connection] = []

    def connect(**_kwargs):
        connection = _Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(db, "psycopg", SimpleNamespace(connect=connect))
    factory = db.ThreadLocalPostgresConnectionFactory(_settings())

    with pytest.raises(RuntimeError, match="tunnel dropped"):
        with factory(readonly=False) as connection:
            connection.broken = True
            raise RuntimeError("tunnel dropped")

    with factory(readonly=False):
        pass

    assert len(connections) == 2
    assert connections[0].closed is True
