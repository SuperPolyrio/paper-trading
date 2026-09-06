from __future__ import annotations

from decimal import Decimal

from quant.calibration.settlement_redeemer import RedeemCandidate
from quant.calibration.settlement_watcher import (
    _negative_risk_amounts,
    run_settlement_watch,
)


def _candidate(outcome: str, size: str) -> RedeemCandidate:
    return RedeemCandidate(
        title="weather",
        outcome=outcome,
        condition_id="0xcondition",
        asset_id="1",
        size=Decimal(size),
        negative_risk=True,
        redeemable=True,
        current_value=Decimal(size),
        current_price=Decimal("1"),
    )


def test_negative_risk_yes_amounts_are_exact_base_units():
    assert _negative_risk_amounts(_candidate("YES", "1.09462")) == (1_094_620, 0)


def test_negative_risk_no_amounts_are_exact_base_units():
    assert _negative_risk_amounts(_candidate("NO", "5")) == (0, 5_000_000)


def test_losing_settlement_is_reconciled_without_relayer_submit() -> None:
    class Store:
        reconciled = False

        def settlement_watch_positions(self, *, account_id):
            return []

        def settle_resolved_pnl_positions(self, *, limit):
            return 0

        def pending_settlements(self, *, account_id):
            if self.reconciled:
                return []
            return [
                {
                    "settlement_key": "settlement-1",
                    "asset_id": "loser",
                    "winning_asset_id": "winner",
                    "expected_real_payout": Decimal("0"),
                }
            ]

        def record_observed_settlement_payout(
            self,
            *,
            settlement_key,
            observed_payout,
            evidence,
        ):
            self.reconciled = True
            return {
                "settlement_key": settlement_key,
                "observed_real_payout": observed_payout,
                "cash_reconciliation_status": "PASS",
                "evidence": evidence,
            }

        def pending_settlements_after(self):
            return []

    store = Store()
    payload = run_settlement_watch(
        store,
        account_id=None,
        redeemer_factory=None,
    )

    assert payload["status"] == "ZERO_PAYOUT_RECONCILED"
    assert payload["pending_redemption_count"] == 0
    assert payload["submit_called"] is False
    assert payload["zero_payout_reconciliations"][0][
        "cash_reconciliation_status"
    ] == "PASS"


def test_idle_settlement_watcher_is_healthy() -> None:
    class Store:
        def settlement_watch_positions(self, *, account_id):
            return []

        def settle_resolved_pnl_positions(self, *, limit):
            return 0

        def pending_settlements(self, *, account_id):
            return []

    payload = run_settlement_watch(
        Store(),
        account_id=None,
        redeemer_factory=None,
    )

    assert payload["status"] == "WATCHING"
    assert payload["pending_redemption_count"] == 0
    assert payload["submit_called"] is False


def test_settlement_discovery_falls_back_to_second_read_route() -> None:
    class Store:
        def settlement_watch_positions(self, *, account_id):
            return []

        def settle_resolved_pnl_positions(self, *, limit):
            return 0

        def pending_settlements(self, *, account_id):
            return []

    class Redeemer:
        def __init__(self, *, error: Exception | None = None):
            self.error = error

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def list_redeemable_positions(self):
            if self.error is not None:
                raise self.error
            return []

    payload = run_settlement_watch(
        Store(),
        account_id=None,
        redeemer_factory=None,
        redeemer_factories=(
            lambda: Redeemer(error=ConnectionError("primary failed")),
            lambda: Redeemer(),
        ),
    )

    assert payload["status"] == "WATCHING"
    assert payload["discovery_error"] is None
    assert [row["status"] for row in payload["discovery_attempts"]] == [
        "FAIL",
        "PASS",
    ]
    assert payload["submit_called"] is False
