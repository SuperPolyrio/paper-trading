from datetime import datetime, timedelta, timezone

import pytest

from quant.simulator.regime import VenueRegimeCatalog, VenueRegimeSnapshot, diff_regime

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _snapshot(regime_id: str, *, valid_from=NOW, valid_to=None) -> VenueRegimeSnapshot:
    return VenueRegimeSnapshot(
        regime_id, "POLYMARKET", valid_from, valid_to, "v2", "py-clob-client-v2", "x",
        "EOA", "USDC", "exchange", "CLOB", {"default": "0.01"}, {"min": "5"},
        {"taker": "0"}, {}, "standard", {"orders": 10}, 15, {"seconds": 5}, "CLOB",
        "https://docs.example/regime",
    )


def test_catalog_prevents_overlapping_regimes_and_resolves_point_in_time_snapshot() -> None:
    catalog = VenueRegimeCatalog()
    first = catalog.freeze(_snapshot("one", valid_to=NOW + timedelta(days=1)))
    second = catalog.freeze(_snapshot("two", valid_from=NOW + timedelta(days=1)))

    assert catalog.effective_at(venue="POLYMARKET", at=NOW) == first
    assert catalog.effective_at(venue="POLYMARKET", at=NOW + timedelta(days=1)) == second
    with pytest.raises(ValueError, match="overlap"):
        catalog.freeze(_snapshot("overlap", valid_from=NOW + timedelta(hours=12)))


def test_contract_canary_reports_changed_contract_fields_without_submission() -> None:
    snapshot = _snapshot("one")
    report = diff_regime(snapshot, {**snapshot.__dict__, "batch_max_size": 20})

    assert report["status"] == "CHANGED"
    assert "batch_max_size" in report["changed"]
    assert report["live_submission_performed"] is False
