"""Orchestration for official account capture and paper reconciliation."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .models import AccountTruthReport, OfficialAccountBundle
from .normalizer import normalize_closed_position, normalize_position
from .official_account_client import OfficialAccountClient, OfficialAccountFetchError
from .reconciler import AccountTruthReconciler, PaperAccountSnapshotLoader
from .snapshot_parser import AccountingSnapshotParser
from .store import PostgresAccountTruthStore


class OfficialAccountTruthService:
    def __init__(
        self,
        *,
        client: OfficialAccountClient,
        store: PostgresAccountTruthStore,
        parser: AccountingSnapshotParser | None = None,
        snapshot_loader: PaperAccountSnapshotLoader | None = None,
        reconciler: AccountTruthReconciler | None = None,
        convergence_window_seconds: float = 300.0,
    ) -> None:
        self.client = client
        self.store = store
        self.parser = parser or AccountingSnapshotParser()
        self.snapshot_loader = snapshot_loader or PaperAccountSnapshotLoader(
            store.connection_factory
        )
        self.reconciler = reconciler or AccountTruthReconciler()
        self.convergence_window_seconds = max(0.0, float(convergence_window_seconds))

    def capture(self, *, account_address: str) -> OfficialAccountBundle:
        account = str(account_address).strip().lower()
        # The ZIP supplies the authoritative valuationTime. Fetch /positions
        # immediately after it so mutable marks have the smallest possible skew.
        try:
            accounting_fetch = self.client.fetch_accounting_snapshot(
                account_address=account
            )
            positions_fetch, position_rows = self.client.fetch_positions(
                account_address=account
            )
            closed_fetch, closed_rows = self.client.fetch_closed_positions(
                account_address=account
            )
        except OfficialAccountFetchError as exc:
            self.store.persist_fetch_artifact(
                account_address=account,
                result=exc.result,
            )
            raise
        accounting = self.parser.parse(accounting_fetch.content)
        positions = tuple(
            normalize_position(row, account_address=account) for row in position_rows
        )
        closed_positions = tuple(
            normalize_closed_position(row, account_address=account)
            for row in closed_rows
        )
        run_identity = (
            f"{account}\x1f{positions_fetch.payload_hash}\x1f"
            f"{closed_fetch.payload_hash}\x1f{accounting_fetch.payload_hash}"
        )
        run_hash = hashlib.sha256(run_identity.encode()).hexdigest()
        observed_at = max(
            positions_fetch.observed_at,
            closed_fetch.observed_at,
            accounting_fetch.observed_at,
        )
        manifest = {
            "schema_version": "official-account-fetch-manifest-v1",
            "account_address": account,
            "observed_at": observed_at.isoformat(),
            "source_as_of": accounting.source_as_of.isoformat(),
            "positions_payload_hash": positions_fetch.payload_hash,
            "closed_positions_payload_hash": closed_fetch.payload_hash,
            "accounting_zip_sha256": accounting.zip_sha256,
            "positions_csv_sha256": accounting.positions_csv_sha256,
            "equity_csv_sha256": accounting.equity_csv_sha256,
            "position_count": len(positions),
            "closed_position_count": len(closed_positions),
            "accounting_position_count": len(accounting.positions),
        }
        bundle = OfficialAccountBundle(
            run_id=f"official-account-snapshot:{run_hash[:32]}",
            account_address=account,
            observed_at=observed_at,
            source_as_of=accounting.source_as_of,
            positions=positions,
            closed_positions=closed_positions,
            accounting=accounting,
            fetch_manifest=manifest,
        )
        positions_artifact_id = self.store.persist_fetch_artifact(
            account_address=account, result=positions_fetch
        )
        closed_artifact_id = self.store.persist_fetch_artifact(
            account_address=account, result=closed_fetch
        )
        accounting_artifact_id = self.store.persist_fetch_artifact(
            account_address=account, result=accounting_fetch
        )
        self.store.persist_bundle(
            bundle,
            positions_artifact_id=positions_artifact_id,
            closed_artifact_id=closed_artifact_id,
            accounting_artifact_id=accounting_artifact_id,
        )
        return bundle

    def capture_and_reconcile(
        self,
        *,
        account_address: str,
        strategy_ids: Sequence[str] = (),
        comparison_scope: str = "WHOLE_ACCOUNT",
    ) -> tuple[OfficialAccountBundle, AccountTruthReport]:
        official = self.capture(account_address=account_address)
        paper = (
            self.snapshot_loader.load(
                strategy_ids=strategy_ids,
                as_of=official.source_as_of,
            )
            if strategy_ids
            else None
        )
        report = self.reconciler.reconcile(
            official=official,
            paper=paper,
            comparison_scope=comparison_scope,
        )
        report = self._apply_convergence(report)
        self.store.persist_reconciliation(report)
        return official, report

    def create_baseline(
        self,
        *,
        account_address: str,
        scope_id: str,
        strategy_ids: Sequence[str] = (),
    ) -> tuple[OfficialAccountBundle, Mapping[str, Any]]:
        official = self.capture(account_address=account_address)
        baseline = self.store.create_baseline(
            scope_id=scope_id,
            official=official,
            strategy_ids=tuple(strategy_ids),
        )
        return official, baseline

    def capture_and_reconcile_delta(
        self,
        *,
        account_address: str,
        scope_id: str,
        strategy_ids: Sequence[str],
    ) -> tuple[OfficialAccountBundle, AccountTruthReport]:
        strategies = tuple(
            dict.fromkeys(str(item) for item in strategy_ids if str(item))
        )
        if not strategies:
            raise ValueError("delta reconciliation requires at least one strategy_id")
        baseline = self.store.load_baseline(
            scope_id=scope_id,
            account_address=account_address,
        )
        baseline_strategies = tuple(str(item) for item in baseline["strategy_ids"])
        if baseline_strategies and baseline_strategies != strategies:
            raise ValueError(
                "delta reconciliation strategy scope differs from immutable baseline"
            )
        baseline_as_of = baseline["source_as_of"]
        official = self.capture(account_address=account_address)
        paper_before = self.snapshot_loader.load(
            strategy_ids=strategies,
            as_of=baseline_as_of,
        )
        paper_after = self.snapshot_loader.load(
            strategy_ids=strategies,
            as_of=official.source_as_of,
        )
        report = self.reconciler.reconcile_delta(
            official=official,
            baseline=baseline,
            paper_before=paper_before,
            paper_after=paper_after,
        )
        report = self._apply_convergence(report)
        self.store.persist_reconciliation(report)
        return official, report

    def capture_and_reconcile_clean_v2_cohort(
        self,
        *,
        account_address: str,
        scope_id: str,
        strategy_id: str,
        asset_ids: Sequence[str],
        venue_cutover_at: datetime,
    ) -> tuple[OfficialAccountBundle, AccountTruthReport]:
        """Reconcile one isolated post-V2 Paper strategy as a zero-delta cohort."""

        strategy = str(strategy_id).strip()
        assets = tuple(dict.fromkeys(str(item).strip() for item in asset_ids if str(item).strip()))
        if not strategy:
            raise ValueError("clean V2 cohort requires a paper strategy_id")
        if not assets:
            raise ValueError("clean V2 cohort requires at least one asset_id")
        baseline = self.store.load_baseline(
            scope_id=scope_id,
            account_address=account_address,
        )
        baseline_strategies = tuple(str(item) for item in baseline["strategy_ids"])
        if baseline_strategies != (strategy,):
            raise ValueError(
                "clean V2 cohort strategy differs from the immutable baseline"
            )
        official = self.capture(account_address=account_address)
        paper_before = self.snapshot_loader.load(
            strategy_ids=(strategy,),
            as_of=baseline["source_as_of"],
        )
        paper_after = self.snapshot_loader.load(
            strategy_ids=(strategy,),
            as_of=official.source_as_of,
        )
        report = self.reconciler.reconcile_delta(
            official=official,
            baseline=baseline,
            paper_before=paper_before,
            paper_after=paper_after,
            clean_cohort_asset_ids=assets,
            clean_cohort_cutover_at=venue_cutover_at,
        )
        report = self._apply_convergence(report)
        self.store.persist_reconciliation(report)
        return official, report

    def _apply_convergence(self, report: AccountTruthReport) -> AccountTruthReport:
        mismatches = self.store.apply_convergence_policy(
            report,
            convergence_window_seconds=self.convergence_window_seconds,
        )
        return self.reconciler.with_mismatches(
            report=report,
            mismatches=mismatches,
        )
