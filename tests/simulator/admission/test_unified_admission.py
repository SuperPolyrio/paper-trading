from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib

import pytest

from quant.simulator.admission import (
    AdmissionOperation,
    AdmissionRequest,
    AdmissionStatus,
    ExposureEffect,
    GeoblockSnapshot,
    GeoblockUnavailable,
    JurisdictionMode,
    MemoryAdmissionStore,
    PolymarketGeoblockClient,
    UnifiedAdmissionService,
)


NOW = datetime(2026, 8, 20, 1, 2, 3, tzinfo=timezone.utc)


def _snapshot(
    *,
    blocked: bool,
    country: str,
    region: str = "",
    observed_at: datetime = NOW,
) -> GeoblockSnapshot:
    raw = f"{blocked}:{country}:{region}:{observed_at.isoformat()}"
    return GeoblockSnapshot(
        blocked=blocked,
        country=country,
        region=region,
        detected_ip="203.0.113.10",
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=60),
        raw_payload_hash=hashlib.sha256(raw.encode()).hexdigest(),
    )


def _request(
    request_id: str,
    *,
    operation: AdmissionOperation = AdmissionOperation.ORDER,
    effect: ExposureEffect = ExposureEffect.INCREASE,
    observed_at: datetime = NOW,
) -> AdmissionRequest:
    return AdmissionRequest(
        request_id=request_id,
        operation=operation,
        account_id="account-1",
        strategy_id="strategy-1",
        asset_id="asset-1",
        condition_id="condition-1",
        exposure_effect=effect,
        exposure_before=Decimal("2"),
        exposure_after=(
            Decimal("1") if effect is ExposureEffect.REDUCE else Decimal("3")
        ),
        observed_at=observed_at,
    )


@pytest.mark.parametrize("effect", list(ExposureEffect))
def test_unrestricted_route_allows_every_order_effect(effect: ExposureEffect) -> None:
    service = UnifiedAdmissionService(store=MemoryAdmissionStore())

    decision = service.decide(
        _request(f"unrestricted-{effect.value}", effect=effect),
        geoblock_snapshot=_snapshot(blocked=False, country="HK"),
    )

    assert decision.status is AdmissionStatus.ALLOWED
    assert decision.jurisdiction_mode is JurisdictionMode.UNRESTRICTED


def test_close_only_allows_reduction_and_denies_increase_or_reversal() -> None:
    service = UnifiedAdmissionService(store=MemoryAdmissionStore())
    snapshot = _snapshot(blocked=True, country="BR", region="SP")

    reduce_decision = service.decide(
        _request("close-reduce", effect=ExposureEffect.REDUCE),
        geoblock_snapshot=snapshot,
    )
    increase_decision = service.decide(
        _request("close-increase", effect=ExposureEffect.INCREASE),
        geoblock_snapshot=snapshot,
    )
    reversal_decision = service.decide(
        _request("close-reversal", effect=ExposureEffect.MIXED),
        geoblock_snapshot=snapshot,
    )

    assert reduce_decision.allowed is True
    assert increase_decision.status is AdmissionStatus.DENIED
    assert reversal_decision.status is AdmissionStatus.DENIED

    reverse_open = AdmissionRequest(
        request_id="close-reverse-open",
        operation=AdmissionOperation.ORDER,
        account_id="account-1",
        strategy_id="strategy-1",
        exposure_effect=ExposureEffect.REDUCE,
        exposure_before=Decimal("1"),
        exposure_after=Decimal("-1"),
        observed_at=NOW,
    )
    reverse_decision = service.decide(
        reverse_open,
        geoblock_snapshot=snapshot,
    )
    assert reverse_decision.status is AdmissionStatus.DENIED
    assert reverse_decision.reason_codes == (
        "close_only_reverse_or_nonreducing_order_denied",
    )

    unverified = AdmissionRequest(
        request_id="close-unverified",
        operation=AdmissionOperation.ORDER,
        account_id="account-1",
        strategy_id="strategy-1",
        exposure_effect=ExposureEffect.REDUCE,
        observed_at=NOW,
    )
    unverified_decision = service.decide(
        unverified,
        geoblock_snapshot=snapshot,
    )
    assert unverified_decision.status is AdmissionStatus.FAIL_CLOSED
    assert unverified_decision.reason_codes == ("close_only_exposure_unverified",)


@pytest.mark.parametrize("effect", [ExposureEffect.INCREASE, ExposureEffect.REDUCE])
def test_full_block_denies_open_and_close(effect: ExposureEffect) -> None:
    service = UnifiedAdmissionService(store=MemoryAdmissionStore())

    decision = service.decide(
        _request(f"full-{effect.value}", effect=effect),
        geoblock_snapshot=_snapshot(blocked=True, country="IR"),
    )

    assert decision.status is AdmissionStatus.DENIED
    assert decision.jurisdiction_mode is JurisdictionMode.BLOCK_COMPLETELY


@pytest.mark.parametrize(
    "operation",
    [
        AdmissionOperation.ORDER_CANCEL,
        AdmissionOperation.READ,
        AdmissionOperation.RECONCILIATION,
    ],
)
def test_control_plane_safety_actions_remain_available(operation: AdmissionOperation) -> None:
    service = UnifiedAdmissionService(store=MemoryAdmissionStore())

    decision = service.decide(_request(operation.value, operation=operation))

    assert decision.allowed is True
    assert decision.jurisdiction_mode is JurisdictionMode.NOT_APPLICABLE


@pytest.mark.parametrize(
    "operation",
    [
        AdmissionOperation.SPLIT,
        AdmissionOperation.MERGE,
        AdmissionOperation.REDEEM,
        AdmissionOperation.NEG_RISK_CONVERT,
        AdmissionOperation.BRIDGE_DEPOSIT,
        AdmissionOperation.BRIDGE_WITHDRAWAL,
        AdmissionOperation.SPONSOR,
        AdmissionOperation.DISPUTE,
    ],
)
def test_non_order_mutations_are_audited_without_invented_order_restrictions(
    operation: AdmissionOperation,
) -> None:
    store = MemoryAdmissionStore()
    service = UnifiedAdmissionService(store=store)

    decision = service.decide(_request(operation.value, operation=operation))

    assert decision.allowed is True
    assert decision.reason_codes == ("order_geoblock_not_applicable",)
    assert store.decisions[operation.value] == decision


class _FailingProvider:
    def snapshot(self, *, now=None):
        raise GeoblockUnavailable("timeout")


def test_order_fails_closed_when_api_times_out() -> None:
    service = UnifiedAdmissionService(
        store=MemoryAdmissionStore(), geoblock_provider=_FailingProvider()
    )

    decision = service.decide(_request("timeout"))

    assert decision.status is AdmissionStatus.FAIL_CLOSED
    assert decision.reason_codes == ("geoblock_api_unavailable",)


def test_expired_snapshot_fails_closed() -> None:
    service = UnifiedAdmissionService(store=MemoryAdmissionStore())
    old = _snapshot(
        blocked=False,
        country="HK",
        observed_at=NOW - timedelta(minutes=5),
    )

    decision = service.decide(_request("stale"), geoblock_snapshot=old)

    assert decision.status is AdmissionStatus.FAIL_CLOSED
    assert decision.reason_codes == ("geoblock_snapshot_stale",)


def test_idempotent_restart_replay_and_payload_collision() -> None:
    store = MemoryAdmissionStore()
    first_service = UnifiedAdmissionService(store=store)
    first = first_service.decide(
        _request("restart-safe"),
        geoblock_snapshot=_snapshot(blocked=False, country="HK"),
    )
    restarted_service = UnifiedAdmissionService(store=store)

    replay = restarted_service.decide(_request("restart-safe"))

    assert replay == first
    with pytest.raises(ValueError, match="conflicts"):
        restarted_service.decide(
            _request("restart-safe", effect=ExposureEffect.REDUCE)
        )


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class _HttpClient:
    payloads: list[dict[str, object]] = []
    calls = 0

    def __init__(self, **kwargs) -> None:
        self.options = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, _endpoint: str) -> _Response:
        payload = self.payloads[self.calls]
        type(self).calls += 1
        return _Response(payload)


def test_geoblock_client_reuses_fresh_cache_and_refreshes_after_expiry(
    monkeypatch,
) -> None:
    _HttpClient.payloads = [
        {"blocked": False, "country": "HK", "region": "", "ip": "1.1.1.1"},
        {"blocked": True, "country": "BR", "region": "SP", "ip": "2.2.2.2"},
    ]
    _HttpClient.calls = 0
    monkeypatch.setattr("quant.simulator.admission.client.httpx.Client", _HttpClient)
    client = PolymarketGeoblockClient(
        proxy_url="http://127.0.0.1:17980",
        ttl_seconds=60,
    )

    first = client.snapshot(now=NOW)
    cached = client.snapshot(now=NOW + timedelta(seconds=59))
    refreshed = client.snapshot(now=NOW + timedelta(seconds=60))

    assert first is cached
    assert first.country == "HK"
    assert refreshed.country == "BR"
    assert refreshed.blocked is True
    assert _HttpClient.calls == 2
    assert first.proxy_url == "http://127.0.0.1:17980"
