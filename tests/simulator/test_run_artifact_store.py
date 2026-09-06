from decimal import Decimal

import pytest

from quant.simulator.run_artifact_store import (
    PortfolioScenarioArtifact,
    SIMULATOR_ARTIFACT_SCHEMA,
)


def test_simulator_artifact_schema_is_additive_and_covers_missing_evidence_tables() -> None:
    schema = "\n".join(SIMULATOR_ARTIFACT_SCHEMA)
    for table in (
        "paper_sim_events",
        "paper_inflight_commands",
        "paper_venue_shadow_runs",
        "paper_order_batches",
        "paper_order_batch_children",
        "portfolio_scenario_results",
        "execution_tca",
        "benchmark_episode_runs",
        "simulator_degradation_events",
    ):
        assert f"CREATE TABLE IF NOT EXISTS quant.{table}" in schema
    assert "DROP TABLE" not in schema


def test_portfolio_scenario_artifact_refuses_ambiguous_or_negative_risk_values() -> None:
    with pytest.raises(ValueError, match="scenario_id"):
        PortfolioScenarioArtifact(
            run_id="run", account_id="account", scenario_id="", cash_value=Decimal("1"),
            position_value=None, nav=None, max_loss=None,
        ).validate()
    with pytest.raises(ValueError, match="locked_capital"):
        PortfolioScenarioArtifact(
            run_id="run", account_id="account", scenario_id="scenario", cash_value=Decimal("1"),
            position_value=None, nav=None, max_loss=None, locked_capital=Decimal("-1"),
        ).validate()


def test_portfolio_scenario_artifact_accepts_unmarked_explicitly() -> None:
    artifact = PortfolioScenarioArtifact(
        run_id="run", account_id="account", scenario_id="illiquid", cash_value=Decimal("5"),
        position_value=None, nav=None, max_loss=Decimal("-3"),
        locked_capital=Decimal("2"), unmarked_value=Decimal("4"),
        model_versions={"valuation": "walk-book-v1"},
    )
    artifact.validate()
