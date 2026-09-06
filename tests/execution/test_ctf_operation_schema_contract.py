from quant.paper import db_migration
from quant.paper.authority import FENCED_TABLES
from quant.paper.paper_ledger import LEDGER_SCHEMA_STATEMENTS
from quant.simulator.complete_set import COMPLETE_SET_SCHEMA_STATEMENTS
from quant.simulator.operations.operation_store import SCHEMA_STATEMENTS


def test_operation_reservation_tables_are_durable_and_migrated() -> None:
    schema = "\n".join(SCHEMA_STATEMENTS)
    tables = set(db_migration.EXECUTION_TABLES)

    assert "simulator_position_operation_reservations" in schema
    assert "simulator_position_operation_token_reservations" in schema
    assert "simulator_position_operation_reservations" in tables
    assert "simulator_position_operation_token_reservations" in tables
    assert (
        "simulator_position_operation_reservations"
        in db_migration.CORE_PARITY_TABLES
    )
    assert "simulator_position_operation_reservations" in FENCED_TABLES
    assert "simulator_position_operations" in FENCED_TABLES


def test_fill_schema_persists_ctf_projection_and_append_only_evidence() -> None:
    schema = "\n".join(LEDGER_SCHEMA_STATEMENTS)

    assert "settlement_match_type" in schema
    assert "settlement_conservation_hash" in schema
    assert "paper_fill_ctf_settlement_audits" in schema
    assert "paper_fill_ctf_settlement_audits" in db_migration.CORE_PARITY_TABLES
    assert "paper_fill_ctf_settlement_audits" in FENCED_TABLES


def test_complete_set_cost_basis_tables_are_migrated_and_fenced() -> None:
    schema = "\n".join(COMPLETE_SET_SCHEMA_STATEMENTS)
    tables = {
        "simulator_complete_set_lots",
        "simulator_complete_set_lot_legs",
        "simulator_complete_set_consumptions",
        "simulator_complete_set_consumption_legs",
    }

    assert all(table in schema for table in tables)
    assert tables <= set(db_migration.EXECUTION_TABLES)
    assert tables <= set(db_migration.CORE_PARITY_TABLES)
    assert tables <= set(FENCED_TABLES)
