"""Deterministic IP-endpoint and signer token buckets for paper gateway commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal

from .command import GatewayCommand

NANOSECONDS_PER_SECOND = Decimal("1000000000")


@dataclass(frozen=True)
class RateLimitResult:
    accepted: bool
    denied: bool
    available_at_ts_ns: int | None
    reason: str


@dataclass
class TokenBucket:
    capacity: Decimal
    refill_per_second: Decimal
    tokens: Decimal | None = None
    updated_ts_ns: int = 0

    def __post_init__(self) -> None:
        self.capacity = Decimal(self.capacity)
        self.refill_per_second = Decimal(self.refill_per_second)
        self.tokens = self.capacity if self.tokens is None else min(self.capacity, Decimal(self.tokens))
        self.updated_ts_ns = int(self.updated_ts_ns)
        if self.capacity <= 0 or self.refill_per_second < 0 or self.tokens < 0:
            raise ValueError("invalid token bucket configuration")

    def reserve(self, now_ts_ns: int, *, cost: Decimal = Decimal("1")) -> RateLimitResult:
        now = int(now_ts_ns)
        cost = Decimal(cost)
        if cost <= 0:
            raise ValueError("token cost must be positive")
        if cost > self.capacity:
            return RateLimitResult(False, True, None, "cost_exceeds_bucket_capacity")
        self._refill(now)
        assert self.tokens is not None
        if self.tokens >= cost:
            self.tokens -= cost
            return RateLimitResult(True, False, now, "accepted")
        if self.refill_per_second <= 0:
            return RateLimitResult(False, True, None, "bucket_never_refills")
        deficit = cost - self.tokens
        wait_ns = int(
            (deficit * NANOSECONDS_PER_SECOND / self.refill_per_second).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        return RateLimitResult(False, False, now + max(1, wait_ns), "throttled")

    def _refill(self, now_ts_ns: int) -> None:
        if now_ts_ns < self.updated_ts_ns:
            raise ValueError("rate limit clock cannot move backwards")
        elapsed = Decimal(now_ts_ns - self.updated_ts_ns)
        assert self.tokens is not None
        self.tokens = min(
            self.capacity,
            self.tokens + elapsed * self.refill_per_second / NANOSECONDS_PER_SECOND,
        )
        self.updated_ts_ns = now_ts_ns


@dataclass(frozen=True)
class RateLimitConfig:
    ip_endpoint_capacity: Decimal = Decimal("30")
    ip_endpoint_refill_per_second: Decimal = Decimal("30")
    signer_trading_capacity: Decimal = Decimal("10")
    signer_trading_refill_per_second: Decimal = Decimal("10")


@dataclass
class DualRateLimiter:
    config: RateLimitConfig = field(default_factory=RateLimitConfig)
    _ip_buckets: dict[str, TokenBucket] = field(default_factory=dict, init=False)
    _signer_buckets: dict[str, TokenBucket] = field(default_factory=dict, init=False)

    def reserve(self, command: GatewayCommand, *, now_ts_ns: int) -> RateLimitResult:
        ip = self._ip_buckets.setdefault(
            f"{command.ip_id}:{command.endpoint}",
            TokenBucket(self.config.ip_endpoint_capacity, self.config.ip_endpoint_refill_per_second, updated_ts_ns=now_ts_ns),
        )
        signer = self._signer_buckets.setdefault(
            command.signer_id,
            TokenBucket(self.config.signer_trading_capacity, self.config.signer_trading_refill_per_second, updated_ts_ns=now_ts_ns),
        )
        ip_preview = ip.reserve(now_ts_ns)
        if ip_preview.denied:
            return RateLimitResult(False, True, None, f"ip_endpoint:{ip_preview.reason}")
        if not ip_preview.accepted:
            return RateLimitResult(False, False, ip_preview.available_at_ts_ns, "ip_endpoint:throttled")
        signer_preview = signer.reserve(now_ts_ns)
        if signer_preview.accepted:
            return RateLimitResult(True, False, now_ts_ns, "accepted")
        # Restore the IP token only when signer blocked the same atomic attempt.
        assert ip.tokens is not None
        ip.tokens = min(ip.capacity, ip.tokens + Decimal("1"))
        if signer_preview.denied:
            return RateLimitResult(False, True, None, f"signer:{signer_preview.reason}")
        return RateLimitResult(False, False, signer_preview.available_at_ts_ns, "signer:throttled")
