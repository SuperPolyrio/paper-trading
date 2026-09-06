#!/usr/bin/env python3
"""Dedicated HTTP server for the tenant-scoped Paper API."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

os.environ.setdefault("POLY_QUANT_DISABLE_DOTENV", "1")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flask import Flask, Response, request  # noqa: E402

from quant.core.db import postgres_connection  # noqa: E402
from quant.paper.control_plane_connection import (  # noqa: E402
    paper_control_plane_connection_factory,
)
from quant.paper.public_api import PostgresPaperApiBackend  # noqa: E402
from scripts.api.routes.paper_v1 import create_paper_v1_blueprint  # noqa: E402


def create_app() -> Flask:
    app = Flask("paper_api")
    ingress_connection_factory = paper_control_plane_connection_factory(
        os.environ,
        fallback_connection_factory=postgres_connection,
    )
    backend = PostgresPaperApiBackend(
        connection_factory=ingress_connection_factory
    )
    app.register_blueprint(create_paper_v1_blueprint(backend))
    allowed_origins = frozenset(
        item.strip()
        for item in os.environ.get("PAPER_API_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    )

    @app.after_request
    def paper_cors(response: Response) -> Response:
        origin = str(request.headers.get("Origin") or "").strip()
        if origin and origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, Idempotency-Key, X-Request-Id, "
                "X-Paper-CSRF"
            )
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            )
            response.headers["Access-Control-Max-Age"] = "600"
        return response

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18510)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    create_app().run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
