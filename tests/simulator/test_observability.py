from quant.simulator.observability import (
    DegradationController,
    DegradationScope,
    DegradationTransition,
    SimulatorMetricsInput,
    TokenDataState,
    build_simulator_metrics,
)


def test_metrics_expose_all_required_simulator_slos() -> None:
    metrics = build_simulator_metrics(
        SimulatorMetricsInput(
            event_queue_ages_ms=(1, 10, 100),
            inflight_command_ages_ms=(20, 300),
            rate_limit_throttle_count=2,
            fill_void_count=1,
        )
    )
    assert metrics["sim_event_queue_age_p99_ms"] == 100
    assert metrics["inflight_command_count"] == 2
    assert metrics["rate_limit_throttle_count"] == 2
    assert metrics["fill_void_count"] == 1


def test_token_failure_does_not_degrade_other_tokens() -> None:
    controller = DegradationController()
    controller.apply(
        scope=DegradationScope.TOKEN,
        identifier="asset-a",
        signal="BOOK_GAP",
    )
    assert controller.state_for(DegradationScope.TOKEN, "asset-a") == "DATA_UNSAFE"
    assert (
        controller.state_for(DegradationScope.TOKEN, "asset-b")
        == TokenDataState.REDUNDANT.value
    )
    assert controller.snapshot()["global_kill_switch_recommended"] is False


def test_account_reconciliation_fault_requests_global_kill_switch() -> None:
    controller = DegradationController()
    transition = controller.apply(
        scope=DegradationScope.ACCOUNT,
        identifier="paper-account-a",
        signal="UNKNOWN_LIVE_ORDER",
    )
    assert transition.requires_global_kill_switch is True
    assert controller.snapshot()["global_kill_switch_recommended"] is True


def test_model_drift_is_scoped_to_one_model_and_recovers_only_after_recalibration() -> (
    None
):
    controller = DegradationController()
    controller.apply(
        scope=DegradationScope.MODEL,
        identifier="taker-v1",
        signal="DRIFT_DETECTED",
    )
    assert controller.state_for(DegradationScope.MODEL, "taker-v1") == "MODEL_STALE"
    controller.apply(
        scope=DegradationScope.MODEL,
        identifier="taker-v1",
        signal="RECALIBRATED",
    )
    assert controller.state_for(DegradationScope.MODEL, "taker-v1") == "MODEL_VALID"


def test_admission_is_scoped_and_single_feed_remains_available() -> None:
    controller = DegradationController()
    controller.apply(
        scope=DegradationScope.TOKEN,
        identifier="unsafe-token",
        signal="BOOK_GAP",
    )
    controller.apply(
        scope=DegradationScope.TOKEN,
        identifier="single-feed-token",
        signal="PRIMARY_FEED_LOST",
    )

    assert (
        controller.admission_reasons(
            token_id="healthy-token",
            model_id="model-v1",
            account_id="account-1",
        )
        == ()
    )
    assert (
        controller.admission_reasons(
            token_id="single-feed-token",
            model_id="model-v1",
            account_id="account-1",
        )
        == ()
    )
    assert controller.admission_reasons(
        token_id="unsafe-token",
        model_id="model-v1",
        account_id="account-1",
    ) == ("token:unsafe-token:DATA_UNSAFE",)


def test_model_and_account_degradation_block_every_matching_command() -> None:
    controller = DegradationController()
    controller.apply(
        scope=DegradationScope.MODEL,
        identifier="model-v1",
        signal="DRIFT_DETECTED",
    )
    controller.apply(
        scope=DegradationScope.ACCOUNT,
        identifier="account-1",
        signal="LEDGER_MISMATCH",
    )

    assert controller.admission_reasons(
        token_id="healthy-token",
        model_id="model-v1",
        account_id="account-1",
    ) == (
        "model:model-v1:MODEL_STALE",
        "account:account-1:RECONCILIATION_REQUIRED",
    )


def test_restore_recovers_state_without_fabricating_a_transition() -> None:
    controller = DegradationController()
    controller.restore(
        DegradationTransition(
            scope=DegradationScope.MODEL,
            identifier="model-v1",
            previous_state="MODEL_VALID",
            state="MODEL_DISABLED",
            signal="CALIBRATION_INVALID",
            requires_global_kill_switch=False,
        )
    )

    assert controller.state_for(DegradationScope.MODEL, "model-v1") == (
        "MODEL_DISABLED"
    )
    assert controller.transitions == ()
