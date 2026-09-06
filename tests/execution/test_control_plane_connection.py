from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quant.paper.authority import (
    AuthorityLeaseHandle,
    AuthorityLeaseToken,
    ControlPlanePostgresConnectionFactory,
    FencedPostgresConnectionFactory,
)


class _Cursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.connection.statements.append((sql, params))


class _Connection:
    def __init__(self):
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.info = SimpleNamespace(backend_pid=123)

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _factory(connection):
    @contextmanager
    def factory(*, readonly=False):
        yield connection

    return factory


def test_control_plane_marker_survives_inner_commits_and_is_reset() -> None:
    connection = _Connection()
    factory = ControlPlanePostgresConnectionFactory(_factory(connection))

    with factory(readonly=False) as active:
        active.commit()

    assert "'on', FALSE" in connection.statements[0][0]
    assert "'off', FALSE" in connection.statements[-1][0]
    assert connection.commits == 4
    assert connection.rollbacks == 0


def test_control_plane_marker_is_reset_after_failure() -> None:
    connection = _Connection()
    factory = ControlPlanePostgresConnectionFactory(_factory(connection))

    with pytest.raises(RuntimeError, match="boom"):
        with factory(readonly=False):
            raise RuntimeError("boom")

    assert "'off', FALSE" in connection.statements[-1][0]
    assert connection.rollbacks == 1


def test_readonly_control_plane_keeps_original_transaction_unchanged() -> None:
    connection = _Connection()
    factory = ControlPlanePostgresConnectionFactory(_factory(connection))

    with factory(readonly=True):
        pass

    assert connection.statements == []
    assert connection.commits == 0


def _token(epoch: int = 1) -> AuthorityLeaseToken:
    now = datetime.now(timezone.utc)
    return AuthorityLeaseToken(
        partition_key="paper-global",
        owner_instance_id="worker-1",
        lease_epoch=epoch,
        lease_until=now,
        heartbeat_at=now,
        acquired_at=now,
        released_at=None,
        fencing_enforced=True,
    )


def test_fenced_marker_is_session_cached_and_refreshed_by_epoch() -> None:
    connection = _Connection()
    handle = AuthorityLeaseHandle(_token())
    factory = FencedPostgresConnectionFactory(_factory(connection), handle)

    with factory(readonly=False):
        pass
    with factory(readonly=False):
        pass

    assert len(connection.statements) == 1
    assert connection.statements[0][1] == ("paper-global", "worker-1", "1")
    handle.replace(_token(epoch=2))
    with factory(readonly=False):
        pass
    assert len(connection.statements) == 2
    assert connection.statements[-1][1] == ("paper-global", "worker-1", "2")


def test_fenced_marker_is_cleared_after_authority_loss() -> None:
    connection = _Connection()
    handle = AuthorityLeaseHandle(_token())
    factory = FencedPostgresConnectionFactory(_factory(connection), handle)

    with factory(readonly=False):
        pass
    handle.clear()
    with factory(readonly=False):
        pass

    assert connection.statements[-1][1] == ("", "", "")


def test_readonly_fenced_connection_does_not_mutate_session() -> None:
    connection = _Connection()
    factory = FencedPostgresConnectionFactory(
        _factory(connection), AuthorityLeaseHandle(_token())
    )

    with factory(readonly=True):
        pass

    assert connection.statements == []
    assert connection.commits == 0
