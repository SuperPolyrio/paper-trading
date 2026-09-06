import asyncio

from quant.paper.live_shadow_service import LivePaperShadowService, LiveShadowStats


def _service(load_watch_targets):
    service = object.__new__(LivePaperShadowService)
    service.store = type("Store", (), {"load_watch_targets": load_watch_targets})()
    service.max_watch_assets = 10
    service.watch_refresh_seconds = 30
    service.seed_watchlist_limit = 0
    service._last_watch_refresh = 0.0
    service._last_seed_reconcile = 0.0
    service._seed_reconcile_task = None
    service._watch_refresh_task = None
    service.stats = LiveShadowStats(worker_id="test")
    return service


def test_periodic_watch_refresh_is_single_flight_and_nonblocking() -> None:
    release = asyncio.Event()
    calls = 0

    async def scenario() -> None:
        nonlocal calls
        service = _service(lambda **_kwargs: [])

        async def slow_db_call(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            await release.wait()
            return []

        service._db_call = slow_db_call
        await service._refresh_watchlist(force=False)
        first_task = service._watch_refresh_task
        assert first_task is not None
        await service._refresh_watchlist(force=False)
        assert service._watch_refresh_task is first_task
        assert calls == 0
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        await first_task

    asyncio.run(scenario())


def test_watch_refresh_timeout_keeps_global_health_clean() -> None:
    async def scenario() -> None:
        service = _service(lambda **_kwargs: [])

        async def failed_db_call(*_args, **_kwargs):
            raise TimeoutError("registry busy")

        service._db_call = failed_db_call
        await service._refresh_watchlist(force=False)
        await asyncio.sleep(0)
        await service._refresh_watchlist(force=False)

        assert service.stats.last_error is None
        assert service.stats.watch_refresh_failures == 1
        assert service.stats.watch_refresh_consecutive_failures == 1
        assert service.stats.last_watch_refresh_error == "TimeoutError: registry busy"

    asyncio.run(scenario())
