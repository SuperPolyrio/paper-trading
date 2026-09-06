"""Explicit read-only CLOB capability wrapper for paper execution."""

from __future__ import annotations

from typing import Any, Iterable

from .polymarket_clob_client import PolymarketClobClient


class PolymarketPaperClobClient:
    """Expose only market metadata and book-read methods to the paper worker."""

    capabilities = frozenset(
        {
            "GET_BOOK",
            "GET_BOOKS",
            "GET_MARKET",
            "GET_CLOB_MARKET_INFO",
            "GET_FEE_RATE",
        }
    )

    def __init__(self, **kwargs: Any) -> None:
        self.__delegate = PolymarketClobClient(**kwargs)

    async def get_book(self, token_id: str) -> dict[str, Any]:
        return await self.__delegate.get_book(token_id)

    async def get_books(
        self,
        token_ids: Iterable[str],
        *,
        batch_size: int = 500,
    ) -> dict[str, dict[str, Any]]:
        return await self.__delegate.get_books(token_ids, batch_size=batch_size)

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        return await self.__delegate.get_market(condition_id)

    async def get_clob_market_info(self, condition_id: str) -> dict[str, Any]:
        return await self.__delegate.get_clob_market_info(condition_id)

    async def get_fee_rate(self, token_id: str) -> int:
        return await self.__delegate.get_fee_rate(token_id)
