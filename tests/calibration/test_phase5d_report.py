from quant.calibration.phase5d_report import build_phase5d_report


def _probe(
    probe_id: str,
    *,
    domain: str = "politics",
    side: str = "BUY",
    order_type: str = "FOK",
    itode: bool = False,
) -> dict:
    return {
        "probe_id": probe_id,
        "probe_state": "CALIBRATABLE",
        "exchange_submit_called": True,
        "market_id": probe_id,
        "condition_id": f"condition-{probe_id}",
        "side": side,
        "order_type": order_type,
        "amount": "1",
        "amount_unit": "QUOTE" if side == "BUY" else "BASE",
        "decision_ts": "2026-07-27T00:00:00Z",
        "artifact_bitmap": {"complete": True},
        "prediction": {
            "requested_amount": "1",
            "amount_unit": "QUOTE" if side == "BUY" else "SHARES",
            "signed_worst_price": "0.50" if side == "BUY" else "0.49",
        },
        "market_snapshot": {
            "source_category": domain,
            "market_title": f"{domain} market",
            "best_bid": "0.49",
            "best_ask": "0.50",
            "tick_size": "0.01",
            "activity_event_count": 20,
            "fee_rate": "0.05",
            "itode": itode,
            "bids": [["0.49", "100"]],
            "asks": [["0.50", "100"]],
        },
        "reconciliation": {
            "predicted_class": "FULL",
            "actual_class": "FULL",
            "order_type": order_type,
            "price_error_ticks": "0",
            "filled_size_relative_error": "0",
            "fee_error": "0",
            "depth_survival_ratio": "1",
            "pnl": {
                "status": "PASS",
                "pnl_reconciled": True,
                "difference": {"total_pnl_error": "0"},
            },
        },
        "timestamps": {},
    }


def test_phase5d_report_is_collecting_and_surfaces_missing_buckets() -> None:
    report = build_phase5d_report([_probe("1")])

    assert report["status"] == "COLLECTING"
    assert report["submitted_probe_count"] == 1
    assert report["remaining_to_minimum"] == 199
    assert report["stratification"]["market_domain"]["counts"]["politics"] == 1
    assert "market_domain:weather" in report["missing_buckets"]
    assert report["exchange_order_submitted"] is False


def test_phase5d_report_deduplicates_and_ignores_no_submit_rows() -> None:
    submitted = _probe("1", domain="sports", side="SELL", order_type="FAK")
    no_submit = {**_probe("2"), "exchange_submit_called": False}

    report = build_phase5d_report([submitted, submitted, no_submit])

    assert report["submitted_probe_count"] == 1
    assert report["stratification"]["side"]["counts"] == {"SELL": 1}
    assert report["stratification"]["delay_cohort"]["counts"] == {
        "SPORTS_DELAY": 1
    }


def test_itod_is_kept_separate_from_baseline() -> None:
    report = build_phase5d_report([_probe("1", domain="crypto", itode=True)])

    assert report["stratification"]["delay_cohort"]["counts"] == {
        "ITOD_250MS": 1
    }


def test_depth_ratio_only_counts_levels_executable_at_signed_worst_price() -> None:
    probe = _probe("1")
    probe["market_snapshot"]["asks"] = [
        ["0.50", "2"],
        ["0.60", "100"],
    ]

    report = build_phase5d_report([probe])

    assert report["stratification"]["depth_ratio"]["counts"] == {
        "0.90-1.10": 1
    }


def test_depth_ratio_uses_signed_normalized_amount() -> None:
    probe = _probe("1")
    probe["amount"] = "1.10"
    probe["prediction"]["requested_amount"] = "1.00"
    probe["market_snapshot"]["asks"] = [["0.50", "2"]]

    report = build_phase5d_report([probe])

    assert report["stratification"]["depth_ratio"]["counts"] == {
        "0.90-1.10": 1
    }


def test_depth_ratio_is_unknown_without_signed_price() -> None:
    probe = _probe("1")
    probe["prediction"].pop("signed_worst_price")

    report = build_phase5d_report([probe])

    assert report["stratification"]["depth_ratio"]["counts"] == {"unknown": 1}
