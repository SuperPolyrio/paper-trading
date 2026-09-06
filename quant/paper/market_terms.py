"""Authoritative, cached Polymarket market terms for paper execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from quant.adapters.polymarket_clob_client import PolymarketClobClient
from quant.simulator.economics import FeeSchedule, fee_schedule_id

from .taker_execution import OrderIntent


class MarketTermsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperMarketTerms:
    asset_id: str
    condition_id: str
    fee_rate_bps: int
    fee_rate: Decimal
    fee_exponent: Decimal
    fee_taker_only: bool
    itode: bool
    seconds_delay: int
    taker_delay_ms: int
    delay_source: str
    source: str
    observed_at: datetime
    expires_at: datetime

    @property
    def schedule_id(self) -> str:
        return fee_schedule_id(
            asset_id=self.asset_id,
            condition_id=self.condition_id,
            effective_from=self.observed_at,
            platform_fee_rate=self.fee_rate,
            platform_fee_exponent=self.fee_exponent,
            platform_taker_only=self.fee_taker_only,
            source=self.source,
        )

    def fee_schedule(
        self,
        *,
        builder_code: str | None = None,
        builder_taker_fee_bps: int = 0,
        builder_maker_fee_bps: int = 0,
    ) -> FeeSchedule:
        return FeeSchedule(
            schedule_id=self.schedule_id,
            asset_id=self.asset_id,
            condition_id=self.condition_id,
            effective_from=self.observed_at,
            effective_until=self.expires_at,
            platform_fee_rate=self.fee_rate,
            platform_fee_exponent=self.fee_exponent,
            platform_taker_only=self.fee_taker_only,
            builder_code=builder_code,
            builder_taker_fee_bps=builder_taker_fee_bps,
            builder_maker_fee_bps=builder_maker_fee_bps,
            economics_regime_id=self.schedule_id,
            source=self.source,
        )

    def apply(self, intent: OrderIntent) -> OrderIntent:
        return replace(
            intent,
            fee_rate=self.fee_rate,
            fee_exponent=self.fee_exponent,
            fee_taker_only=self.fee_taker_only,
            venue_taker_delay_ms=self.taker_delay_ms,
            venue_delay_source=self.delay_source,
            venue_itode=self.itode,
            venue_seconds_delay=self.seconds_delay,
            fee_schedule_id=self.schedule_id,
            fee_schedule_source=self.source,
            economics_regime_id=self.schedule_id,
        )


class MarketTermsRepository(Protocol):
    def load_market_terms(
        self,
        asset_id: str,
        *,
        now: datetime,
    ) -> PaperMarketTerms | None: ...

    def upsert_market_terms(self, terms: PaperMarketTerms) -> None: ...


class CachedPolymarketTermsResolver:
    def __init__(
        self,
        client: PolymarketClobClient,
        repository: MarketTermsRepository,
        *,
        ttl_seconds: int = 300,
    ) -> None:
        self.client = client
        self.repository = repository
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._memory: dict[str, PaperMarketTerms] = {}
        self._inflight: dict[str, asyncio.Task[PaperMarketTerms]] = {}
        self._db_call: Callable[..., Awaitable[Any]] | None = None

    def set_db_call(self, db_call: Callable[..., Awaitable[Any]]) -> None:
        self._db_call = db_call

    async def close(self) -> None:
        tasks = list(self._inflight.values())
        self._inflight.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_db(self, func: Any, /, *args: Any, **kwargs: Any) -> Any:
        if self._db_call is not None:
            return await self._db_call(func, *args, **kwargs)
        return await asyncio.to_thread(func, *args, **kwargs)

    async def resolve(self, intent: OrderIntent) -> PaperMarketTerms:
        return await self.resolve_asset(
            asset_id=intent.asset_id,
            condition_id=intent.condition_id,
        )

    def needs_refresh(
        self,
        asset_id: str,
        *,
        now: datetime | None = None,
        refresh_margin_seconds: int = 60,
    ) -> bool:
        observed_now = now or datetime.now(timezone.utc)
        cached = self._memory.get(str(asset_id))
        return cached is None or cached.expires_at <= observed_now + timedelta(
            seconds=max(0, int(refresh_margin_seconds))
        )

    async def resolve_asset(
        self,
        *,
        asset_id: str,
        condition_id: str,
    ) -> PaperMarketTerms:
        normalized_asset_id = str(asset_id)
        now = datetime.now(timezone.utc)
        cached = self._memory.get(normalized_asset_id)
        if cached is not None and cached.expires_at > now:
            return cached
        inflight = self._inflight.get(normalized_asset_id)
        if inflight is not None:
            return await asyncio.shield(inflight)
        task = asyncio.create_task(
            self._resolve_uncached(
                asset_id=normalized_asset_id,
                condition_id=str(condition_id),
                now=now,
            ),
            name=f"paper-market-terms:{normalized_asset_id[:16]}",
        )
        self._inflight[normalized_asset_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._inflight.get(normalized_asset_id) is task and task.done():
                self._inflight.pop(normalized_asset_id, None)

    async def _resolve_uncached(
        self,
        *,
        asset_id: str,
        condition_id: str,
        now: datetime,
    ) -> PaperMarketTerms:
        persisted = await self._run_db(
            self.repository.load_market_terms,
            asset_id,
            now=now,
        )
        if persisted is not None:
            # Also materialize the immutable schedule row when upgrading a DB
            # that previously stored only the mutable current terms record.
            await self._run_db(self.repository.upsert_market_terms, persisted)
            self._memory[asset_id] = persisted
            return persisted

        fee_rate_bps, clob_market, market = await asyncio.gather(
            self.client.get_fee_rate(asset_id),
            self.client.get_clob_market_info(condition_id),
            self.client.get_market(condition_id),
        )
        if fee_rate_bps < 0:
            raise MarketTermsUnavailable("negative CLOB fee rate")
        itode = _boolean(clob_market.get("itode"), field_name="itode")
        seconds_delay = _nonnegative_int(market.get("seconds_delay"))
        taker_delay_ms = max(250 if itode else 0, seconds_delay * 1000)
        delay_source = (
            "clob_market_seconds_delay"
            if seconds_delay > 0
            else "clob_market_itode"
            if itode
            else "clob_market_no_delay"
        )
        if fee_rate_bps == 0:
            terms = PaperMarketTerms(
                asset_id=asset_id,
                condition_id=condition_id,
                fee_rate_bps=0,
                fee_rate=Decimal(0),
                fee_exponent=Decimal(0),
                fee_taker_only=True,
                itode=itode,
                seconds_delay=seconds_delay,
                taker_delay_ms=taker_delay_ms,
                delay_source=delay_source,
                source="clob_fee_rate_zero",
                observed_at=now,
                expires_at=now + timedelta(seconds=self.ttl_seconds),
            )
        else:
            raw_fee = clob_market.get("fd")
            fee: dict[str, Any] = raw_fee if isinstance(raw_fee, dict) else {}
            rate = _decimal(fee.get("r"))
            exponent = _decimal(fee.get("e"))
            if rate is None or exponent is None:
                raise MarketTermsUnavailable(
                    "fee-enabled market is missing authoritative fd.r/fd.e"
                )
            terms = PaperMarketTerms(
                asset_id=asset_id,
                condition_id=condition_id,
                fee_rate_bps=fee_rate_bps,
                fee_rate=rate,
                fee_exponent=exponent,
                fee_taker_only=bool(fee.get("to", True)),
                itode=itode,
                seconds_delay=seconds_delay,
                taker_delay_ms=taker_delay_ms,
                delay_source=delay_source,
                source="clob_market_fd",
                observed_at=now,
                expires_at=now + timedelta(seconds=self.ttl_seconds),
            )
        await self._run_db(self.repository.upsert_market_terms, terms)
        self._memory[asset_id] = terms
        return terms


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _nonnegative_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        raise MarketTermsUnavailable("market seconds_delay is invalid") from None
    if parsed < 0:
        raise MarketTermsUnavailable("market seconds_delay is negative")
    return parsed


def _boolean(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, "", 0, "0"):
        return False
    if value in (1, "1"):
        return True
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes"}:
        return True
    if normalized in {"false", "no"}:
        return False
    raise MarketTermsUnavailable(f"market {field_name} is invalid")
