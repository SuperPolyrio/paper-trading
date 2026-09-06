from pathlib import Path

from quant.calibration.live_probe_runner import LiveProbeRunner
from quant.calibration.probe_plan import load_probe_plan
from quant.calibration.probe_scheduler import NoSubmitProbeRunner
from quant.paper.authority import ControlPlanePostgresConnectionFactory


CONFIG = Path("configs/calibration/taker_live_acceptance_v1.yaml")


def _dependencies() -> dict[str, object]:
    return {
        "calibration_store": object(),
        "shadow_store": object(),
        "adapter": object(),
        "user_ws": object(),
    }


def test_no_submit_keeps_short_paper_prediction_wait() -> None:
    runner = NoSubmitProbeRunner(load_probe_plan(CONFIG), **_dependencies())

    assert runner.paper_prediction_wait_seconds == 30.0


def test_calibration_defaults_to_control_plane_shadow_writes() -> None:
    dependencies = _dependencies()
    dependencies.pop("shadow_store")

    runner = NoSubmitProbeRunner(load_probe_plan(CONFIG), **dependencies)

    assert isinstance(
        runner.shadow_store.connection_factory,
        ControlPlanePostgresConnectionFactory,
    )


def test_live_probe_allows_cross_host_paper_prediction_to_finish() -> None:
    runner = LiveProbeRunner(
        load_probe_plan(CONFIG),
        approved_asset_id="asset",
        **_dependencies(),
    )

    assert runner.paper_prediction_wait_seconds == 300.0
    assert runner.candidate_wait_seconds == 90.0
    assert runner.paper_prediction_max_wall_seconds == 15.0


def test_calibration_can_route_only_shadow_writes_to_paper_authority(
    tmp_path: Path,
) -> None:
    credential = tmp_path / "paper-db-password"
    credential.write_text("target-secret\n", encoding="utf-8")
    credential.chmod(0o600)
    dependencies = _dependencies()
    dependencies.pop("shadow_store")
    runner = NoSubmitProbeRunner(
        load_probe_plan(CONFIG),
        environ={
            "POLY_QUANT_PAPER_POSTGRES_HOST": "127.0.0.1",
            "POLY_QUANT_PAPER_POSTGRES_PORT": "45435",
            "POLY_QUANT_PAPER_POSTGRES_USER": "paper_authority",
            "POLY_QUANT_PAPER_POSTGRES_DATABASE": "poly_quant_paper",
            "POLY_QUANT_PAPER_POSTGRES_PASSWORD_FILE": str(credential),
        },
        **dependencies,
    )

    settings = runner.shadow_store.connection_factory.connection_factory.settings
    assert settings.host == "127.0.0.1"
    assert settings.port == 45435
    assert settings.password == "target-secret"
