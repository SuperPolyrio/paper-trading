from quant.paper.health_spool import HealthSpool


def _sample(index: int) -> dict[str, object]:
    return {
        "sampled_at": f"2026-07-29T00:00:0{index}+00:00",
        "websocket_messages": index,
    }


def test_health_spool_acknowledges_only_persisted_prefix(tmp_path) -> None:
    spool = HealthSpool(tmp_path / "health.jsonl")
    spool.append(_sample(1))
    spool.append(_sample(2))

    batch = spool.peek(limit=1)
    assert [row["websocket_messages"] for row in batch.records] == [1]
    spool.append(_sample(3))
    spool.acknowledge(batch.consumed_lines)

    remaining = spool.peek(limit=10)
    assert [row["websocket_messages"] for row in remaining.records] == [2, 3]


def test_health_spool_discards_corrupt_prefix_after_successful_batch(tmp_path) -> None:
    path = tmp_path / "health.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    spool = HealthSpool(path)
    spool.append(_sample(1))

    batch = spool.peek(limit=10)
    assert [row["websocket_messages"] for row in batch.records] == [1]
    assert batch.consumed_lines == 2
    spool.acknowledge(batch.consumed_lines)

    assert spool.peek(limit=10).records == ()
