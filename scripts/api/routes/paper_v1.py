"""Versioned Flask routes for the tenant-scoped public paper API."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from flask import Blueprint, Response, g, jsonify, request

from quant.paper.public_api import (
    API_VERSION,
    ApiIdentity,
    PaperApiError,
    PostgresPaperApiBackend,
    normalize_limit,
    require_idempotency_key,
    translate_domain_error,
)

LOGGER = logging.getLogger(__name__)
RETAIL_SESSION_COOKIE = "paper_retail_session"
RETAIL_SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value


def _parquet_bytes(rows: list[dict[str, Any]]) -> bytes:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - deployment dependency gate
        raise PaperApiError(
            "PAPER_EXPORT_DEPENDENCY_MISSING",
            "Parquet export requires pyarrow",
            status_code=503,
        ) from exc
    normalized = [
        {
            key: (
                json.dumps(value, sort_keys=True, separators=(",", ":"))
                if isinstance(value, (dict, list))
                else value
            )
            for key, value in row.items()
        }
        for row in rows
    ]
    table = (
        pa.Table.from_pylist(normalized)
        if normalized
        else pa.table({"_empty": pa.array([], type=pa.string())})
    )
    output = pa.BufferOutputStream()
    pq.write_table(table, output, compression="zstd", use_dictionary=True)
    return output.getvalue().to_pybytes()


def _error_payload(error: PaperApiError) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "details": _json_value(error.details),
            "request_id": str(getattr(g, "paper_request_id", "")),
        }
    }


def _success(data: Any) -> dict[str, Any]:
    return {
        "data": _json_value(data),
        "meta": {
            "request_id": str(g.paper_request_id),
            "api_version": API_VERSION,
        },
    }


def _json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            "request body must be a JSON object",
            status_code=400,
        )
    return payload


def _uuid(value: Any, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            f"{field} must be a UUID",
            status_code=400,
        ) from exc


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        selected = Decimal(str(value))
    except Exception as exc:
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            f"{field} must be numeric",
            status_code=400,
        ) from exc
    if positive and selected <= 0:
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            f"{field} must be positive",
            status_code=400,
        )
    return selected


def _integer(value: Any, field: str, *, minimum: int | None = None) -> int:
    try:
        selected = int(value)
    except (TypeError, ValueError) as exc:
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            f"{field} must be an integer",
            status_code=400,
        ) from exc
    if minimum is not None and selected < minimum:
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            f"{field} must be >= {minimum}",
            status_code=400,
        )
    return selected


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            f"{field} must be boolean",
            status_code=400,
        )
    return value


def _datetime(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        selected = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            f"{field} must be an ISO-8601 timestamp",
            status_code=400,
        ) from exc
    if selected.tzinfo is None:
        raise PaperApiError(
            "PAPER_VALIDATION_ERROR",
            f"{field} must include a timezone",
            status_code=400,
        )
    return selected.astimezone(timezone.utc)


def build_openapi_spec() -> dict[str, Any]:
    error_ref = {"$ref": "#/components/schemas/ErrorResponse"}
    security = [{"PaperApiKey": []}]
    retail_security = [{"RetailSessionCookie": []}, {"PaperApiKey": []}]
    idem = {
        "name": "Idempotency-Key",
        "in": "header",
        "required": True,
        "schema": {"type": "string", "minLength": 8, "maxLength": 160},
    }
    limit = {
        "name": "limit",
        "in": "query",
        "schema": {"type": "integer", "minimum": 1, "maximum": 200},
    }
    cursor = {
        "name": "cursor",
        "in": "query",
        "schema": {"type": "string"},
    }
    csrf = {
        "name": "X-Paper-CSRF",
        "in": "header",
        "required": True,
        "schema": {"type": "string", "minLength": 64, "maxLength": 64},
        "description": "Required for browser-cookie mutations.",
    }
    error_responses = {
        "400": {
            "description": "Validation error",
            "content": {"application/json": {"schema": error_ref}},
        },
        "401": {
            "description": "Authentication required",
            "content": {"application/json": {"schema": error_ref}},
        },
        "403": {
            "description": "Forbidden",
            "content": {"application/json": {"schema": error_ref}},
        },
        "409": {
            "description": "Conflict",
            "content": {"application/json": {"schema": error_ref}},
        },
        "429": {
            "description": "Quota exceeded",
            "content": {"application/json": {"schema": error_ref}},
        },
    }

    def json_body(schema: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }

    paths: dict[str, Any] = {
        "/v1/paper/health": {
            "get": {
                "operationId": "getHealth",
                "responses": {"200": {"description": "Paper API health"}},
            }
        },
        "/v1/paper/openapi.json": {
            "get": {
                "operationId": "getOpenApi",
                "responses": {"200": {"description": "OpenAPI document"}},
            }
        },
        "/v1/paper/accounts": {
            "get": {
                "operationId": "listAccounts",
                "security": security,
                "parameters": [limit, cursor],
                "responses": {"200": {"description": "Accounts"}, **error_responses},
            },
            "post": {
                "operationId": "createAccount",
                "security": security,
                "parameters": [idem],
                "requestBody": json_body(
                    {"$ref": "#/components/schemas/CreateAccountRequest"}
                ),
                "responses": {
                    "201": {"description": "Account created"},
                    **error_responses,
                },
            },
        },
        "/v1/paper/accounts/{account_id}": {
            "get": {
                "operationId": "getAccount",
                "security": security,
                "parameters": [
                    {
                        "name": "account_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    }
                ],
                "responses": {
                    "200": {"description": "Account"},
                    "404": {"description": "Not found"},
                    **error_responses,
                },
            }
        },
        "/v1/paper/accounts/{account_id}/fork": {
            "post": {
                "operationId": "forkAccount",
                "security": security,
                "parameters": [
                    {
                        "name": "account_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    },
                    idem,
                ],
                "requestBody": json_body(
                    {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    }
                ),
                "responses": {
                    "201": {"description": "Fork created"},
                    **error_responses,
                },
            }
        },
        "/v1/paper/orders": {
            "get": {
                "operationId": "listOrders",
                "security": security,
                "parameters": [
                    {
                        "name": "account_id",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    },
                    limit,
                    cursor,
                ],
                "responses": {"200": {"description": "Orders"}, **error_responses},
            },
            "post": {
                "operationId": "createOrder",
                "security": security,
                "parameters": [idem],
                "requestBody": json_body(
                    {"$ref": "#/components/schemas/CreateOrderRequest"}
                ),
                "responses": {
                    "202": {"description": "Order queued"},
                    **error_responses,
                },
            },
        },
        "/v1/paper/orders/cancel": {
            "post": {
                "operationId": "cancelOrders",
                "security": security,
                "parameters": [idem],
                "requestBody": json_body(
                    {
                        "type": "object",
                        "required": ["order_ids"],
                        "properties": {
                            "order_ids": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 3000,
                                "items": {"type": "integer"},
                            }
                        },
                    }
                ),
                "responses": {
                    "200": {"description": "Bulk cancel requested"},
                    **error_responses,
                },
            }
        },
        "/v1/paper/orders/cancel-market": {
            "post": {
                "operationId": "cancelMarketOrders",
                "security": security,
                "parameters": [idem],
                "requestBody": json_body(
                    {
                        "type": "object",
                        "required": ["account_id"],
                        "properties": {
                            "account_id": {"type": "string", "format": "uuid"},
                            "asset_id": {"type": "string"},
                            "condition_id": {"type": "string"},
                        },
                    }
                ),
                "responses": {
                    "200": {"description": "Market cancel requested"},
                    **error_responses,
                },
            }
        },
        "/v1/paper/orders/cancel-all": {
            "post": {
                "operationId": "cancelAllOrders",
                "security": security,
                "parameters": [idem],
                "requestBody": json_body(
                    {
                        "type": "object",
                        "required": ["account_id"],
                        "properties": {
                            "account_id": {"type": "string", "format": "uuid"}
                        },
                    }
                ),
                "responses": {
                    "200": {"description": "Account cancel-all requested"},
                    **error_responses,
                },
            }
        },
        "/v1/paper/orders/{order_id}": {
            "get": {
                "operationId": "getOrder",
                "security": security,
                "parameters": [
                    {
                        "name": "order_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {
                    "200": {"description": "Order"},
                    "404": {"description": "Not found"},
                    **error_responses,
                },
            },
            "delete": {
                "operationId": "cancelOrder",
                "security": security,
                "parameters": [
                    {
                        "name": "order_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    },
                    idem,
                ],
                "responses": {
                    "200": {"description": "Cancel requested"},
                    **error_responses,
                },
            },
        },
        "/v1/paper/orders/{order_id}/replace": {
            "post": {
                "operationId": "replaceOrder",
                "security": security,
                "parameters": [
                    {
                        "name": "order_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    },
                    idem,
                ],
                "requestBody": json_body(
                    {
                        "type": "object",
                        "required": ["limit_price", "size"],
                        "properties": {
                            "limit_price": {"type": "string"},
                            "size": {"type": "string"},
                        },
                    }
                ),
                "responses": {
                    "200": {"description": "Replace requested"},
                    **error_responses,
                },
            }
        },
    }
    for resource in ("positions", "fills", "ledger", "journal", "tca"):
        paths[f"/v1/paper/accounts/{{account_id}}/{resource}"] = {
            "get": {
                "operationId": f"list{resource.title()}",
                "security": security,
                "parameters": [
                    {
                        "name": "account_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    },
                    limit,
                    cursor,
                ],
                "responses": {
                    "200": {"description": resource.title()},
                    **error_responses,
                },
            }
        }
    paths["/v1/paper/accounts/{account_id}/performance"] = {
        "get": {
            "operationId": "getAccountPerformance",
            "security": security,
            "parameters": [
                {
                    "name": "account_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "format": "uuid"},
                },
                limit,
                cursor,
            ],
            "responses": {
                "200": {"description": "Current performance and NAV history"},
                **error_responses,
            },
        }
    }
    paths["/v1/paper/audit/{order_id}"] = {
        "get": {
            "operationId": "getOrderAudit",
            "security": security,
            "parameters": [
                {
                    "name": "order_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "integer"},
                }
            ],
            "responses": {
                "200": {"description": "Order timeline and execution evidence"},
                "404": {"description": "Not found"},
                **error_responses,
            },
        }
    }
    paths["/v1/paper/accounts/{account_id}/export"] = {
        "get": {
            "operationId": "exportAccountResource",
            "security": security,
            "parameters": [
                {
                    "name": "account_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "format": "uuid"},
                },
                {
                    "name": "resource",
                    "in": "query",
                    "required": True,
                    "schema": {
                        "type": "string",
                        "enum": [
                            "orders",
                            "positions",
                            "fills",
                            "ledger",
                            "journal",
                            "nav",
                            "tca",
                        ],
                    },
                },
                {
                    "name": "format",
                    "in": "query",
                    "schema": {
                        "type": "string",
                        "enum": ["csv", "jsonl", "parquet"],
                    },
                },
            ],
            "responses": {
                "200": {"description": "Bounded tenant-scoped account export"},
                **error_responses,
            },
        }
    }
    replay_id = {
        "name": "replay_session_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
    }
    paths["/v1/paper/replays"] = {
        "get": {
            "operationId": "listReplaySessions",
            "security": security,
            "parameters": [limit, cursor],
            "responses": {"200": {"description": "Replay sessions"}, **error_responses},
        },
        "post": {
            "operationId": "createReplaySession",
            "security": security,
            "parameters": [idem],
            "requestBody": json_body(
                {"$ref": "#/components/schemas/CreateReplaySessionRequest"}
            ),
            "responses": {
                "201": {"description": "Replay session created"},
                **error_responses,
            },
        },
    }
    paths["/v1/paper/replays/{replay_session_id}"] = {
        "get": {
            "operationId": "getReplaySession",
            "security": security,
            "parameters": [replay_id],
            "responses": {
                "200": {"description": "Replay session"},
                "404": {"description": "Not found"},
                **error_responses,
            },
        }
    }
    for action in ("pause", "resume", "fork"):
        operation = action.title() + "ReplaySession"
        operation_path: dict[str, Any] = {
            "operationId": operation[0].lower() + operation[1:],
            "security": security,
            "parameters": [replay_id, idem],
            "responses": {
                "200": {"description": f"Replay {action} result"},
                **error_responses,
            },
        }
        if action == "resume":
            operation_path["requestBody"] = json_body(
                {
                    "type": "object",
                    "properties": {
                        "max_events": {"type": "integer", "minimum": 1, "maximum": 5000}
                    },
                }
            )
        elif action == "fork":
            operation_path["requestBody"] = json_body(
                {"$ref": "#/components/schemas/ForkReplaySessionRequest"}
            )
            operation_path["responses"]["201"] = operation_path["responses"].pop("200")
        paths[f"/v1/paper/replays/{{replay_session_id}}/{action}"] = {
            "post": operation_path
        }
    paths["/v1/paper/replays/{replay_session_id}/events"] = {
        "get": {
            "operationId": "listReplayEvents",
            "security": security,
            "parameters": [replay_id, limit, cursor],
            "responses": {
                "200": {"description": "Frozen replay events"},
                **error_responses,
            },
        }
    }
    paths["/v1/paper/replays/{replay_session_id}/report"] = {
        "get": {
            "operationId": "getReplayReport",
            "security": security,
            "parameters": [replay_id],
            "responses": {
                "200": {"description": "Deterministic strategy report"},
                **error_responses,
            },
        }
    }
    scenario_id = {
        "name": "scenario_run_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
    }
    paths["/v1/paper/scenarios"] = {
        "get": {
            "operationId": "listScenarioRuns",
            "security": security,
            "parameters": [
                {
                    "name": "account_id",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "format": "uuid"},
                },
                limit,
            ],
            "responses": {
                "200": {"description": "Non-execution scenario runs"},
                **error_responses,
            },
        },
        "post": {
            "operationId": "createScenarioRun",
            "security": security,
            "parameters": [idem],
            "requestBody": json_body(
                {"$ref": "#/components/schemas/CreateScenarioRequest"}
            ),
            "responses": {
                "201": {"description": "Scenario run created"},
                **error_responses,
            },
        },
    }
    paths["/v1/paper/scenarios/{scenario_run_id}"] = {
        "get": {
            "operationId": "getScenarioRun",
            "security": security,
            "parameters": [scenario_id],
            "responses": {"200": {"description": "Scenario run"}, **error_responses},
        }
    }
    conditional_id = {
        "name": "conditional_order_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
    }
    paths["/v1/paper/conditional-orders"] = {
        "get": {
            "operationId": "listConditionalOrders",
            "security": security,
            "parameters": [
                {
                    "name": "account_id",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "format": "uuid"},
                },
                limit,
            ],
            "responses": {
                "200": {"description": "Conditional orders"},
                **error_responses,
            },
        },
        "post": {
            "operationId": "createConditionalOrder",
            "security": security,
            "parameters": [idem],
            "requestBody": json_body(
                {"$ref": "#/components/schemas/CreateConditionalOrderRequest"}
            ),
            "responses": {
                "201": {"description": "Conditional order armed"},
                **error_responses,
            },
        },
    }
    paths["/v1/paper/conditional-orders/{conditional_order_id}"] = {
        "get": {
            "operationId": "getConditionalOrder",
            "security": security,
            "parameters": [conditional_id],
            "responses": {
                "200": {"description": "Conditional order"},
                **error_responses,
            },
        },
        "delete": {
            "operationId": "cancelConditionalOrder",
            "security": security,
            "parameters": [conditional_id, idem],
            "requestBody": json_body(
                {"type": "object", "properties": {"reason": {"type": "string"}}}
            ),
            "responses": {
                "200": {"description": "Conditional order cancelled"},
                **error_responses,
            },
        },
    }
    paths["/v1/paper/conditional-order-groups"] = {
        "post": {
            "operationId": "createConditionalOrderGroup",
            "security": security,
            "parameters": [idem],
            "requestBody": json_body(
                {"$ref": "#/components/schemas/CreateConditionalOrderGroupRequest"}
            ),
            "responses": {
                "201": {"description": "Conditional order group armed"},
                **error_responses,
            },
        }
    }

    def retail_operation(
        operation_id: str,
        *,
        description: str,
        mutation: bool = False,
        public: bool = False,
        optional_auth: bool = False,
        parameters: list[dict[str, Any]] | None = None,
        status_code: str = "200",
    ) -> dict[str, Any]:
        operation: dict[str, Any] = {
            "operationId": operation_id,
            "responses": {
                status_code: {"description": description},
                **error_responses,
            },
        }
        if public:
            operation["security"] = []
        elif optional_auth:
            operation["security"] = [{}, *retail_security]
        else:
            operation["security"] = retail_security
        selected_parameters = list(parameters or [])
        if mutation:
            selected_parameters.extend((idem, csrf))
            operation["requestBody"] = json_body(
                {"type": "object", "additionalProperties": True}
            )
        if selected_parameters:
            operation["parameters"] = selected_parameters
        return operation

    def retail_path_parameter(name: str, *, uuid: bool = False) -> dict[str, Any]:
        return {
            "name": name,
            "in": "path",
            "required": True,
            "schema": {"type": "string", **({"format": "uuid"} if uuid else {})},
        }

    market_slug = retail_path_parameter("market_slug")
    virtual_wallet_id = retail_path_parameter("virtual_wallet_id")
    notification_id = retail_path_parameter("notification_id", uuid=True)
    competition_id = retail_path_parameter("competition_id", uuid=True)
    position_operation_id = retail_path_parameter("position_operation_id")
    paths.update(
        {
            "/v1/paper/retail/session/guest": {
                "post": retail_operation(
                    "createRetailGuestSession",
                    description="Guest session and default 10,000 pUSD wallet created",
                    public=True,
                    status_code="201",
                )
            },
            "/v1/paper/retail/session/challenge": {
                "post": retail_operation(
                    "createRetailWalletChallenge",
                    description="EVM wallet-signature challenge issued",
                    public=True,
                )
            },
            "/v1/paper/retail/session/wallet": {
                "post": retail_operation(
                    "createRetailWalletSession",
                    description="Verified EVM session and paper wallet created",
                    public=True,
                    status_code="201",
                )
            },
            "/v1/paper/retail/session": {
                "get": retail_operation(
                    "getRetailSession",
                    description="Current retail session",
                    optional_auth=True,
                )
            },
            "/v1/paper/retail/session/logout": {
                "post": retail_operation(
                    "logoutRetailSession",
                    description="Retail session revoked",
                    parameters=[csrf],
                )
            },
            "/v1/paper/retail/wallets": {
                "get": retail_operation(
                    "listRetailWallets", description="Paper virtual wallets"
                )
            },
            "/v1/paper/retail/wallets/fork": {
                "post": retail_operation(
                    "forkRetailWallet",
                    description="Paper wallet forked from an immutable snapshot",
                    mutation=True,
                    status_code="201",
                )
            },
            "/v1/paper/retail/wallets/reset": {
                "post": retail_operation(
                    "resetRetailWallet",
                    description="Clean wallet generation created with initial capital",
                    mutation=True,
                    status_code="201",
                )
            },
            "/v1/paper/retail/wallets/{virtual_wallet_id}/default": {
                "put": retail_operation(
                    "selectRetailWallet",
                    description="Default paper wallet selected",
                    mutation=True,
                    parameters=[virtual_wallet_id],
                )
            },
            "/v1/paper/retail/markets": {
                "get": retail_operation(
                    "listRetailMarkets",
                    description="Tradable market discovery results",
                    parameters=[limit],
                )
            },
            "/v1/paper/retail/markets/{market_slug}": {
                "get": retail_operation(
                    "getRetailMarket",
                    description="Market, tokens, rules, L2 and lifecycle detail",
                    parameters=[market_slug],
                )
            },
            "/v1/paper/retail/watchlist": {
                "get": retail_operation(
                    "getRetailWatchlist", description="Current market watchlist"
                )
            },
            "/v1/paper/retail/watchlist/{market_slug}": {
                "put": retail_operation(
                    "setRetailWatchlistEntry",
                    description="Market watchlist membership changed",
                    mutation=True,
                    parameters=[market_slug],
                )
            },
            "/v1/paper/retail/preferences": {
                "get": retail_operation(
                    "getRetailPreferences",
                    description="Locale, timezone and accessibility preferences",
                ),
                "put": retail_operation(
                    "setRetailPreferences",
                    description="Locale, timezone and accessibility preferences updated",
                    mutation=True,
                ),
            },
            "/v1/paper/retail/predictions": {
                "get": retail_operation(
                    "listRetailPredictions",
                    description="Prediction journal entries",
                    parameters=[limit],
                ),
                "post": retail_operation(
                    "createRetailPrediction",
                    description="Prediction journal entry created",
                    mutation=True,
                    status_code="201",
                ),
            },
            "/v1/paper/retail/predictions/report": {
                "get": retail_operation(
                    "getRetailPredictionReport",
                    description="Brier, log-loss, calibration, CLV and capital-days report",
                )
            },
            "/v1/paper/retail/risk-profile": {
                "get": retail_operation(
                    "getRetailRiskProfile", description="Retail account risk limits"
                ),
                "put": retail_operation(
                    "setRetailRiskProfile",
                    description="Retail account risk limits updated",
                    mutation=True,
                ),
            },
            "/v1/paper/retail/notifications": {
                "get": retail_operation(
                    "listRetailNotifications",
                    description="Order, data, risk and lifecycle notifications",
                    parameters=[limit],
                )
            },
            "/v1/paper/retail/notifications/{notification_id}/read": {
                "put": retail_operation(
                    "markRetailNotificationRead",
                    description="Notification marked read",
                    mutation=True,
                    parameters=[notification_id],
                )
            },
            "/v1/paper/retail/account-truth": {
                "get": retail_operation(
                    "getRetailAccountTruth",
                    description="Official account-truth reconciliation without ledger overwrite",
                )
            },
            "/v1/paper/retail/pnl-attribution": {
                "get": retail_operation(
                    "getRetailPnlAttribution",
                    description="Auditable realized and unrealized PnL attribution",
                )
            },
            "/v1/paper/retail/event-risk": {
                "get": retail_operation(
                    "getRetailEventRisk",
                    description="Event-level exposure and worst-case payout risk",
                )
            },
            "/v1/paper/retail/event-relations": {
                "get": retail_operation(
                    "getRetailEventRelations",
                    description="Evidence-bound event and outcome relationship graph",
                )
            },
            "/v1/paper/retail/lifecycle": {
                "get": retail_operation(
                    "getRetailLifecycle",
                    description="Resolution, challenge, dispute and redeem lifecycle",
                )
            },
            "/v1/paper/retail/position-operations": {
                "get": retail_operation(
                    "listRetailPositionOperations",
                    description="Confirmed-only Paper split, merge and conversion operations",
                    parameters=[limit],
                ),
                "post": retail_operation(
                    "createRetailPositionOperation",
                    description="Paper position operation completed with a durable receipt",
                    mutation=True,
                    status_code="201",
                ),
            },
            "/v1/paper/retail/position-operations/candidates": {
                "get": retail_operation(
                    "getRetailPositionOperationCandidates",
                    description="Server-resolved merge, redeem and conversion choices",
                    parameters=[limit],
                )
            },
            "/v1/paper/retail/position-operations/{position_operation_id}": {
                "get": retail_operation(
                    "getRetailPositionOperation",
                    description="Paper position operation lifecycle and receipt",
                    parameters=[position_operation_id],
                )
            },
            "/v1/paper/retail/maker-workbench": {
                "get": retail_operation(
                    "getRetailMakerWorkbench",
                    description="Maker queue, fill model, markout and rebate evidence",
                    parameters=[limit],
                )
            },
            "/v1/paper/retail/portfolio": {
                "put": retail_operation(
                    "setRetailPortfolioVisibility",
                    description="Portfolio sharing and privacy preferences updated",
                    mutation=True,
                )
            },
            "/v1/paper/retail/portfolio/refresh-metrics": {
                "post": retail_operation(
                    "refreshRetailPortfolioMetrics",
                    description="Server-derived portfolio metrics refreshed",
                    mutation=True,
                )
            },
            "/v1/paper/retail/portfolios/{virtual_wallet_id}": {
                "get": retail_operation(
                    "getRetailPublicPortfolio",
                    description="As-of privacy-filtered public positions and activity",
                    parameters=[virtual_wallet_id, limit],
                )
            },
            "/v1/paper/retail/portfolios/{virtual_wallet_id}/journal": {
                "get": retail_operation(
                    "getRetailPublicPredictionJournal",
                    description="Privacy and delay filtered public prediction journal",
                    parameters=[virtual_wallet_id, limit],
                )
            },
            "/v1/paper/retail/following": {
                "get": retail_operation(
                    "listRetailFollowing", description="Followed public paper wallets"
                )
            },
            "/v1/paper/retail/following/{virtual_wallet_id}": {
                "put": retail_operation(
                    "setRetailFollow",
                    description="Public paper wallet follow state changed",
                    mutation=True,
                    parameters=[virtual_wallet_id],
                )
            },
            "/v1/paper/retail/competitions": {
                "get": retail_operation(
                    "listRetailCompetitions",
                    description="Paper competitions and server-derived standings",
                ),
                "post": retail_operation(
                    "createRetailCompetition",
                    description="Paper competition created",
                    mutation=True,
                    status_code="201",
                ),
            },
            "/v1/paper/retail/competitions/{competition_id}/join": {
                "post": retail_operation(
                    "joinRetailCompetition",
                    description="Dedicated competition wallet created",
                    mutation=True,
                    parameters=[competition_id],
                    status_code="201",
                )
            },
            "/v1/paper/retail/competitions/{competition_id}/standings": {
                "get": retail_operation(
                    "getRetailCompetitionStandings",
                    description="Server-recomputed as-of competition standings",
                    parameters=[competition_id],
                )
            },
            "/v1/paper/retail/data-requests": {
                "get": retail_operation(
                    "listRetailDataRequests",
                    description="Data export and deletion requests",
                ),
                "post": retail_operation(
                    "createRetailDataRequest",
                    description="Data-subject request created",
                    mutation=True,
                    status_code="201",
                ),
            },
            "/v1/paper/retail/data-requests/{request_id}/download": {
                "get": retail_operation(
                    "downloadRetailDataExport",
                    description="Checksum-verified tenant-scoped Paper data export",
                    parameters=[retail_path_parameter("request_id", uuid=True)],
                )
            },
            "/v1/paper/retail/official-history": {
                "get": retail_operation(
                    "listRetailOfficialHistorySyncs",
                    description="Official-wallet shadow imports and account-truth comparisons",
                    parameters=[limit],
                ),
                "post": retail_operation(
                    "createRetailOfficialHistorySync",
                    description="Official-wallet shadow import queued without ledger overwrite",
                    mutation=True,
                    status_code="202",
                ),
            },
            "/v1/paper/retail/leaderboard": {
                "get": retail_operation(
                    "getRetailLeaderboard",
                    description="Server-computed public portfolio leaderboard",
                    parameters=[limit],
                )
            },
        }
    )
    admin_paths = {
        "/v1/paper/admin/dashboard": ("get", "getAdminDashboard"),
        "/v1/paper/admin/jobs": ("get", "listAdminJobs"),
        "/v1/paper/admin/tenant/freeze": ("post", "freezeTenant"),
        "/v1/paper/admin/tenant/unfreeze": ("post", "unfreezeTenant"),
        "/v1/paper/admin/accounts/{account_id}/freeze": ("post", "freezeAccount"),
        "/v1/paper/admin/accounts/{account_id}/unfreeze": ("post", "unfreezeAccount"),
        "/v1/paper/admin/accounts/{account_id}/kill": ("post", "killAccount"),
        "/v1/paper/admin/accounts/{account_id}/reconcile": ("post", "reconcileAccount"),
        "/v1/paper/admin/dlq": ("get", "listDlqEvents"),
        "/v1/paper/admin/dlq/{dlq_event_id}/replay": ("post", "replayDlqEvent"),
        "/v1/paper/admin/dlq/{dlq_event_id}/ignore": ("post", "ignoreDlqEvent"),
        "/v1/paper/admin/notices": ("get", "listMaintenanceNotices"),
        "/v1/paper/admin/incidents": ("get", "listIncidents"),
        "/v1/paper/admin/retention": ("get", "listRetentionPolicies"),
        "/v1/paper/admin/evidence-bundles": ("get", "listEvidenceBundles"),
        "/v1/paper/admin/evidence-bundles/{bundle_id}/download": (
            "get",
            "downloadEvidenceBundle",
        ),
    }
    for path, (method, operation_id) in admin_paths.items():
        parameters = (
            [limit] if path.endswith(("/jobs", "/dlq", "/evidence-bundles")) else []
        )
        for parameter_name in ("account_id", "dlq_event_id", "bundle_id"):
            if "{" + parameter_name + "}" in path:
                parameters.append(
                    {
                        "name": parameter_name,
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    }
                )
        if method != "get":
            parameters.append(idem)
        paths[path] = {
            method: {
                "operationId": operation_id,
                "security": security,
                "parameters": parameters,
                "responses": {
                    "200": {"description": "Admin operation result"},
                    **error_responses,
                },
            }
        }
    paths["/v1/paper/admin/notices"]["post"] = {
        "operationId": "createMaintenanceNotice",
        "security": security,
        "parameters": [idem],
        "responses": {"201": {"description": "Notice created"}, **error_responses},
    }
    paths["/v1/paper/admin/incidents"]["post"] = {
        "operationId": "createIncident",
        "security": security,
        "parameters": [idem],
        "responses": {"201": {"description": "Incident created"}, **error_responses},
    }
    paths["/v1/paper/admin/evidence-bundles"]["post"] = {
        "operationId": "createEvidenceBundle",
        "security": security,
        "parameters": [idem],
        "responses": {
            "201": {"description": "Evidence bundle created"},
            **error_responses,
        },
    }
    for path, operation_id in (
        ("/v1/paper/admin/notices/{notice_id}/status", "setMaintenanceNoticeStatus"),
        ("/v1/paper/admin/incidents/{incident_id}/status", "setIncidentStatus"),
        ("/v1/paper/admin/incidents/{incident_id}/notes", "addIncidentNote"),
        ("/v1/paper/admin/retention/{resource_type}", "upsertRetentionPolicy"),
        ("/v1/paper/admin/retention/{resource_type}/run", "runRetention"),
    ):
        parameter_name = next(
            name
            for name in ("notice_id", "incident_id", "resource_type")
            if "{" + name + "}" in path
        )
        paths[path] = {
            "post": {
                "operationId": operation_id,
                "security": security,
                "parameters": [
                    {
                        "name": parameter_name,
                        "in": "path",
                        "required": True,
                        "schema": {
                            "type": "string",
                            **(
                                {"format": "uuid"}
                                if parameter_name.endswith("_id")
                                else {}
                            ),
                        },
                    },
                    idem,
                ],
                "responses": {
                    "200": {"description": "Admin mutation result"},
                    **error_responses,
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Polymarket Paper API",
            "version": API_VERSION,
            "description": "Tenant-scoped paper execution. This API never submits real orders.",
        },
        "servers": [{"url": "/"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "PaperApiKey": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "ppk",
                },
                "RetailSessionCookie": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": RETAIL_SESSION_COOKIE,
                    "description": "HttpOnly browser session; mutations also require X-Paper-CSRF.",
                },
            },
            "schemas": {
                "ErrorResponse": {
                    "type": "object",
                    "required": ["error"],
                    "properties": {
                        "error": {
                            "type": "object",
                            "required": ["code", "message", "request_id"],
                            "properties": {
                                "code": {"type": "string"},
                                "message": {"type": "string"},
                                "request_id": {"type": "string", "format": "uuid"},
                                "details": {"type": "object"},
                            },
                        }
                    },
                },
                "CreateAccountRequest": {
                    "type": "object",
                    "required": ["name", "initial_cash"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "initial_cash": {
                            "type": "string",
                            "pattern": "^[0-9]+(?:\\.[0-9]+)?$",
                        },
                    },
                },
                "CreateOrderRequest": {
                    "type": "object",
                    "required": [
                        "account_id",
                        "strategy_id",
                        "asset_id",
                        "side",
                        "time_in_force",
                        "limit_price",
                        "size",
                    ],
                    "properties": {
                        "account_id": {"type": "string", "format": "uuid"},
                        "strategy_id": {"type": "string", "format": "uuid"},
                        "deployment_id": {"type": "string", "format": "uuid"},
                        "asset_id": {"type": "string"},
                        "side": {"type": "string", "enum": ["BUY", "SELL"]},
                        "time_in_force": {
                            "type": "string",
                            "enum": ["GTC", "GTD", "FOK", "FAK"],
                        },
                        "limit_price": {"type": "string"},
                        "size": {"type": "string"},
                        "amount_unit": {"type": "string", "enum": ["SHARES", "QUOTE"]},
                        "post_only": {"type": "boolean"},
                    },
                },
                "CreateScenarioRequest": {
                    "type": "object",
                    "required": ["account_id", "name", "inputs"],
                    "properties": {
                        "account_id": {"type": "string", "format": "uuid"},
                        "strategy_id": {"type": "string", "format": "uuid"},
                        "name": {"type": "string", "minLength": 1},
                        "inputs": {"type": "object"},
                    },
                },
                "CreateConditionalOrderRequest": {
                    "type": "object",
                    "required": [
                        "account_id",
                        "strategy_id",
                        "order_type",
                        "trigger",
                        "child_order",
                    ],
                    "properties": {
                        "account_id": {"type": "string", "format": "uuid"},
                        "strategy_id": {"type": "string", "format": "uuid"},
                        "order_type": {"type": "string"},
                        "trigger": {"type": "object"},
                        "child_order": {"type": "object"},
                        "group_id": {"type": "string", "format": "uuid"},
                        "group_policy": {
                            "type": "string",
                            "enum": ["NONE", "OCO", "OTO", "BRACKET"],
                        },
                        "expires_at": {"type": "string", "format": "date-time"},
                    },
                },
                "CreateConditionalOrderGroupRequest": {
                    "type": "object",
                    "required": [
                        "account_id",
                        "strategy_id",
                        "group_policy",
                        "orders",
                    ],
                    "properties": {
                        "account_id": {"type": "string", "format": "uuid"},
                        "strategy_id": {"type": "string", "format": "uuid"},
                        "group_policy": {
                            "type": "string",
                            "enum": ["OCO", "OTO", "BRACKET"],
                        },
                        "orders": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 8,
                            "items": {
                                "type": "object",
                                "required": ["order_type", "trigger", "child_order"],
                                "properties": {
                                    "order_type": {"type": "string"},
                                    "trigger": {"type": "object"},
                                    "child_order": {"type": "object"},
                                    "parent_intent_id": {
                                        "type": "integer",
                                        "minimum": 1,
                                    },
                                    "expires_at": {
                                        "type": "string",
                                        "format": "date-time",
                                    },
                                },
                            },
                        },
                    },
                },
                "CreateReplaySessionRequest": {
                    "type": "object",
                    "required": [
                        "account_id",
                        "name",
                        "start_ts",
                        "end_ts",
                        "strategy_version",
                        "execution_model",
                    ],
                    "properties": {
                        "account_id": {"type": "string", "format": "uuid"},
                        "strategy_id": {"type": "string", "format": "uuid"},
                        "name": {"type": "string", "minLength": 1},
                        "start_ts": {"type": "string", "format": "date-time"},
                        "end_ts": {"type": "string", "format": "date-time"},
                        "speed": {"type": "string", "default": "1"},
                        "seed": {"type": "integer", "default": 0},
                        "strategy_version": {"type": "string", "minLength": 1},
                        "execution_model": {"type": "string", "minLength": 1},
                        "benchmark": {
                            "type": "string",
                            "enum": ["CASH", "CONSERVATIVE_NAV"],
                        },
                    },
                },
                "ForkReplaySessionRequest": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "speed": {"type": "string"},
                        "seed": {"type": "integer"},
                        "execution_model": {"type": "string"},
                        "benchmark": {
                            "type": "string",
                            "enum": ["CASH", "CONSERVATIVE_NAV"],
                        },
                    },
                },
            },
        },
    }


def create_paper_v1_blueprint(
    backend: Any | None = None,
    *,
    backend_factory: Callable[[], Any] | None = None,
) -> Blueprint:
    bp = Blueprint("paper_v1", __name__, url_prefix="/v1/paper")
    selected_backend = backend

    def get_backend() -> Any:
        nonlocal selected_backend
        if selected_backend is None:
            selected_backend = (
                backend_factory() if backend_factory else PostgresPaperApiBackend()
            )
        return selected_backend

    def identity() -> ApiIdentity:
        selected = getattr(g, "paper_identity", None)
        if selected is None:
            raise PaperApiError(
                "PAPER_AUTH_REQUIRED",
                "paper API authentication is required",
                status_code=401,
            )
        return selected

    def retail_csrf(token: str) -> str:
        return hmac.new(
            get_backend().pepper,
            f"paper-retail-csrf:{token}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def retail_session_response(
        result: Mapping[str, Any], *, token: str, status_code: int = 200
    ) -> Response:
        public = dict(result)
        public.pop("token", None)
        public["csrf_token"] = retail_csrf(token)
        response = jsonify(_success(public))
        response.status_code = status_code
        response.set_cookie(
            RETAIL_SESSION_COOKIE,
            token,
            max_age=RETAIL_SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=bool(request.is_secure),
            samesite="Strict",
            path="/",
        )
        return response

    def mutate(
        operation: str,
        payload: Mapping[str, Any],
        callback: Callable[[str], Any],
        *,
        status_code: int,
    ) -> Response:
        idem_key = require_idempotency_key(request.headers.get("Idempotency-Key"))
        api = get_backend()
        claim = api.claim_idempotency(
            identity(),
            operation=operation,
            idempotency_key=idem_key,
            payload=payload,
        )
        if claim.replayed:
            response = jsonify(_json_value(dict(claim.response_body or {})))
            response.status_code = int(claim.response_status or 200)
            response.headers["Idempotent-Replayed"] = "true"
            return response
        try:
            body = _success(callback(idem_key))
            api.complete_idempotency(
                identity(),
                claim,
                status_code=status_code,
                response_body=body,
            )
        except Exception as exc:
            translated = translate_domain_error(exc)
            api.fail_idempotency(identity(), claim, error_code=translated.code)
            raise translated from exc
        response = jsonify(body)
        response.status_code = status_code
        response.headers["Idempotent-Replayed"] = "false"
        return response

    @bp.before_request
    def before_request() -> Response | None:
        g.paper_request_id = uuid4()
        g.paper_started_at = time.perf_counter()
        g.paper_error_code = None
        if request.method == "OPTIONS":
            return Response(status=204)
        if request.endpoint in {
            "paper_v1.openapi",
            "paper_v1.health",
            "paper_v1.retail_guest_session",
            "paper_v1.retail_wallet_challenge",
            "paper_v1.retail_wallet_session",
        }:
            return None
        authorization = str(request.headers.get("Authorization") or "").strip()
        cookie_token = str(request.cookies.get(RETAIL_SESSION_COOKIE) or "").strip()
        if (
            request.endpoint == "paper_v1.retail_session"
            and not authorization
            and not cookie_token
        ):
            g.paper_cookie_auth = False
            return None
        if authorization.startswith("Bearer "):
            token = authorization[7:].strip()
            g.paper_cookie_auth = False
        elif cookie_token:
            token = cookie_token
            g.paper_cookie_auth = True
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                supplied_csrf = str(request.headers.get("X-Paper-CSRF") or "")
                if not hmac.compare_digest(supplied_csrf, retail_csrf(token)):
                    raise PaperApiError(
                        "PAPER_CSRF_INVALID",
                        "retail session mutation requires a valid X-Paper-CSRF header",
                        status_code=403,
                    )
        else:
            raise PaperApiError(
                "PAPER_AUTH_REQUIRED",
                "paper API key or retail browser session is required",
                status_code=401,
            )
        g.paper_identity = get_backend().authenticate(token)
        decision = get_backend().charge_request_quota(
            g.paper_identity,
            request_id=g.paper_request_id,
        )
        g.paper_rate_limit = decision
        return None

    @bp.after_request
    def after_request(response: Response) -> Response:
        response.headers["X-Request-Id"] = str(g.paper_request_id)
        response.headers["X-Paper-Api-Version"] = API_VERSION
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Paper-Trading-Mode"] = "PAPER_ONLY"
        decision = getattr(g, "paper_rate_limit", None)
        if decision is not None:
            response.headers["RateLimit-Limit"] = str(decision.hard_limit)
            response.headers["RateLimit-Remaining"] = str(
                max(Decimal(0), decision.hard_limit - decision.used_after)
            )
            response.headers["RateLimit-Reset"] = str(decision.window_seconds)
        current_identity = getattr(g, "paper_identity", None)
        if current_identity is not None:
            latency = Decimal(
                str(
                    round(
                        (time.perf_counter() - g.paper_started_at) * 1000,
                        3,
                    )
                )
            )
            try:
                get_backend().record_request(
                    current_identity,
                    request_id=g.paper_request_id,
                    method=request.method,
                    route=request.url_rule.rule if request.url_rule else request.path,
                    response_status=response.status_code,
                    latency_ms=latency,
                    error_code=getattr(g, "paper_error_code", None),
                )
            except Exception:
                LOGGER.exception("failed to write paper API request audit")
        return response

    @bp.errorhandler(Exception)
    def handle_error(exc: Exception) -> tuple[Response, int]:
        error = translate_domain_error(exc)
        g.paper_error_code = error.code
        if error.status_code >= 500:
            LOGGER.exception("unhandled paper API error")
        response = jsonify(_error_payload(error))
        if error.status_code == 429:
            response.headers["Retry-After"] = "1"
        return response, error.status_code

    @bp.get("/health")
    def health() -> Response:
        return jsonify(
            {
                "status": "ok",
                "service": "paper-api",
                "api_version": API_VERSION,
                "mode": "PAPER_ONLY",
            }
        )

    @bp.get("/openapi.json")
    def openapi() -> Response:
        return jsonify(build_openapi_spec())

    @bp.post("/retail/session/guest")
    def retail_guest_session() -> Response:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "request body must be a JSON object",
                status_code=400,
            )
        guest_subject = f"guest:{uuid4()}"
        result = get_backend().provision_retail_identity(
            provider="GUEST",
            provider_subject=guest_subject,
            display_name=str(payload.get("display_name") or "Paper trader"),
        )
        return retail_session_response(
            result, token=str(result["token"]), status_code=201
        )

    @bp.post("/retail/session/challenge")
    def retail_wallet_challenge() -> Response:
        payload = _json_body()
        get_backend().ensure_retail_schema()
        result = get_backend().retail_service.issue_wallet_challenge(
            wallet_address=str(payload.get("wallet_address") or ""),
            domain=request.host,
        )
        return jsonify(_success(result))

    @bp.post("/retail/session/wallet")
    def retail_wallet_session() -> Response:
        payload = _json_body()
        get_backend().ensure_retail_schema()
        address = get_backend().retail_service.consume_wallet_challenge(
            challenge_id=_uuid(payload.get("challenge_id"), "challenge_id"),
            wallet_address=str(payload.get("wallet_address") or ""),
            signature=str(payload.get("signature") or ""),
        )
        result = get_backend().provision_retail_identity(
            provider="EVM",
            provider_subject=address,
            display_name=str(payload.get("display_name") or f"{address[:6]}...{address[-4:]}"),
            wallet_address=address,
        )
        return retail_session_response(
            result, token=str(result["token"]), status_code=201
        )

    @bp.get("/retail/session")
    def retail_session() -> Response:
        if getattr(g, "paper_identity", None) is None:
            return jsonify(_success({"authenticated": False, "wallet": None}))
        result = get_backend().get_retail_session(identity())
        result["authenticated"] = True
        token = str(request.cookies.get(RETAIL_SESSION_COOKIE) or "")
        if token:
            result["csrf_token"] = retail_csrf(token)
        return jsonify(_success(result))

    @bp.post("/retail/session/logout")
    def retail_logout() -> Response:
        get_backend().revoke_api_key_for_principal(
            identity().principal, identity().api_key_id
        )
        response = jsonify(_success({"logged_out": True}))
        response.delete_cookie(RETAIL_SESSION_COOKIE, path="/")
        return response

    @bp.get("/retail/wallets")
    def retail_wallets() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                {
                    "items": get_backend().retail_service.list_wallets(
                        identity().principal
                    )
                }
            )
        )

    @bp.post("/retail/wallets/fork")
    def retail_fork_wallet() -> Response:
        payload = _json_body()
        return mutate(
            "retail_fork_wallet",
            payload,
            lambda key: get_backend().retail_service.fork_virtual_wallet(
                identity().principal,
                name=str(payload.get("name") or "Paper wallet copy"),
                idempotency_key=key,
            ),
            status_code=201,
        )

    @bp.post("/retail/wallets/reset")
    def retail_reset_wallet() -> Response:
        payload = _json_body()
        return mutate(
            "retail_reset_wallet",
            payload,
            lambda key: get_backend().retail_service.reset_virtual_wallet(
                identity().principal,
                confirm_virtual_wallet_id=str(
                    payload.get("confirm_virtual_wallet_id") or ""
                ),
                idempotency_key=key,
            ),
            status_code=201,
        )

    @bp.put("/retail/wallets/<path:virtual_wallet_id>/default")
    def retail_select_wallet(virtual_wallet_id: str) -> Response:
        return mutate(
            f"retail_select_wallet:{virtual_wallet_id}",
            {"virtual_wallet_id": virtual_wallet_id},
            lambda _key: get_backend().retail_service.set_default_wallet(
                identity().principal, virtual_wallet_id
            ),
            status_code=200,
        )

    @bp.get("/retail/markets")
    def retail_markets() -> Response:
        identity().require_scope("paper:read")
        get_backend().ensure_retail_schema()
        result = get_backend().retail_service.list_markets(
            principal=identity().principal,
            query=str(request.args.get("q") or ""),
            category=str(request.args.get("category") or "").strip() or None,
            state=str(request.args.get("state") or "LIVE"),
            collection=str(request.args.get("collection") or "all"),
            limit=normalize_limit(request.args.get("limit")),
            offset=_integer(request.args.get("offset") or 0, "offset", minimum=0),
        )
        return jsonify(_success({"items": result}))

    @bp.get("/retail/markets/<path:market_slug>")
    def retail_market(market_slug: str) -> Response:
        identity().require_scope("paper:read")
        get_backend().ensure_retail_schema()
        return jsonify(
            _success(
                get_backend().retail_service.get_market(
                    market_slug, principal=identity().principal
                )
            )
        )

    @bp.get("/retail/watchlist")
    def retail_watchlist() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                {"market_slugs": get_backend().retail_service.list_watchlist(identity().principal)}
            )
        )

    @bp.put("/retail/watchlist/<path:market_slug>")
    def retail_set_watchlist(market_slug: str) -> Response:
        payload = _json_body()
        enabled = _boolean(payload.get("enabled"), "enabled")
        return mutate(
            f"retail_watchlist:{market_slug}",
            {"market_slug": market_slug, "enabled": enabled},
            lambda _key: {
                "market_slug": market_slug,
                "enabled": get_backend().retail_service.set_watchlist(
                    identity().principal, market_slug=market_slug, enabled=enabled
                ),
            },
            status_code=200,
        )

    @bp.get("/retail/preferences")
    def retail_preferences() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(get_backend().retail_service.get_preferences(identity().principal))
        )

    @bp.put("/retail/preferences")
    def retail_update_preferences() -> Response:
        payload = _json_body()
        return mutate(
            "retail_preferences",
            payload,
            lambda _key: get_backend().retail_service.update_preferences(
                identity().principal, payload
            ),
            status_code=200,
        )

    @bp.get("/retail/predictions")
    def retail_predictions() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                {
                    "items": get_backend().retail_service.list_predictions(
                        identity().principal,
                        limit=normalize_limit(request.args.get("limit"), maximum=200),
                    )
                }
            )
        )

    @bp.post("/retail/predictions")
    def retail_create_prediction() -> Response:
        payload = _json_body()
        return mutate(
            "retail_create_prediction",
            payload,
            lambda _key: get_backend().retail_service.create_prediction(
                identity().principal, payload
            ),
            status_code=201,
        )

    @bp.get("/retail/predictions/report")
    def retail_prediction_report() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(get_backend().retail_service.prediction_report(identity().principal))
        )

    @bp.put("/retail/risk-profile")
    def retail_risk_profile() -> Response:
        payload = _json_body()
        return mutate(
            "retail_risk_profile",
            payload,
            lambda _key: get_backend().retail_service.upsert_risk_profile(
                identity().principal, payload
            ),
            status_code=200,
        )

    @bp.get("/retail/risk-profile")
    def retail_get_risk_profile() -> Response:
        identity().require_scope("paper:read")
        profile = get_backend().retail_service.get_risk_profile(identity().principal)
        if profile is None:
            return jsonify(_success({}))
        return jsonify(_success(profile))

    @bp.get("/retail/notifications")
    def retail_notifications() -> Response:
        identity().require_scope("paper:read")
        unread_only = str(request.args.get("unread_only") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        return jsonify(
            _success(
                {
                    "items": get_backend().retail_service.list_notifications(
                        identity().principal,
                        unread_only=unread_only,
                        limit=normalize_limit(request.args.get("limit")),
                    )
                }
            )
        )

    @bp.put("/retail/notifications/<uuid:notification_id>/read")
    def retail_mark_notification_read(notification_id: UUID) -> Response:
        return mutate(
            f"retail_notification_read:{notification_id}",
            {"notification_id": str(notification_id)},
            lambda _key: {
                "notification_id": str(notification_id),
                "read": get_backend().retail_service.mark_notification_read(
                    identity().principal, notification_id
                ),
            },
            status_code=200,
        )

    @bp.get("/retail/account-truth")
    def retail_account_truth() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(get_backend().retail_service.get_account_truth(identity().principal))
        )

    @bp.get("/retail/pnl-attribution")
    def retail_pnl_attribution() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                get_backend().retail_service.get_pnl_attribution(identity().principal)
            )
        )

    @bp.get("/retail/event-risk")
    def retail_event_risk() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(get_backend().retail_service.get_event_risk(identity().principal))
        )

    @bp.get("/retail/event-relations")
    def retail_event_relations() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                get_backend().retail_service.get_event_relationship_graph(
                    identity().principal,
                    market_slug=request.args.get("market_slug"),
                )
            )
        )

    @bp.get("/retail/lifecycle")
    def retail_lifecycle() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                get_backend().retail_service.get_account_lifecycle(identity().principal)
            )
        )

    @bp.get("/retail/position-operations")
    def retail_position_operations() -> Response:
        return jsonify(
            _success(
                get_backend().list_retail_position_operations(
                    identity(),
                    limit=normalize_limit(request.args.get("limit"), maximum=200),
                )
            )
        )

    @bp.get("/retail/position-operations/candidates")
    def retail_position_operation_candidates() -> Response:
        return jsonify(
            _success(
                get_backend().get_retail_position_operation_candidates(
                    identity(),
                    limit=normalize_limit(request.args.get("limit"), maximum=200),
                )
            )
        )

    @bp.get("/retail/position-operations/<path:position_operation_id>")
    def retail_position_operation(position_operation_id: str) -> Response:
        return jsonify(
            _success(
                get_backend().get_retail_position_operation(
                    identity(), position_operation_id
                )
            )
        )

    @bp.post("/retail/position-operations")
    def retail_create_position_operation() -> Response:
        payload = _json_body()
        operation_type = str(payload.get("operation_type") or "").strip().upper()
        amount = _decimal(payload.get("amount"), "amount", positive=True)
        canonical = {
            "operation_type": operation_type,
            "amount": str(amount),
            "market_slug": str(payload.get("market_slug") or "").strip() or None,
            "matrix_id": str(payload.get("matrix_id") or "").strip() or None,
        }
        return mutate(
            "retail_position_operation",
            canonical,
            lambda key: get_backend().create_retail_position_operation(
                identity(), payload=canonical, idempotency_key=key
            ),
            status_code=201,
        )

    @bp.get("/retail/maker-workbench")
    def retail_maker_workbench() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                get_backend().retail_service.get_maker_workbench(
                    identity().principal,
                    limit=normalize_limit(request.args.get("limit"), maximum=200),
                )
            )
        )

    @bp.put("/retail/portfolio")
    def retail_portfolio_visibility() -> Response:
        payload = _json_body()
        return mutate(
            "retail_portfolio_visibility",
            payload,
            lambda _key: get_backend().retail_service.set_portfolio_visibility(
                identity().principal, payload
            ),
            status_code=200,
        )

    @bp.post("/retail/portfolio/refresh-metrics")
    def retail_refresh_portfolio_metrics() -> Response:
        return mutate(
            "retail_refresh_portfolio_metrics",
            {},
            lambda _key: get_backend().retail_service.refresh_public_metrics(
                identity().principal
            ),
            status_code=200,
        )

    @bp.get("/retail/portfolios/<path:virtual_wallet_id>/journal")
    def retail_public_prediction_journal(virtual_wallet_id: str) -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                get_backend().retail_service.get_public_prediction_journal(
                    identity().principal,
                    virtual_wallet_id=virtual_wallet_id,
                    limit=normalize_limit(request.args.get("limit"), maximum=200),
                )
            )
        )

    @bp.get("/retail/portfolios/<virtual_wallet_id>")
    def retail_public_portfolio(virtual_wallet_id: str) -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                get_backend().retail_service.get_public_portfolio(
                    identity().principal,
                    virtual_wallet_id=virtual_wallet_id,
                    activity_limit=normalize_limit(
                        request.args.get("limit"), maximum=200
                    ),
                )
            )
        )

    @bp.get("/retail/following")
    def retail_following() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                {
                    "virtual_wallet_ids": get_backend().retail_service.list_following(
                        identity().principal
                    )
                }
            )
        )

    @bp.put("/retail/following/<path:virtual_wallet_id>")
    def retail_set_follow(virtual_wallet_id: str) -> Response:
        payload = _json_body()
        enabled = _boolean(payload.get("enabled"), "enabled")
        return mutate(
            f"retail_follow:{virtual_wallet_id}",
            {"virtual_wallet_id": virtual_wallet_id, "enabled": enabled},
            lambda _key: {
                "virtual_wallet_id": virtual_wallet_id,
                "enabled": get_backend().retail_service.set_follow(
                    identity().principal,
                    followed_wallet_id=virtual_wallet_id,
                    enabled=enabled,
                ),
            },
            status_code=200,
        )

    @bp.get("/retail/competitions")
    def retail_competitions() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                {
                    "items": get_backend().retail_service.list_competitions(
                        identity().principal
                    )
                }
            )
        )

    @bp.post("/retail/competitions")
    def retail_create_competition() -> Response:
        payload = _json_body()
        return mutate(
            "retail_create_competition",
            payload,
            lambda key: get_backend().retail_service.create_competition(
                identity().principal, payload, idempotency_key=key
            ),
            status_code=201,
        )

    @bp.post("/retail/competitions/<uuid:competition_id>/join")
    def retail_join_competition(competition_id: UUID) -> Response:
        payload = {"competition_id": str(competition_id)}
        return mutate(
            f"retail_join_competition:{competition_id}",
            payload,
            lambda key: get_backend().retail_service.join_competition(
                identity().principal, competition_id, idempotency_key=key
            ),
            status_code=201,
        )

    @bp.get("/retail/competitions/<uuid:competition_id>/standings")
    def retail_competition_standings(competition_id: UUID) -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                get_backend().retail_service.get_competition_standings(
                    identity().principal,
                    competition_id=competition_id,
                    metric=str(request.args.get("metric") or "return"),
                )
            )
        )

    @bp.get("/retail/data-requests")
    def retail_data_requests() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                {
                    "items": get_backend().retail_service.list_data_subject_requests(
                        identity().principal
                    )
                }
            )
        )

    @bp.post("/retail/data-requests")
    def retail_create_data_request() -> Response:
        payload = _json_body()
        return mutate(
            "retail_create_data_request",
            payload,
            lambda key: get_backend().retail_service.create_data_subject_request(
                identity().principal,
                request_type=str(payload.get("request_type") or ""),
                reason=str(payload.get("reason") or ""),
                idempotency_key=key,
            ),
            status_code=201,
        )

    @bp.get("/retail/data-requests/<uuid:request_id>/download")
    def retail_download_data_request(request_id: UUID) -> Response:
        metadata, payload = get_backend().retail_service.get_data_subject_export(
            identity().principal, request_id
        )
        get_backend().charge_export_quota(
            identity(), byte_count=len(payload), request_id=g.paper_request_id
        )
        response = Response(payload, mimetype="application/zip")
        response.headers["Content-Disposition"] = (
            f'attachment; filename="paper-retail-export-{request_id}.zip"'
        )
        response.headers["X-Content-SHA256"] = str(metadata["artifact_sha256"])
        return response

    @bp.get("/retail/official-history")
    def retail_official_history() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                {
                    "items": get_backend().retail_service.list_official_history_sync_requests(
                        identity().principal,
                        limit=normalize_limit(request.args.get("limit"), maximum=200),
                    )
                }
            )
        )

    @bp.post("/retail/official-history")
    def retail_create_official_history() -> Response:
        payload = _json_body()
        now = datetime.now(timezone.utc)
        return mutate(
            "retail_official_history",
            payload,
            lambda key: get_backend().retail_service.create_official_history_sync_request(
                identity().principal,
                window_start=payload.get("window_start") or "2020-01-01T00:00:00Z",
                window_end=payload.get("window_end") or now,
                compare_to_paper=_boolean(
                    payload.get("compare_to_paper", False), "compare_to_paper"
                ),
                idempotency_key=key,
            ),
            status_code=202,
        )

    @bp.get("/retail/leaderboard")
    def retail_leaderboard() -> Response:
        identity().require_scope("paper:read")
        return jsonify(
            _success(
                {
                    "items": get_backend().retail_service.list_leaderboard(
                        metric=str(request.args.get("metric") or "return"),
                        limit=normalize_limit(request.args.get("limit"), maximum=100),
                    )
                }
            )
        )

    @bp.get("/accounts")
    def list_accounts() -> Response:
        result = get_backend().list_accounts(
            identity(),
            limit=normalize_limit(request.args.get("limit")),
            cursor=request.args.get("cursor"),
        )
        return jsonify(_success(result))

    @bp.post("/accounts")
    def create_account() -> Response:
        payload = _json_body()
        name = str(payload.get("name") or "").strip()
        if not name:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "name is required",
                status_code=400,
            )
        initial_cash = _decimal(
            payload.get("initial_cash"), "initial_cash", positive=True
        )
        return mutate(
            "create_account",
            payload,
            lambda key: get_backend().create_account(
                identity(), name=name, initial_cash=initial_cash, idempotency_key=key
            ),
            status_code=201,
        )

    @bp.get("/accounts/<uuid:account_id>")
    def get_account(account_id: UUID) -> Response:
        return jsonify(_success(get_backend().get_account(identity(), account_id)))

    @bp.post("/accounts/<uuid:account_id>/fork")
    def fork_account(account_id: UUID) -> Response:
        payload = _json_body()
        name = str(payload.get("name") or "").strip()
        if not name:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR", "name is required", status_code=400
            )
        return mutate(
            f"fork_account:{account_id}",
            payload,
            lambda key: get_backend().fork_account(
                identity(),
                parent_account_id=account_id,
                name=name,
                idempotency_key=key,
            ),
            status_code=201,
        )

    @bp.get("/orders")
    def list_orders() -> Response:
        account_id = _uuid(request.args.get("account_id"), "account_id")
        result = get_backend().list_orders(
            identity(),
            account_id=account_id,
            limit=normalize_limit(request.args.get("limit")),
            cursor=request.args.get("cursor"),
        )
        return jsonify(_success(result))

    @bp.post("/orders")
    def create_order() -> Response:
        payload = _json_body()
        for field in (
            "account_id",
            "strategy_id",
            "asset_id",
            "side",
            "time_in_force",
            "limit_price",
            "size",
        ):
            if payload.get(field) in (None, ""):
                raise PaperApiError(
                    "PAPER_VALIDATION_ERROR",
                    f"{field} is required",
                    status_code=400,
                )
        payload["account_id"] = str(_uuid(payload["account_id"], "account_id"))
        payload["strategy_id"] = str(_uuid(payload["strategy_id"], "strategy_id"))
        if payload.get("deployment_id"):
            payload["deployment_id"] = str(
                _uuid(payload["deployment_id"], "deployment_id")
            )
        side = str(payload["side"]).upper()
        tif = str(payload["time_in_force"]).upper()
        if side not in {"BUY", "SELL"} or tif not in {"GTC", "GTD", "FOK", "FAK"}:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "invalid side or time_in_force",
                status_code=400,
            )
        price = _decimal(payload["limit_price"], "limit_price", positive=True)
        size = _decimal(payload["size"], "size", positive=True)
        amount_unit = str(payload.get("amount_unit") or "SHARES").upper()
        if amount_unit not in {"SHARES", "QUOTE"}:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "amount_unit must be SHARES or QUOTE",
                status_code=400,
            )
        if (side == "SELL" or tif in {"GTC", "GTD"}) and amount_unit != "SHARES":
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "SELL and GTC/GTD orders require amount_unit=SHARES",
                status_code=400,
            )
        if "post_only" in payload and not isinstance(payload["post_only"], bool):
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "post_only must be boolean",
                status_code=400,
            )
        if price >= 1:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "limit_price must be between 0 and 1",
                status_code=400,
            )
        payload.update(
            {
                "side": side,
                "time_in_force": tif,
                "limit_price": str(price),
                "size": str(size),
                "amount_unit": amount_unit,
            }
        )
        if tif == "GTD":
            if not payload.get("expires_at"):
                raise PaperApiError(
                    "PAPER_VALIDATION_ERROR",
                    "expires_at is required for GTD orders",
                    status_code=400,
                )
            expiration = _datetime(payload["expires_at"], "expires_at")
            if expiration < datetime.now(timezone.utc) + timedelta(minutes=3):
                raise PaperApiError(
                    "PAPER_VALIDATION_ERROR",
                    "GTD stated expiration must be at least 3 minutes in the future",
                    status_code=400,
                )
            payload["expires_at"] = expiration.isoformat()
        return mutate(
            "create_order",
            payload,
            lambda key: get_backend().submit_order(
                identity(), payload=payload, idempotency_key=key
            ),
            status_code=202,
        )

    @bp.get("/orders/<int:order_id>")
    def get_order(order_id: int) -> Response:
        return jsonify(_success(get_backend().get_order(identity(), order_id)))

    @bp.get("/orders/<int:order_id>/audit")
    @bp.get("/audit/<int:order_id>")
    def get_order_audit(order_id: int) -> Response:
        return jsonify(_success(get_backend().get_order_audit(identity(), order_id)))

    @bp.post("/orders/cancel")
    def cancel_orders() -> Response:
        payload = _json_body()
        raw_ids = payload.get("order_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "order_ids must be a non-empty array",
                status_code=400,
            )
        try:
            order_ids = [int(value) for value in raw_ids]
        except (TypeError, ValueError):
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "order_ids must contain integers",
                status_code=400,
            ) from None
        canonical = {"order_ids": sorted(set(order_ids))}
        return mutate(
            "cancel_orders",
            canonical,
            lambda _key: get_backend().cancel_orders(
                identity(), canonical["order_ids"]
            ),
            status_code=200,
        )

    @bp.post("/orders/cancel-market")
    def cancel_market_orders() -> Response:
        payload = _json_body()
        account_id = _uuid(payload.get("account_id"), "account_id")
        asset_id = str(payload.get("asset_id") or "").strip() or None
        condition_id = str(payload.get("condition_id") or "").strip() or None
        if asset_id is None and condition_id is None:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "asset_id or condition_id is required",
                status_code=400,
            )
        canonical = {
            "account_id": str(account_id),
            "asset_id": asset_id,
            "condition_id": condition_id,
        }
        return mutate(
            "cancel_market_orders",
            canonical,
            lambda _key: get_backend().cancel_market_orders(
                identity(),
                account_id=account_id,
                asset_id=asset_id,
                condition_id=condition_id,
            ),
            status_code=200,
        )

    @bp.post("/orders/cancel-all")
    def cancel_all_orders() -> Response:
        payload = _json_body()
        account_id = _uuid(payload.get("account_id"), "account_id")
        canonical = {"account_id": str(account_id)}
        return mutate(
            "cancel_all_orders",
            canonical,
            lambda _key: get_backend().cancel_all_orders(
                identity(), account_id=account_id
            ),
            status_code=200,
        )

    @bp.delete("/orders/<int:order_id>")
    def cancel_order(order_id: int) -> Response:
        payload = {"order_id": order_id}
        return mutate(
            f"cancel_order:{order_id}",
            payload,
            lambda _key: get_backend().cancel_order(identity(), order_id),
            status_code=200,
        )

    @bp.post("/orders/<int:order_id>/replace")
    def replace_order(order_id: int) -> Response:
        payload = _json_body()
        price = _decimal(payload.get("limit_price"), "limit_price", positive=True)
        size = _decimal(payload.get("size"), "size", positive=True)
        if price >= 1:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "limit_price must be between 0 and 1",
                status_code=400,
            )
        canonical = {"order_id": order_id, "limit_price": str(price), "size": str(size)}
        return mutate(
            f"replace_order:{order_id}",
            canonical,
            lambda _key: get_backend().replace_order(
                identity(), order_id, limit_price=price, size=size
            ),
            status_code=200,
        )

    @bp.get("/accounts/<uuid:account_id>/positions")
    def list_positions(account_id: UUID) -> Response:
        return jsonify(
            _success(
                get_backend().list_positions(
                    identity(),
                    account_id=account_id,
                    limit=normalize_limit(request.args.get("limit")),
                    cursor=request.args.get("cursor"),
                )
            )
        )

    @bp.get("/accounts/<uuid:account_id>/fills")
    def list_fills(account_id: UUID) -> Response:
        return jsonify(
            _success(
                get_backend().list_fills(
                    identity(),
                    account_id=account_id,
                    limit=normalize_limit(request.args.get("limit")),
                    cursor=request.args.get("cursor"),
                )
            )
        )

    @bp.get("/accounts/<uuid:account_id>/ledger")
    def list_ledger(account_id: UUID) -> Response:
        return jsonify(
            _success(
                get_backend().list_ledger(
                    identity(),
                    account_id=account_id,
                    limit=normalize_limit(request.args.get("limit")),
                    cursor=request.args.get("cursor"),
                )
            )
        )

    @bp.get("/accounts/<uuid:account_id>/journal")
    def list_journal(account_id: UUID) -> Response:
        return jsonify(
            _success(
                get_backend().list_journal(
                    identity(),
                    account_id=account_id,
                    limit=normalize_limit(request.args.get("limit")),
                    cursor=request.args.get("cursor"),
                )
            )
        )

    @bp.get("/accounts/<uuid:account_id>/tca")
    def list_tca(account_id: UUID) -> Response:
        return jsonify(
            _success(
                get_backend().list_tca(
                    identity(),
                    account_id=account_id,
                    limit=normalize_limit(request.args.get("limit")),
                    cursor=request.args.get("cursor"),
                )
            )
        )

    @bp.get("/accounts/<uuid:account_id>/performance")
    def get_performance(account_id: UUID) -> Response:
        return jsonify(
            _success(
                get_backend().get_performance(
                    identity(),
                    account_id=account_id,
                    limit=normalize_limit(request.args.get("limit"), default=100),
                    cursor=request.args.get("cursor"),
                )
            )
        )

    @bp.get("/accounts/<uuid:account_id>/export")
    def export_account(account_id: UUID) -> Response:
        resource = str(request.args.get("resource") or "").strip().lower()
        output_format = str(request.args.get("format") or "csv").strip().lower()
        if output_format not in {"csv", "jsonl", "parquet"}:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "format must be csv, jsonl, or parquet",
                status_code=400,
            )
        rows = [
            _json_value(row)
            for row in get_backend().export_account(
                identity(), account_id=account_id, resource=resource
            )
        ]
        if output_format == "parquet":
            payload = _parquet_bytes(rows)
            mimetype = "application/vnd.apache.parquet"
        elif output_format == "jsonl":
            rendered = "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in rows
            )
            mimetype = "application/x-ndjson"
            payload = rendered.encode("utf-8")
        else:
            columns = sorted({str(key) for row in rows for key in row})
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            if columns:
                writer.writeheader()
                for row in rows:
                    writer.writerow(
                        {
                            key: json.dumps(
                                value, sort_keys=True, separators=(",", ":")
                            )
                            if isinstance(value, (dict, list))
                            else value
                            for key, value in row.items()
                        }
                    )
            rendered = stream.getvalue()
            mimetype = "text/csv"
            payload = rendered.encode("utf-8")
        get_backend().charge_export_quota(
            identity(),
            byte_count=len(payload),
            request_id=g.paper_request_id,
        )
        digest = hashlib.sha256(payload).hexdigest()
        response = Response(payload, mimetype=mimetype)
        response.headers["Content-Disposition"] = (
            f'attachment; filename="paper-{account_id}-{resource}.{output_format}"'
        )
        response.headers["X-Content-SHA256"] = digest
        response.headers["X-Export-Row-Count"] = str(len(rows))
        return response

    @bp.get("/replays")
    def list_replay_sessions() -> Response:
        return jsonify(
            _success(
                get_backend().list_replay_sessions(
                    identity(),
                    limit=normalize_limit(request.args.get("limit")),
                    cursor=request.args.get("cursor"),
                )
            )
        )

    @bp.post("/replays")
    def create_replay_session() -> Response:
        payload = _json_body()
        required = (
            "account_id",
            "name",
            "start_ts",
            "end_ts",
            "strategy_version",
            "execution_model",
        )
        for field in required:
            if payload.get(field) in (None, ""):
                raise PaperApiError(
                    "PAPER_VALIDATION_ERROR", f"{field} is required", status_code=400
                )
        payload["account_id"] = str(_uuid(payload["account_id"], "account_id"))
        if payload.get("strategy_id"):
            payload["strategy_id"] = str(_uuid(payload["strategy_id"], "strategy_id"))
        payload["start_ts"] = _datetime(payload["start_ts"], "start_ts").isoformat()
        payload["end_ts"] = _datetime(payload["end_ts"], "end_ts").isoformat()
        speed = _decimal(payload.get("speed", "1"), "speed", positive=True)
        seed = _integer(payload.get("seed", 0), "seed", minimum=0)
        benchmark = str(payload.get("benchmark") or "CASH").upper()
        if benchmark not in {"CASH", "CONSERVATIVE_NAV"}:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "benchmark must be CASH or CONSERVATIVE_NAV",
                status_code=400,
            )
        payload.update({"speed": str(speed), "seed": seed, "benchmark": benchmark})
        return mutate(
            "create_replay_session",
            payload,
            lambda key: get_backend().create_replay_session(
                identity(), payload=payload, idempotency_key=key
            ),
            status_code=201,
        )

    @bp.get("/replays/<uuid:replay_session_id>")
    def get_replay_session(replay_session_id: UUID) -> Response:
        return jsonify(
            _success(get_backend().get_replay_session(identity(), replay_session_id))
        )

    @bp.post("/replays/<uuid:replay_session_id>/pause")
    def pause_replay_session(replay_session_id: UUID) -> Response:
        payload = {"replay_session_id": str(replay_session_id)}
        return mutate(
            f"pause_replay_session:{replay_session_id}",
            payload,
            lambda _key: get_backend().pause_replay_session(
                identity(), replay_session_id
            ),
            status_code=200,
        )

    @bp.post("/replays/<uuid:replay_session_id>/resume")
    def resume_replay_session(replay_session_id: UUID) -> Response:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR", "JSON object required", status_code=400
            )
        max_events = _integer(payload.get("max_events", 1000), "max_events", minimum=1)
        if max_events > 5000:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "max_events must be <= 5000",
                status_code=400,
            )
        canonical = {
            "replay_session_id": str(replay_session_id),
            "max_events": max_events,
        }
        return mutate(
            f"resume_replay_session:{replay_session_id}",
            canonical,
            lambda _key: get_backend().resume_replay_session(
                identity(), replay_session_id, max_events=max_events
            ),
            status_code=200,
        )

    @bp.post("/replays/<uuid:replay_session_id>/fork")
    def fork_replay_session(replay_session_id: UUID) -> Response:
        payload = _json_body()
        name = str(payload.get("name") or "").strip()
        if not name:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR", "name is required", status_code=400
            )
        payload["name"] = name
        if payload.get("speed") not in (None, ""):
            payload["speed"] = str(_decimal(payload["speed"], "speed", positive=True))
        if payload.get("seed") not in (None, ""):
            payload["seed"] = _integer(payload["seed"], "seed", minimum=0)
        if payload.get("benchmark"):
            benchmark = str(payload["benchmark"]).upper()
            if benchmark not in {"CASH", "CONSERVATIVE_NAV"}:
                raise PaperApiError(
                    "PAPER_VALIDATION_ERROR",
                    "benchmark must be CASH or CONSERVATIVE_NAV",
                    status_code=400,
                )
            payload["benchmark"] = benchmark
        return mutate(
            f"fork_replay_session:{replay_session_id}",
            payload,
            lambda key: get_backend().fork_replay_session(
                identity(),
                replay_session_id,
                payload=payload,
                idempotency_key=key,
            ),
            status_code=201,
        )

    @bp.get("/replays/<uuid:replay_session_id>/events")
    def list_replay_events(replay_session_id: UUID) -> Response:
        return jsonify(
            _success(
                get_backend().list_replay_events(
                    identity(),
                    replay_session_id,
                    limit=normalize_limit(request.args.get("limit")),
                    cursor=request.args.get("cursor"),
                )
            )
        )

    @bp.get("/replays/<uuid:replay_session_id>/report")
    def get_replay_report(replay_session_id: UUID) -> Response:
        return jsonify(
            _success(get_backend().get_replay_report(identity(), replay_session_id))
        )

    @bp.get("/scenarios")
    def list_scenario_runs() -> Response:
        account_id = _uuid(request.args.get("account_id"), "account_id")
        rows = get_backend().list_scenario_runs(
            identity(),
            account_id=account_id,
            limit=normalize_limit(request.args.get("limit"), default=100),
        )
        return jsonify(_success({"items": rows}))

    @bp.post("/scenarios")
    def create_scenario_run() -> Response:
        payload = _json_body()
        for field in ("account_id", "name", "inputs"):
            if payload.get(field) in (None, ""):
                raise PaperApiError(
                    "PAPER_VALIDATION_ERROR", f"{field} is required", status_code=400
                )
        if not isinstance(payload["inputs"], dict):
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR", "inputs must be an object", status_code=400
            )
        payload["account_id"] = str(_uuid(payload["account_id"], "account_id"))
        if payload.get("strategy_id"):
            payload["strategy_id"] = str(_uuid(payload["strategy_id"], "strategy_id"))
        return mutate(
            "create_scenario_run",
            payload,
            lambda key: get_backend().create_scenario_run(
                identity(), payload=payload, idempotency_key=key
            ),
            status_code=201,
        )

    @bp.get("/scenarios/<uuid:scenario_run_id>")
    def get_scenario_run(scenario_run_id: UUID) -> Response:
        return jsonify(
            _success(get_backend().get_scenario_run(identity(), scenario_run_id))
        )

    @bp.get("/conditional-orders")
    def list_conditional_orders() -> Response:
        account_id = _uuid(request.args.get("account_id"), "account_id")
        rows = get_backend().list_conditional_orders(
            identity(),
            account_id=account_id,
            limit=normalize_limit(request.args.get("limit"), default=100),
        )
        return jsonify(_success({"items": rows}))

    @bp.post("/conditional-orders")
    def create_conditional_order() -> Response:
        payload = _json_body()
        for field in (
            "account_id",
            "strategy_id",
            "order_type",
            "trigger",
            "child_order",
        ):
            if payload.get(field) in (None, ""):
                raise PaperApiError(
                    "PAPER_VALIDATION_ERROR", f"{field} is required", status_code=400
                )
        if not isinstance(payload["trigger"], dict) or not isinstance(
            payload["child_order"], dict
        ):
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "trigger and child_order must be objects",
                status_code=400,
            )
        payload["account_id"] = str(_uuid(payload["account_id"], "account_id"))
        payload["strategy_id"] = str(_uuid(payload["strategy_id"], "strategy_id"))
        for field in ("group_id", "parent_conditional_order_id"):
            if payload.get(field):
                payload[field] = str(_uuid(payload[field], field))
        if payload.get("parent_intent_id") is not None:
            payload["parent_intent_id"] = _integer(
                payload["parent_intent_id"], "parent_intent_id", minimum=1
            )
        if payload.get("expires_at"):
            payload["expires_at"] = _datetime(
                payload["expires_at"], "expires_at"
            ).isoformat()
        return mutate(
            "create_conditional_order",
            payload,
            lambda key: get_backend().create_conditional_order(
                identity(), payload=payload, idempotency_key=key
            ),
            status_code=201,
        )

    @bp.post("/conditional-order-groups")
    def create_conditional_order_group() -> Response:
        payload = _json_body()
        for field in ("account_id", "strategy_id", "group_policy", "orders"):
            if payload.get(field) in (None, ""):
                raise PaperApiError(
                    "PAPER_VALIDATION_ERROR", f"{field} is required", status_code=400
                )
        policy = str(payload["group_policy"]).upper()
        if policy not in {"OCO", "OTO", "BRACKET"}:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "group_policy must be OCO, OTO, or BRACKET",
                status_code=400,
            )
        orders = payload["orders"]
        if not isinstance(orders, list) or not orders or len(orders) > 8:
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR",
                "orders must contain 1..8 conditional legs",
                status_code=400,
            )
        normalized_orders = []
        for index, raw in enumerate(orders):
            if not isinstance(raw, dict):
                raise PaperApiError(
                    "PAPER_VALIDATION_ERROR",
                    f"orders[{index}] must be an object",
                    status_code=400,
                )
            for field in ("order_type", "trigger", "child_order"):
                if raw.get(field) in (None, ""):
                    raise PaperApiError(
                        "PAPER_VALIDATION_ERROR",
                        f"orders[{index}].{field} is required",
                        status_code=400,
                    )
            if not isinstance(raw["trigger"], dict) or not isinstance(
                raw["child_order"], dict
            ):
                raise PaperApiError(
                    "PAPER_VALIDATION_ERROR",
                    f"orders[{index}] trigger and child_order must be objects",
                    status_code=400,
                )
            leg = dict(raw)
            if leg.get("parent_intent_id") is not None:
                leg["parent_intent_id"] = _integer(
                    leg["parent_intent_id"],
                    f"orders[{index}].parent_intent_id",
                    minimum=1,
                )
            if leg.get("expires_at"):
                leg["expires_at"] = _datetime(
                    leg["expires_at"], f"orders[{index}].expires_at"
                ).isoformat()
            normalized_orders.append(leg)
        canonical = {
            "account_id": str(_uuid(payload["account_id"], "account_id")),
            "strategy_id": str(_uuid(payload["strategy_id"], "strategy_id")),
            "group_policy": policy,
            "orders": normalized_orders,
        }
        return mutate(
            "create_conditional_order_group",
            canonical,
            lambda key: get_backend().create_conditional_order_group(
                identity(), payload=canonical, idempotency_key=key
            ),
            status_code=201,
        )

    @bp.get("/conditional-orders/<uuid:conditional_order_id>")
    def get_conditional_order(conditional_order_id: UUID) -> Response:
        return jsonify(
            _success(
                get_backend().get_conditional_order(identity(), conditional_order_id)
            )
        )

    @bp.delete("/conditional-orders/<uuid:conditional_order_id>")
    def cancel_conditional_order(conditional_order_id: UUID) -> Response:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            raise PaperApiError(
                "PAPER_VALIDATION_ERROR", "JSON object required", status_code=400
            )
        reason = str(payload.get("reason") or "public_api_cancel").strip()
        canonical = {
            "conditional_order_id": str(conditional_order_id),
            "reason": reason,
        }
        return mutate(
            f"cancel_conditional_order:{conditional_order_id}",
            canonical,
            lambda _key: get_backend().cancel_conditional_order(
                identity(), conditional_order_id, reason=reason
            ),
            status_code=200,
        )

    @bp.get("/admin/dashboard")
    def admin_dashboard() -> Response:
        return jsonify(_success(get_backend().admin_dashboard(identity())))

    @bp.get("/admin/jobs")
    def list_admin_jobs() -> Response:
        rows = get_backend().list_admin_jobs(
            identity(), limit=normalize_limit(request.args.get("limit"), default=100)
        )
        return jsonify(_success({"items": rows}))

    @bp.post("/admin/tenant/<action>")
    def set_tenant_freeze(action: str) -> Response:
        if action not in {"freeze", "unfreeze"}:
            raise PaperApiError(
                "PAPER_NOT_FOUND", "admin action was not found", status_code=404
            )
        payload = _json_body()
        reason = str(payload.get("reason") or "").strip()
        return mutate(
            f"admin_tenant_{action}",
            payload,
            lambda _key: get_backend().set_tenant_freeze(
                identity(), frozen=action == "freeze", reason=reason
            ),
            status_code=200,
        )

    @bp.post("/admin/accounts/<uuid:account_id>/<action>")
    def administer_account(account_id: UUID, action: str) -> Response:
        if action not in {"freeze", "unfreeze", "kill", "reconcile"}:
            raise PaperApiError(
                "PAPER_NOT_FOUND", "admin action was not found", status_code=404
            )
        payload = _json_body()
        reason = str(payload.get("reason") or "").strip()

        def callback(key: str) -> Any:
            if action == "kill":
                return get_backend().kill_account(identity(), account_id, reason=reason)
            if action == "reconcile":
                return get_backend().reconcile_account(
                    identity(),
                    account_id,
                    mode=str(payload.get("mode") or "DRY_RUN"),
                    idempotency_key=key,
                )
            return get_backend().set_account_freeze(
                identity(), account_id, frozen=action == "freeze", reason=reason
            )

        return mutate(
            f"admin_account_{action}:{account_id}", payload, callback, status_code=200
        )

    @bp.get("/admin/dlq")
    def list_dlq() -> Response:
        rows = get_backend().list_dlq(
            identity(), limit=normalize_limit(request.args.get("limit"), default=100)
        )
        return jsonify(_success({"items": rows}))

    @bp.post("/admin/dlq/<uuid:dlq_event_id>/<action>")
    def administer_dlq(dlq_event_id: UUID, action: str) -> Response:
        if action not in {"replay", "ignore"}:
            raise PaperApiError(
                "PAPER_NOT_FOUND", "DLQ action was not found", status_code=404
            )
        payload = _json_body()
        return mutate(
            f"admin_dlq_{action}:{dlq_event_id}",
            payload,
            lambda key: (
                get_backend().replay_dlq_event(
                    identity(), dlq_event_id, idempotency_key=key
                )
                if action == "replay"
                else get_backend().ignore_dlq_event(
                    identity(),
                    dlq_event_id,
                    reason=str(payload.get("reason") or "").strip(),
                )
            ),
            status_code=200,
        )

    @bp.get("/admin/notices")
    def list_notices() -> Response:
        return jsonify(_success({"items": get_backend().list_notices(identity())}))

    @bp.post("/admin/notices")
    def create_notice() -> Response:
        payload = _json_body()
        starts_at = _datetime(payload.get("starts_at"), "starts_at")
        ends_at = (
            _datetime(payload["ends_at"], "ends_at") if payload.get("ends_at") else None
        )
        return mutate(
            "admin_notice_create",
            payload,
            lambda _key: get_backend().create_notice(
                identity(),
                title=str(payload.get("title") or ""),
                message=str(payload.get("message") or ""),
                starts_at=starts_at,
                ends_at=ends_at,
            ),
            status_code=201,
        )

    @bp.post("/admin/notices/<uuid:notice_id>/status")
    def set_notice_status(notice_id: UUID) -> Response:
        payload = _json_body()
        return mutate(
            f"admin_notice_status:{notice_id}",
            payload,
            lambda _key: get_backend().set_notice_status(
                identity(), notice_id, status=str(payload.get("status") or "")
            ),
            status_code=200,
        )

    @bp.get("/admin/incidents")
    def list_incidents() -> Response:
        return jsonify(_success({"items": get_backend().list_incidents(identity())}))

    @bp.post("/admin/incidents")
    def create_incident() -> Response:
        payload = _json_body()
        started_at = _datetime(payload.get("started_at"), "started_at")
        return mutate(
            "admin_incident_create",
            payload,
            lambda _key: get_backend().create_incident(
                identity(),
                title=str(payload.get("title") or ""),
                summary=str(payload.get("summary") or ""),
                severity=str(payload.get("severity") or ""),
                started_at=started_at,
            ),
            status_code=201,
        )

    @bp.post("/admin/incidents/<uuid:incident_id>/notes")
    def add_incident_note(incident_id: UUID) -> Response:
        payload = _json_body()
        return mutate(
            f"admin_incident_note:{incident_id}",
            payload,
            lambda _key: get_backend().add_incident_note(
                identity(), incident_id, body=str(payload.get("body") or "")
            ),
            status_code=200,
        )

    @bp.post("/admin/incidents/<uuid:incident_id>/status")
    def set_incident_status(incident_id: UUID) -> Response:
        payload = _json_body()
        return mutate(
            f"admin_incident_status:{incident_id}",
            payload,
            lambda _key: get_backend().set_incident_status(
                identity(), incident_id, status=str(payload.get("status") or "")
            ),
            status_code=200,
        )

    @bp.get("/admin/retention")
    def list_retention_policies() -> Response:
        rows = get_backend().list_retention_policies(identity())
        return jsonify(_success({"items": rows}))

    @bp.post("/admin/retention/<resource_type>")
    def upsert_retention_policy(resource_type: str) -> Response:
        payload = _json_body()
        return mutate(
            f"admin_retention_policy:{resource_type.upper()}",
            payload,
            lambda _key: get_backend().upsert_retention_policy(
                identity(),
                resource_type=resource_type,
                retention_days=_integer(
                    payload.get("retention_days"), "retention_days", minimum=1
                ),
                legal_hold=_boolean(payload.get("legal_hold", False), "legal_hold"),
            ),
            status_code=200,
        )

    @bp.post("/admin/retention/<resource_type>/run")
    def run_retention(resource_type: str) -> Response:
        payload = _json_body()
        return mutate(
            f"admin_retention_run:{resource_type.upper()}",
            payload,
            lambda key: get_backend().run_retention(
                identity(),
                resource_type=resource_type,
                mode=str(payload.get("mode") or "DRY_RUN"),
                idempotency_key=key,
            ),
            status_code=200,
        )

    @bp.get("/admin/evidence-bundles")
    def list_evidence_bundles() -> Response:
        rows = get_backend().list_evidence_bundles(
            identity(), limit=normalize_limit(request.args.get("limit"), default=100)
        )
        return jsonify(_success({"items": rows}))

    @bp.post("/admin/evidence-bundles")
    def create_evidence_bundle() -> Response:
        payload = _json_body()
        account_id = (
            _uuid(payload["account_id"], "account_id")
            if payload.get("account_id")
            else None
        )
        incident_id = (
            _uuid(payload["incident_id"], "incident_id")
            if payload.get("incident_id")
            else None
        )
        return mutate(
            "admin_evidence_bundle_create",
            payload,
            lambda key: get_backend().create_evidence_bundle(
                identity(),
                account_id=account_id,
                incident_id=incident_id,
                idempotency_key=key,
            ),
            status_code=201,
        )

    @bp.get("/admin/evidence-bundles/<uuid:bundle_id>/download")
    def download_evidence_bundle(bundle_id: UUID) -> Response:
        metadata, payload = get_backend().get_evidence_bundle(identity(), bundle_id)
        get_backend().charge_export_quota(
            identity(), byte_count=len(payload), request_id=g.paper_request_id
        )
        response = Response(payload, mimetype="application/zip")
        response.headers["Content-Disposition"] = (
            f'attachment; filename="paper-evidence-{bundle_id}.zip"'
        )
        response.headers["X-Content-SHA256"] = str(metadata["content_sha256"])
        return response

    return bp
