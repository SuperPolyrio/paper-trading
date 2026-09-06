from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "plan_maker_holdout_candidates.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("maker_candidate_planner_cli", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_api_proxy_matches_the_live_candidate_services() -> None:
    module = _module()

    assert module.DEFAULT_MAKER_API_PROXY_URL == "http://127.0.0.1:18080"


def test_cli_uses_paper_control_plane_for_books_and_trade_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    module = _module()
    control_env = tmp_path / "paper-control.env"
    control_env.write_text(
        "POLY_QUANT_PAPER_POSTGRES_HOST=127.0.0.1\n"
        "POLY_QUANT_PAPER_POSTGRES_PASSWORD=secret\n",
        encoding="utf-8",
    )
    factory = object()
    captured = {}

    monkeypatch.setattr(
        module,
        "load_paper_control_plane_env",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        module,
        "paper_control_plane_connection_factory",
        lambda *_args, **_kwargs: factory,
    )

    def load_pool(**kwargs):
        captured["pool_factory"] = kwargs["connection_factory"]
        return []

    class LiveClient:
        def __init__(self, connection_factory):
            captured["evidence_factory"] = connection_factory

    monkeypatch.setattr(module, "load_maker_candidate_pool", load_pool)
    monkeypatch.setattr(
        module,
        "reconcile_candidates_with_rest_books",
        lambda candidates, _books: candidates,
    )
    monkeypatch.setattr(
        module,
        "preselect_maker_candidates",
        lambda candidates, **_kwargs: (candidates, []),
    )
    monkeypatch.setattr(
        module.PolymarketClobClient,
        "get_books",
        lambda *_args, **_kwargs: _empty_books(),
    )
    monkeypatch.setattr(module, "PersistedMakerTradeEvidenceClient", LiveClient)
    monkeypatch.setattr(
        module,
        "rank_maker_candidates",
        lambda *_args, **_kwargs: {"status": "NO_CANDIDATES"},
    )

    output = tmp_path / "plan.json"
    exit_code = module.main(
        [
            "--paper-control-env",
            str(control_env),
            "--discovery-scope",
            "WATCHLIST_STRICT",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 2
    assert captured["pool_factory"] is factory
    assert captured["evidence_factory"] is factory
    assert output.exists()


def test_required_fill_outcome_rejects_nofill_only_plan() -> None:
    module = _module()
    payload = {
        "status": "FORECAST_READY",
        "predicted_outcome_counts": {"NO_FILL": 3, "PARTIAL": 0, "FULL": 0},
    }

    module._apply_required_outcome_gate(payload, "PARTIAL_OR_FULL")

    assert payload["candidate_pool_status"] == "FORECAST_READY"
    assert payload["status"] == "NO_TARGET_CANDIDATES"
    assert payload["outcome_selection_gate"] == {
        "required_outcome": "PARTIAL_OR_FULL",
        "required_targets": ["FULL", "PARTIAL"],
        "available_required_targets": [],
        "status": "NO_TARGET_CANDIDATES",
    }


def test_prospective_candidate_preserves_current_tape_without_claiming_truth() -> None:
    module = _module()

    row = module._prospective_label_candidate(
        {
            "asset_id": "asset-1",
            "market_id": "market-1",
            "condition_id": "condition-1",
            "market_title": "Current active market",
            "market_slug": "current-active-market",
            "outcome_name": "YES",
            "category": "crypto",
            "min_order_size": Decimal(5),
            "resolved_placement": "NEAR_OPPOSITE",
            "preselection_limit_price": Decimal("0.003"),
            "preselection_queue_ahead": Decimal("5.61"),
            "recent_public_compatible_trade_count": 1,
            "recent_public_compatible_trade_volume": Decimal("17.52"),
            "recent_public_compatible_last_at": datetime(
                2026, 8, 28, 5, 23, 46, tzinfo=timezone.utc
            ),
        },
        side="BUY",
        order_size=Decimal(45),
    )

    assert row["gross_notional_usd"] == "0.135"
    assert row["queue_ahead"] == "5.61"
    assert row["side"] == "BUY"
    assert row["recent_public_compatible_trade_count"] == 1
    assert row["evidence_class"] == "PROSPECTIVE_CURRENT_TAPE_ONLY"
    assert row["prediction_truth_claimed"] is False
    assert row["requires_targeted_hot_preflight"] is True
    assert row["requires_authenticated_own_order_truth"] is True
    assert row["exchange_submit_called"] is False


def test_targeted_trade_refresh_recovers_tape_hidden_by_global_page() -> None:
    module = _module()
    observed_at = datetime(2026, 8, 28, 6, tzinfo=timezone.utc)

    class Client:
        def fetch_recent_taker_trades(self, *, condition_id: str, limit: int):
            assert condition_id == "condition-1"
            assert limit == 500
            return (
                {
                    "asset": "asset-1",
                    "side": "SELL",
                    "price": "0.11",
                    "size": "7.5",
                    "timestamp": int(observed_at.timestamp()) - 10,
                    "transactionHash": "0xtape",
                },
            )

    rows, report = module._refresh_candidates_with_targeted_public_trades(
        [
            {
                "asset_id": "asset-1",
                "condition_id": "condition-1",
                "recent_public_compatible_trade_count": 0,
            }
        ],
        client=Client(),
        max_lookups=1,
        observed_at=observed_at,
        lookback_seconds=60,
    )

    assert rows[0]["recent_public_trade_count"] == 1
    assert rows[0]["recent_public_trade_is_own_order_truth"] is False
    assert report["condition_lookups"] == 1
    assert report["prediction_truth_claimed"] is False
    assert report["own_order_execution_truth_claimed"] is False
    assert report["exchange_submit_called"] is False


def test_active_live_probe_is_protected_from_watchlist_rotation(tmp_path: Path) -> None:
    module = _module()
    observed_at = datetime(2026, 8, 28, 6, tzinfo=timezone.utc)
    (tmp_path / "run-1.submission.json").write_text(
        '{"run_id":"run-1","asset_id":"asset-live"}\n',
        encoding="utf-8",
    )
    (tmp_path / "run-1.recovery.json").write_text(
        '{"status":"OPEN_ORDER_REQUIRES_EXACT_CANCEL",'
        '"artifact_complete":false,"order_still_open":true}\n',
        encoding="utf-8",
    )

    protected = module._load_protected_probe_assets(
        tmp_path,
        grace_seconds=3600,
        observed_at=observed_at,
    )

    assert protected == {"asset-live"}


def test_recent_completed_probe_is_protected_only_during_grace(tmp_path: Path) -> None:
    module = _module()
    observed_at = datetime(2026, 8, 28, 6, tzinfo=timezone.utc)
    (tmp_path / "run-2.submission.json").write_text(
        '{"run_id":"run-2","asset_id":"asset-complete"}\n',
        encoding="utf-8",
    )
    (tmp_path / "run-2.recovery.json").write_text(
        '{"status":"CALIBRATABLE","artifact_complete":true,'
        '"order_still_open":false,'
        '"completed_at":"2026-08-28T05:30:00+00:00"}\n',
        encoding="utf-8",
    )

    assert module._load_protected_probe_assets(
        tmp_path,
        grace_seconds=3600,
        observed_at=observed_at,
    ) == {"asset-complete"}
    assert (
        module._load_protected_probe_assets(
            tmp_path,
            grace_seconds=60,
            observed_at=observed_at,
        )
        == set()
    )


async def _empty_books():
    return {}
