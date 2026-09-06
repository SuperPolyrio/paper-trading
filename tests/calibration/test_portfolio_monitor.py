from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from quant.calibration import portfolio_monitor
from quant.calibration.portfolio_monitor import (
    apply_rest_book_fallback,
    build_portfolio_snapshot,
    load_shadow_health,
)


class _Store:
    def pnl_positions(self, *, account_id=None):
        return [{"asset_id": "1", "mark_status": "READY"}]

    def pnl_summary(self, *, account_id=None):
        return {
            "open_positions": 1,
            "marked_open_positions": 1,
            "real_unrealized_pnl": Decimal("0.25"),
        }


def test_live_portfolio_requires_healthy_transport(tmp_path):
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "transport_state": "REDUNDANT",
                "route_states": {"primary": "CONNECTED", "secondary": "CONNECTED"},
            }
        ),
        encoding="utf-8",
    )
    payload = build_portfolio_snapshot(
        _Store(),
        account_id="0xabc",
        shadow_status_path=status,
    )
    assert payload["status"] == "READY"
    assert payload["summary"]["real_unrealized_pnl"] == Decimal("0.25")


def test_live_portfolio_reads_health_from_explicit_paper_control_plane(
    tmp_path, monkeypatch
):
    health_factory = object()
    observed = {}

    def fake_health(_path, *, connection_factory=None):
        observed["connection_factory"] = connection_factory
        return {"status": "READY"}

    monkeypatch.setattr(portfolio_monitor, "load_shadow_health", fake_health)
    payload = build_portfolio_snapshot(
        _Store(),
        account_id="0xabc",
        shadow_status_path=tmp_path / "unused.json",
        health_connection_factory=health_factory,
    )

    assert payload["status"] == "READY"
    assert observed["connection_factory"] is health_factory


def test_missing_shadow_status_is_unavailable(tmp_path):
    assert load_shadow_health(tmp_path / "missing.json")["status"] == "UNAVAILABLE"


class _RestBooks:
    def get_book_snapshot(self, *, asset_id):
        if asset_id == "missing":
            return {
                "rest_best_bid": None,
                "rest_best_ask": None,
                "rest_book_observed_at": None,
            }
        return {
            "rest_best_bid": "0.42",
            "rest_best_ask": "0.43",
            "rest_book_hash": "book-hash",
            "rest_book_observed_at": datetime.now(timezone.utc).isoformat(),
        }


def test_rest_book_fallback_marks_portfolio_without_changing_execution_state():
    positions, stats = apply_rest_book_fallback(
        [
            {
                "asset_id": "token-1",
                "real_quantity": Decimal("5"),
                "paper_quantity": Decimal("5"),
                "real_cost_basis": Decimal("2"),
                "paper_cost_basis": Decimal("2"),
                "mark_status": "UNAVAILABLE",
                "market_state": "STALE",
                "execution_eligible": False,
            },
            {
                "asset_id": "missing",
                "real_quantity": Decimal("1"),
                "paper_quantity": Decimal("1"),
                "real_cost_basis": Decimal("1"),
                "paper_cost_basis": Decimal("1"),
                "mark_status": "UNAVAILABLE",
            },
        ],
        rest_book_client=_RestBooks(),
    )

    assert stats["attempted"] == 2
    assert stats["marked"] == 1
    assert stats["unavailable"] == 1
    assert positions[0]["mark_source"] == "REST_BBO_FALLBACK"
    assert positions[0]["mark_quality"] == "REPORTING_ONLY"
    assert positions[0]["real_liquidation_value"] == Decimal("2.10")
    assert positions[0]["execution_eligible"] is False
    assert positions[1]["mark_status"] == "UNAVAILABLE"


def test_rest_book_fallback_marks_one_sided_book_at_zero_for_conservative_nav():
    class _OneSidedBook:
        def get_book_snapshot(self, *, asset_id):
            return {
                "rest_best_bid": None,
                "rest_best_ask": "0.006",
                "rest_book_observed_at": datetime.now(timezone.utc).isoformat(),
            }

    positions, stats = apply_rest_book_fallback(
        [
            {
                "asset_id": "token-1",
                "real_quantity": Decimal("5"),
                "paper_quantity": Decimal("5"),
                "real_cost_basis": Decimal("2"),
                "paper_cost_basis": Decimal("2"),
                "mark_status": "UNAVAILABLE",
            }
        ],
        rest_book_client=_OneSidedBook(),
    )

    assert stats["marked"] == 1
    assert stats["conservative_zero_marks"] == 1
    assert positions[0]["mark_source"] == "REST_NO_BID_CONSERVATIVE_ZERO"
    assert positions[0]["mark_bid"] == Decimal("0")
    assert positions[0]["real_liquidation_value"] == Decimal("0")


def test_rest_book_fallback_uses_batch_reader_when_available():
    class _BatchBooks:
        def get_reporting_book_snapshots(self, *, asset_ids):
            assert asset_ids == ["token-1", "token-2"]
            return {
                "token-1": {
                    "rest_best_bid": "0.31",
                    "rest_best_ask": "0.32",
                    "rest_book_observed_at": datetime.now(timezone.utc).isoformat(),
                },
                "token-2": {"reporting_book_error": "bounded_timeout"},
            }

    positions, stats = apply_rest_book_fallback(
        [
            {
                "asset_id": "token-1",
                "real_quantity": "2",
                "paper_quantity": "2",
                "real_cost_basis": "0.5",
                "paper_cost_basis": "0.5",
                "mark_status": "UNAVAILABLE",
            },
            {
                "asset_id": "token-2",
                "real_quantity": "1",
                "paper_quantity": "1",
                "real_cost_basis": "0.5",
                "paper_cost_basis": "0.5",
                "mark_status": "UNAVAILABLE",
            },
        ],
        rest_book_client=_BatchBooks(),
    )

    assert stats["batch_mode"] is True
    assert stats["marked"] == 1
    assert stats["unavailable"] == 1
    assert positions[0]["mark_bid"] == Decimal("0.31")
    assert positions[1]["mark_status"] == "UNAVAILABLE"
