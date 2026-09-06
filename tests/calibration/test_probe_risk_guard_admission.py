from quant.calibration.probe_risk_guard import _write_route_allowed


def test_unified_admission_is_authoritative_over_legacy_blocked_flag() -> None:
    assert _write_route_allowed(
        {"blocked": True, "admission": {"allowed": True}}
    )
    assert not _write_route_allowed(
        {"blocked": False, "admission": {"allowed": False}}
    )


def test_legacy_geoblock_remains_fail_closed_without_unified_decision() -> None:
    assert _write_route_allowed({"blocked": False})
    assert not _write_route_allowed({"blocked": True})
    assert not _write_route_allowed({})
