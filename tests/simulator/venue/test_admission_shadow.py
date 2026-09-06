from decimal import Decimal

from quant.simulator.venue import (
    CommandState,
    CommandType,
    GatewayCommand,
    GatewayConfig,
    GatewayLatencyModel,
    HeartbeatConfig,
    RateLimitConfig,
    ReconciliationOutcome,
    VenueAdmissionRequest,
    VenueAdmissionShadow,
    VenueGateway,
    VenueMode,
)


class _Sink:
    def __init__(self) -> None:
        self.events: dict[str, object] = {}
        self.commands: dict[str, object] = {}
        self.run_heartbeats: list[tuple[str, int, str]] = []

    def persist_event(self, event, *, run_id: str) -> bool:
        key = f"{run_id}:{event.event_id}"
        inserted = key not in self.events
        self.events[key] = event
        return inserted

    def persist_inflight(self, item, **_kwargs) -> None:
        self.commands[item.command.command_id] = item

    def persist_shadow_run_heartbeat(
        self,
        *,
        run_id: str,
        heartbeat_ts_ns: int,
        status: str,
    ) -> None:
        self.run_heartbeats.append((run_id, heartbeat_ts_ns, status))


class _ReleaseSink:
    def __init__(self) -> None:
        self.event_keys: set[str] = set()
        self.calls: list[dict[str, object]] = []
        self.reconciliations: list[tuple[str, int]] = []

    def release_order_reservation_from_command(self, intent_id: int, **kwargs):
        self.calls.append({"intent_id": intent_id, **kwargs})
        key = str(kwargs["release_event_key"])
        if key in self.event_keys:
            return {"application_status": "ALREADY_APPLIED"}
        self.event_keys.add(key)
        return {"application_status": "APPLIED"}

    def reconcile_stale_shadow_reservations(
        self,
        *,
        current_run_id: str,
        timeout_ns: int,
    ) -> int:
        self.reconciliations.append((current_run_id, timeout_ns))
        return 2


def _request(suffix: str, *, created_ts_ns: int = 0) -> VenueAdmissionRequest:
    return VenueAdmissionRequest(
        intent_id=suffix,
        account_id="paper-account",
        signer_id="paper-signer",
        ip_id="paper-ip",
        created_ts_ns=created_ts_ns,
        client_order_id=f"client:{suffix}",
        paper_admitted=True,
        side="BUY",
        time_in_force="FOK",
        asset_id="asset",
        limit_price="0.5",
        size="1",
    )


def test_admission_shadow_persists_lifecycle_and_comparison_without_live_submit() -> None:
    sink = _Sink()
    shadow = VenueAdmissionShadow(run_id="run-1", artifact_sink=sink)

    result = shadow.evaluate(_request("one"))

    assert result.gateway_accepted_now is True
    assert result.agreement is True
    assert result.lifecycle_events_persisted > 0
    assert result.comparison_event_persisted is True
    assert result.live_submission_performed is False
    assert list(sink.commands) == ["paper-shadow:one"]
    comparison = [
        event
        for event in sink.events.values()
        if event.model_version == VenueAdmissionShadow.MODEL_VERSION
    ]
    assert len(comparison) == 1
    assert comparison[0].payload["live_submission_performed"] is False
    assert sink.run_heartbeats == [("run-1", 0, "RUNNING")]
    shadow.mark_stopped(now_ts_ns=1)
    assert sink.run_heartbeats[-1] == ("run-1", 1, "STOPPED")


def test_running_heartbeat_persistence_is_rate_limited_but_stop_is_forced() -> None:
    sink = _Sink()
    shadow = VenueAdmissionShadow(run_id="run-heartbeat-rate", artifact_sink=sink)

    shadow.pulse(now_ts_ns=0)
    shadow.pulse(now_ts_ns=1_000_000_000)
    shadow.pulse(now_ts_ns=5_000_000_000)
    shadow.mark_stopped(now_ts_ns=5_000_000_001)

    assert sink.run_heartbeats == [
        ("run-heartbeat-rate", 0, "RUNNING"),
        ("run-heartbeat-rate", 5_000_000_000, "RUNNING"),
        ("run-heartbeat-rate", 5_000_000_001, "STOPPED"),
    ]


def test_long_lived_shadow_carries_dual_rate_limit_state_across_intents() -> None:
    gateway = VenueGateway(
        GatewayConfig(
            rate_limits=RateLimitConfig(
                ip_endpoint_capacity=Decimal(1),
                ip_endpoint_refill_per_second=Decimal(1),
                signer_trading_capacity=Decimal(1),
                signer_trading_refill_per_second=Decimal(1),
            )
        )
    )
    shadow = VenueAdmissionShadow(run_id="run-rate", gateway=gateway)

    first = shadow.evaluate(_request("first"))
    second = shadow.evaluate(_request("second"))
    later = shadow.evaluate(_request("later", created_ts_ns=2_000_000_000))

    assert first.gateway_accepted_now is True
    assert second.gateway_queued is True
    assert second.agreement is False
    assert second.reason in {"ip_endpoint:throttled", "signer:throttled"}
    assert later.gateway_accepted_now is True


def test_late_live_intent_uses_gateway_watermark_without_losing_causal_time() -> None:
    sink = _Sink()
    shadow = VenueAdmissionShadow(run_id="run-late", artifact_sink=sink)
    shadow.pulse(now_ts_ns=2_000_000_000)

    result = shadow.evaluate(_request("late", created_ts_ns=1_000_000_000))

    assert result.gateway_accepted_now is True
    record = shadow.gateway.command("paper-shadow:late")
    assert record.command.created_ts_ns == 2_000_000_000
    assert record.command.payload["requested_created_ts_ns"] == 1_000_000_000
    assert record.command.payload["effective_created_ts_ns"] == 2_000_000_000
    assert record.command.payload["late_by_ns"] == 1_000_000_000
    comparison = [
        event
        for event in sink.events.values()
        if event.model_version == VenueAdmissionShadow.MODEL_VERSION
    ][0]
    assert comparison.payload["late_by_ns"] == 1_000_000_000


def test_queued_admission_refreshes_to_accepted_without_resubmission() -> None:
    gateway = VenueGateway(
        GatewayConfig(
            rate_limits=RateLimitConfig(
                ip_endpoint_capacity=Decimal(1),
                ip_endpoint_refill_per_second=Decimal(1),
                signer_trading_capacity=Decimal(1),
                signer_trading_refill_per_second=Decimal(1),
            )
        )
    )
    shadow = VenueAdmissionShadow(run_id="run-queued-refresh", gateway=gateway)
    shadow.evaluate(_request("first"))
    queued = shadow.evaluate(_request("queued"))

    admitted = shadow.evaluate(
        _request("queued", created_ts_ns=2_000_000_000)
    )

    assert queued.gateway_queued is True
    assert admitted.gateway_queued is False
    assert admitted.gateway_accepted_now is True
    assert admitted.reason == "accepted_after_queue"
    assert len(gateway.commands) == 2


def test_admission_shadow_observes_maintenance_denial_without_enforcement() -> None:
    gateway = VenueGateway()
    gateway.venue.set_mode(VenueMode.CANCEL_ONLY)
    result = VenueAdmissionShadow(
        run_id="run-maintenance",
        gateway=gateway,
    ).evaluate(_request("denied"))

    assert result.gateway_terminally_denied is True
    assert result.gateway_accepted_now is False
    assert result.paper_admitted is True
    assert result.agreement is False
    assert result.reason == "venue_cancel_only"
    assert result.live_submission_performed is False


def test_duplicate_intent_is_idempotent_in_gateway_and_artifact_sink() -> None:
    sink = _Sink()
    shadow = VenueAdmissionShadow(run_id="run-idempotent", artifact_sink=sink)
    request = _request("same")

    first = shadow.evaluate(request)
    event_count = len(sink.events)
    second = shadow.evaluate(request)

    assert first.command_id == second.command_id
    assert second == first
    assert len(sink.events) == event_count


def test_unknown_submit_is_persisted_and_reconciled_without_retry() -> None:
    sink = _Sink()
    gateway = VenueGateway(
        GatewayConfig(latency=GatewayLatencyModel(response_ns=100))
    )
    shadow = VenueAdmissionShadow(
        run_id="run-unknown",
        gateway=gateway,
        artifact_sink=sink,
    )
    shadow.evaluate(_request("unknown"))

    unknown = shadow.mark_outcome_unknown("unknown", now_ts_ns=1)
    reconciled = shadow.reconcile(
        "unknown",
        ReconciliationOutcome.ACCEPTED_LIVE,
        now_ts_ns=2,
    )

    assert unknown.command_state == CommandState.SUBMIT_OUTCOME_UNKNOWN.value
    assert reconciled.command_state == CommandState.ACKED_LIVE.value
    assert len(gateway.commands) == 1
    assert len(gateway.working_orders) == 1
    states = [transition.state for transition in gateway.lifecycle]
    assert states.count(CommandState.SUBMIT_OUTCOME_UNKNOWN) == 1
    assert states.count(CommandState.RECONCILING) == 1


def test_shadow_observes_cancel_only_cancel_without_live_submission() -> None:
    sink = _Sink()
    gateway = VenueGateway()
    shadow = VenueAdmissionShadow(
        run_id="run-cancel-only",
        gateway=gateway,
        artifact_sink=sink,
    )
    shadow.evaluate(_request("working"))
    gateway.venue.set_mode(VenueMode.CANCEL_ONLY)
    cancel = GatewayCommand.build(
        command_type=CommandType.CANCEL,
        command_id="paper-shadow-cancel:working",
        account_id="paper-account",
        signer_id="paper-signer",
        ip_id="paper-ip",
        endpoint="/order",
        created_ts_ns=1,
        order_id="client:working",
    )

    result = shadow.observe_gateway_command(cancel)

    assert result.disposition == "ACCEPTED_IMMEDIATELY"
    assert result.command_state == CommandState.TERMINAL.value
    assert result.venue_mode == VenueMode.CANCEL_ONLY.value
    assert result.live_submission_performed is False
    assert gateway.working_orders == ()
    assert gateway.command("paper-shadow:working").state is CommandState.TERMINAL


def test_heartbeat_pulse_atomically_releases_reservation_once() -> None:
    gateway = VenueGateway(
        GatewayConfig(
            heartbeat=HeartbeatConfig(required=True, timeout_ns=10)
        )
    )
    releases = _ReleaseSink()
    shadow = VenueAdmissionShadow(
        run_id="run-heartbeat",
        gateway=gateway,
        reservation_release_sink=releases,
    )
    shadow.evaluate(_request("17"))

    pulse = shadow.pulse(now_ts_ns=11)

    assert pulse.heartbeat_auto_cancels == 1
    assert pulse.reservation_release_events == 1
    assert releases.calls[0]["intent_id"] == 17
    assert releases.calls[0]["reason"] == "HEARTBEAT_AUTO_CANCEL"
    assert gateway.working_orders == ()

    _persisted, duplicate_releases, duplicate_auto_cancels = shadow._persist_lifecycle(
        0,
        venue_mode="NORMAL",
    )
    assert duplicate_auto_cancels == 1
    assert duplicate_releases == 0
    assert len(releases.event_keys) == 1

    assert shadow.reconcile_stale_reservations() == 2
    assert releases.reconciliations == [("run-heartbeat", 10)]
