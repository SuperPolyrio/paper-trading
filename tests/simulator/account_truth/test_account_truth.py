from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from quant.simulator.account_truth import (
    AccountingEquity,
    AccountingPosition,
    AccountingSnapshotParser,
    AccountTruthGateStatus,
    AccountTruthReconciler,
    ClosedPosition,
    MismatchType,
    OfficialAccountBundle,
    OfficialAccountClient,
    OfficialAccountFetchError,
    OfficialPosition,
    PaperAccountSnapshot,
    PaperPosition,
    ParsedAccountingSnapshot,
)
from quant.simulator.account_truth.report import (
    combined_gate_status,
    load_execution_gates,
    write_account_truth_report,
)

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
ACCOUNT = "0x1111111111111111111111111111111111111111"


def _zip(
    positions: str = (
        "conditionId,asset,size,curPrice,valuationTime\n"
        "0xcondition,asset-1,2,0.45,2026-08-20T08:00:00Z\n"
    ),
    equity: str = (
        "cashBalance,positionsValue,equity,valuationTime\n"
        "9.18,0.9,10.08,2026-08-20T08:00:00Z\n"
    ),
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("positions.csv", positions)
        archive.writestr("equity.csv", equity)
    return buffer.getvalue()


def test_accounting_snapshot_parser_uses_decimal_and_authoritative_time() -> None:
    parsed = AccountingSnapshotParser().parse(_zip())

    assert parsed.source_as_of == NOW
    assert parsed.positions[0].size == Decimal(2)
    assert parsed.equity.equity == Decimal("10.08")
    assert len(parsed.zip_sha256) == 64


def test_accounting_snapshot_parser_rejects_path_traversal() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../positions.csv", "x")
        archive.writestr("equity.csv", "x")

    with pytest.raises(ValueError, match="unsafe path"):
        AccountingSnapshotParser().parse(buffer.getvalue())


def test_accounting_snapshot_parser_rejects_missing_schema() -> None:
    with pytest.raises(ValueError, match="positions.csv missing columns"):
        AccountingSnapshotParser().parse(_zip(positions="asset,size\na,1\n"))


class _Response:
    def __init__(self, payload, *, content_type="application/json", status_code=200):
        self.content = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self):
        self.trust_env = True
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        offset = int(kwargs.get("params", {}).get("offset", 0))
        if url.endswith("/positions"):
            rows = [
                {"asset": f"asset-{index}", "conditionId": "0xcondition"}
                for index in range(3)
            ]
            limit = int(kwargs["params"]["limit"])
            return _Response(rows[offset : offset + limit])
        if url.endswith("/closed-positions"):
            return _Response([])
        return _Response(_zip(), content_type="application/zip")


def test_official_client_paginates_and_uses_explicit_proxy() -> None:
    session = _Session()
    client = OfficialAccountClient(
        session=session,
        proxy_url="http://127.0.0.1:1234",
    )

    result, rows = client.fetch_positions(account_address=ACCOUNT, page_size=2)

    assert len(rows) == 3
    assert json.loads(result.content) == sorted(
        json.loads(result.content), key=lambda row: row["asset"]
    )
    assert session.calls[0][1]["proxies"]["https"] == "http://127.0.0.1:1234"
    assert session.calls[1][1]["params"]["offset"] == 2


def test_official_client_exposes_structured_http_failure_evidence() -> None:
    class FailingSession(_Session):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return _Response(
                {"error": "maintenance"},
                status_code=503,
            )

    client = OfficialAccountClient(session=FailingSession())

    with pytest.raises(OfficialAccountFetchError) as raised:
        client.fetch_accounting_snapshot(account_address=ACCOUNT)

    assert raised.value.result.fetch_status == "FAIL"
    assert raised.value.result.error_code == "HTTP_503"
    assert raised.value.result.http_status == 503
    assert len(raised.value.result.payload_hash) == 64


def _official_bundle() -> OfficialAccountBundle:
    position = OfficialPosition(
        account_address=ACCOUNT,
        asset_id="asset-1",
        condition_id="0xcondition",
        size=Decimal(2),
        avg_price=Decimal("0.4"),
        initial_value=Decimal("0.8"),
        gross_initial_value=Decimal("0.82"),
        entry_fees_usdc=Decimal("0.02"),
        current_value=Decimal("0.9"),
        cash_pnl=Decimal("0.1"),
        realized_pnl=Decimal("0.05"),
        current_price=Decimal("0.45"),
        total_bought=Decimal(2),
        redeemable=False,
        mergeable=False,
    )
    accounting = ParsedAccountingSnapshot(
        positions=(
            AccountingPosition(
                condition_id="0xcondition",
                asset_id="asset-1",
                size=Decimal(2),
                current_price=Decimal("0.45"),
                valuation_time=NOW,
            ),
        ),
        equity=AccountingEquity(
            cash_balance=Decimal("9.18"),
            positions_value=Decimal("0.9"),
            equity=Decimal("10.08"),
            valuation_time=NOW,
        ),
        positions_csv_sha256="p" * 64,
        equity_csv_sha256="e" * 64,
        zip_sha256="z" * 64,
    )
    return OfficialAccountBundle(
        run_id="official-run",
        account_address=ACCOUNT,
        observed_at=NOW,
        source_as_of=NOW,
        positions=(position,),
        closed_positions=(),
        accounting=accounting,
        fetch_manifest={},
    )


def _paper_snapshot(*, quantity=Decimal(2), provisional=0) -> PaperAccountSnapshot:
    return PaperAccountSnapshot(
        strategy_ids=("strategy",),
        as_of=NOW,
        initial_cash=Decimal(10),
        cash_balance=Decimal("9.18"),
        realized_pnl=Decimal("0.05"),
        positions={
            "asset-1": PaperPosition(
                asset_id="asset-1",
                condition_id="0xcondition",
                quantity=quantity,
                cost_basis=Decimal("0.82"),
                entry_fees=Decimal("0.02"),
                realized_pnl=Decimal("0.05"),
                current_value=Decimal("0.9"),
                mark_price=Decimal("0.45"),
            )
        },
        nav=Decimal("10.08"),
        provisional_fill_count=provisional,
        ledger_checkpoint="checkpoint",
    )


def test_matching_account_truth_passes() -> None:
    report = AccountTruthReconciler().reconcile(
        official=_official_bundle(), paper=_paper_snapshot()
    )

    assert report.status is AccountTruthGateStatus.PASS
    assert report.official_source_status == "PASS"
    assert report.mismatches == ()
    contract = report.summary["pnl_truth_contract"]
    assert contract["status"] == "PASS"
    assert contract["claim"] == "OFFICIAL_AS_OF_PNL_CURVE_EQUIVALENCE"
    assert all(contract["checks"].values())


def test_exact_open_closed_realized_overlap_is_not_double_counted() -> None:
    official = _official_bundle()
    closed = ClosedPosition(
        account_address=ACCOUNT,
        asset_id="asset-1",
        condition_id="0xcondition",
        avg_price=Decimal("0.4"),
        total_bought=Decimal(2),
        realized_pnl=Decimal("0.05"),
        current_price=Decimal("0.45"),
        closed_at=NOW,
    )

    report = AccountTruthReconciler().reconcile(
        official=replace(official, closed_positions=(closed,)),
        paper=_paper_snapshot(),
    )

    assert report.status is AccountTruthGateStatus.PASS
    assert report.mismatches == ()
    contract = report.summary["pnl_truth_contract"]
    assert contract["checks"]["closed_position_fields_complete"] is True
    assert contract["closed_position_fields_compared"] == ["realized_pnl"]


def test_account_equity_uses_accounting_snapshot_marks() -> None:
    official = _official_bundle()
    accounting_position = replace(
        official.accounting.positions[0], current_price=Decimal("0.5")
    )
    accounting = replace(
        official.accounting,
        positions=(accounting_position,),
        equity=replace(
            official.accounting.equity,
            positions_value=Decimal(1),
            equity=Decimal("10.18"),
        ),
    )

    report = AccountTruthReconciler().reconcile(
        official=replace(official, accounting=accounting),
        paper=_paper_snapshot(),
    )

    assert report.status is AccountTruthGateStatus.PASS_WITH_TIMING_LAG
    assert not any(
        row.comparison_type == "OFFICIAL_VS_PAPER_ACCOUNT"
        and row.field_name == "equity"
        for row in report.mismatches
    )


def test_position_mismatch_and_provisional_finality_are_distinct() -> None:
    report = AccountTruthReconciler().reconcile(
        official=_official_bundle(),
        paper=_paper_snapshot(quantity=Decimal(1), provisional=1),
    )

    assert report.status is AccountTruthGateStatus.FAIL_ACCOUNT_TRUTH
    assert {row.mismatch_type.value for row in report.mismatches} >= {
        "POSITION_MISMATCH",
        "FINALITY_MISMATCH",
    }


def test_pending_convergence_is_retryable_without_hiding_original_type() -> None:
    reconciler = AccountTruthReconciler()
    report = reconciler.reconcile(
        official=_official_bundle(),
        paper=_paper_snapshot(quantity=Decimal(1)),
    )
    original = report.mismatches[0]
    pending = replace(
        original,
        mismatch_type=MismatchType.PENDING_CONVERGENCE,
        severity="WARNING",
        retryable=True,
        evidence={"original_mismatch_type": original.mismatch_type.value},
    )

    classified = reconciler.with_mismatches(report=report, mismatches=(pending,))

    assert classified.status is AccountTruthGateStatus.PASS_WITH_TIMING_LAG
    assert classified.summary["mismatch_type_counts"] == {"PENDING_CONVERGENCE": 1}
    assert classified.summary["pnl_truth_contract"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert (
        classified.summary["pnl_truth_contract"]["checks"]["account_truth_exact"]
        is False
    )
    assert (
        classified.mismatches[0].evidence["original_mismatch_type"]
        == original.mismatch_type.value
    )


def test_official_only_capture_is_insufficient_account_evidence() -> None:
    report = AccountTruthReconciler().reconcile(
        official=_official_bundle(), paper=None, comparison_scope="OFFICIAL_ONLY"
    )

    assert report.status is AccountTruthGateStatus.INSUFFICIENT_EVIDENCE
    assert report.official_source_status == "PASS"
    assert report.summary["pnl_truth_contract"]["status"] == "INSUFFICIENT_EVIDENCE"


def test_official_display_rounding_is_not_a_source_conflict() -> None:
    official = _official_bundle()
    rounded = replace(
        official,
        positions=(replace(official.positions[0], current_value=Decimal("0.90005")),),
    )

    report = AccountTruthReconciler().reconcile(
        official=rounded, paper=None, comparison_scope="OFFICIAL_ONLY"
    )

    assert report.official_source_status == "PASS"
    assert report.mismatches == ()


def test_matching_account_delta_since_baseline_passes() -> None:
    baseline_time = NOW - timedelta(minutes=5)
    baseline = {
        "baseline_id": "baseline-1",
        "source_as_of": baseline_time,
        "baseline_payload": {
            "cash_balance": "10",
            "positions": {},
        },
    }
    paper_before = PaperAccountSnapshot(
        strategy_ids=("strategy",),
        as_of=baseline_time,
        initial_cash=Decimal(10),
        cash_balance=Decimal(10),
        realized_pnl=Decimal(0),
        positions={},
        nav=Decimal(10),
        ledger_checkpoint="before",
    )

    report = AccountTruthReconciler().reconcile_delta(
        official=_official_bundle(),
        baseline=baseline,
        paper_before=paper_before,
        paper_after=_paper_snapshot(),
    )

    assert report.status is AccountTruthGateStatus.PASS
    assert report.summary["compared_delta_asset_count"] == 1
    assert report.mismatches == ()
    assert report.summary["pnl_truth_contract"]["status"] == "PARTIAL_OPERATION_DELTA"
    assert report.summary["pnl_truth_contract"]["claim"] == "OPERATION_DELTA_ONLY"


def test_clean_v2_cohort_delta_proves_normalized_pnl_curve() -> None:
    baseline_time = NOW - timedelta(minutes=5)
    baseline = {
        "baseline_id": "baseline-clean-v2",
        "source_as_of": baseline_time,
        "baseline_payload": {
            "cash_balance": "10",
            "positions": {},
        },
    }
    paper_before = PaperAccountSnapshot(
        strategy_ids=("clean-v2-strategy",),
        as_of=baseline_time,
        initial_cash=Decimal(10),
        cash_balance=Decimal(10),
        realized_pnl=Decimal(0),
        positions={},
        nav=Decimal(10),
        ledger_checkpoint="before",
    )
    paper_after = replace(
        _paper_snapshot(),
        strategy_ids=("clean-v2-strategy",),
    )

    report = AccountTruthReconciler().reconcile_delta(
        official=_official_bundle(),
        baseline=baseline,
        paper_before=paper_before,
        paper_after=paper_after,
        clean_cohort_asset_ids=("asset-1",),
        clean_cohort_cutover_at=datetime(2026, 7, 24, 4, tzinfo=timezone.utc),
    )

    assert report.status is AccountTruthGateStatus.PASS
    assert report.comparison_scope == "CLEAN_V2_COHORT_DELTA"
    assert report.summary["official_cash_delta"] == "-0.82"
    assert report.summary["official_cohort_marked_value"] == "0.90"
    assert report.summary["pnl_truth_contract"]["status"] == "PASS"
    assert (
        report.summary["pnl_truth_contract"]["claim"]
        == "OFFICIAL_POST_V2_COHORT_PNL_CURVE_EQUIVALENCE"
    )
    assert all(report.summary["pnl_truth_contract"]["checks"].values())


def test_clean_v2_closed_position_uses_cash_for_exact_economic_pnl() -> None:
    baseline_time = NOW - timedelta(minutes=5)
    baseline = {
        "baseline_id": "baseline-clean-v2-closed",
        "source_as_of": baseline_time,
        "baseline_payload": {"cash_balance": "10", "positions": {}},
    }
    closed = ClosedPosition(
        account_address=ACCOUNT,
        asset_id="asset-1",
        condition_id="0xcondition",
        avg_price=Decimal("0.4924"),
        total_bought=Decimal(5),
        realized_pnl=Decimal("-0.4728"),
        current_price=Decimal("0.41"),
        closed_at=NOW,
    )
    accounting = ParsedAccountingSnapshot(
        positions=(),
        equity=AccountingEquity(
            cash_balance=Decimal("9.527130"),
            positions_value=Decimal(0),
            equity=Decimal("9.527130"),
            valuation_time=NOW,
        ),
        positions_csv_sha256="p" * 64,
        equity_csv_sha256="e" * 64,
        zip_sha256="z" * 64,
    )
    official = replace(
        _official_bundle(),
        positions=(),
        closed_positions=(closed,),
        accounting=accounting,
    )
    paper_before = PaperAccountSnapshot(
        strategy_ids=("clean-v2-strategy",),
        as_of=baseline_time,
        initial_cash=Decimal(10),
        cash_balance=Decimal(10),
        realized_pnl=Decimal(0),
        positions={},
        nav=Decimal(10),
        ledger_checkpoint="before",
    )
    paper_after = PaperAccountSnapshot(
        strategy_ids=("clean-v2-strategy",),
        as_of=NOW,
        initial_cash=Decimal(10),
        cash_balance=Decimal("9.527130"),
        realized_pnl=Decimal("-0.472870"),
        positions={
            "asset-1": PaperPosition(
                asset_id="asset-1",
                condition_id="0xcondition",
                quantity=Decimal(0),
                cost_basis=Decimal(0),
                entry_fees=Decimal(0),
                realized_pnl=Decimal("-0.472870"),
                current_value=Decimal(0),
                mark_price=Decimal("0.41"),
            )
        },
        nav=Decimal("9.527130"),
        ledger_checkpoint="after",
    )

    report = AccountTruthReconciler().reconcile_delta(
        official=official,
        baseline=baseline,
        paper_before=paper_before,
        paper_after=paper_after,
        clean_cohort_asset_ids=("asset-1",),
        clean_cohort_cutover_at=datetime(2026, 7, 24, 4, tzinfo=timezone.utc),
    )

    assert report.status is AccountTruthGateStatus.PASS
    assert report.summary["official_cohort_realized_pnl_delta"] == "-0.472870"
    assert (
        report.summary["official_cohort_reported_realized_pnl_delta"] == "-0.4728"
    )
    assert report.summary["official_cohort_realized_pnl_source"] == (
        "ACCOUNTING_CASH_DELTA_PLUS_MARKED_VALUE"
    )
    closed_row = next(
        row
        for row in report.comparison_rows
        if row["comparison_type"] == "OFFICIAL_VS_PAPER_CLOSED_POSITION"
    )
    assert closed_row["status"] == "MATCH"
    assert closed_row["tolerance"] == "0.0001"
    assert report.summary["pnl_truth_contract"]["status"] == "PASS"
    assert report.summary["clean_open_position_count"] == 0
    assert report.summary["clean_closed_position_count"] == 1

    classified = AccountTruthReconciler().with_mismatches(
        report=report,
        mismatches=report.mismatches,
    )

    contract = classified.summary["pnl_truth_contract"]
    assert contract["status"] == "PASS"
    assert contract["missing_position_fields"] == []
    assert contract["checks"]["cohort_economic_change_present"] is True


def test_clean_v2_reclassification_accepts_legacy_summary_without_scope_counts() -> None:
    baseline_time = NOW - timedelta(minutes=5)
    baseline = {
        "baseline_id": "baseline-legacy",
        "source_as_of": baseline_time,
        "baseline_payload": {"cash_balance": "10", "positions": {}},
    }
    report = AccountTruthReconciler().reconcile_delta(
        official=_official_bundle(),
        baseline=baseline,
        paper_before=PaperAccountSnapshot(
            strategy_ids=("clean-v2-strategy",),
            as_of=baseline_time,
            initial_cash=Decimal(10),
            cash_balance=Decimal(10),
            realized_pnl=Decimal(0),
            positions={},
            nav=Decimal(10),
        ),
        paper_after=replace(
            _paper_snapshot(), strategy_ids=("clean-v2-strategy",)
        ),
        clean_cohort_asset_ids=("asset-1",),
        clean_cohort_cutover_at=datetime(2026, 7, 24, 4, tzinfo=timezone.utc),
    )
    legacy_summary = dict(report.summary)
    legacy_summary.pop("clean_open_position_count")
    legacy_summary.pop("clean_closed_position_count")

    classified = AccountTruthReconciler().with_mismatches(
        report=replace(report, summary=legacy_summary),
        mismatches=report.mismatches,
    )

    assert classified.status is report.status
    assert "pnl_truth_contract" in classified.summary


def test_clean_v2_cohort_rejects_asset_present_at_baseline() -> None:
    baseline_time = NOW - timedelta(minutes=5)
    baseline = {
        "baseline_id": "baseline-contaminated",
        "source_as_of": baseline_time,
        "baseline_payload": {
            "cash_balance": "10",
            "positions": {"asset-1": {"size": "0", "closed_realized_pnl": "1"}},
        },
    }
    paper_before = PaperAccountSnapshot(
        strategy_ids=("clean-v2-strategy",),
        as_of=baseline_time,
        initial_cash=Decimal(10),
        cash_balance=Decimal(10),
        realized_pnl=Decimal(0),
        positions={},
        nav=Decimal(10),
    )

    with pytest.raises(ValueError, match="existed before baseline"):
        AccountTruthReconciler().reconcile_delta(
            official=_official_bundle(),
            baseline=baseline,
            paper_before=paper_before,
            paper_after=replace(
                _paper_snapshot(), strategy_ids=("clean-v2-strategy",)
            ),
            clean_cohort_asset_ids=("asset-1",),
            clean_cohort_cutover_at=datetime(
                2026, 7, 24, 4, tzinfo=timezone.utc
            ),
        )


def test_clean_v2_cohort_fails_closed_on_out_of_scope_asset_change() -> None:
    baseline_time = NOW - timedelta(minutes=5)
    baseline = {
        "baseline_id": "baseline-isolated",
        "source_as_of": baseline_time,
        "baseline_payload": {"cash_balance": "10", "positions": {}},
    }
    paper_before = PaperAccountSnapshot(
        strategy_ids=("clean-v2-strategy",),
        as_of=baseline_time,
        initial_cash=Decimal(10),
        cash_balance=Decimal(10),
        realized_pnl=Decimal(0),
        positions={},
        nav=Decimal(10),
    )

    report = AccountTruthReconciler().reconcile_delta(
        official=_official_bundle(),
        baseline=baseline,
        paper_before=paper_before,
        paper_after=replace(
            _paper_snapshot(), strategy_ids=("clean-v2-strategy",)
        ),
        clean_cohort_asset_ids=("different-asset",),
        clean_cohort_cutover_at=datetime(2026, 7, 24, 4, tzinfo=timezone.utc),
    )

    assert report.status is AccountTruthGateStatus.FAIL_ACCOUNT_TRUTH
    assert report.summary["out_of_scope_changed_assets"] == ["asset-1"]
    assert report.summary["pnl_truth_contract"]["status"] == "FAIL"


def test_account_delta_detects_quantity_and_cash_mismatch() -> None:
    baseline_time = NOW - timedelta(minutes=5)
    baseline = {
        "baseline_id": "baseline-1",
        "source_as_of": baseline_time,
        "baseline_payload": {
            "cash_balance": "10",
            "positions": {},
        },
    }
    paper_before = PaperAccountSnapshot(
        strategy_ids=("strategy",),
        as_of=baseline_time,
        initial_cash=Decimal(10),
        cash_balance=Decimal(10),
        realized_pnl=Decimal(0),
        positions={},
        nav=Decimal(10),
        ledger_checkpoint="before",
    )
    paper_after = replace(
        _paper_snapshot(quantity=Decimal(1)),
        cash_balance=Decimal("9.50"),
    )

    report = AccountTruthReconciler().reconcile_delta(
        official=_official_bundle(),
        baseline=baseline,
        paper_before=paper_before,
        paper_after=paper_after,
    )

    assert report.status is AccountTruthGateStatus.FAIL_ACCOUNT_TRUTH
    assert {(row.field_name, row.mismatch_type.value) for row in report.mismatches} >= {
        ("size", "POSITION_MISMATCH"),
        ("cash_balance", "CASH_MISMATCH"),
    }


def test_matching_sell_delta_releases_cash_and_cost_basis() -> None:
    baseline_time = NOW - timedelta(minutes=5)
    baseline = {
        "baseline_id": "baseline-sell",
        "source_as_of": baseline_time,
        "baseline_payload": {
            "cash_balance": "9.18",
            "positions": {
                "asset-1": {
                    "size": "2",
                    "gross_initial_value": "0.82",
                    "entry_fees_usdc": "0.02",
                    "open_realized_pnl": "0.05",
                    "closed_realized_pnl": "0",
                }
            },
        },
    }
    official = _official_bundle()
    official_position = replace(
        official.positions[0],
        size=Decimal(1),
        initial_value=Decimal("0.4"),
        gross_initial_value=Decimal("0.41"),
        entry_fees_usdc=Decimal("0.01"),
        current_value=Decimal("0.45"),
        realized_pnl=Decimal("0.12"),
    )
    accounting_position = replace(official.accounting.positions[0], size=Decimal(1))
    accounting = replace(
        official.accounting,
        positions=(accounting_position,),
        equity=replace(
            official.accounting.equity,
            cash_balance=Decimal("9.70"),
            positions_value=Decimal("0.45"),
            equity=Decimal("10.15"),
        ),
    )
    official = replace(official, positions=(official_position,), accounting=accounting)
    paper_before = replace(_paper_snapshot(), as_of=baseline_time)
    paper_after_position = replace(
        _paper_snapshot().positions["asset-1"],
        quantity=Decimal(1),
        cost_basis=Decimal("0.41"),
        entry_fees=Decimal("0.01"),
        realized_pnl=Decimal("0.12"),
        current_value=Decimal("0.45"),
    )
    paper_after = replace(
        _paper_snapshot(),
        cash_balance=Decimal("9.70"),
        realized_pnl=Decimal("0.12"),
        positions={"asset-1": paper_after_position},
        nav=Decimal("10.15"),
    )

    report = AccountTruthReconciler().reconcile_delta(
        official=official,
        baseline=baseline,
        paper_before=paper_before,
        paper_after=paper_after,
    )

    assert report.status is AccountTruthGateStatus.PASS
    assert report.summary["official_cash_delta"] == "0.52"
    assert report.summary["paper_cash_delta"] == "0.52"
    assert report.mismatches == ()


def test_report_keeps_account_and_execution_gates_separate(tmp_path) -> None:
    official = _official_bundle()
    report = AccountTruthReconciler().reconcile(
        official=official, paper=_paper_snapshot()
    )
    taker = tmp_path / "taker.json"
    maker = tmp_path / "maker.json"
    taker.write_text(
        json.dumps({"status": "PASS", "promotion_allowed": True}),
        encoding="utf-8",
    )
    maker.write_text(
        json.dumps({"status": "BLOCKED", "promotion_allowed": False}),
        encoding="utf-8",
    )

    execution = load_execution_gates((taker, maker))
    paths = write_account_truth_report(
        output_root=tmp_path / "reports",
        official=official,
        report=report,
        execution_gate_paths=(taker, maker),
    )

    assert execution["status"] == "FAIL_EXECUTION_FIDELITY"
    assert combined_gate_status(report.status, execution) == "FAIL_EXECUTION_FIDELITY"
    summary = json.loads(Path(paths["summary"]).read_text(encoding="utf-8"))
    assert summary["account_truth_gate"] == "PASS"
    assert summary["combined_gate"] == "FAIL_EXECUTION_FIDELITY"
    assert summary["pnl_truth_contract"]["status"] == "PASS"
    comparison_rows = [
        json.loads(line)
        for line in Path(paths["items"]).read_text(encoding="utf-8").splitlines()
    ]
    assert comparison_rows
    assert all(row["record_type"] == "FIELD_COMPARISON" for row in comparison_rows)
    assert all(row["status"] == "MATCH" for row in comparison_rows)
    account_return = json.loads(
        Path(paths["account_return"]).read_text(encoding="utf-8")
    )
    assert account_return["paper"]["cash_balance"] == "9.18"
    assert account_return["paper"]["nav"] == "10.08"
    assert account_return["comparison_basis"] == "AS_OF_WHOLE_ACCOUNT"
    assert account_return["current_totals_comparable"] is True


def test_official_capture_does_not_overwrite_latest_comparable_report(tmp_path) -> None:
    official = _official_bundle()
    comparable = AccountTruthReconciler().reconcile(
        official=official,
        paper=_paper_snapshot(),
    )
    official_only = AccountTruthReconciler().reconcile(
        official=official,
        paper=None,
        comparison_scope="OFFICIAL_ONLY",
    )
    root = tmp_path / "reports"

    write_account_truth_report(
        output_root=root,
        official=official,
        report=comparable,
    )
    write_account_truth_report(
        output_root=root,
        official=official,
        report=official_only,
    )

    latest = json.loads((root / "latest/reconciliation-summary.json").read_text())
    latest_comparable = json.loads(
        (root / "latest-comparable/reconciliation-summary.json").read_text()
    )
    assert latest["comparison_scope"] == "OFFICIAL_ONLY"
    assert latest_comparable["comparison_scope"] == "WHOLE_ACCOUNT"
    assert latest_comparable["comparison_item_count"] > 0


def test_execution_gate_accepts_complete_run_without_claiming_promotion(
    tmp_path,
) -> None:
    run = tmp_path / "run-reconciliation.json"
    run.write_text(
        json.dumps(
            {
                "schema_version": "calibration_reconciliation_run_v1",
                "status": "PASS",
                "probe_count": 1,
                "calibratable_probe_count": 1,
            }
        ),
        encoding="utf-8",
    )

    execution = load_execution_gates((run,))

    assert execution["status"] == "PASS"
    assert execution["sources"][0]["evidence_scope"] == "RUN_RECONCILIATION"
    assert execution["sources"][0]["gate_passed"] is True
    assert execution["sources"][0]["promotion_allowed"] is None
