import os
from pathlib import Path
import subprocess


SCRIPT = Path("scripts/run_live_calibration_probe_secure.sh")


def test_secure_runner_supports_frozen_prepare_and_live_overrides() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert 'mode="${LIVE_CALIBRATION_MODE:-live}"' in content
    assert '"--${mode}"' in content
    assert 'args+=(--run-id "$LIVE_CALIBRATION_RUN_ID")' in content
    assert 'args+=(--market-id "$LIVE_CALIBRATION_MARKET_ID")' in content
    assert 'args+=(--asset-id "$LIVE_CALIBRATION_ASSET_ID")' in content
    assert (
        'args+=(--clean-cohort-id "$LIVE_CALIBRATION_CLEAN_COHORT_ID")'
        in content
    )
    assert 'args+=(--side "$LIVE_CALIBRATION_SIDE")' in content
    assert 'args+=(--order-type "$LIVE_CALIBRATION_ORDER_TYPE")' in content
    assert 'args+=(--amount "$LIVE_CALIBRATION_AMOUNT")' in content
    assert 'args+=(--amount-unit "$LIVE_CALIBRATION_AMOUNT_UNIT")' in content
    assert 'live_load_paper_control_env "${paper_control_env}"' in content
    assert 'source "${paper_control_env}"' not in content


def test_secure_runner_rejects_unknown_mode_before_loading_credentials() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert "live|prepare-live|no-submit" in content
    assert "unsupported LIVE_CALIBRATION_MODE" in content


def test_env_loader_exports_only_required_keys_without_sourcing(tmp_path: Path) -> None:
    env_path = tmp_path / "calibration.env"
    env_path.write_text(
        "\n".join(
            [
                "POLY_QUANT_PROBE_PRIVATE_KEY=private-value",
                "POLY_QUANT_PROBE_API_KEY=key-value",
                "POLY_QUANT_PROBE_API_SECRET=secret-value",
                "POLY_QUANT_PROBE_API_PASSPHRASE=pass-value",
                "UNTRUSTED_SHELL=$(touch should-not-exist)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "source scripts/live_calibration_credentials.sh; "
                f"live_load_calibration_env '{env_path}'; "
                "printf '%s:%s:%s:%s:%s' "
                "${#POLY_QUANT_PROBE_PRIVATE_KEY} ${#POLY_QUANT_PROBE_API_KEY} "
                "${#POLY_QUANT_PROBE_API_SECRET} ${#POLY_QUANT_PROBE_API_PASSPHRASE} "
                "${UNTRUSTED_SHELL-unset}"
            ),
        ],
        cwd=Path.cwd(),
        env={**os.environ, "LIVE_CALIBRATION_PYTHON_BIN": "/home/jiahuaiyu/.conda/envs/prediction-market-quant/bin/python"},
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "13:9:12:10:unset"
    assert not (Path.cwd() / "should-not-exist").exists()


def test_paper_control_env_loader_exports_only_whitelisted_values(tmp_path: Path) -> None:
    env_path = tmp_path / "paper-db-control.env"
    marker = tmp_path / "must-not-exist"
    env_path.write_text(
        "POLY_QUANT_PAPER_POSTGRES_HOST=127.0.0.1\n"
        "POLY_QUANT_PAPER_POSTGRES_PORT=45435\n"
        f"UNTRUSTED=$(touch {marker})\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "source scripts/live_calibration_credentials.sh; "
                f"live_load_paper_control_env '{env_path}'; "
                "printf '%s:%s:%s' "
                '"$POLY_QUANT_PAPER_POSTGRES_HOST" '
                '"$POLY_QUANT_PAPER_POSTGRES_PORT" '
                '"${UNTRUSTED-unset}"'
            ),
        ],
        cwd=Path.cwd(),
        env={
            **os.environ,
            "LIVE_CALIBRATION_PYTHON_BIN": (
                "/home/jiahuaiyu/.conda/envs/prediction-market-quant/bin/python"
            ),
        },
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "127.0.0.1:45435:unset"
    assert not marker.exists()
