from __future__ import annotations

from pathlib import Path

import pytest

from quant.paper.control_plane_connection import (
    load_paper_control_plane_env,
    paper_control_plane_connection_factory,
)


def test_paper_control_plane_connection_uses_fallback_without_remote_host() -> None:
    fallback = object()

    factory = paper_control_plane_connection_factory(
        {}, fallback_connection_factory=fallback
    )

    assert factory.connection_factory is fallback


def test_paper_control_plane_connection_uses_explicit_remote_settings(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "paper-password"
    password_file.write_text("secret\n", encoding="utf-8")
    password_file.chmod(0o600)

    factory = paper_control_plane_connection_factory(
        {
            "POLY_QUANT_PAPER_POSTGRES_HOST": "127.0.0.1",
            "POLY_QUANT_PAPER_POSTGRES_PORT": "45435",
            "POLY_QUANT_PAPER_POSTGRES_USER": "paper_reader",
            "POLY_QUANT_PAPER_POSTGRES_DATABASE": "paper",
            "POLY_QUANT_PAPER_POSTGRES_PASSWORD_FILE": str(password_file),
        },
        fallback_connection_factory=object(),
    )

    settings = factory.connection_factory.settings
    assert settings.host == "127.0.0.1"
    assert settings.port == 45435
    assert settings.user == "paper_reader"
    assert settings.database == "paper"
    assert settings.password == "secret"


def test_paper_control_plane_connection_rejects_open_password_file(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "paper-password"
    password_file.write_text("secret\n", encoding="utf-8")
    password_file.chmod(0o644)

    with pytest.raises(PermissionError, match="must not be group/world accessible"):
        paper_control_plane_connection_factory(
            {
                "POLY_QUANT_PAPER_POSTGRES_HOST": "127.0.0.1",
                "POLY_QUANT_PAPER_POSTGRES_PASSWORD_FILE": str(password_file),
            },
            fallback_connection_factory=object(),
        )


def test_explicit_control_env_overrides_host_and_ignores_unlisted_keys(
    tmp_path: Path,
) -> None:
    control_env = tmp_path / "paper-control.env"
    control_env.write_text(
        "POLY_QUANT_PAPER_POSTGRES_HOST=paper.example\n"
        "POLY_QUANT_PAPER_POSTGRES_PORT=45435\n"
        "POLY_QUANT_PROBE_PRIVATE_KEY=must-not-load\n",
        encoding="utf-8",
    )
    control_env.chmod(0o600)
    environ = {
        "POLY_QUANT_PAPER_POSTGRES_HOST": "stale-local-host",
        "POLY_QUANT_PROBE_PRIVATE_KEY": "original",
    }

    assert load_paper_control_plane_env(control_env, environ=environ)

    assert environ["POLY_QUANT_PAPER_POSTGRES_HOST"] == "paper.example"
    assert environ["POLY_QUANT_PAPER_POSTGRES_PORT"] == "45435"
    assert environ["POLY_QUANT_PROBE_PRIVATE_KEY"] == "original"


def test_explicit_control_env_rejects_open_permissions(tmp_path: Path) -> None:
    control_env = tmp_path / "paper-control.env"
    control_env.write_text(
        "POLY_QUANT_PAPER_POSTGRES_HOST=paper.example\n", encoding="utf-8"
    )
    control_env.chmod(0o644)

    with pytest.raises(PermissionError, match="paper DB control env"):
        load_paper_control_plane_env(control_env, environ={})
