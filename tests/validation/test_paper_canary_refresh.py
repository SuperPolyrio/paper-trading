from quant.paper import canary


class _Store:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def request_book_refresh(self, asset_id: str) -> bool:
        self.requested.append(asset_id)
        return True

    def request_book_refresh_batch(self, asset_ids: list[str]) -> list[str]:
        self.requested.extend(asset_ids)
        return asset_ids


def test_wait_candidates_refreshes_stale_eligible_book_once(monkeypatch) -> None:
    fresh_calls = 0

    def fake_candidates(
        *,
        limit: int,
        max_age_seconds: float | None,
        paper_account_id: str,
    ):
        nonlocal fresh_calls
        assert limit == 2
        assert paper_account_id == "paper-account"
        if max_age_seconds is None:
            return [{"asset_id": "asset-1"}]
        fresh_calls += 1
        return [] if fresh_calls == 1 else [{"asset_id": "asset-1", "best_ask": "0.6"}]

    monkeypatch.setattr(canary, "_candidates", fake_candidates)
    monkeypatch.setattr(canary.time, "sleep", lambda _seconds: None)
    store = _Store()
    requested: list[str] = []

    candidates = canary._wait_candidates(
        limit=2,
        wait_seconds=1,
        store=store,
        refresh_requested_assets=requested,
    )

    assert candidates == [{"asset_id": "asset-1", "best_ask": "0.6"}]
    assert store.requested == ["asset-1"]
    assert requested == ["asset-1"]


def test_canary_candidate_query_uses_execution_catalog(monkeypatch) -> None:
    captured = {}

    class Cursor:
        def execute(self, query, parameters):
            captured["query"] = query
            captured["parameters"] = parameters

        def fetchall(self):
            return []

    class Connection:
        def cursor(self):
            return CursorContext()

    class CursorContext:
        def __enter__(self):
            return Cursor()

        def __exit__(self, *_args):
            return False

    class ConnectionContext:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(canary, "postgres_connection", lambda **_kwargs: ConnectionContext())

    assert canary._candidates(limit=12) == []
    assert "JOIN quant.paper_execution_market_catalog r" in captured["query"]
    assert "paper_market_registry_tokens" not in captured["query"]
    assert "quant.simulator_oms_orders" in captured["query"]
    assert "paper-account" in captured["parameters"]


def test_canary_submits_through_the_control_plane_fence() -> None:
    factory = canary._ingress_connection_factory()

    assert factory.connection_factory is canary.postgres_connection
