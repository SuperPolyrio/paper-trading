from decimal import Decimal

from quant.calibration.representative_phase5c import build_phase5c_matrix


def _audit() -> dict:
    rows = []
    for domain in ("politics", "sports", "weather", "crypto"):
        for index in range(3):
            rows.append(
                {
                    "status": "PASS",
                    "issues": [],
                    "category_group": domain,
                    "source_category": domain,
                    "market_id": f"{domain}-{index}",
                    "condition_id": f"condition-{domain}-{index}",
                    "asset_id": f"asset-{domain}-{index}",
                    "market_title": f"{domain} market {index}",
                    "event_slug": f"{domain}-event-{index}",
                    "outcome_name": "YES",
                    "fee_rate_bps": 1000,
                    "tick_size": "0.01",
                    "min_order_size": "5",
                    "seconds_delay": 0,
                    "rest_best_bid": "0.49",
                    "rest_best_ask": "0.50",
                }
            )
    return {"exchange_order_submitted": False, "rows": rows}


def test_phase5c_matrix_splits_delayed_sports_and_caps_spend() -> None:
    matrix = build_phase5c_matrix(_audit(), max_buy_amount_usd=Decimal("1"))

    assert matrix["ready_for_run_preparation"] is True
    assert matrix["candidate_count"] == 24
    assert matrix["selected_market_count"] == 12
    assert matrix["selected_event_count"] == 12
    assert matrix["maximum_total_buy_spend_usd"] == "12"
    assert [batch["planned_order_count"] for batch in matrix["batches"]] == [9, 9, 3, 3]
    assert all(
        order["requires_specific_run_approval"] is False
        for batch in matrix["batches"]
        for order in batch["orders"]
    )
    sell_orders = [
        order
        for batch in matrix["batches"]
        for order in batch["orders"]
        if order["side"] == "SELL"
    ]
    assert len(sell_orders) == 12
    assert all(order["readiness"] == "WAITING_FOR_CONFIRMED_POSITION" for order in sell_orders)
    assert matrix["risk_policy"]["legacy_usd20_campaign_cap_applies"] is False
    assert matrix["exchange_order_submitted"] is False


def test_phase5c_matrix_reports_domain_shortfall() -> None:
    audit = _audit()
    audit["rows"] = [
        row for row in audit["rows"] if row["category_group"] != "weather"
    ]

    matrix = build_phase5c_matrix(audit)

    assert matrix["ready_for_run_preparation"] is False
    assert matrix["missing_domains"] == ["weather"]


def test_no_delay_audit_rows_are_emitted_as_incremental_baseline_batch() -> None:
    audit = _audit()
    audit["rows"] = [audit["rows"][0]]
    audit["rows"][0]["delay_cohort"] = "NO_DELAY"

    matrix = build_phase5c_matrix(audit)

    assert matrix["ready_for_run_preparation"] is False
    assert [batch["batch_id"] for batch in matrix["batches"]] == [
        "baseline-no-delay-buy",
        "baseline-no-delay-sell",
    ]
    assert matrix["batches"][0]["orders"][0]["readiness"] == (
        "READY_AFTER_PREFLIGHT"
    )


def test_phase5c_matrix_rejects_sibling_markets_from_same_event() -> None:
    audit = _audit()
    audit["rows"][1]["event_slug"] = audit["rows"][0]["event_slug"]

    matrix = build_phase5c_matrix(audit)

    assert matrix["domain_counts"]["politics"] == 2
    assert matrix["missing_domains"] == ["politics"]
