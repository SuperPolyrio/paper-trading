from __future__ import annotations

import pytest

from quant.core.db import PostgresSettings
from quant.paper.local_db_e2e import _book_message, validate_disposable_target


def _settings(*, host: str, port: int, database: str) -> PostgresSettings:
    return PostgresSettings(
        host=host,
        port=port,
        user="paper",
        password="test-only",
        database=database,
    )


def test_disposable_target_requires_distinct_loopback_test_database() -> None:
    source = _settings(host="127.0.0.1", port=45432, database="poly_data_core")
    validate_disposable_target(
        source,
        _settings(host="127.0.0.1", port=55433, database="paper_target_e2e"),
    )

    with pytest.raises(ValueError, match="loopback"):
        validate_disposable_target(
            source,
            _settings(host="10.148.0.2", port=55433, database="paper_target_e2e"),
        )
    with pytest.raises(ValueError, match="start with"):
        validate_disposable_target(
            source,
            _settings(host="127.0.0.1", port=55433, database="production"),
        )
    with pytest.raises(ValueError, match="independent"):
        validate_disposable_target(
            source,
            _settings(host="127.0.0.1", port=45432, database="paper_target_e2e"),
        )


def test_local_book_fixture_is_full_two_sided_snapshot() -> None:
    message = _book_message("asset-1")

    assert message["event_type"] == "book"
    assert message["asset_id"] == "asset-1"
    assert message["bids"][0] == {"price": "0.40", "size": "100"}
    assert message["asks"][0] == {"price": "0.60", "size": "100"}
