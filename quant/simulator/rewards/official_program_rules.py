"""Versioned program rules extracted from the vendored Polymarket docs."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .referral_reward import ReferralProgramSchedule
from .taker_rebate import TakerRebateProgramSchedule, TakerTier


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DOCS_SNAPSHOT_ROOT = (
    REPOSITORY_ROOT
    / "docs/reference/polymarket_official/snapshots/2026-08-18T033128Z/raw"
)
HELP_SNAPSHOT_ROOT = (
    REPOSITORY_ROOT
    / "docs/reference/polymarket_official/help_center/snapshots/2026-08-18T073151Z/markdown/en/articles"
)


def official_taker_rebate_schedule() -> TakerRebateProgramSchedule:
    path = DOCS_SNAPSHOT_ROOT / "programs/taker-rebates.md"
    return TakerRebateProgramSchedule(
        schedule_id=f"polymarket-taker-rebate-2026-05-28:{_sha256(path)[:16]}",
        effective_from=datetime(2026, 5, 28, tzinfo=timezone.utc),
        effective_until=None,
        category_weights={
            "sports": Decimal("1.0"),
            "politics": Decimal("1.3"),
            "finance": Decimal("1.3"),
            "mentions": Decimal("1.3"),
            "tech": Decimal("1.3"),
            "economics": Decimal("1.7"),
            "culture": Decimal("1.7"),
            "weather": Decimal("1.7"),
            "other": Decimal("1.7"),
            "crypto": Decimal("2.3"),
            "geopolitics": Decimal(0),
        },
        tiers=(
            TakerTier(0, "None", Decimal(0), Decimal(0), Decimal(0)),
            TakerTier(1, "Bronze", Decimal(2_000), Decimal("0.03"), Decimal(10)),
            TakerTier(2, "Silver", Decimal(20_000), Decimal("0.08"), Decimal(50)),
            TakerTier(3, "Gold", Decimal(200_000), Decimal("0.18"), Decimal(250)),
            TakerTier(4, "Platinum", Decimal(1_000_000), Decimal("0.32"), Decimal(1_500)),
            TakerTier(5, "Diamond", Decimal(4_000_000), Decimal("0.44"), Decimal(7_500)),
            TakerTier(6, "Obsidian", Decimal(10_000_000), Decimal("0.50"), Decimal(25_000)),
        ),
        minimum_payout=Decimal(1),
        rolling_days=30,
        source=f"OFFICIAL_DOC_SNAPSHOT:{path.relative_to(REPOSITORY_ROOT)}",
    )


def official_referral_schedule() -> ReferralProgramSchedule:
    path = DOCS_SNAPSHOT_ROOT / "programs/referral-program.md"
    return ReferralProgramSchedule(
        schedule_id=f"polymarket-referral-2026-05-28:{_sha256(path)[:16]}",
        effective_from=datetime(2026, 5, 28, tzinfo=timezone.utc),
        effective_until=None,
        minimum_owner_lifetime_volume=Decimal(10_000),
        direct_rate=Decimal("0.10"),
        indirect_rate=Decimal("0.05"),
        earning_window_days=30,
        platinum_tier_level=4,
        source=f"OFFICIAL_DOC_SNAPSHOT:{path.relative_to(REPOSITORY_ROOT)}",
        source_document_hash=_sha256(path),
    )


def bundled_program_rule_rows() -> tuple[dict[str, Any], ...]:
    """Return auditable rules, retaining official conflicts instead of guessing."""

    taker_path = DOCS_SNAPSHOT_ROOT / "programs/taker-rebates.md"
    referral_path = DOCS_SNAPSHOT_ROOT / "programs/referral-program.md"
    holding_help = HELP_SNAPSHOT_ROOT / "13364459-holding-rewards.md"
    holding_concepts = DOCS_SNAPSHOT_ROOT / "concepts/positions-tokens.md"
    return (
        {
            "_source": "OFFICIAL_DOC_TAKER_REBATE",
            "effective_date": "2026-05-28",
            "schedule": official_taker_rebate_schedule().schedule_id,
            "document": str(taker_path.relative_to(REPOSITORY_ROOT)),
            "document_sha256": _sha256(taker_path),
        },
        {
            "_source": "OFFICIAL_DOC_REFERRAL",
            "effective_date": "2026-05-28",
            "schedule": official_referral_schedule().schedule_id,
            "document": str(referral_path.relative_to(REPOSITORY_ROOT)),
            "document_sha256": _sha256(referral_path),
        },
        {
            "_source": "OFFICIAL_DOC_HOLDING_REWARD",
            "effective_date": "2026-06-01",
            "annual_rate": "0.0325",
            "sampling": "RANDOM_ONCE_PER_HOUR",
            "document": str(holding_help.relative_to(REPOSITORY_ROOT)),
            "document_sha256": _sha256(holding_help),
            "conflict_group": "holding-reward-current-rate",
        },
        {
            "_source": "OFFICIAL_DOC_HOLDING_REWARD",
            "effective_date": "2026-08-18",
            "annual_rate": "0.04",
            "sampling": "RANDOM_ONCE_PER_HOUR",
            "document": str(holding_concepts.relative_to(REPOSITORY_ROOT)),
            "document_sha256": _sha256(holding_concepts),
            "conflict_group": "holding-reward-current-rate",
            "requires_manual_authority_resolution": True,
        },
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
