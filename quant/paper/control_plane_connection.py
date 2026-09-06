"""Build the paper-authority control-plane connection from explicit env."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from quant.core.db import PostgresSettings, ThreadLocalPostgresConnectionFactory

from .authority import ControlPlanePostgresConnectionFactory

PAPER_CONTROL_ENV_KEYS = (
    "POLY_QUANT_PAPER_POSTGRES_HOST",
    "POLY_QUANT_PAPER_POSTGRES_PORT",
    "POLY_QUANT_PAPER_POSTGRES_USER",
    "POLY_QUANT_PAPER_POSTGRES_DATABASE",
    "POLY_QUANT_PAPER_POSTGRES_SEARCH_PATH",
    "POLY_QUANT_PAPER_POSTGRES_PASSWORD_FILE",
    "POLY_QUANT_PAPER_POSTGRES_CONNECT_TIMEOUT_SECONDS",
    "POLY_QUANT_PAPER_POSTGRES_STATEMENT_TIMEOUT_MS",
    "POLY_QUANT_PAPER_POSTGRES_LOCK_TIMEOUT_MS",
    "POLY_QUANT_PAPER_POSTGRES_TCP_USER_TIMEOUT_MS",
)


def load_paper_control_plane_env(
    path: Path,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> bool:
    """Load an explicit Paper control plane without importing unrelated secrets."""

    selected = path.expanduser()
    if not selected.is_file():
        return False
    mode = stat.S_IMODE(selected.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(
            f"paper DB control env must not be group/world accessible: {selected}"
        )
    target = environ if environ is not None else os.environ
    values = dotenv_values(selected)
    for key in PAPER_CONTROL_ENV_KEYS:
        value = values.get(key)
        if value is not None:
            target[key] = str(value)
    return True


def paper_control_plane_connection_factory(
    environ: Mapping[str, str],
    *,
    fallback_connection_factory: Any,
) -> ControlPlanePostgresConnectionFactory:
    """Return GCP paper authority when configured, otherwise the fallback DB."""

    host = str(environ.get("POLY_QUANT_PAPER_POSTGRES_HOST") or "").strip()
    if not host:
        return ControlPlanePostgresConnectionFactory(fallback_connection_factory)

    password = str(environ.get("POLY_QUANT_PAPER_POSTGRES_PASSWORD") or "")
    password_file = str(
        environ.get("POLY_QUANT_PAPER_POSTGRES_PASSWORD_FILE") or ""
    ).strip()
    if password_file:
        path = Path(password_file).expanduser()
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"paper DB credential file must not be group/world accessible: {path}"
            )
        password = path.read_text(encoding="utf-8").strip()
    if not password:
        raise ValueError("paper DB credential is unavailable")

    settings = PostgresSettings(
        host=host,
        port=int(environ.get("POLY_QUANT_PAPER_POSTGRES_PORT") or 5432),
        user=str(
            environ.get("POLY_QUANT_PAPER_POSTGRES_USER") or "paper_authority"
        ).strip(),
        password=password,
        database=str(
            environ.get("POLY_QUANT_PAPER_POSTGRES_DATABASE") or "poly_quant_paper"
        ).strip(),
        search_path=str(
            environ.get("POLY_QUANT_PAPER_POSTGRES_SEARCH_PATH")
            or "quant,core,oracle,ops,public"
        ).strip(),
        connect_timeout_seconds=int(
            environ.get("POLY_QUANT_PAPER_POSTGRES_CONNECT_TIMEOUT_SECONDS") or 5
        ),
        statement_timeout_ms=int(
            environ.get("POLY_QUANT_PAPER_POSTGRES_STATEMENT_TIMEOUT_MS") or 20000
        ),
        lock_timeout_ms=int(
            environ.get("POLY_QUANT_PAPER_POSTGRES_LOCK_TIMEOUT_MS") or 5000
        ),
        tcp_user_timeout_ms=int(
            environ.get("POLY_QUANT_PAPER_POSTGRES_TCP_USER_TIMEOUT_MS") or 15000
        ),
        keepalives_idle_seconds=10,
        keepalives_interval_seconds=5,
        keepalives_count=3,
    )
    return ControlPlanePostgresConnectionFactory(
        ThreadLocalPostgresConnectionFactory(settings)
    )
