from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quant.paper import db_migration


class _PrivilegeCursor:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.executed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _query: str) -> None:
        self.executed = True

    def fetchone(self):
        return {"allowed": self.allowed}


class _PrivilegeConnection:
    def __init__(self, allowed: bool) -> None:
        self.test_cursor = _PrivilegeCursor(allowed)

    def cursor(self):
        return self.test_cursor


def test_authority_tables_exclude_registry_and_l2_history() -> None:
    tables = set(db_migration.EXECUTION_TABLES)

    assert "paper_market_registry_tokens" not in tables
    assert "paper_market_lifecycle_events" not in tables
    assert "paper_registry_outbox" not in tables
    assert "paper_lob_subscription_targets" not in tables
    assert "l2_archive_manifest" not in tables
    assert {
        "paper_execution_market_catalog",
        "paper_live_order_intents",
        "paper_execution_profile_decisions",
        "paper_ledger_entries",
        "paper_journal_lines",
        "paper_calibration_pnl_positions",
    } <= tables
    assert "paper_execution_market_catalog" not in db_migration.SNAPSHOT_TABLES


def test_settings_from_prefix_requires_complete_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = "TEST_PAPER_DB"
    for suffix in ("HOST", "PORT", "USER", "PASSWORD", "DATABASE"):
        monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)

    with pytest.raises(ValueError, match="missing TEST_PAPER_DB_HOST"):
        db_migration.settings_from_prefix(prefix)

    values = {
        "HOST": "10.0.0.5",
        "PORT": "5432",
        "USER": "paper",
        "PASSWORD": "secret",
        "DATABASE": "paper_authority",
    }
    for suffix, value in values.items():
        monkeypatch.setenv(f"{prefix}_{suffix}", value)

    settings = db_migration.settings_from_prefix(prefix)
    assert settings.host == "10.0.0.5"
    assert settings.port == 5432
    assert settings.database == "paper_authority"


def test_settings_from_prefix_applies_explicit_route_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = "TEST_PAPER_SOURCE"
    values = {
        "HOST": "127.0.0.1",
        "PORT": "45434",
        "USER": "paper",
        "PASSWORD": "secret",
        "DATABASE": "paper_authority",
    }
    for suffix, value in values.items():
        monkeypatch.setenv(f"{prefix}_{suffix}", value)

    settings = db_migration.settings_from_prefix(
        prefix,
        host_override="10.148.0.2",
        port_override=45433,
    )

    assert settings.host == "10.148.0.2"
    assert settings.port == 45433
    assert settings.user == "paper"
    assert settings.password == "secret"


def test_catalog_source_query_reconnects_after_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, params) -> None:
            assert query == "SELECT asset_id"
            assert params == ("scope",)

        def fetchall(self):
            return [{"asset_id": "123"}]

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def connection(_settings, *, readonly):
        assert readonly is True
        calls.append(True)
        if len(calls) == 1:
            raise db_migration.OperationalError("tunnel closed")
        yield Connection()

    monkeypatch.setattr(db_migration, "postgres_connection", connection)
    monkeypatch.setattr(db_migration.time, "sleep", lambda _seconds: None)

    rows, attempts = db_migration._fetch_catalog_source_rows(
        object(),
        "SELECT asset_id",
        ("scope",),
    )

    assert rows == [{"asset_id": "123"}]
    assert attempts == 2
    assert len(calls) == 2


def test_canonical_normalizes_database_values() -> None:
    value = {
        "decimal": Decimal("1.2300"),
        "timestamp": datetime(2026, 8, 6, 4, 0, tzinfo=timezone.utc),
        "payload": {"b": 2, "a": 1},
        "bytes": b"\x00\xff",
    }

    assert db_migration._canonical(value) == {
        "bytes": "00ff",
        "decimal": "1.2300",
        "payload": {"a": 1, "b": 2},
        "timestamp": "2026-08-06T04:00:00+00:00",
    }


def test_migration_checksum_is_stable_and_versioned() -> None:
    first = db_migration._migration_checksum()
    second = db_migration._migration_checksum()

    assert first == second
    assert len(first) == 64
    assert db_migration.MIGRATION_VERSION.startswith("0029-")
    assert "paper_fee_schedules" in db_migration.SNAPSHOT_TABLES
    assert "paper_fill_fee_charges" in db_migration.CORE_PARITY_TABLES
    assert "paper_reward_schedules" in db_migration.SNAPSHOT_TABLES
    assert "paper_reward_accruals" in db_migration.CORE_PARITY_TABLES
    assert "paper_reward_payouts" in db_migration.CORE_PARITY_TABLES
    assert "paper_reward_reconciliations" in db_migration.CORE_PARITY_TABLES
    assert "paper_reward_aggregate_reconciliations" in db_migration.CORE_PARITY_TABLES
    assert "paper_official_reward_source_checks" in db_migration.CORE_PARITY_TABLES
    assert "paper_reward_clawbacks" in db_migration.CORE_PARITY_TABLES
    assert "paper_account_economic_events" in db_migration.CORE_PARITY_TABLES
    assert "paper_maker_rebate_fill_equivalents" in db_migration.CORE_PARITY_TABLES
    assert "paper_maker_rebate_daily_estimates" in db_migration.CORE_PARITY_TABLES
    assert "paper_taker_weighted_volume_events" in db_migration.CORE_PARITY_TABLES
    assert "paper_taker_tier_snapshots" in db_migration.CORE_PARITY_TABLES
    assert "paper_taker_rebate_daily_estimates" in db_migration.CORE_PARITY_TABLES
    assert "paper_liquidity_reward_order_samples" in db_migration.CORE_PARITY_TABLES
    assert "paper_liquidity_reward_sample_scores" in db_migration.CORE_PARITY_TABLES
    assert "paper_liquidity_reward_epoch_estimates" in db_migration.CORE_PARITY_TABLES
    assert "paper_holding_reward_position_samples" in db_migration.CORE_PARITY_TABLES
    assert "paper_holding_reward_daily_estimates" in db_migration.CORE_PARITY_TABLES
    assert "paper_referral_fee_evidence" in db_migration.CORE_PARITY_TABLES
    assert "paper_referral_reward_daily_estimates" in db_migration.CORE_PARITY_TABLES
    assert "paper_account_return_reports" in db_migration.CORE_PARITY_TABLES
    assert "simulator_combo_command_attempts" in db_migration.CORE_PARITY_TABLES
    assert "simulator_combo_positions" in db_migration.CORE_PARITY_TABLES
    assert "simulator_dispute_cases" in db_migration.CORE_PARITY_TABLES
    assert "simulator_venue_migration_replays" in db_migration.CORE_PARITY_TABLES
    assert "simulator_integrity_cases" in db_migration.CORE_PARITY_TABLES


def test_tenant_snapshot_requires_explicit_rls_bypass_identity() -> None:
    denied = _PrivilegeConnection(False)
    with pytest.raises(RuntimeError, match="source migration identity"):
        db_migration._require_tenant_migration_privilege(
            denied,
            ["paper_tenants", "paper_accounts"],
            endpoint="source",
        )
    assert denied.test_cursor.executed is True

    allowed = _PrivilegeConnection(True)
    db_migration._require_tenant_migration_privilege(
        allowed,
        ["paper_tenants"],
        endpoint="target",
    )


def test_non_tenant_snapshot_does_not_require_rls_bypass() -> None:
    denied = _PrivilegeConnection(False)
    db_migration._require_tenant_migration_privilege(
        denied,
        ["paper_accounts", "paper_positions"],
        endpoint="source",
    )
    assert denied.test_cursor.executed is False


def test_execution_hot_path_does_not_read_registry_or_core_market_tables() -> None:
    root = Path(__file__).resolve().parents[2]
    sources = (
        root / "quant/paper/paper_ledger.py",
        root / "quant/risk/event_risk/context_loader.py",
    )

    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert "paper_market_registry_tokens" not in text
        assert "paper_market_registry_markets" not in text
        assert "core.markets" not in text


def test_catalog_sync_preserves_target_authority_assets() -> None:
    for table in (
        "paper_live_watchlist",
        "paper_live_order_intents",
        "paper_positions",
        "paper_calibration_pnl_positions",
    ):
        assert table in db_migration.TARGET_CATALOG_PROTECTED_ASSETS_QUERY
        assert table in db_migration.CATALOG_DELETE_UNREFERENCED_QUERY

    assert "enabled=TRUE" in db_migration.CATALOG_DELETE_UNREFERENCED_QUERY
    assert "QUEUED" in db_migration.CATALOG_DELETE_UNREFERENCED_QUERY
    assert "quantity <> 0" in db_migration.CATALOG_DELETE_UNREFERENCED_QUERY


@pytest.mark.parametrize(
    ("host", "expected"),
    (("10.148.0.5", True), ("127.0.0.1", False), ("8.8.8.8", False)),
)
def test_private_route_rejects_loopback_and_public_hosts(
    host: str, expected: bool
) -> None:
    private, addresses = db_migration._private_route(host)

    assert private is expected
    assert addresses == [host]
