"""Adapters for Polymarket reward and rebate truth sources."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import (
    OfficialRewardRecord,
    RewardStatus,
    RewardType,
    deterministic_reward_id,
    payload_hash,
)


class RewardSdkClient(Protocol):
    def get_current_rewards(self) -> list: ...

    def get_earnings_for_user_for_day(self, date: str) -> list: ...

    def get_total_earnings_for_user_for_day(self, date: str) -> list: ...


@dataclass(frozen=True)
class EarningsCompletenessResult:
    reward_date: date
    status: str
    detail_count: int
    total_count: int
    detail_by_asset: Mapping[str, Decimal]
    total_by_asset: Mapping[str, Decimal]
    delta_by_asset: Mapping[str, Decimal]
    tolerance: Decimal

    @property
    def complete(self) -> bool:
        return self.status in {"PASS", "PASS_NO_ACTIVITY"}


class PolymarketOfficialRewardClient:
    """The only network boundary used by reward ingestion services."""

    def __init__(
        self,
        *,
        sdk_client: RewardSdkClient | None = None,
        base_url: str = "https://clob.polymarket.com",
        data_base_url: str = "https://data-api.polymarket.com",
        bridge_base_url: str = "https://bridge.polymarket.com",
        session: requests.Session | None = None,
        proxy_url: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.sdk_client = sdk_client
        self.base_url = base_url.rstrip("/")
        self.data_base_url = data_base_url.rstrip("/")
        self.bridge_base_url = bridge_base_url.rstrip("/")
        self.session = session or requests.Session()
        if session is None:
            # Production collectors must not inherit the interactive shell's
            # Clash/proxy state. A proxy is used only when explicitly supplied.
            self.session.trust_env = False
            retry = Retry(
                total=3,
                connect=3,
                read=3,
                status=3,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
        self.proxy_url = proxy_url
        self.timeout_seconds = timeout_seconds

    @property
    def _proxies(self) -> dict[str, str] | None:
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    def fetch_reward_schedules(self) -> list[Mapping[str, Any]]:
        if self.sdk_client is None:
            raise RuntimeError("authenticated reward SDK client is not configured")
        return list(self.sdk_client.get_current_rewards())

    def fetch_user_earnings(self, reward_date: date) -> list[Mapping[str, Any]]:
        if self.sdk_client is None:
            raise RuntimeError("authenticated reward SDK client is not configured")
        return list(
            self.sdk_client.get_earnings_for_user_for_day(reward_date.isoformat())
        )

    def fetch_user_total_earnings(self, reward_date: date) -> list[Mapping[str, Any]]:
        if self.sdk_client is None:
            raise RuntimeError("authenticated reward SDK client is not configured")
        return list(
            self.sdk_client.get_total_earnings_for_user_for_day(reward_date.isoformat())
        )

    def fetch_maker_rebates(
        self, *, reward_date: date, maker_address: str
    ) -> list[Mapping[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/rebates/current",
            params={"date": reward_date.isoformat(), "maker_address": maker_address},
            proxies=self._proxies,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if payload is None:
            return []
        if isinstance(payload, Mapping) and isinstance(payload.get("data"), list):
            payload = payload["data"]
        if not isinstance(payload, list):
            raise TypeError("maker rebate endpoint returned a non-list payload")
        return payload

    def fetch_user_activity_page(
        self,
        *,
        user_address: str,
        activity_type: str,
        start: int,
        end: int,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        """Read one stable, ascending Data API activity page."""

        if not 1 <= limit <= 500:
            raise ValueError("activity page limit must be between 1 and 500")
        if not 0 <= offset <= 5_000:
            raise ValueError("activity offset must be between 0 and 5000")
        params: dict[str, Any] = {
            "user": user_address,
            "type": activity_type,
            "start": int(start),
            "end": int(end),
            "sortBy": "TIMESTAMP",
            "sortDirection": "ASC",
            "limit": int(limit),
            "offset": int(offset),
        }
        if activity_type in {"DEPOSIT", "WITHDRAWAL"}:
            params["excludeDepositsWithdrawals"] = "false"
        response = self.session.get(
            f"{self.data_base_url}/activity",
            params=params,
            proxies=self._proxies,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise TypeError("account activity endpoint returned a non-list payload")
        return payload

    def fetch_user_activities(
        self,
        *,
        user_address: str,
        activity_types: Sequence[str],
        start: int,
        end: int,
        page_size: int = 500,
        max_offset: int = 5_000,
    ) -> list[Mapping[str, Any]]:
        """Read complete activity windows, splitting before the API offset cap."""

        if start > end:
            raise ValueError("activity start must not follow end")
        if not 1 <= page_size <= 500:
            raise ValueError("activity page size must be between 1 and 500")
        rows: list[Mapping[str, Any]] = []
        for activity_type in activity_types:
            pending = [(int(start), int(end))]
            while pending:
                window_start, window_end = pending.pop()
                window_rows: list[Mapping[str, Any]] = []
                offset = 0
                while True:
                    page = self.fetch_user_activity_page(
                        user_address=user_address,
                        activity_type=str(activity_type),
                        start=window_start,
                        end=window_end,
                        limit=page_size,
                        offset=offset,
                    )
                    window_rows.extend(page)
                    if len(page) < page_size:
                        rows.extend(window_rows)
                        break
                    next_offset = offset + page_size
                    if next_offset > max_offset:
                        if window_start >= window_end:
                            raise RuntimeError(
                                "activity window exceeds offset budget; refusing "
                                "to return truncated official history"
                            )
                        midpoint = (window_start + window_end) // 2
                        pending.extend(
                            ((window_start, midpoint), (midpoint + 1, window_end))
                        )
                        break
                    offset = next_offset
        unique = {_activity_identity(row): row for row in rows}
        return sorted(
            unique.values(),
            key=lambda row: (int(row.get("timestamp") or 0), _activity_identity(row)),
        )

    def fetch_bridge_transactions(
        self,
        *,
        bridge_address: str,
        limit: int = 100,
        max_pages: int = 10_000,
    ) -> list[Mapping[str, Any]]:
        """Walk the opaque Bridge API cursor until it returns null."""

        if not 1 <= limit <= 100:
            raise ValueError("bridge page limit must be between 1 and 100")
        cursor: str | None = None
        rows: list[Mapping[str, Any]] = []
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": limit}
            if cursor is not None:
                params["cursor"] = cursor
            response = self.session.get(
                f"{self.bridge_base_url}/status/{bridge_address}",
                params=params,
                proxies=self._proxies,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping) or not isinstance(
                payload.get("transactions"), list
            ):
                raise TypeError("bridge status endpoint returned invalid payload")
            rows.extend(payload["transactions"])
            next_cursor = payload.get("nextCursor")
            if next_cursor is None:
                unique = {_bridge_identity(row): row for row in rows}
                return sorted(
                    unique.values(),
                    key=lambda row: (
                        int(row.get("createdTimeMs") or 0),
                        _bridge_identity(row),
                    ),
                )
            cursor = str(next_cursor)
            if cursor in seen_cursors:
                raise RuntimeError("bridge status cursor loop detected")
            seen_cursors.add(cursor)
        raise RuntimeError("bridge status exceeded max_pages; refusing truncation")


def normalize_maker_rebates(
    rows: Sequence[Mapping[str, Any]],
    *,
    account_id: str,
) -> tuple[OfficialRewardRecord, ...]:
    records: list[OfficialRewardRecord] = []
    for row in rows:
        reward_date = date.fromisoformat(str(row["date"])[:10])
        condition_id = _optional_text(row.get("condition_id"))
        reward_asset_address = _optional_text(row.get("asset_address"))
        amount = Decimal(str(row["rebated_fees_usdc"]))
        source_event_id = deterministic_reward_id(
            "clob-maker-rebate",
            {
                "account_id": account_id.lower(),
                "reward_asset_address": reward_asset_address,
                "condition_id": condition_id,
                "date": reward_date.isoformat(),
            },
        )
        records.append(
            OfficialRewardRecord(
                source="CLOB_REBATES_CURRENT",
                source_event_id=source_event_id,
                reward_type=RewardType.MAKER_REBATE,
                account_id=account_id,
                amount=amount,
                currency="USDC",
                reward_date=reward_date,
                status=RewardStatus.PAYABLE,
                condition_id=condition_id,
                asset_id=None,
                raw_payload_hash=payload_hash(row),
                rule_version="polymarket-clob-rebates-current-v1",
                metadata={
                    "maker_address": row.get("maker_address"),
                    "reward_asset_address": reward_asset_address,
                    "adapter_schema_version": "polymarket-clob-rebates-current-v1",
                    "rule_version": "polymarket-clob-rebates-current-v1",
                },
            )
        )
    return tuple(records)


def normalize_user_earnings(
    rows: Sequence[Mapping[str, Any]],
    *,
    account_id: str,
    reward_type: RewardType = RewardType.LIQUIDITY_REWARD,
) -> tuple[OfficialRewardRecord, ...]:
    records: list[OfficialRewardRecord] = []
    for row in rows:
        parsed_at = _timestamp(row.get("date"))
        condition_id = _optional_text(row.get("condition_id"))
        reward_asset_address = _optional_text(row.get("asset_address"))
        source_event_id = deterministic_reward_id(
            "clob-user-earning",
            {
                "account_id": account_id.lower(),
                "reward_asset_address": reward_asset_address,
                "condition_id": condition_id,
                "date": parsed_at.date().isoformat(),
                "reward_type": reward_type.value,
            },
        )
        records.append(
            OfficialRewardRecord(
                source="CLOB_REWARDS_USER",
                source_event_id=source_event_id,
                reward_type=reward_type,
                account_id=account_id,
                amount=Decimal(str(row["earnings"])),
                currency=_reward_asset_currency(reward_asset_address),
                reward_date=parsed_at.date(),
                status=RewardStatus.ACCRUED,
                condition_id=condition_id,
                asset_id=None,
                raw_payload_hash=payload_hash(row),
                rule_version="polymarket-clob-user-earnings-v1",
                metadata={
                    "asset_rate": row.get("asset_rate"),
                    "maker_address": row.get("maker_address"),
                    "reward_asset_address": reward_asset_address,
                    "adapter_schema_version": "polymarket-clob-user-earnings-v1",
                    "rule_version": "polymarket-clob-user-earnings-v1",
                },
            )
        )
    return tuple(records)


def reconcile_user_earnings_totals(
    detail_rows: Sequence[Mapping[str, Any]],
    total_rows: Sequence[Mapping[str, Any]],
    *,
    reward_date: date,
    tolerance: Decimal = Decimal("0.000001"),
) -> EarningsCompletenessResult:
    """Prove the paginated per-market response sums to the official control total."""

    detail = _earnings_by_reward_asset(detail_rows)
    total = _earnings_by_reward_asset(total_rows)
    assets = sorted(set(detail) | set(total))
    deltas = {
        asset: total.get(asset, Decimal(0)) - detail.get(asset, Decimal(0))
        for asset in assets
    }
    if not detail_rows and not total_rows:
        status = "PASS_NO_ACTIVITY"
    elif all(abs(delta) <= tolerance for delta in deltas.values()):
        status = "PASS"
    else:
        status = "MISMATCH"
    return EarningsCompletenessResult(
        reward_date=reward_date,
        status=status,
        detail_count=len(detail_rows),
        total_count=len(total_rows),
        detail_by_asset=detail,
        total_by_asset=total,
        delta_by_asset=deltas,
        tolerance=Decimal(tolerance),
    )


def _timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("official reward date is required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _reward_asset_currency(asset_address: str | None) -> str:
    if not asset_address:
        return "REWARD_ASSET_UNKNOWN"
    normalized = asset_address.lower()
    if normalized == "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb":
        return "pUSD"
    if normalized in {
        "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
        "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
    }:
        return "USDC"
    return f"REWARD_ASSET:{normalized}"


def _earnings_by_reward_asset(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for row in rows:
        asset = str(row.get("asset_address") or "").strip().lower()
        if not asset:
            raise ValueError("official user earnings row is missing asset_address")
        amount = Decimal(str(row.get("earnings")))
        if amount < 0:
            raise ValueError("official user earnings cannot be negative")
        result[asset] = result.get(asset, Decimal(0)) + amount
    return result


def _activity_identity(row: Mapping[str, Any]) -> str:
    return payload_hash(
        {
            "asset": row.get("asset"),
            "conditionId": row.get("conditionId"),
            "outcomeIndex": row.get("outcomeIndex"),
            "side": row.get("side"),
            "size": row.get("size"),
            "timestamp": row.get("timestamp"),
            "transactionHash": row.get("transactionHash"),
            "type": row.get("type"),
            "usdcSize": row.get("usdcSize"),
        }
    )


def _bridge_identity(row: Mapping[str, Any]) -> str:
    return payload_hash(
        {
            "createdTimeMs": row.get("createdTimeMs"),
            "fromAmountBaseUnit": row.get("fromAmountBaseUnit"),
            "fromChainId": row.get("fromChainId"),
            "fromTokenAddress": row.get("fromTokenAddress"),
            "status": row.get("status"),
            "toChainId": row.get("toChainId"),
            "toTokenAddress": row.get("toTokenAddress"),
            "txHash": row.get("txHash"),
        }
    )
