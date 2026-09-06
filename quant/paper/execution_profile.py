"""Immutable execution-profile selection for live paper orders."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from .taker_execution import (
    ArrivalBookCheckpoint,
    OrderIntent,
    PaperLatencyModel,
    TakerExecutionConfig,
)


class ExecutionProfile(str, Enum):
    SMALL_TAKER_L2 = "SMALL_TAKER_L2"
    CONSERVATIVE_DEPTH = "CONSERVATIVE_DEPTH"
    MAKER_STRICT_TRADE_EVIDENCE = "MAKER_STRICT_TRADE_EVIDENCE"
    STRICT_NO_FILL = "STRICT_NO_FILL"


@dataclass(frozen=True)
class ExecutionProfileDecision:
    intent_id: int
    profile: ExecutionProfile
    execution_allowed: bool
    resolver_version: str
    execution_model_version: str
    execution_config_hash: str
    fee_schedule_version: str
    latency_model_version: str
    queue_model_version: str
    book_checkpoint_id: str | None
    book_generation: int | None
    coverage_grade: str | None
    calibration_domain: str
    depth_haircut: Decimal
    reason_codes: tuple[str, ...]
    latency_feed_delay_ms: int = 0
    latency_strategy_delay_ms: int = 0
    latency_order_delay_ms: int = 100
    max_book_age_ms: int = 2_000
    configured_grade_b_depth_haircut: Decimal = Decimal("0.25")
    configured_fee_bps: Decimal = Decimal(0)
    decision_hash: str = ""

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.depth_haircut <= Decimal(1):
            raise ValueError("depth_haircut must be in [0, 1]")
        if not self.decision_hash:
            object.__setattr__(self, "decision_hash", self._calculate_hash())

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["profile"] = self.profile.value
        payload["depth_haircut"] = str(self.depth_haircut)
        payload["configured_grade_b_depth_haircut"] = str(
            self.configured_grade_b_depth_haircut
        )
        payload["configured_fee_bps"] = str(self.configured_fee_bps)
        payload["reason_codes"] = list(self.reason_codes)
        return payload

    def to_execution_config(self) -> TakerExecutionConfig:
        config = TakerExecutionConfig(
            latency=PaperLatencyModel(
                feed_delay_ms=self.latency_feed_delay_ms,
                strategy_delay_ms=self.latency_strategy_delay_ms,
                order_delay_ms=self.latency_order_delay_ms,
            ),
            max_book_age_ms=self.max_book_age_ms,
            grade_b_depth_haircut=self.configured_grade_b_depth_haircut,
            fee_bps=self.configured_fee_bps,
            model_version=self.execution_model_version,
        )
        if config.config_hash != self.execution_config_hash:
            raise ValueError("frozen execution config hash is invalid")
        return config

    @property
    def hash_is_valid(self) -> bool:
        return self.decision_hash == self._calculate_hash()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExecutionProfileDecision:
        return cls(
            intent_id=int(payload["intent_id"]),
            profile=ExecutionProfile(str(payload["profile"])),
            execution_allowed=bool(payload["execution_allowed"]),
            resolver_version=str(payload["resolver_version"]),
            execution_model_version=str(payload["execution_model_version"]),
            execution_config_hash=str(payload["execution_config_hash"]),
            fee_schedule_version=str(payload["fee_schedule_version"]),
            latency_model_version=str(payload["latency_model_version"]),
            queue_model_version=str(payload["queue_model_version"]),
            book_checkpoint_id=(
                str(payload["book_checkpoint_id"])
                if payload.get("book_checkpoint_id")
                else None
            ),
            book_generation=(
                int(payload["book_generation"])
                if payload.get("book_generation") is not None
                else None
            ),
            coverage_grade=(
                str(payload["coverage_grade"])
                if payload.get("coverage_grade")
                else None
            ),
            calibration_domain=str(payload["calibration_domain"]),
            depth_haircut=Decimal(str(payload["depth_haircut"])),
            reason_codes=tuple(str(item) for item in payload.get("reason_codes", ())),
            latency_feed_delay_ms=int(
                payload["latency_feed_delay_ms"]
                if payload.get("latency_feed_delay_ms") is not None
                else 0
            ),
            latency_strategy_delay_ms=int(
                payload["latency_strategy_delay_ms"]
                if payload.get("latency_strategy_delay_ms") is not None
                else 0
            ),
            latency_order_delay_ms=int(
                payload["latency_order_delay_ms"]
                if payload.get("latency_order_delay_ms") is not None
                else 100
            ),
            max_book_age_ms=int(
                payload["max_book_age_ms"]
                if payload.get("max_book_age_ms") is not None
                else 2_000
            ),
            configured_grade_b_depth_haircut=Decimal(
                str(payload.get("configured_grade_b_depth_haircut") or "0.25")
            ),
            configured_fee_bps=Decimal(str(payload.get("configured_fee_bps") or "0")),
            decision_hash=str(payload.get("decision_hash") or ""),
        )

    def _calculate_hash(self) -> str:
        payload = self.as_dict()
        payload["decision_hash"] = ""
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ExecutionProfileResolver:
    """Select the least optimistic profile justified by frozen evidence."""

    VERSION = "paper-execution-profile-resolver-v1"
    LATENCY_VERSION = "paper-latency-fixed-v1"
    MAKER_QUEUE_VERSION = "paper_maker_queue_strict_ws_trade_v2"

    def resolve(
        self,
        *,
        intent_id: int,
        intent: OrderIntent,
        checkpoint: ArrivalBookCheckpoint | None,
        risk_status: str,
        config: TakerExecutionConfig,
    ) -> ExecutionProfileDecision:
        common = {
            "intent_id": int(intent_id),
            "resolver_version": self.VERSION,
            "execution_model_version": config.model_version,
            "execution_config_hash": config.config_hash,
            "fee_schedule_version": intent.fee_schedule_id or "UNBOUND",
            "latency_model_version": (
                f"{self.LATENCY_VERSION}:{config.config_hash}"
            ),
            "book_checkpoint_id": checkpoint.checkpoint_id if checkpoint else None,
            "book_generation": checkpoint.generation if checkpoint else None,
            "coverage_grade": checkpoint.coverage_grade if checkpoint else None,
            "calibration_domain": "UNCALIBRATED",
            "latency_feed_delay_ms": config.latency.feed_delay_ms,
            "latency_strategy_delay_ms": config.latency.strategy_delay_ms,
            "latency_order_delay_ms": config.latency.order_delay_ms,
            "max_book_age_ms": config.max_book_age_ms,
            "configured_grade_b_depth_haircut": config.grade_b_depth_haircut,
            "configured_fee_bps": config.fee_bps,
        }
        unsafe_reason = self._unsafe_checkpoint_reason(checkpoint)
        if str(risk_status).upper() not in {"ACCEPT", "ACCEPT_STRESSED"}:
            return ExecutionProfileDecision(
                **common,
                profile=ExecutionProfile.STRICT_NO_FILL,
                execution_allowed=False,
                queue_model_version="NOT_APPLICABLE",
                depth_haircut=Decimal(0),
                reason_codes=("risk_not_accepted",),
            )
        if unsafe_reason is not None:
            return ExecutionProfileDecision(
                **common,
                profile=ExecutionProfile.STRICT_NO_FILL,
                execution_allowed=False,
                queue_model_version=(
                    self.MAKER_QUEUE_VERSION if intent.post_only else "NOT_APPLICABLE"
                ),
                depth_haircut=Decimal(0),
                reason_codes=(unsafe_reason,),
            )
        if intent.post_only:
            return ExecutionProfileDecision(
                **common,
                profile=ExecutionProfile.MAKER_STRICT_TRADE_EVIDENCE,
                execution_allowed=True,
                queue_model_version=self.MAKER_QUEUE_VERSION,
                depth_haircut=Decimal(1),
                reason_codes=("maker_requires_public_trade_evidence",),
            )
        if str(risk_status).upper() == "ACCEPT_STRESSED" or (
            checkpoint is not None and checkpoint.coverage_grade == "B"
        ):
            return ExecutionProfileDecision(
                **common,
                profile=ExecutionProfile.CONSERVATIVE_DEPTH,
                execution_allowed=True,
                queue_model_version="NOT_APPLICABLE",
                depth_haircut=config.grade_b_depth_haircut,
                reason_codes=(
                    "capacity_stressed"
                    if str(risk_status).upper() == "ACCEPT_STRESSED"
                    else "coverage_grade_b",
                ),
            )
        return ExecutionProfileDecision(
            **common,
            profile=ExecutionProfile.SMALL_TAKER_L2,
            execution_allowed=True,
            queue_model_version="NOT_APPLICABLE",
            depth_haircut=Decimal(1),
            reason_codes=("safe_visible_l2_uncalibrated",),
        )

    @staticmethod
    def _unsafe_checkpoint_reason(
        checkpoint: ArrivalBookCheckpoint | None,
    ) -> str | None:
        if checkpoint is None:
            return "arrival_checkpoint_missing"
        if checkpoint.has_gap:
            return "arrival_checkpoint_gap"
        if checkpoint.coverage_grade not in {"A_PLUS", "A", "B"}:
            return "arrival_coverage_not_executable"
        if checkpoint.book_status.upper() not in {
            "READY",
            "READY_HIGH",
            "READY_MEDIUM",
        }:
            return "arrival_book_not_ready"
        return None
