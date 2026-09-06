from quant.simulator.shadow_worker import SchedulerOwnedShadowWorker
from quant.simulator.venue.command import CommandType, GatewayCommand
from quant.simulator.venue.gateway import VenueGateway


class _Sink:
    def __init__(self) -> None:
        self.events = []
        self.commands = []

    def persist_event(self, event, *, run_id: str) -> bool:
        self.events.append((run_id, event.event_id))
        return True

    def persist_inflight(self, item, **_kwargs) -> None:
        self.commands.append(item.command.command_id)


def test_shadow_worker_replays_gateway_lifecycle_through_one_journal() -> None:
    gateway = VenueGateway()
    command = GatewayCommand.build(
        command_type=CommandType.SUBMIT,
        command_id="shadow-command",
        account_id="account",
        signer_id="signer",
        endpoint="/order",
        created_ts_ns=0,
    )
    gateway.submit(command)
    gateway.advance(0)
    sink = _Sink()
    result = SchedulerOwnedShadowWorker(sink).replay_gateway(
        run_id="shadow-run-1", gateway=gateway
    )
    assert result.event_count == len(gateway.lifecycle_sim_events())
    assert result.command_count == 1
    assert result.persisted_event_count == result.event_count
    assert sink.commands == ["shadow-command"]
    assert len(sink.events) == result.event_count
    assert result.live_submission_performed is False
