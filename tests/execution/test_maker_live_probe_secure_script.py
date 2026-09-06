from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_secure_maker_probe_loads_control_and_trading_credentials() -> None:
    script = (ROOT / "scripts/run_maker_live_probe_secure.sh").read_text(
        encoding="utf-8"
    )

    assert "live_load_paper_control_env" in script
    assert "live_load_calibration_env" in script
    assert 'scripts/run_maker_live_probe.py "$@"' in script
    assert "source " not in script.replace(
        "source scripts/live_calibration_credentials.sh", ""
    )
