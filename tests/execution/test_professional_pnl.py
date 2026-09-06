from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant.paper.professional_pnl import (
    ProfessionalNavPoint,
    build_professional_pnl_report,
    nav_point_from_snapshot,
)


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_professional_curves_adjust_external_capital_and_drawdown() -> None:
    report = build_professional_pnl_report(
        [
            ProfessionalNavPoint(NOW, Decimal("10000"), Decimal("10000"), Decimal("10000"), Decimal("10000")),
            ProfessionalNavPoint(
                NOW + timedelta(days=1),
                Decimal("11100"),
                Decimal("11200"),
                Decimal("11000"),
                Decimal("11050"),
                external_capital_flow=Decimal("1000"),
            ),
            ProfessionalNavPoint(
                NOW + timedelta(days=2),
                Decimal("10600"),
                Decimal("10700"),
                Decimal("10500"),
                Decimal("10550"),
            ),
        ],
        initial_nav=Decimal("10000"),
    )
    official = report["curves"]["official_mark"]
    assert official["pnl"] == Decimal("-400")
    assert official["return"] == Decimal("-0.04")
    assert official["modified_dietz_return"] == Decimal("-400") / Decimal("10500")
    assert official["max_drawdown"] == Decimal("500")
    assert report["cumulative_external_capital_flow"] == Decimal("1000")
    assert report["status"] == "COMPLETE"


def test_unavailable_curve_and_unpriced_inventory_are_not_hidden() -> None:
    report = build_professional_pnl_report(
        [
            ProfessionalNavPoint(
                NOW,
                None,
                Decimal("10010"),
                Decimal("9990"),
                None,
                unpriced_quantity=Decimal("7"),
                complete=False,
            )
        ],
        initial_nav=Decimal("10000"),
    )
    assert report["status"] == "DEGRADED"
    assert report["quality"]["unpriced_quantity"] == Decimal("7")
    assert report["curves"]["official_mark"]["status"] == "UNAVAILABLE"


def test_snapshot_adapter_does_not_invent_official_or_confirmed_nav() -> None:
    point = nav_point_from_snapshot(
        {
            "observed_at": NOW,
            "equity": Decimal("10020"),
            "conservative_equity": Decimal("10000"),
            "nav_complete": True,
            "metadata": {"valuation_views": {"unliquidated_quantity": "2"}},
        }
    )
    assert point.official_mark is None
    assert point.research_mid == Decimal("10020")
    assert point.confirmed_return is None
    assert point.unpriced_quantity == Decimal("2")
