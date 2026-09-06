from types import SimpleNamespace

from quant.simulator.venue import (
    BatchChildExecution,
    BatchLedgerFinalizer,
    CommandType,
    DurableBatchExecutor,
    GatewayCommand,
    PaperOrderBatch,
    VenueGateway,
    VenueMode,
)


class _ArtifactSink:
    def __init__(self) -> None:
        self.persisted = []
        self.eligible: set[str] = set()
        self.marked: list[dict[str, object]] = []

    def persist_order_batch(self, batch, result, **kwargs) -> None:
        self.persisted.append((batch, result, kwargs))
        self.eligible = set(result.ledger_eligible_command_ids())

    def batch_ledger_eligible_command_ids(self, *, batch_id, command_ids):
        del batch_id
        return frozenset(self.eligible.intersection(command_ids))

    def mark_batch_child_execution(self, **kwargs) -> None:
        self.marked.append(kwargs)
        if kwargs["ledger_status"] in {"APPLIED", "NO_EFFECT"}:
            self.eligible.discard(str(kwargs["command_id"]))


class _LedgerSink:
    def __init__(self) -> None:
        self.audit_keys: list[str] = []

    def append(self, result) -> None:
        if result.audit_key not in self.audit_keys:
            self.audit_keys.append(result.audit_key)


def _command(name: str, *, post_only: bool) -> GatewayCommand:
    return GatewayCommand.build(
        command_id=f"command:{name}",
        command_type=CommandType.SUBMIT,
        account_id="account:batch",
        signer_id="signer:batch",
        endpoint="/orders",
        created_ts_ns=10,
        order_id=f"order:{name}",
        post_only=post_only,
    )


def _execution(command_id: str, *, status: str = "FILLED", has_fill: bool = True):
    return SimpleNamespace(
        audit_key=f"audit:{command_id}",
        status=status,
        fills=(object(),) if has_fill else (),
        filled_size=1 if has_fill else 0,
        filled_notional="0.5" if has_fill else "0",
        total_fee="0",
    )


def test_durable_batch_keeps_partial_children_independent_and_replay_safe() -> None:
    gateway = VenueGateway()
    gateway.venue.set_mode(VenueMode.POST_ONLY)
    orders = tuple(
        [_command(f"accepted:{index}", post_only=True) for index in range(8)]
        + [_command(f"denied:{index}", post_only=False) for index in range(7)]
    )
    batch = PaperOrderBatch.build("batch:mixed", orders, submitted_ts_ns=10)
    artifacts = _ArtifactSink()
    durable = DurableBatchExecutor(
        gateway=gateway,
        artifact_sink=artifacts,
        strategy_id="strategy:batch",
        config_hash="config:batch",
    )

    submitted = durable.submit(batch, now_ts_ns=15)

    assert submitted.batch.arrival_ts_ns == 15
    assert submitted.result.accepted_count == 8
    assert submitted.result.terminally_denied_count == 7
    assert submitted.result.batch_state == "PARTIAL_RESULT"
    assert len(artifacts.persisted) == 1

    ledger = _LedgerSink()
    finalizer = BatchLedgerFinalizer(
        artifact_sink=artifacts,
        ledger_sink=ledger,
    )
    children = tuple(
        BatchChildExecution(command.command_id, _execution(command.command_id))
        for command in orders
    )

    first = finalizer.finalize(batch_id=batch.batch_id, children=children)
    replay = finalizer.finalize(batch_id=batch.batch_id, children=children)

    assert first.applied_count == 8
    assert first.skipped_count == 7
    assert len(ledger.audit_keys) == 8
    assert replay.applied_count == 0
    assert replay.skipped_count == 15
    assert len(ledger.audit_keys) == 8


def test_admitted_terminal_child_without_fill_is_recorded_without_ledger_effect() -> None:
    command = _command("no-fill", post_only=True)
    batch = PaperOrderBatch.build("batch:no-fill", (command,), submitted_ts_ns=10)
    artifacts = _ArtifactSink()
    submitted = DurableBatchExecutor(
        gateway=VenueGateway(),
        artifact_sink=artifacts,
        strategy_id="strategy:batch",
        config_hash="config:batch",
    ).submit(batch)
    assert submitted.result.accepted_count == 1
    ledger = _LedgerSink()

    report = BatchLedgerFinalizer(
        artifact_sink=artifacts,
        ledger_sink=ledger,
    ).finalize(
        batch_id=batch.batch_id,
        children=(
            BatchChildExecution(
                command.command_id,
                _execution(command.command_id, status="REJECTED", has_fill=False),
            ),
        ),
    )

    assert report.no_effect_count == 1
    assert ledger.audit_keys == []
    assert artifacts.marked[-1]["ledger_status"] == "NO_EFFECT"
